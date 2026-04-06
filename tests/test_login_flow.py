from src.app.common import database as database_module
from src.app.common.database import Database
from src.app.view.login_window import (
    has_saved_credentials,
    should_auto_login,
    update_auto_login_setting,
)


def _use_temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "123pan.db"
    monkeypatch.setattr(database_module, "_get_db_path", lambda: db_path)
    Database.reset()
    return Database.instance()


def test_should_auto_login_requires_saved_credentials(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.set_many_config({
        "userName": "alice",
        "passWord": "secret",
        "autoLogin": True,
    })

    assert has_saved_credentials(db) is True
    assert should_auto_login(db) is True

    db.set_config("passWord", "")
    assert has_saved_credentials(db) is False
    assert should_auto_login(db) is False


def test_update_auto_login_setting_persists_value(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    monkeypatch.setattr("src.app.view.login_window.Database.instance", lambda: db)

    update_auto_login_setting(True)
    assert db.get_config("autoLogin", None) is True

    update_auto_login_setting(False)
    assert db.get_config("autoLogin", None) is False
