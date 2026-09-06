import importlib
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.app.common import config as config_module
from src.app.common import database as database_module
from src.app.common.database import (
    CURRENT_SCHEMA_VERSION,
    Database,
    _safe_float,
    _safe_int,
    get_download_part_mode,
    get_download_part_size,
    get_upload_part_size,
)


def _use_temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "123pan-open.db"
    monkeypatch.setattr(database_module, "_get_db_path", lambda: db_path)
    Database.reset()
    return Database.instance()


def test_database_initializes_default_config(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)

    assert db.get_config("rememberPassword", None) is False
    assert db.get_config("stayLoggedIn", None) is True
    assert db.get_config("defaultDownloadPath", "")
    assert db.get_config("maxDownloadThreads", None) == 1
    assert db.get_config("retryMaxAttempts", None) == 3
    assert db.get_config("downloadPartMode", None) == "auto"


def test_database_set_and_get_config(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)

    db.set_config("rememberPassword", True)
    db.set_config("retryMaxAttempts", 7)

    assert db.get_config("rememberPassword", None) is True
    assert db.get_config("retryMaxAttempts", None) == 7


def test_database_set_many_config_updates_multiple_values(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)

    db.set_many_config({
        "defaultDownloadPath": str(tmp_path / "downloads"),
        "maxConcurrentDownloads": 5,
    })

    all_config = db.get_all_config()
    assert all_config["defaultDownloadPath"] == str(tmp_path / "downloads")
    assert all_config["maxConcurrentDownloads"] == 5


class TestAutoLoginMigration:
    """测试 autoLogin 迁移到 rememberPassword + stayLoggedIn"""

    def _create_legacy_db(self, db_path, auto_login_value):
        """创建旧版 DB（schema version 1，含 autoLogin 键）"""
        import sqlite3, json  # pylint: disable=reimported
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA user_version = 1")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS config "
            "(key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            ("autoLogin", json.dumps(auto_login_value)),
        )
        conn.commit()
        conn.close()

    def test_migrates_auto_login_true(self, tmp_path, monkeypatch):
        db_path = tmp_path / "123pan-open.db"
        monkeypatch.setattr(database_module, "_get_db_path", lambda: db_path)
        Database.reset()

        self._create_legacy_db(db_path, True)

        Database.reset()
        db = Database.instance()

        assert db.get_config("rememberPassword", None) is True
        assert db.get_config("stayLoggedIn", None) is True
        assert db.get_config("autoLogin", "NOT_FOUND") == "NOT_FOUND"

    def test_migrates_auto_login_false(self, tmp_path, monkeypatch):
        db_path = tmp_path / "123pan-open.db"
        monkeypatch.setattr(database_module, "_get_db_path", lambda: db_path)
        Database.reset()

        self._create_legacy_db(db_path, False)

        Database.reset()
        db = Database.instance()

        assert db.get_config("rememberPassword", None) is False
        assert db.get_config("stayLoggedIn", None) is True
        assert db.get_config("autoLogin", "NOT_FOUND") == "NOT_FOUND"

    def test_no_migration_when_no_auto_login_key(self, tmp_path, monkeypatch):
        db_path = tmp_path / "123pan-open.db"
        monkeypatch.setattr(database_module, "_get_db_path", lambda: db_path)
        Database.reset()

        import sqlite3  # pylint: disable=reimported
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA user_version = 1")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS config "
            "(key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)"
        )
        conn.commit()
        conn.close()

        Database.reset()
        db = Database.instance()

        # _init_defaults 用 INSERT OR IGNORE 设置默认值
        assert db.get_config("rememberPassword", None) is False
        assert db.get_config("stayLoggedIn", None) is True


# ---- 1a. _safe_int / _safe_float ----


def test_safe_int_none_returns_default():
    assert _safe_int(None, 42) == 42


def test_safe_int_non_numeric_returns_default():
    assert _safe_int("abc", 7) == 7


def test_safe_int_float_string_returns_default():
    assert _safe_int("3.7", 0) == 0


def test_safe_int_clamps_min():
    assert _safe_int(5, 0, min_val=10) == 10


def test_safe_int_clamps_max():
    assert _safe_int(200, 0, max_val=100) == 100


def test_safe_int_within_range():
    assert _safe_int(50, 0, min_val=10, max_val=100) == 50


def test_safe_float_none_returns_default():
    assert _safe_float(None, 3.14) == 3.14


def test_safe_float_clamps_min_max():
    assert _safe_float(0.5, 0.0, min_val=1.0, max_val=5.0) == 1.0
    assert _safe_float(10.0, 0.0, min_val=1.0, max_val=5.0) == 5.0


# ---- 1b. Download Task CRUD ----


def test_save_and_get_download_task_roundtrip(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.save_download_task({
        "resume_id": "rid-1",
        "account_name": "alice",
        "file_name": "test.bin",
        "file_id": 100,
        "file_type": 0,
        "file_size": 2048,
        "save_path": "/tmp/test.bin",
        "current_dir_id": 5,
        "etag": "abc123",
        "s3key_flag": 1,
        "status": "等待中",
        "progress": 0,
        "error": "",
        "supports_resume": 1,
        "metadata_version": 2,
    })
    task = db.get_download_task("rid-1")
    assert task is not None
    assert task["resume_id"] == "rid-1"
    assert task["account_name"] == "alice"
    assert task["file_name"] == "test.bin"
    assert task["file_id"] == 100
    assert task["file_size"] == 2048
    assert task["save_path"] == "/tmp/test.bin"
    assert task["etag"] == "abc123"
    assert task["status"] == "等待中"


def test_get_download_task_returns_none_when_missing(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    assert db.get_download_task("nonexistent") is None


def test_get_download_tasks_filters_by_account(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.save_download_task({"resume_id": "a", "account_name": "alice", "file_name": "f", "file_id": 1, "save_path": "/a"})
    db.save_download_task({"resume_id": "b", "account_name": "bob", "file_name": "f", "file_id": 2, "save_path": "/b"})
    assert [t["resume_id"] for t in db.get_download_tasks("alice")] == ["a"]
    assert len(db.get_download_tasks()) == 2


def test_update_download_task_modifies_fields(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.save_download_task({"resume_id": "u1", "account_name": "a", "file_name": "f", "file_id": 1, "save_path": "/f"})
    db.update_download_task("u1", status="已完成", progress=100)
    task = db.get_download_task("u1")
    assert task["status"] == "已完成"
    assert task["progress"] == 100


def test_delete_download_task_removes_task(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.save_download_task({"resume_id": "d1", "account_name": "a", "file_name": "f", "file_id": 1, "save_path": "/f"})
    db.delete_download_task("d1")
    assert db.get_download_task("d1") is None


# ---- 1c. Download Parts CRUD ----


def test_record_and_get_download_parts(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.save_download_task({"resume_id": "p1", "account_name": "a", "file_name": "f", "file_id": 1, "save_path": "/f"})
    db.record_download_part("p1", {"index": 0, "start": 0, "end": 99, "expected_size": 100, "actual_size": 100, "md5": "h0"})
    db.record_download_part("p1", {"index": 1, "start": 100, "end": 199, "expected_size": 100, "actual_size": 100, "md5": "h1"})
    parts = db.get_download_parts("p1")
    assert len(parts) == 2
    assert parts[0]["part_index"] == 0
    assert parts[1]["md5"] == "h1"


def test_remove_download_part(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.save_download_task({"resume_id": "p2", "account_name": "a", "file_name": "f", "file_id": 1, "save_path": "/f"})
    db.record_download_part("p2", {"index": 0, "start": 0, "end": 99, "expected_size": 100, "md5": "h"})
    db.remove_download_part("p2", 0)
    assert db.get_download_parts("p2") == []


def test_record_download_part_upsert_replaces(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.save_download_task({"resume_id": "p3", "account_name": "a", "file_name": "f", "file_id": 1, "save_path": "/f"})
    db.record_download_part("p3", {"index": 0, "start": 0, "end": 99, "expected_size": 100, "md5": "old"})
    db.record_download_part("p3", {"index": 0, "start": 0, "end": 99, "expected_size": 100, "md5": "new"})
    parts = db.get_download_parts("p3")
    assert len(parts) == 1
    assert parts[0]["md5"] == "new"


def test_delete_task_cascades_parts(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.save_download_task({"resume_id": "p4", "account_name": "a", "file_name": "f", "file_id": 1, "save_path": "/f"})
    db.record_download_part("p4", {"index": 0, "start": 0, "end": 99, "expected_size": 100, "md5": "h"})
    db.delete_download_task("p4")
    assert db.get_download_parts("p4") == []


# ---- 1d. Upload Task CRUD ----


def test_save_and_get_upload_task_roundtrip(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.save_upload_task({
        "task_id": "ut-1",
        "account_name": "alice",
        "file_name": "upload.bin",
        "file_size": 4096,
        "local_path": "/tmp/upload.bin",
        "target_dir_id": 7,
        "status": "等待中",
        "progress": 0,
        "error": "",
        "bucket": "bkt",
        "storage_node": "node1",
        "upload_key": "key1",
        "upload_id_s3": "s3id",
        "up_file_id": 42,
        "total_parts": 1,
        "block_size": 5 * 1024 * 1024,
        "etag": "etag-val",
        "file_mtime": 1700000000.0,
    })
    task = db.get_upload_task("ut-1")
    assert task is not None
    assert task["task_id"] == "ut-1"
    assert task["account_name"] == "alice"
    assert task["file_size"] == 4096
    assert task["local_path"] == "/tmp/upload.bin"
    assert task["target_dir_id"] == 7
    assert task["bucket"] == "bkt"
    assert task["etag"] == "etag-val"


def test_get_upload_task_returns_none_when_missing(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    assert db.get_upload_task("nope") is None


def test_get_upload_tasks_filters_by_account(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.save_upload_task({"task_id": "u1", "account_name": "alice", "file_name": "f", "local_path": "/f"})
    db.save_upload_task({"task_id": "u2", "account_name": "bob", "file_name": "f", "local_path": "/f"})
    assert [t["task_id"] for t in db.get_upload_tasks("alice")] == ["u1"]
    assert len(db.get_upload_tasks()) == 2


def test_update_upload_task_modifies_fields(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.save_upload_task({"task_id": "uu1", "account_name": "a", "file_name": "f", "local_path": "/f"})
    db.update_upload_task("uu1", status="已完成", progress=100)
    task = db.get_upload_task("uu1")
    assert task["status"] == "已完成"
    assert task["progress"] == 100


def test_delete_upload_task_removes_task(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.save_upload_task({"task_id": "dd1", "account_name": "a", "file_name": "f", "local_path": "/f"})
    db.delete_upload_task("dd1")
    assert db.get_upload_task("dd1") is None


# ---- 1e. Upload Parts CRUD ----


def test_record_and_get_upload_parts(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.save_upload_task({"task_id": "up1", "account_name": "a", "file_name": "f", "local_path": "/f"})
    db.record_upload_part("up1", 0, "etag-0")
    db.record_upload_part("up1", 1, "etag-1")
    parts = db.get_upload_parts("up1")
    assert len(parts) == 2
    assert parts[0]["part_index"] == 0
    assert parts[0]["etag"] == "etag-0"
    assert parts[0]["uploaded"] == 1


def test_delete_upload_parts_clears_all(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.save_upload_task({"task_id": "up2", "account_name": "a", "file_name": "f", "local_path": "/f"})
    db.record_upload_part("up2", 0, "e")
    db.record_upload_part("up2", 1, "e")
    db.delete_upload_parts("up2")
    assert db.get_upload_parts("up2") == []


def test_delete_upload_task_cascades_parts(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.save_upload_task({"task_id": "up3", "account_name": "a", "file_name": "f", "local_path": "/f"})
    db.record_upload_part("up3", 0, "e")
    db.delete_upload_task("up3")
    assert db.get_upload_parts("up3") == []


def test_reset_commits_unflushed_download_parts(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.save_download_task({
        "resume_id": "dl-reset",
        "account_name": "a",
        "file_name": "f.bin",
        "file_id": 1,
        "save_path": str(tmp_path / "f.bin"),
    })
    db.record_download_part("dl-reset", {
        "index": 0,
        "start": 0,
        "end": 9,
        "expected_size": 10,
        "actual_size": 10,
        "md5": "hash-0",
    }, commit=False)

    Database.reset()
    db2 = Database.instance()

    parts = db2.get_download_parts("dl-reset")
    assert len(parts) == 1
    assert parts[0]["md5"] == "hash-0"


def test_reset_commits_unflushed_upload_parts(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.save_upload_task({"task_id": "up-reset", "account_name": "a", "file_name": "f", "local_path": "/f"})
    db.record_upload_part("up-reset", 0, "etag-0", commit=False)

    Database.reset()
    db2 = Database.instance()

    parts = db2.get_upload_parts("up-reset")
    assert len(parts) == 1
    assert parts[0]["etag"] == "etag-0"


def test_reset_raises_when_commit_fails(tmp_path, monkeypatch):
    """P1-10: commit 失败时 reset() 不再抛异常，但仍清除单例引用。"""
    db = _use_temp_db(tmp_path, monkeypatch)
    real_conn = db._conn

    class _FailingConn:
        def commit(self):
            raise sqlite3.OperationalError("commit failed")

        def close(self):
            return None

    db._conn = _FailingConn()

    # P1-10: commit 失败不再抛异常
    Database.reset()

    # 单例已被清除，重新创建
    import src.app.common.database as db_mod  # pylint: disable=reimported
    assert db_mod._db_instance is None


# ---- 1f. get_upload_part_size / get_download_part_size ----


def test_get_upload_part_size_reads_config(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.set_config("uploadPartSizeMB", 8)
    assert get_upload_part_size() == 8 * 1024 * 1024


def test_get_upload_part_size_clamps_invalid(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.set_config("uploadPartSizeMB", 0)
    assert get_upload_part_size() == 5 * 1024 * 1024  # clamped to min=5
    db.set_config("uploadPartSizeMB", 99)
    assert get_upload_part_size() == 16 * 1024 * 1024  # clamped to max=16


def test_get_download_part_size_reads_config(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.set_config("downloadPartSizeMB", 10)
    assert get_download_part_size() == 10 * 1024 * 1024


def test_get_download_part_mode_reads_config(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.set_config("downloadPartMode", "fixed")

    assert get_download_part_mode() == "fixed"


def test_get_download_part_mode_defaults_invalid_to_auto(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.set_config("downloadPartMode", "bad")

    assert get_download_part_mode() == "auto"


# ---- 1g. update_*_task 入参守卫 ----


def test_update_download_task_noop_without_fields(temp_db):
    temp_db.update_download_task("no-such-id")  # 无字段时直接返回，不抛异常


def test_update_download_task_rejects_unknown_columns(temp_db):
    with pytest.raises(ValueError, match="Unknown download task columns"):
        temp_db.update_download_task("rid", not_a_column=1)


def test_update_upload_task_noop_without_fields(temp_db):
    temp_db.update_upload_task("no-such-id")


def test_update_upload_task_rejects_unknown_columns(temp_db):
    with pytest.raises(ValueError, match="Unknown upload task columns"):
        temp_db.update_upload_task("tid", not_a_column=1)


# ---- 1h. get_config / get_all_config 对非法 JSON 的防御 ----


def test_get_config_returns_default_for_invalid_json(temp_db):
    temp_db._conn.execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES ('bad', 'not-json')"
    )
    temp_db._conn.commit()

    assert temp_db.get_config("bad", "fallback") == "fallback"


def test_get_all_config_skips_invalid_json_rows(temp_db):
    temp_db.set_config("good", 1)
    temp_db._conn.execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES ('bad', '{oops')"
    )
    temp_db._conn.commit()

    result = temp_db.get_all_config()

    assert result["good"] == 1
    assert "bad" not in result


# ---- 1i. _get_db_path / reset / closed 防御 ----


def test_get_db_path_creates_config_dir(tmp_path, monkeypatch):
    target_dir = tmp_path / "cfg"
    monkeypatch.setattr(config_module, "CONFIG_DIR", target_dir)

    path = database_module._get_db_path()

    assert path == target_dir / "123pan-open.db"
    assert target_dir.exists()


def test_reset_swallows_close_failure(tmp_path, monkeypatch):
    """141-142 行：conn.close() 抛异常时 reset() 仍清除单例且不抛出。"""
    db = _use_temp_db(tmp_path, monkeypatch)

    class _CloseFailingConn:
        def commit(self):
            return None

        def close(self):
            raise sqlite3.OperationalError("close failed")

    db._conn = _CloseFailingConn()

    Database.reset()

    assert database_module._db_instance is None


def test_config_access_after_reset_raises(tmp_path, monkeypatch):
    """146 行：reset 后旧实例上的操作应抛 RuntimeError。"""
    db = _use_temp_db(tmp_path, monkeypatch)

    Database.reset()  # 旧实例被标记为 closed

    with pytest.raises(RuntimeError, match="Database connection is closed"):
        db.get_config("rememberPassword", None)


# ---- 1j. _migrate 补列与回滚 ----


def test_migrate_maps_invalid_auto_login_json_to_false(tmp_path, monkeypatch):
    """237-238 行：autoLogin 值不是合法 JSON 时按 False 迁移。"""
    db_path = tmp_path / "123pan-open.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version = 1")
    conn.execute(
        "CREATE TABLE config (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO config (key, value) VALUES ('autoLogin', 'not-json')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(database_module, "_get_db_path", lambda: db_path)
    Database.reset()
    db = Database.instance()

    assert db.get_config("rememberPassword", None) is False
    assert db.get_config("stayLoggedIn", None) is True


def test_migrate_adds_missing_columns_from_v2_schema(tmp_path, monkeypatch):
    """254/264 行：v2 旧库缺 delete_requested / file_mtime / part_size 时补列。"""
    db_path = tmp_path / "123pan-open.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version = 2")
    conn.execute(
        "CREATE TABLE config (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE upload_tasks ("
        "account_name TEXT NOT NULL DEFAULT '', "
        "task_id TEXT PRIMARY KEY, file_name TEXT NOT NULL, local_path TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE download_tasks ("
        "account_name TEXT NOT NULL DEFAULT '', "
        "resume_id TEXT PRIMARY KEY, file_name TEXT NOT NULL, save_path TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(database_module, "_get_db_path", lambda: db_path)
    Database.reset()
    db = Database.instance()

    upload_cols = {
        row[1] for row in db._conn.execute("PRAGMA table_info(upload_tasks)").fetchall()
    }
    assert {"delete_requested", "file_mtime"} <= upload_cols
    download_cols = {
        row[1]
        for row in db._conn.execute("PRAGMA table_info(download_tasks)").fetchall()
    }
    assert "part_size" in download_cols
    assert db._conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION


class _MigrationBoomConn:
    """代理真实连接：仅在写入 user_version 的 PRAGMA 上抛错，触发 _migrate 回滚。"""

    def __init__(self, real_conn):
        self._real = real_conn

    def execute(self, sql, *args, **kwargs):
        if sql.startswith("PRAGMA user_version ="):
            raise sqlite3.OperationalError("simulated migration failure")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_migrate_rolls_back_and_reraises_on_failure(tmp_path, monkeypatch):
    """282-284 行：迁移中途失败时回滚事务并向上抛出。"""
    db_path = tmp_path / "123pan-open.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version = 1")
    conn.execute(
        "CREATE TABLE config (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO config (key, value) VALUES ('autoLogin', 'true')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(database_module, "_get_db_path", lambda: db_path)
    real_connect = sqlite3.connect

    def _connect(path, *args, **kwargs):
        return _MigrationBoomConn(real_connect(path, *args, **kwargs))

    monkeypatch.setattr(database_module.sqlite3, "connect", _connect)
    Database.reset()

    with pytest.raises(sqlite3.OperationalError):
        Database.instance()

    assert database_module._db_instance is None
    # 回滚校验：user_version 与 autoLogin 保持原样，迁移产物不存在
    raw = real_connect(str(db_path))
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 1
    assert (
        raw.execute("SELECT value FROM config WHERE key = 'autoLogin'").fetchone()[0]
        == "true"
    )
    assert (
        raw.execute(
            "SELECT COUNT(*) FROM config WHERE key = 'rememberPassword'"
        ).fetchone()[0]
        == 0
    )
    raw.close()


# ---- 1k. config.py：isWin11 与 CONFIG_DIR 平台分支 ----


@pytest.mark.parametrize(
    ("platform_name", "win_build", "expected"),
    [
        ("darwin", None, False),
        ("win32", 22631, True),
        ("win32", 19045, False),
        ("win32", None, False),  # getwindowsversion 缺失 → AttributeError 防御分支
    ],
)
def test_is_win11_build_matrix(monkeypatch, platform_name, win_build, expected):
    monkeypatch.setattr(sys, "platform", platform_name)
    if win_build is None:
        monkeypatch.delattr(sys, "getwindowsversion", raising=False)
    else:
        monkeypatch.setattr(
            sys, "getwindowsversion", lambda: SimpleNamespace(build=win_build),
            raising=False,
        )

    assert config_module.isWin11() is expected


def test_config_dir_windows_uses_appdata(tmp_path):
    """18 行：Windows 下 CONFIG_DIR 取 APPDATA。"""
    original = config_module.CONFIG_DIR
    with patch.object(config_module.platform, "system", return_value="Windows"), \
         patch.dict(os.environ, {"APPDATA": str(tmp_path / "roaming")}):
        module = importlib.reload(config_module)
        assert module.CONFIG_DIR == Path(tmp_path / "roaming") / "123pan-open"
    importlib.reload(config_module)  # 恢复当前平台分支
    assert config_module.CONFIG_DIR == original


def test_config_dir_linux_uses_xdg(tmp_path):
    """25-26 行：Linux 下优先 XDG_CONFIG_HOME。"""
    original = config_module.CONFIG_DIR
    xdg_dir = tmp_path / "xdg"
    with patch.object(config_module.platform, "system", return_value="Linux"), \
         patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg_dir)}):
        module = importlib.reload(config_module)
        assert module.CONFIG_DIR == xdg_dir / "123pan-open"
    importlib.reload(config_module)
    assert config_module.CONFIG_DIR == original


def test_config_dir_linux_falls_back_to_home():
    """26 行：无 XDG_CONFIG_HOME 时回退 ~/.config。"""
    original = config_module.CONFIG_DIR
    with patch.object(config_module.platform, "system", return_value="Linux"), \
         patch.dict(os.environ):
        os.environ.pop("XDG_CONFIG_HOME", None)
        module = importlib.reload(config_module)
        assert module.CONFIG_DIR == Path.home() / ".config" / "123pan-open"
    importlib.reload(config_module)
    assert config_module.CONFIG_DIR == original
