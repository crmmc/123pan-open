import sys
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QApplication

sys.modules.setdefault("qrcode", MagicMock())

from src.app.common import database as database_module
from src.app.common.database import Database
from src.app.view.login_window import (
    has_saved_credentials,
    login_with_credentials,
    try_token_probe,
)

# QRLoginPage 等 Qt widget 需要 QApplication 实例
app = QApplication.instance() or QApplication([])


def _use_temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "123pan-open.db"
    monkeypatch.setattr(database_module, "_get_db_path", lambda: db_path)
    Database.reset()
    return Database.instance()


class TestHasSavedCredentials:
    def test_returns_true_when_both_present(self, tmp_path, monkeypatch):
        db = _use_temp_db(tmp_path, monkeypatch)
        db.set_config("userName", "alice")
        with patch("src.app.view.login_window.load_credential", return_value="secret"):
            assert has_saved_credentials(db) is True

    def test_returns_false_when_no_password(self, tmp_path, monkeypatch):
        db = _use_temp_db(tmp_path, monkeypatch)
        db.set_config("userName", "alice")
        with patch("src.app.view.login_window.load_credential", return_value=""):
            assert has_saved_credentials(db) is False

    def test_returns_false_when_no_username(self, tmp_path, monkeypatch):
        db = _use_temp_db(tmp_path, monkeypatch)
        db.set_config("userName", "")
        with patch("src.app.view.login_window.load_credential", return_value="secret"):
            assert has_saved_credentials(db) is False


class TestTryTokenProbe:
    def test_returns_none_when_no_token(self, tmp_path, monkeypatch):
        db = _use_temp_db(tmp_path, monkeypatch)
        with patch("src.app.view.login_window.load_credential", return_value=""):
            assert try_token_probe(db) is None

    def test_returns_pan_when_token_valid(self, tmp_path, monkeypatch):
        db = _use_temp_db(tmp_path, monkeypatch)
        mock_pan = MagicMock()
        mock_pan.user_info.return_value = {"user": "alice"}
        with patch("src.app.view.login_window.load_credential", return_value="valid-token"), \
             patch("src.app.view.login_window.Pan123", return_value=mock_pan):
            result = try_token_probe(db)
        assert result is mock_pan

    def test_clears_token_when_invalid(self, tmp_path, monkeypatch):
        db = _use_temp_db(tmp_path, monkeypatch)
        mock_pan = MagicMock()
        mock_pan.user_info.return_value = None
        with patch("src.app.view.login_window.load_credential", return_value="expired-token"), \
             patch("src.app.view.login_window.Pan123", return_value=mock_pan), \
             patch("src.app.view.login_window.delete_credential") as mock_delete:
            result = try_token_probe(db)
        assert result is None
        assert db.get_config("authorization", "") == ""
        mock_delete.assert_called_once_with("authorization")

    def test_clears_token_on_exception(self, tmp_path, monkeypatch):
        db = _use_temp_db(tmp_path, monkeypatch)
        with patch("src.app.view.login_window.load_credential", return_value="bad-token"), \
             patch("src.app.view.login_window.Pan123", side_effect=Exception("connection error")), \
             patch("src.app.view.login_window.delete_credential") as mock_delete:
            result = try_token_probe(db)
        assert result is None
        assert db.get_config("authorization", "") == ""
        mock_delete.assert_called_once_with("authorization")


class TestLoginWithCredentials:
    def test_uses_current_input_password_instead_of_saved_password(self, tmp_path, monkeypatch):
        _use_temp_db(tmp_path, monkeypatch)
        mock_pan = MagicMock()
        mock_pan.login.return_value = 200
        with patch("src.app.view.login_window.Pan123", return_value=mock_pan) as mock_ctor:
            result = login_with_credentials("alice", "new-secret")

        assert result is mock_pan
        mock_ctor.assert_called_once_with(
            readfile=False,
            user_name="alice",
            password="new-secret",
        )


class TestQRLoginPage:
    """QR 登录页面逻辑测试（直接调用方法，不依赖 Qt event loop）。"""

    def _make_page(self):
        from src.app.view.qr_login_page import QRLoginPage
        page = QRLoginPage()
        # 模拟已初始化状态
        page._pan_temp = MagicMock()
        page._uni_id = "test-uni-id"
        page._consecutive_errors = 0
        # 保持 Python 引用防止 C++ 对象被过早删除
        self._page_ref = page
        return page

    def test_poll_login_success_emits_signal(self):
        page = self._make_page()
        mock_pan = MagicMock()
        signals = []
        page.loginSuccess.connect(lambda obj: signals.append(obj))
        # _on_poll_result → _handle_login_success (async via QThreadPool) → _on_login_verified → emit
        # 直接调用 _on_login_verified 测试最终信号发射
        page._on_login_verified(page._qr_flow_id, mock_pan)
        assert len(signals) == 1
        assert signals[0] is mock_pan

    def test_poll_waiting_no_signal(self):
        page = self._make_page()
        signals = []
        page.loginSuccess.connect(lambda obj: signals.append(obj))
        page._on_poll_result(page._qr_flow_id, {"loginStatus": 0})
        assert len(signals) == 0

    def test_poll_consecutive_errors_stops(self):
        page = self._make_page()
        page.poll_timer.start(1000)
        for _ in range(3):
            page._on_poll_error(page._qr_flow_id)
        assert not page.poll_timer.isActive()

    def test_do_poll_skips_when_poll_already_in_flight(self):
        page = self._make_page()
        page._poll_in_flight = True

        with patch("src.app.view.qr_login_page.QThreadPool.globalInstance") as mock_pool:
            page._do_poll()

        mock_pool.return_value.start.assert_not_called()

    def test_login_verified_drops_stale_flow_result(self):
        page = self._make_page()
        signals = []
        page.loginSuccess.connect(lambda obj: signals.append(obj))

        page._on_login_verified(page._qr_flow_id + 1, MagicMock())

        assert signals == []

    def test_qr_generated_closes_pan_temp_for_stale_flow(self):
        page = self._make_page()
        stale_pan = MagicMock()

        page._on_qr_generated(page._qr_flow_id + 1, {
            "_pan_temp": stale_pan,
            "uniID": "stale-uni",
            "url": "https://example.test/qr",
        })

        stale_pan.close.assert_called_once()


class TestQRLoginSuccess:
    """LoginDialog._on_qr_login_success 配置持久化测试。"""

    def _make_dialog(self, tmp_path, monkeypatch):
        db = _use_temp_db(tmp_path, monkeypatch)
        from src.app.view.login_window import LoginDialog
        dialog = LoginDialog()
        return dialog, db

    def test_saves_token_when_stay_logged_in(self, tmp_path, monkeypatch):
        dialog, db = self._make_dialog(tmp_path, monkeypatch)
        dialog.cb_stay_logged_in.setChecked(True)
        mock_pan = MagicMock()
        mock_pan.authorization = "Bearer test-jwt"
        mock_pan.user_name = "test-user"
        mock_pan.devicetype = "test-device"
        mock_pan.osversion = "test-os"
        mock_pan.loginuuid = "test-uuid"
        # Prevent dialog.accept() from actually closing
        with patch.object(dialog, "accept"), \
             patch("src.app.view.login_window.save_credential") as mock_save:
            dialog._on_qr_login_success(mock_pan)
            mock_save.assert_any_call("authorization", "Bearer test-jwt")
        assert db.get_config("userName", "") == "test-user"
        assert dialog.pan is mock_pan

    def test_clears_token_when_stay_logged_in_unchecked(self, tmp_path, monkeypatch):
        dialog, db = self._make_dialog(tmp_path, monkeypatch)
        dialog.cb_stay_logged_in.setChecked(False)
        mock_pan = MagicMock()
        mock_pan.authorization = "Bearer test-jwt"
        mock_pan.user_name = "test-user"
        mock_pan.devicetype = "test-device"
        mock_pan.osversion = "test-os"
        mock_pan.loginuuid = "test-uuid"
        with patch.object(dialog, "accept"), \
             patch("src.app.view.login_window.delete_credential") as mock_del:
            dialog._on_qr_login_success(mock_pan)
            mock_del.assert_any_call("authorization")

    def test_qr_login_clears_saved_password_state(self, tmp_path, monkeypatch):
        dialog, db = self._make_dialog(tmp_path, monkeypatch)
        db.set_config("rememberPassword", True)
        mock_pan = MagicMock()
        mock_pan.authorization = "Bearer test-jwt"
        mock_pan.user_name = "new-user"
        mock_pan.devicetype = "test-device"
        mock_pan.osversion = "test-os"
        mock_pan.loginuuid = "test-uuid"
        with patch.object(dialog, "accept"):
            dialog._on_qr_login_success(mock_pan)
        # QR 登录总是清除记住密码状态
        assert db.get_config("rememberPassword", None) is False
        assert db.get_config("passWord", "") == ""

    def test_qr_login_deletes_saved_password_credential(self, tmp_path, monkeypatch):
        dialog, _db = self._make_dialog(tmp_path, monkeypatch)
        mock_pan = MagicMock()
        mock_pan.authorization = "Bearer test-jwt"
        mock_pan.user_name = "new-user"
        mock_pan.devicetype = "test-device"
        mock_pan.osversion = "test-os"
        mock_pan.loginuuid = "test-uuid"

        with patch.object(dialog, "accept"), \
             patch("src.app.view.login_window.delete_credential") as mock_delete:
            dialog._on_qr_login_success(mock_pan)

        mock_delete.assert_any_call("passWord")


class TestLoginDialogConfig:
    def test_dialog_loads_stay_logged_in_from_config(self, tmp_path, monkeypatch):
        db = _use_temp_db(tmp_path, monkeypatch)
        db.set_config("stayLoggedIn", False)
        from src.app.view.login_window import LoginDialog

        dialog = LoginDialog()

        assert dialog.cb_stay_logged_in.isChecked() is False

    def test_dialog_does_not_prefill_password_when_not_remembered(self, tmp_path, monkeypatch):
        db = _use_temp_db(tmp_path, monkeypatch)
        db.set_config("rememberPassword", False)
        from src.app.view.login_window import LoginDialog

        with patch("src.app.view.login_window.load_credential", return_value="secret"):
            dialog = LoginDialog()

        assert dialog.le_pass.text() == ""
