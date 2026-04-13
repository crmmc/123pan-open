import sqlite3

import pytest

from src.app.common import database as database_module
from src.app.common.database import Database, _safe_int, _safe_float, get_upload_part_size, get_download_part_size


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
        import sqlite3, json
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

        import sqlite3
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
    import src.app.common.database as db_mod
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
