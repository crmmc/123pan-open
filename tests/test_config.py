from src.app.common import database as database_module
from src.app.common.database import Database


def _use_temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "123pan.db"
    monkeypatch.setattr(database_module, "_get_db_path", lambda: db_path)
    Database.reset()
    return Database.instance()


def test_database_initializes_default_config(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)

    assert db.get_config("autoLogin", None) is False
    assert db.get_config("defaultDownloadPath", "")
    assert db.get_config("maxDownloadThreads", None) == 3
    assert db.get_config("retryMaxAttempts", None) == 3
    assert db.get_config("retryBackoffFactor", None) == 0.5


def test_database_set_and_get_config(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)

    db.set_config("autoLogin", True)
    db.set_config("retryMaxAttempts", 7)

    assert db.get_config("autoLogin", None) is True
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
