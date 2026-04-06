import json
import sqlite3
import threading
import time
from pathlib import Path

from .log import get_logger

logger = get_logger(__name__)

# 延迟导入 CONFIG_DIR 避免循环
_db_instance = None
_db_lock = threading.Lock()


def _get_db_path():
    from .config import CONFIG_DIR
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return CONFIG_DIR / "123pan.db"


class Database:
    """SQLite 单例数据库，WAL 模式，线程安全写。"""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._write_lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._create_tables()
        self._init_defaults()

    @classmethod
    def instance(cls) -> "Database":
        global _db_instance
        if _db_instance is None:
            with _db_lock:
                if _db_instance is None:
                    _db_instance = cls(_get_db_path())
        return _db_instance

    @classmethod
    def reset(cls):
        global _db_instance
        with _db_lock:
            if _db_instance is not None:
                try:
                    _db_instance._conn.close()
                except Exception:
                    pass
                _db_instance = None

    def _create_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS config (
                key   TEXT PRIMARY KEY NOT NULL,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS download_tasks (
                resume_id        TEXT PRIMARY KEY,
                account_name     TEXT NOT NULL DEFAULT '',
                file_name        TEXT NOT NULL,
                file_id          INTEGER NOT NULL,
                file_type        INTEGER NOT NULL DEFAULT 0,
                file_size        INTEGER NOT NULL DEFAULT 0,
                save_path        TEXT NOT NULL,
                current_dir_id   INTEGER NOT NULL DEFAULT 0,
                etag             TEXT NOT NULL DEFAULT '',
                s3key_flag       INTEGER NOT NULL DEFAULT 0,
                status           TEXT NOT NULL DEFAULT '等待中',
                progress         INTEGER NOT NULL DEFAULT 0,
                error            TEXT NOT NULL DEFAULT '',
                supports_resume  INTEGER NOT NULL DEFAULT 0,
                metadata_version INTEGER NOT NULL DEFAULT 2,
                created_at       REAL,
                updated_at       REAL
            );
            CREATE TABLE IF NOT EXISTS download_parts (
                resume_id     TEXT NOT NULL,
                part_index    INTEGER NOT NULL,
                start_byte    INTEGER NOT NULL,
                end_byte      INTEGER NOT NULL,
                expected_size INTEGER NOT NULL,
                actual_size   INTEGER NOT NULL DEFAULT 0,
                md5           TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (resume_id, part_index),
                FOREIGN KEY (resume_id) REFERENCES download_tasks(resume_id)
                    ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS upload_tasks (
                task_id       TEXT PRIMARY KEY,
                account_name  TEXT NOT NULL DEFAULT '',
                file_name     TEXT NOT NULL,
                file_size     INTEGER NOT NULL DEFAULT 0,
                local_path    TEXT NOT NULL,
                target_dir_id INTEGER NOT NULL DEFAULT 0,
                status        TEXT NOT NULL DEFAULT '等待中',
                progress      INTEGER NOT NULL DEFAULT 0,
                error         TEXT NOT NULL DEFAULT '',
                bucket        TEXT NOT NULL DEFAULT '',
                storage_node  TEXT NOT NULL DEFAULT '',
                upload_key    TEXT NOT NULL DEFAULT '',
                upload_id_s3  TEXT NOT NULL DEFAULT '',
                up_file_id    INTEGER NOT NULL DEFAULT 0,
                total_parts   INTEGER NOT NULL DEFAULT 0,
                block_size    INTEGER NOT NULL DEFAULT 8388608,
                etag          TEXT NOT NULL DEFAULT '',
                created_at    REAL,
                updated_at    REAL
            );
            CREATE TABLE IF NOT EXISTS upload_parts (
                task_id    TEXT NOT NULL,
                part_index INTEGER NOT NULL,
                etag       TEXT NOT NULL DEFAULT '',
                uploaded   INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (task_id, part_index),
                FOREIGN KEY (task_id) REFERENCES upload_tasks(task_id)
                    ON DELETE CASCADE
            );
        """)

    def _init_defaults(self):
        """首次创建时写入默认配置。"""
        defaults = {
            "defaultDownloadPath": str(Path.home() / "Downloads"),
            "askDownloadLocation": True,
            "autoLogin": False,
            "maxDownloadThreads": 3,
            "maxUploadThreads": 16,
            "maxConcurrentDownloads": 3,
            "maxConcurrentUploads": 3,
            "retryMaxAttempts": 3,
            "retryBackoffFactor": 0.5,
        }
        with self._write_lock:
            for key, value in defaults.items():
                self._conn.execute(
                    "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
                    (key, json.dumps(value)),
                )
            self._conn.commit()

    # ---- Config ----

    def get_config(self, key: str, default=None):
        row = self._conn.execute(
            "SELECT value FROM config WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return default
        return json.loads(row[0])

    def set_config(self, key: str, value) -> None:
        with self._write_lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                (key, json.dumps(value)),
            )
            self._conn.commit()

    def set_many_config(self, items: dict) -> None:
        with self._write_lock:
            for key, value in items.items():
                self._conn.execute(
                    "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                    (key, json.dumps(value)),
                )
            self._conn.commit()

    def get_all_config(self) -> dict:
        rows = self._conn.execute("SELECT key, value FROM config").fetchall()
        return {key: json.loads(value) for key, value in rows}

    # ---- Download tasks ----

    def save_download_task(self, task: dict) -> None:
        now = time.time()
        with self._write_lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO download_tasks
                (resume_id, account_name, file_name, file_id, file_type,
                 file_size, save_path, current_dir_id, etag, s3key_flag,
                 status, progress, error, supports_resume, metadata_version,
                 created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task["resume_id"], task.get("account_name", ""),
                    task["file_name"], task["file_id"],
                    task.get("file_type", 0), task.get("file_size", 0),
                    task["save_path"], task.get("current_dir_id", 0),
                    task.get("etag", ""), int(task.get("s3key_flag", 0)),
                    task.get("status", "等待中"), task.get("progress", 0),
                    task.get("error", ""),
                    int(task.get("supports_resume", 0)),
                    task.get("metadata_version", 2), now, now,
                ),
            )
            self._conn.execute(
                """UPDATE download_tasks
                SET account_name = ?,
                    file_name = ?,
                    file_id = ?,
                    file_type = ?,
                    file_size = ?,
                    save_path = ?,
                    current_dir_id = ?,
                    etag = ?,
                    s3key_flag = ?,
                    status = ?,
                    progress = ?,
                    error = ?,
                    supports_resume = ?,
                    metadata_version = ?,
                    updated_at = ?
                WHERE resume_id = ?""",
                (
                    task.get("account_name", ""),
                    task["file_name"],
                    task["file_id"],
                    task.get("file_type", 0),
                    task.get("file_size", 0),
                    task["save_path"],
                    task.get("current_dir_id", 0),
                    task.get("etag", ""),
                    int(task.get("s3key_flag", 0)),
                    task.get("status", "等待中"),
                    task.get("progress", 0),
                    task.get("error", ""),
                    int(task.get("supports_resume", 0)),
                    task.get("metadata_version", 2),
                    now,
                    task["resume_id"],
                ),
            )
            self._conn.commit()

    def get_download_task(self, resume_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM download_tasks WHERE resume_id = ?", (resume_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_download_task(row)

    def get_download_tasks(self, account_name: str = None) -> list[dict]:
        if account_name:
            rows = self._conn.execute(
                "SELECT * FROM download_tasks WHERE account_name = ?",
                (account_name,),
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM download_tasks").fetchall()
        return [self._row_to_download_task(row) for row in rows]

    def update_download_task(self, resume_id: str, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [resume_id]
        with self._write_lock:
            self._conn.execute(
                f"UPDATE download_tasks SET {set_clause} WHERE resume_id = ?",
                values,
            )
            self._conn.commit()

    def delete_download_task(self, resume_id: str) -> None:
        with self._write_lock:
            self._conn.execute(
                "DELETE FROM download_tasks WHERE resume_id = ?", (resume_id,)
            )
            self._conn.commit()

    def _row_to_download_task(self, row) -> dict:
        cols = [
            "resume_id", "account_name", "file_name", "file_id", "file_type",
            "file_size", "save_path", "current_dir_id", "etag", "s3key_flag",
            "status", "progress", "error", "supports_resume",
            "metadata_version", "created_at", "updated_at",
        ]
        return dict(zip(cols, row))

    # ---- Download parts ----

    def record_download_part(self, resume_id: str, part: dict) -> None:
        with self._write_lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO download_parts
                (resume_id, part_index, start_byte, end_byte,
                 expected_size, actual_size, md5)
                VALUES (?,?,?,?,?,?,?)""",
                (
                    resume_id, part["index"], part["start"], part["end"],
                    part["expected_size"], part.get("actual_size", 0),
                    part.get("md5", ""),
                ),
            )
            self._conn.commit()

    def remove_download_part(self, resume_id: str, part_index: int) -> None:
        with self._write_lock:
            self._conn.execute(
                "DELETE FROM download_parts WHERE resume_id = ? AND part_index = ?",
                (resume_id, part_index),
            )
            self._conn.commit()

    def get_download_parts(self, resume_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM download_parts WHERE resume_id = ? ORDER BY part_index",
            (resume_id,),
        ).fetchall()
        cols = [
            "resume_id", "part_index", "start_byte", "end_byte",
            "expected_size", "actual_size", "md5",
        ]
        return [dict(zip(cols, row)) for row in rows]

    # ---- Upload tasks ----

    def save_upload_task(self, task: dict) -> None:
        now = time.time()
        with self._write_lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO upload_tasks
                (task_id, account_name, file_name, file_size, local_path,
                 target_dir_id, status, progress, error, bucket,
                 storage_node, upload_key, upload_id_s3, up_file_id,
                 total_parts, block_size, etag, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task["task_id"], task.get("account_name", ""),
                    task["file_name"], task.get("file_size", 0),
                    task["local_path"], task.get("target_dir_id", 0),
                    task.get("status", "等待中"), task.get("progress", 0),
                    task.get("error", ""), task.get("bucket", ""),
                    task.get("storage_node", ""), task.get("upload_key", ""),
                    task.get("upload_id_s3", ""), task.get("up_file_id", 0),
                    task.get("total_parts", 0), task.get("block_size", 8388608),
                    task.get("etag", ""), now, now,
                ),
            )
            self._conn.execute(
                """UPDATE upload_tasks
                SET account_name = ?,
                    file_name = ?,
                    file_size = ?,
                    local_path = ?,
                    target_dir_id = ?,
                    status = ?,
                    progress = ?,
                    error = ?,
                    bucket = ?,
                    storage_node = ?,
                    upload_key = ?,
                    upload_id_s3 = ?,
                    up_file_id = ?,
                    total_parts = ?,
                    block_size = ?,
                    etag = ?,
                    updated_at = ?
                WHERE task_id = ?""",
                (
                    task.get("account_name", ""),
                    task["file_name"],
                    task.get("file_size", 0),
                    task["local_path"],
                    task.get("target_dir_id", 0),
                    task.get("status", "等待中"),
                    task.get("progress", 0),
                    task.get("error", ""),
                    task.get("bucket", ""),
                    task.get("storage_node", ""),
                    task.get("upload_key", ""),
                    task.get("upload_id_s3", ""),
                    task.get("up_file_id", 0),
                    task.get("total_parts", 0),
                    task.get("block_size", 8388608),
                    task.get("etag", ""),
                    now,
                    task["task_id"],
                ),
            )
            self._conn.commit()

    def get_upload_task(self, task_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM upload_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_upload_task(row)

    def get_upload_tasks(self, account_name: str = None) -> list[dict]:
        if account_name:
            rows = self._conn.execute(
                "SELECT * FROM upload_tasks WHERE account_name = ?",
                (account_name,),
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM upload_tasks").fetchall()
        return [self._row_to_upload_task(row) for row in rows]

    def update_upload_task(self, task_id: str, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [task_id]
        with self._write_lock:
            self._conn.execute(
                f"UPDATE upload_tasks SET {set_clause} WHERE task_id = ?",
                values,
            )
            self._conn.commit()

    def delete_upload_task(self, task_id: str) -> None:
        with self._write_lock:
            self._conn.execute(
                "DELETE FROM upload_tasks WHERE task_id = ?", (task_id,)
            )
            self._conn.commit()

    def _row_to_upload_task(self, row) -> dict:
        cols = [
            "task_id", "account_name", "file_name", "file_size", "local_path",
            "target_dir_id", "status", "progress", "error", "bucket",
            "storage_node", "upload_key", "upload_id_s3", "up_file_id",
            "total_parts", "block_size", "etag", "created_at", "updated_at",
        ]
        return dict(zip(cols, row))

    # ---- Upload parts ----

    def record_upload_part(self, task_id: str, part_index: int, etag: str = "") -> None:
        with self._write_lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO upload_parts
                (task_id, part_index, etag, uploaded)
                VALUES (?, ?, ?, 1)""",
                (task_id, part_index, etag),
            )
            self._conn.commit()

    def get_upload_parts(self, task_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT task_id, part_index, etag, uploaded FROM upload_parts "
            "WHERE task_id = ? ORDER BY part_index",
            (task_id,),
        ).fetchall()
        return [
            {"task_id": r[0], "part_index": r[1], "etag": r[2], "uploaded": r[3]}
            for r in rows
        ]
