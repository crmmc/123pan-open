from PySide6.QtWidgets import QApplication
from unittest.mock import patch

from src.app.common import database as database_module
from src.app.common.database import Database
from src.app.view.setting_interface import SettingInterface

app = QApplication.instance() or QApplication([])


def _use_temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "123pan-open.db"
    monkeypatch.setattr(database_module, "_get_db_path", lambda: db_path)
    Database.reset()
    return Database.instance()


def test_setting_interface_uses_database_defaults_for_download_controls(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)

    interface = SettingInterface()

    assert interface.downloadThreadsSpinBox.value() == 1
    assert interface.concurrentDownloadsSpinBox.value() == 5


def test_setting_interface_clears_password_and_token_when_switches_disabled(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.set_many_config({
        "rememberPassword": True,
        "stayLoggedIn": True,
    })

    interface = SettingInterface()

    with patch("src.app.common.credential_store.delete_credential") as mock_del:
        interface._SettingInterface__onRememberPasswordChanged(False)
        interface._SettingInterface__onStayLoggedInChanged(False)
        mock_del.assert_any_call("passWord")
        mock_del.assert_any_call("authorization")

    assert db.get_config("rememberPassword", None) is False
    assert db.get_config("stayLoggedIn", None) is False


def test_setting_interface_saves_current_password_when_remember_enabled(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    interface = SettingInterface.__new__(SettingInterface)
    interface.window = lambda: type("_Window", (), {"pan": type("_Pan", (), {"password": "secret"})()})()

    with patch("src.app.common.credential_store.save_credential") as mock_save:
        interface._SettingInterface__onRememberPasswordChanged(True)

    assert db.get_config("rememberPassword", None) is True
    mock_save.assert_called_once_with("passWord", "secret")


def test_setting_interface_saves_current_authorization_when_stay_logged_in_enabled(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    interface = SettingInterface.__new__(SettingInterface)
    interface.window = lambda: type(
        "_Window",
        (),
        {"pan": type("_Pan", (), {"authorization": "Bearer token"})()},
    )()

    with patch("src.app.common.credential_store.save_credential") as mock_save:
        interface._SettingInterface__onStayLoggedInChanged(True)

    assert db.get_config("stayLoggedIn", None) is True
    mock_save.assert_called_once_with("authorization", "Bearer token")


def test_setting_interface_clamps_invalid_numeric_config(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.set_many_config({
        "maxDownloadThreads": "oops",
        "maxUploadThreads": 99,
        "maxConcurrentDownloads": -3,
        "maxConcurrentUploads": "bad",
        "retryMaxAttempts": None,
        "downloadPartSizeMB": "NaN",
        "uploadPartSizeMB": 100,
    })

    interface = SettingInterface()

    assert interface.downloadThreadsSpinBox.value() == 1
    assert interface.uploadThreadsSpinBox.value() == 16
    assert interface.concurrentDownloadsSpinBox.value() == 1
    assert interface.concurrentUploadsSpinBox.value() == 3
    assert interface.retryAttemptsComboBox.currentIndex() == 3
    assert interface.downloadPartSizeSpinBox.value() == 5
    assert interface.uploadPartSizeSpinBox.value() == 16
