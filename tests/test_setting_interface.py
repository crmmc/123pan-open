from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from src.app.common import database as database_module
from src.app.common.database import Database
from src.app.common.log import LOG_FILE
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


# ---------------------------------------------------------------------------
# 批次 8 追加：剩余设置卡片读写与 refresh_from_db
# ---------------------------------------------------------------------------


def test_setting_interface_about_card_appends_build_time(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    monkeypatch.setattr("src.app.view.setting_interface.BUILD_TIME", "20260906")

    interface = SettingInterface()

    assert "构建于 20260906" in interface.aboutCard.contentLabel.text()


def test_setting_interface_about_card_omits_empty_build_time(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    monkeypatch.setattr("src.app.view.setting_interface.BUILD_TIME", "")

    interface = SettingInterface()

    assert "构建于" not in interface.aboutCard.contentLabel.text()


def test_setting_interface_download_folder_card_persists_choice(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    interface = SettingInterface()
    target = str(tmp_path / "downloads")

    with patch(
        "src.app.view.setting_interface.QFileDialog.getExistingDirectory",
        return_value=target,
    ):
        interface._SettingInterface__onDownloadFolderCardClicked()

    assert db.get_config("defaultDownloadPath", "") == target
    assert interface.downloadFolderCard.contentLabel.text() == target


def test_setting_interface_download_folder_card_ignores_empty_choice(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    interface = SettingInterface()
    before = db.get_config("defaultDownloadPath", None)

    with patch(
        "src.app.view.setting_interface.QFileDialog.getExistingDirectory",
        return_value="",
    ):
        interface._SettingInterface__onDownloadFolderCardClicked()

    assert before is not None  # 建库时已写入默认下载目录
    assert db.get_config("defaultDownloadPath", None) == before
    assert interface.downloadFolderCard.contentLabel.text() == before


def test_setting_interface_download_folder_card_ignores_unchanged_folder(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    same = str(tmp_path / "same")
    db.set_config("defaultDownloadPath", same)
    interface = SettingInterface()

    with patch(
        "src.app.view.setting_interface.QFileDialog.getExistingDirectory",
        return_value=same,
    ):
        interface._SettingInterface__onDownloadFolderCardClicked()

    assert db.get_config("defaultDownloadPath", "") == same
    assert interface.downloadFolderCard.contentLabel.text() == same


def test_setting_interface_ask_download_location_slot_persists(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    interface = SettingInterface()

    interface._SettingInterface__onAskDownloadLocationChanged(False)
    interface._SettingInterface__onAskDownloadLocationChanged(True)

    assert db.get_config("askDownloadLocation", None) is True


def test_setting_interface_numeric_slots_persist_values(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    interface = SettingInterface()

    interface._SettingInterface__onDownloadThreadsChanged(8)
    interface._SettingInterface__onUploadThreadsChanged(2)
    interface._SettingInterface__onConcurrentDownloadsChanged(3)
    interface._SettingInterface__onConcurrentUploadsChanged(1)
    interface._SettingInterface__onDownloadPartSizeChanged(20)
    interface._SettingInterface__onUploadPartSizeChanged(10)

    assert db.get_config("maxDownloadThreads", None) == 8
    assert db.get_config("maxUploadThreads", None) == 2
    assert db.get_config("maxConcurrentDownloads", None) == 3
    assert db.get_config("maxConcurrentUploads", None) == 1
    assert db.get_config("downloadPartSizeMB", None) == 20
    assert db.get_config("uploadPartSizeMB", None) == 10


def test_setting_interface_part_mode_slot_maps_index_to_mode(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    interface = SettingInterface()

    interface._SettingInterface__onDownloadPartModeChanged(0)
    assert db.get_config("downloadPartMode", "") == "auto"

    interface._SettingInterface__onDownloadPartModeChanged(1)
    assert db.get_config("downloadPartMode", "") == "fixed"


def test_setting_interface_retry_attempts_slot_persists_selected_number(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    interface = SettingInterface()

    interface.retryAttemptsComboBox.setCurrentIndex(5)  # 触发 currentIndexChanged

    assert db.get_config("retryMaxAttempts", None) == 5


def test_setting_interface_log_level_slot_persists_and_applies(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    interface = SettingInterface()

    with patch("src.app.view.setting_interface.set_log_level") as mock_set:
        interface.logLevelComboBox.setCurrentIndex(2)  # WARNING

    assert db.get_config("logLevel", None) == "WARNING"
    mock_set.assert_called_once_with("WARNING")


def test_setting_interface_open_log_card_opens_log_file_url(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    interface = SettingInterface()

    with patch("src.app.view.setting_interface.QDesktopServices.openUrl") as mock_open:
        interface._SettingInterface__onOpenLogFileClicked()

    mock_open.assert_called_once()
    url = mock_open.call_args[0][0]
    assert url.toLocalFile() == str(LOG_FILE)


def test_setting_interface_refresh_from_db_reloads_all_controls(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    interface = SettingInterface()
    folder = str(tmp_path / "新目录")
    db.set_many_config({
        "defaultDownloadPath": folder,
        "askDownloadLocation": False,
        "rememberPassword": True,
        "stayLoggedIn": False,
        "maxDownloadThreads": 4,
        "maxUploadThreads": 8,
        "maxConcurrentDownloads": 2,
        "maxConcurrentUploads": 4,
        "retryMaxAttempts": 5,
        "downloadPartSizeMB": 16,
        "downloadPartMode": "fixed",
        "uploadPartSizeMB": 12,
        "logLevel": "WARNING",
    })

    with patch("src.app.common.credential_store.save_credential"), \
         patch("src.app.common.credential_store.delete_credential"), \
         patch("src.app.view.setting_interface.set_log_level"):
        interface.refresh_from_db()

    assert interface.downloadFolderCard.contentLabel.text() == folder
    assert interface.askDownloadLocationCard.isChecked() is False
    assert interface.rememberPasswordCard.isChecked() is True
    assert interface.stayLoggedInCard.isChecked() is False
    assert interface.downloadThreadsSpinBox.value() == 4
    assert interface.uploadThreadsSpinBox.value() == 8
    assert interface.concurrentDownloadsSpinBox.value() == 2
    assert interface.concurrentUploadsSpinBox.value() == 4
    assert interface.retryAttemptsComboBox.currentIndex() == 5
    assert interface.downloadPartSizeSpinBox.value() == 16
    assert interface.downloadPartModeComboBox.currentIndex() == 1
    assert interface.uploadPartSizeSpinBox.value() == 12
    assert interface.logLevelComboBox.currentIndex() == 2


def test_setting_interface_refresh_from_db_keeps_unknown_log_level(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    interface = SettingInterface()
    db.set_config("logLevel", "BOGUS")

    with patch("src.app.common.credential_store.save_credential"), \
         patch("src.app.common.credential_store.delete_credential"), \
         patch("src.app.view.setting_interface.set_log_level"):
        interface.refresh_from_db()

    assert interface.logLevelComboBox.currentIndex() == 1  # 非法值保持默认 INFO
