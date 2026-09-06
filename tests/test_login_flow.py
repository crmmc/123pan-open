import sys
from unittest.mock import MagicMock, patch

import pytest
import requests
from PySide6.QtGui import QCloseEvent, QImage
from PySide6.QtWidgets import QApplication

sys.modules.setdefault("qrcode", MagicMock())

from src.app.common import database as database_module
from src.app.common.database import Database
from src.app.view import login_window as login_module
from src.app.view import qr_login_page as qr_module
from src.app.view.login_window import (
    _LoginTask,
    has_saved_credentials,
    login_with_credentials,
    try_token_probe,
)
from src.app.view.qr_login_page import (
    _QRGenerateTask,
    _QRLoginVerifyTask,
    _QRPollTask,
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


# ==================== 批次 7 追加：登录任务与二维码状态机 ====================


class _FakeThreadPool:
    """同步执行的假线程池：start 仅入队，run_pending 时逐一同步运行。"""

    def __init__(self):
        self.tasks = []

    def start(self, task, _priority=0):
        self.tasks.append(task)

    def run_pending(self):
        while self.tasks:
            self.tasks.pop(0).run()


@pytest.fixture
def fake_login_pool(monkeypatch):
    """把 login_window 引用的 QThreadPool 换成可同步执行的假池。"""
    pool = _FakeThreadPool()
    monkeypatch.setattr(
        login_module, "QThreadPool", MagicMock(globalInstance=MagicMock(return_value=pool))
    )
    return pool


@pytest.fixture
def fake_qr_pool(monkeypatch):
    """把 qr_login_page 引用的 QThreadPool 换成可同步执行的假池。"""
    pool = _FakeThreadPool()
    monkeypatch.setattr(
        qr_module, "QThreadPool", MagicMock(globalInstance=MagicMock(return_value=pool))
    )
    return pool


@pytest.fixture
def fake_qr_render(monkeypatch):
    """二维码渲染 mock：qrcode.make 返回哑图，ImageQt 换成小尺寸真实 QImage。"""
    monkeypatch.setattr(qr_module.qrcode, "make", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(
        qr_module, "ImageQt", lambda _img: QImage(20, 20, QImage.Format.Format_RGB32)
    )


@pytest.fixture
def qr_page(qapp, fake_qr_pool):
    """真实构造 QRLoginPage（异步任务在假池中排队）。"""
    from src.app.view.qr_login_page import QRLoginPage

    return QRLoginPage()


class TestLoginWithCredentialsFailure:
    """login_with_credentials 登录码拒绝时的报错分支。"""

    @pytest.mark.parametrize(
        ("code", "login_message", "expected"),
        [
            (-1, "账号或密码错误", "登录失败: 账号或密码错误"),
            (500, "", "登录失败: 返回码: 500"),
        ],
    )
    def test_raises_runtime_error_when_code_rejected(self, code, login_message, expected):
        mock_pan = MagicMock()
        mock_pan.login.return_value = code
        mock_pan._login_message = login_message
        with patch("src.app.view.login_window.Pan123", return_value=mock_pan):
            with pytest.raises(RuntimeError, match=expected):
                login_with_credentials("alice", "bad")

    def test_returns_pan_when_login_succeeds(self):
        mock_pan = MagicMock()
        mock_pan.login.return_value = 0
        with patch("src.app.view.login_window.Pan123", return_value=mock_pan):
            assert login_with_credentials("alice", "pw") is mock_pan


class TestLoginTask:
    """_LoginTask 异步任务的成功与异常消息映射。"""

    def test_run_emits_success_with_pan(self):
        mock_pan = MagicMock()
        success, errors = [], []
        with patch("src.app.view.login_window.login_with_credentials", return_value=mock_pan):
            task = _LoginTask("alice", "pw")
            task.signals.success.connect(lambda pan: success.append(pan))
            task.signals.error.connect(lambda msg: errors.append(msg))
            task.run()
        assert success == [mock_pan]
        assert errors == []

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (requests.exceptions.ConnectTimeout("t1"), "连接超时，服务器无响应: t1"),
            (requests.exceptions.ReadTimeout("t2"), "读取超时，服务器响应过慢: t2"),
            (requests.exceptions.ConnectionError("t3"), "网络连接失败，请检查网络: t3"),
            (requests.exceptions.RequestException("t4"), "请求异常: t4"),
            (RuntimeError("登录失败: bad"), "登录失败: bad"),
            (ValueError("oops"), "登录时发生未知异常: ValueError: oops"),
        ],
    )
    def test_run_maps_exceptions_to_messages(self, exc, expected):
        errors = []
        with patch("src.app.view.login_window.login_with_credentials", side_effect=exc):
            task = _LoginTask("alice", "pw")
            task.signals.error.connect(lambda msg: errors.append(msg))
            task.run()
        assert errors == [expected]


class TestLoginDialogLifecycle:
    """LoginDialog 关闭路径与 Tab 切换。"""

    def _make_dialog(self, tmp_path, monkeypatch):
        _use_temp_db(tmp_path, monkeypatch)
        from src.app.view.login_window import LoginDialog

        return LoginDialog()

    def test_reject_stops_polling_and_closes_pan_temp(self, tmp_path, monkeypatch):
        dialog = self._make_dialog(tmp_path, monkeypatch)
        pan_temp = MagicMock()
        dialog.qr_page._pan_temp = pan_temp
        flow = dialog.qr_page._qr_flow_id

        dialog.reject()

        pan_temp.close.assert_called_once()
        assert dialog.qr_page._pan_temp is None
        assert dialog.qr_page._qr_flow_id == flow + 1

    def test_close_event_stops_polling_and_accepts(self, tmp_path, monkeypatch):
        dialog = self._make_dialog(tmp_path, monkeypatch)
        pan_temp = MagicMock()
        dialog.qr_page._pan_temp = pan_temp
        flow = dialog.qr_page._qr_flow_id
        event = QCloseEvent()

        dialog.closeEvent(event)

        assert event.isAccepted()
        pan_temp.close.assert_called_once()
        assert dialog.qr_page._qr_flow_id == flow + 1

    @pytest.mark.parametrize(("state", "should_delete"), [(0, True), (2, False)])
    def test_remember_password_change_deletes_credential(
        self, tmp_path, monkeypatch, state, should_delete
    ):
        dialog = self._make_dialog(tmp_path, monkeypatch)
        with patch("src.app.view.login_window.delete_credential") as mock_delete:
            dialog._on_remember_password_changed(state)
        if should_delete:
            mock_delete.assert_called_once_with("passWord")
        else:
            mock_delete.assert_not_called()

    @pytest.mark.parametrize(
        ("route_key", "page_index", "width", "height"),
        [("password", 0, 345, 320), ("qrcode", 1, 391, 400)],
    )
    def test_tab_change_switches_page_and_size(
        self, tmp_path, monkeypatch, route_key, page_index, width, height
    ):
        dialog = self._make_dialog(tmp_path, monkeypatch)
        with patch.object(dialog.qr_page, "stop_polling") as mock_stop, \
             patch.object(dialog.qr_page, "start_qr_flow") as mock_start:
            dialog._on_tab_changed(route_key)
        assert dialog.stacked_widget.currentIndex() == page_index
        assert dialog.minimumSize().width() == width
        assert dialog.minimumSize().height() == height
        if route_key == "password":
            mock_stop.assert_called_once()
            mock_start.assert_not_called()
        else:
            mock_start.assert_called_once()
            mock_stop.assert_not_called()


class TestLoginDialogOnOk:
    """LoginDialog.on_ok 异步登录流转与结果持久化。"""

    def _make_dialog(self, tmp_path, monkeypatch):
        db = _use_temp_db(tmp_path, monkeypatch)
        from src.app.view.login_window import LoginDialog

        return LoginDialog(), db

    def test_on_ok_without_input_prompts_and_keeps_button(self, tmp_path, monkeypatch):
        dialog, _ = self._make_dialog(tmp_path, monkeypatch)
        with patch("src.app.view.login_window.MessageBox") as mock_box:
            dialog.on_ok()
        mock_box.assert_called_once_with("提示", "请输入用户名和密码。", dialog)
        assert dialog.btn_ok.isEnabled()

    def test_on_ok_runs_task_and_persists_config(self, tmp_path, monkeypatch, fake_login_pool):
        dialog, db = self._make_dialog(tmp_path, monkeypatch)
        dialog.le_user.setText("alice")
        dialog.le_pass.setText("pw123")
        dialog.cb_remember_password.setChecked(True)
        dialog.cb_stay_logged_in.setChecked(True)
        mock_pan = MagicMock()
        mock_pan.authorization = "Bearer tok"
        mock_pan.devicetype = "dev"
        mock_pan.osversion = "os"
        mock_pan.loginuuid = "uuid"
        with patch("src.app.view.login_window.login_with_credentials", return_value=mock_pan) as mock_login, \
             patch("src.app.view.login_window.save_credential") as mock_save, \
             patch.object(dialog, "accept") as mock_accept:
            dialog.on_ok()
            assert not dialog.btn_ok.isEnabled()
            fake_login_pool.run_pending()

        mock_login.assert_called_once_with("alice", "pw123")
        mock_accept.assert_called_once()
        assert dialog.pan is mock_pan
        assert dialog.btn_ok.isEnabled()
        assert db.get_config("userName", "") == "alice"
        assert db.get_config("passWord", "") == ""
        assert db.get_config("rememberPassword", False) is True
        assert db.get_config("deviceType", "") == "dev"
        mock_save.assert_any_call("passWord", "pw123")
        mock_save.assert_any_call("authorization", "Bearer tok")

    def test_on_login_success_swallows_credential_io_error(self, tmp_path, monkeypatch, fake_login_pool):
        dialog, _ = self._make_dialog(tmp_path, monkeypatch)
        dialog.le_user.setText("alice")
        dialog.le_pass.setText("pw123")
        dialog.cb_remember_password.setChecked(True)
        mock_pan = MagicMock()
        mock_pan.devicetype = "dev"
        mock_pan.osversion = "os"
        mock_pan.loginuuid = "uuid"
        with patch("src.app.view.login_window.login_with_credentials", return_value=mock_pan), \
             patch("src.app.view.login_window.save_credential", side_effect=IOError("disk full")), \
             patch.object(dialog, "accept") as mock_accept:
            dialog.on_ok()
            fake_login_pool.run_pending()
        mock_accept.assert_called_once()

    def test_on_login_success_deletes_credentials_when_not_remembered(
        self, tmp_path, monkeypatch, fake_login_pool
    ):
        dialog, _ = self._make_dialog(tmp_path, monkeypatch)
        dialog.le_user.setText("alice")
        dialog.le_pass.setText("pw123")
        dialog.cb_remember_password.setChecked(False)
        dialog.cb_stay_logged_in.setChecked(False)
        mock_pan = MagicMock()
        mock_pan.authorization = "Bearer tok"
        mock_pan.devicetype = "dev"
        mock_pan.osversion = "os"
        mock_pan.loginuuid = "uuid"
        with patch("src.app.view.login_window.login_with_credentials", return_value=mock_pan), \
             patch("src.app.view.login_window.delete_credential") as mock_delete, \
             patch("src.app.view.login_window.save_credential") as mock_save, \
             patch.object(dialog, "accept"):
            dialog.on_ok()
            fake_login_pool.run_pending()
        mock_delete.assert_any_call("passWord")
        mock_delete.assert_any_call("authorization")
        mock_save.assert_not_called()

    def test_on_login_success_swallows_unexpected_db_error(self, tmp_path, monkeypatch, fake_login_pool):
        dialog, _ = self._make_dialog(tmp_path, monkeypatch)
        dialog.le_user.setText("alice")
        dialog.le_pass.setText("pw123")
        db_mock = MagicMock()
        db_mock.set_many_config.side_effect = ValueError("db closed")
        mock_pan = MagicMock()
        with patch("src.app.view.login_window.login_with_credentials", return_value=mock_pan), \
             patch("src.app.view.login_window.Database") as mock_db_cls, \
             patch.object(dialog, "accept") as mock_accept:
            mock_db_cls.instance.return_value = db_mock
            dialog.on_ok()
            fake_login_pool.run_pending()
        mock_accept.assert_called_once()

    def test_on_login_error_restores_button_and_prompts(self, tmp_path, monkeypatch):
        dialog, _ = self._make_dialog(tmp_path, monkeypatch)
        dialog.btn_ok.setEnabled(False)
        with patch("src.app.view.login_window.MessageBox") as mock_box:
            dialog._on_login_error("bad creds")
        assert dialog.login_error == "bad creds"
        assert dialog.btn_ok.isEnabled()
        mock_box.assert_called_once_with("登录失败", "bad creds", dialog)

    def test_get_pan_returns_saved_pan(self, tmp_path, monkeypatch):
        dialog, _ = self._make_dialog(tmp_path, monkeypatch)
        mock_pan = MagicMock()
        dialog.pan = mock_pan
        assert dialog.get_pan() is mock_pan

    def test_qr_login_success_survives_config_error(self, tmp_path, monkeypatch):
        dialog, _ = self._make_dialog(tmp_path, monkeypatch)
        db_mock = MagicMock()
        db_mock.set_many_config.side_effect = ValueError("db closed")
        with patch("src.app.view.login_window.Database") as mock_db_cls, \
             patch.object(dialog, "accept") as mock_accept:
            mock_db_cls.instance.return_value = db_mock
            dialog._on_qr_login_success(MagicMock())
        mock_accept.assert_called_once()


class TestQRGenerateTask:
    """_QRGenerateTask 同步执行结果。"""

    def test_run_emits_finished_with_pan_temp(self):
        pan_mock = MagicMock()
        pan_mock.qr_generate.return_value = {"uniID": "u1", "url": "https://qr.example/x"}
        finished, errors = [], []
        with patch("src.app.view.qr_login_page.Pan123", return_value=pan_mock) as mock_ctor:
            task = _QRGenerateTask()
            task.signals.finished.connect(lambda data: finished.append(data))
            task.signals.error.connect(lambda msg: errors.append(msg))
            task.run()
        mock_ctor.assert_called_once_with(readfile=True, user_name="", password="")
        assert errors == []
        assert finished[0]["uniID"] == "u1"
        assert finished[0]["_pan_temp"] is pan_mock

    def test_run_emits_error_on_failure(self):
        errors = []
        with patch("src.app.view.qr_login_page.Pan123", side_effect=Exception("boom")):
            task = _QRGenerateTask()
            task.signals.error.connect(lambda msg: errors.append(msg))
            task.run()
        assert errors == ["boom"]


class TestQRPollTask:
    """_QRPollTask 同步执行结果。"""

    def test_run_emits_poll_result(self):
        pan_temp = MagicMock()
        pan_temp.qr_poll.return_value = {"loginStatus": 1}
        results, errors = [], []
        task = _QRPollTask(pan_temp, "u2")
        task.signals.result.connect(lambda result: results.append(result))
        task.signals.error.connect(lambda: errors.append("error"))
        task.run()
        pan_temp.qr_poll.assert_called_once_with("u2")
        assert results == [{"loginStatus": 1}]
        assert errors == []

    def test_run_emits_error_on_failure(self):
        pan_temp = MagicMock()
        pan_temp.qr_poll.side_effect = Exception("net")
        results, errors = [], []
        task = _QRPollTask(pan_temp, "u2")
        task.signals.result.connect(lambda result: results.append(result))
        task.signals.error.connect(lambda: errors.append("error"))
        task.run()
        assert results == []
        assert errors == ["error"]


class TestQRLoginVerifyTask:
    """_QRLoginVerifyTask 凭证验证各分支。"""

    @staticmethod
    def _run_task(task):
        success, errors = [], []
        task.signals.success.connect(lambda pan: success.append(pan))
        task.signals.error.connect(lambda msg: errors.append(msg))
        task.run()
        return success, errors

    def test_wx_platform_without_token_reports_unsupported(self):
        pan_temp = MagicMock()
        pan_temp.qr_wx_code.return_value = "wxcode"
        task = _QRLoginVerifyTask("", 4, pan_temp, "u3")
        success, errors = self._run_task(task)
        pan_temp.qr_wx_code.assert_called_once_with("u3")
        assert success == []
        assert errors == ["微信登录暂不支持，请使用 123云盘 App 扫码"]

    def test_wx_platform_error_propagates(self):
        pan_temp = MagicMock()
        pan_temp.qr_wx_code.side_effect = Exception("wx fail")
        task = _QRLoginVerifyTask("", 4, pan_temp, "u3")
        _, errors = self._run_task(task)
        assert errors == ["wx fail"]

    def test_missing_token_reports_error(self):
        task = _QRLoginVerifyTask("", 0, MagicMock(), "u4")
        success, errors = self._run_task(task)
        assert success == []
        assert errors == ["登录失败：未获取到凭证"]

    def test_success_emits_pan_with_nickname(self):
        db_mock = MagicMock()
        db_mock.get_config.return_value = "cfg"
        pan_mock = MagicMock()
        pan_mock.user_info.return_value = {"Nickname": "bob"}
        with patch("src.app.view.qr_login_page.Pan123", return_value=pan_mock) as mock_ctor, \
             patch("src.app.view.qr_login_page.Database") as mock_db_cls:
            mock_db_cls.instance.return_value = db_mock
            task = _QRLoginVerifyTask("tok", 1, MagicMock(), "u5")
            success, errors = self._run_task(task)
        mock_ctor.assert_called_once_with(
            readfile=False, user_name="", password="", authorization="Bearer tok"
        )
        assert pan_mock.devicetype == "cfg"
        assert pan_mock.osversion == "cfg"
        assert pan_mock.loginuuid == "cfg"
        assert pan_mock.user_name == "bob"
        assert success == [pan_mock]
        assert errors == []

    def test_user_info_none_reports_error(self):
        pan_mock = MagicMock()
        pan_mock.user_info.return_value = None
        with patch("src.app.view.qr_login_page.Pan123", return_value=pan_mock), \
             patch("src.app.view.qr_login_page.Database"):
            task = _QRLoginVerifyTask("tok", 0, MagicMock(), "u5")
            success, errors = self._run_task(task)
        assert success == []
        assert errors == ["登录验证失败，请重试"]

    def test_unexpected_error_propagates(self):
        with patch("src.app.view.qr_login_page.Pan123", side_effect=Exception("server 500")):
            task = _QRLoginVerifyTask("tok", 0, MagicMock(), "u5")
            _, errors = self._run_task(task)
        assert errors == ["server 500"]


class TestQRFlowGeneration:
    """start_qr_flow 全流程：生成成功 / 失败 / 过期流。"""

    def test_start_qr_flow_generates_code_and_starts_polling(self, qr_page, fake_qr_render, fake_qr_pool):
        pan_mock = MagicMock()
        pan_mock.qr_generate.return_value = {"uniID": "u1", "url": "https://qr.example/x"}
        with patch("src.app.view.qr_login_page.Pan123", return_value=pan_mock):
            qr_page.start_qr_flow()
            fake_qr_pool.run_pending()
        assert qr_page._uni_id == "u1"
        assert qr_page._pan_temp is pan_mock
        assert qr_page.status_label.text() == "请使用微信或 123云盘 App 扫码"
        assert qr_page.poll_timer.isActive()
        assert qr_page.expiry_timer.isActive()
        assert not qr_page.qr_label.pixmap().isNull()
        assert qr_page.overlay.isHidden()

    def test_start_qr_flow_generate_error_shows_overlay(self, qr_page, fake_qr_pool):
        with patch("src.app.view.qr_login_page.Pan123", side_effect=Exception("net down")):
            qr_page.start_qr_flow()
            fake_qr_pool.run_pending()
        assert qr_page.status_label.text() == "获取二维码失败，请重试"
        assert not qr_page.overlay.isHidden()

    def test_generate_error_stale_flow_ignored(self, qr_page):
        qr_page._on_qr_generate_error(qr_page._qr_flow_id + 1, "x")
        assert qr_page.status_label.text() == "请使用微信扫一扫"
        assert qr_page.overlay.isHidden()


class TestQRPollStates:
    """_on_poll_result 轮询状态机（表测试驱动各状态迁移）。"""

    @pytest.mark.parametrize(
        ("status", "expected_text", "expect_overlay"),
        [
            (0, "请使用微信扫一扫", False),  # 等待扫码：状态不变
            (1, "扫码成功，请在手机上确认", True),
            (2, "登录已取消", True),
            (-1, "请使用微信扫一扫", False),  # 未知状态：仅记录日志
        ],
    )
    def test_poll_result_status_transitions(
        self, qr_page, status, expected_text, expect_overlay
    ):
        pan_temp = MagicMock()
        qr_page._pan_temp = pan_temp
        qr_page._uni_id = "u1"
        qr_page._on_poll_result(qr_page._qr_flow_id, {"loginStatus": status})
        assert qr_page.status_label.text() == expected_text
        assert qr_page.overlay.isHidden() is (not expect_overlay)
        assert qr_page._poll_in_flight is False

    def test_poll_result_cancel_stops_and_closes(self, qr_page):
        pan_temp = MagicMock()
        qr_page._pan_temp = pan_temp
        qr_page._uni_id = "u1"
        flow = qr_page._qr_flow_id
        qr_page._on_poll_result(flow, {"loginStatus": 2})
        assert not qr_page.poll_timer.isActive()
        assert not qr_page.expiry_timer.isActive()
        pan_temp.close.assert_called_once()
        assert qr_page._pan_temp is None
        assert qr_page._qr_flow_id == flow + 1

    def test_poll_result_scanned_shows_check_overlay(self, qr_page):
        qr_page._pan_temp = MagicMock()
        qr_page._uni_id = "u1"
        qr_page._on_poll_result(qr_page._qr_flow_id, {"loginStatus": 1})
        assert qr_page.overlay.text() == "\u2713"
        assert not qr_page.overlay.isHidden()

    def test_poll_result_confirmed_runs_verify_and_emits_login(self, qr_page, fake_qr_pool):
        pan_temp = MagicMock()
        qr_page._pan_temp = pan_temp
        qr_page._uni_id = "u9"
        flow = qr_page._qr_flow_id
        mock_pan = MagicMock()
        mock_pan.user_info.return_value = {"Nickname": "ann"}
        db_mock = MagicMock()
        db_mock.get_config.return_value = ""
        received = []
        qr_page.loginSuccess.connect(lambda pan: received.append(pan))
        with patch("src.app.view.qr_login_page.Pan123", return_value=mock_pan), \
             patch("src.app.view.qr_login_page.Database") as mock_db_cls:
            mock_db_cls.instance.return_value = db_mock
            qr_page._on_poll_result(
                flow, {"loginStatus": 3, "token": "tok", "scanPlatform": 1}
            )
            assert qr_page.status_label.text() == "登录成功"
            assert qr_page._pan_temp is None
            assert not qr_page.poll_timer.isActive()
            pan_temp.close.assert_not_called()
            fake_qr_pool.run_pending()
        assert received == [mock_pan]

    def test_poll_result_expired_restarts_flow(self, qr_page, fake_qr_pool):
        pan_temp = MagicMock()
        qr_page._pan_temp = pan_temp
        qr_page._uni_id = "u1"
        qr_page._on_poll_result(qr_page._qr_flow_id, {"loginStatus": 4})
        assert qr_page._qr_refresh_count == 1
        assert qr_page.status_label.text() == "正在获取二维码..."
        pan_temp.close.assert_called_once()
        assert len(fake_qr_pool.tasks) == 1

    def test_poll_result_expired_over_limit_stops_refresh(self, qr_page, fake_qr_pool):
        pan_temp = MagicMock()
        qr_page._pan_temp = pan_temp
        qr_page._qr_refresh_count = 6
        qr_page._on_poll_result(qr_page._qr_flow_id, {"loginStatus": 4})
        assert qr_page.status_label.text() == "二维码已过期，请关闭后重试"
        assert qr_page._pan_temp is None
        assert fake_qr_pool.tasks == []
        assert not qr_page.overlay.isHidden()

    def test_poll_error_stale_flow_ignored(self, qr_page):
        qr_page._poll_in_flight = True
        qr_page._on_poll_error(qr_page._qr_flow_id + 1)
        assert qr_page._poll_in_flight is True
        assert qr_page._consecutive_errors == 0

    def test_poll_result_stale_flow_ignored(self, qr_page):
        pan_temp = MagicMock()
        qr_page._pan_temp = pan_temp
        qr_page._on_poll_result(qr_page._qr_flow_id + 1, {"loginStatus": 1})
        pan_temp.close.assert_not_called()
        assert qr_page.status_label.text() == "请使用微信扫一扫"
        assert qr_page.overlay.isHidden()


class TestQRPollDispatch:
    """_do_poll 任务派发与错误计数。"""

    def test_do_poll_starts_task_and_resets_in_flight(self, qr_page, fake_qr_pool):
        pan_temp = MagicMock()
        pan_temp.qr_poll.return_value = {"loginStatus": 0}
        qr_page._pan_temp = pan_temp
        qr_page._uni_id = "u7"
        qr_page._do_poll()
        assert len(fake_qr_pool.tasks) == 1
        assert qr_page._poll_in_flight is True
        fake_qr_pool.run_pending()
        pan_temp.qr_poll.assert_called_once_with("u7")
        assert qr_page._poll_in_flight is False
        assert qr_page._consecutive_errors == 0

    def test_do_poll_error_counts_consecutive_failures(self, qr_page, fake_qr_pool):
        pan_temp = MagicMock()
        pan_temp.qr_poll.side_effect = Exception("net")
        qr_page._pan_temp = pan_temp
        qr_page._uni_id = "u7"
        qr_page._do_poll()
        fake_qr_pool.run_pending()
        assert qr_page._consecutive_errors == 1
        assert qr_page._poll_in_flight is False


class TestQRExpiryAndOverlay:
    """二维码过期刷新与遮罩。"""

    def test_expired_refreshes_flow_within_limit(self, qr_page, fake_qr_pool):
        pan_temp = MagicMock()
        qr_page._pan_temp = pan_temp
        qr_page._on_expired()
        assert qr_page._qr_refresh_count == 1
        pan_temp.close.assert_called_once()
        assert len(fake_qr_pool.tasks) == 1

    def test_expired_over_limit_shows_manual_refresh(self, qr_page, fake_qr_pool):
        qr_page._qr_refresh_count = 6
        qr_page._on_expired()
        assert qr_page.status_label.text() == "二维码已过期，请手动刷新"
        assert fake_qr_pool.tasks == []
        assert not qr_page.overlay.isHidden()

    def test_scanned_overlay_marks_check(self, qr_page):
        qr_page._show_scanned_overlay()
        assert qr_page.overlay.text() == "\u2713"
        assert not qr_page.overlay.isHidden()

    @pytest.mark.parametrize(
        ("error_msg", "expected_text"),
        [
            ("微信登录暂不支持，请使用 123云盘 App 扫码", "微信登录暂不支持，请使用 123云盘 App 扫码"),
            ("登录失败：未获取到凭证", "登录失败：未获取到凭证"),
            ("服务器 500", "登录验证失败，请重试"),
        ],
    )
    def test_login_verify_error_updates_status(self, qr_page, error_msg, expected_text):
        qr_page._on_login_verify_error(qr_page._qr_flow_id, error_msg)
        assert qr_page.status_label.text() == expected_text
        assert not qr_page.overlay.isHidden()

    def test_login_verify_error_stale_flow_ignored(self, qr_page):
        qr_page._on_login_verify_error(qr_page._qr_flow_id + 1, "服务器 500")
        assert qr_page.status_label.text() == "请使用微信扫一扫"
