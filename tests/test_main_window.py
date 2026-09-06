import sys
import time
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.modules.setdefault("qrcode", MagicMock())

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QStackedWidget, QWidget

from src.app.common.database import Database
from src.app.view import main_window as mw
from src.app.view.main_window import MainWindow, QDialog


def _make_transfer(
    upload_threads=None,
    download_threads=None,
    upload_tasks=None,
    download_tasks=None,
):
    return type(
        "_Transfer",
        (),
        {
            "upload_threads": upload_threads or [],
            "download_threads": download_threads or [],
            "upload_tasks": upload_tasks or [],
            "download_tasks": download_tasks or [],
        },
    )()


def test_stop_all_transfers_uses_thread_lists():
    upload_thread = MagicMock()
    upload_thread.isRunning.return_value = False
    download_thread = MagicMock()
    download_thread.isRunning.return_value = False
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = _make_transfer(
        upload_threads=[upload_thread],
        download_threads=[download_thread],
    )

    MainWindow._stop_all_transfers(window)

    upload_thread.cancel.assert_called_once()
    download_thread.cancel.assert_called_once()
    upload_thread.wait.assert_called()
    download_thread.wait.assert_called()


def test_stop_all_transfers_save_progress_calls_pause():
    thread = MagicMock()
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = _make_transfer(upload_threads=[thread])

    MainWindow._stop_all_transfers(window, save_progress=True)

    thread.pause.assert_called_once()
    thread.cancel.assert_not_called()


def test_stop_all_transfers_default_calls_cancel():
    thread = MagicMock()
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = _make_transfer(upload_threads=[thread])

    MainWindow._stop_all_transfers(window)

    thread.cancel.assert_called_once()
    thread.pause.assert_not_called()


def test_stop_all_transfers_suspends_auto_start_during_wait():
    thread = MagicMock()
    thread.isRunning.return_value = False
    transfer = _make_transfer(upload_threads=[thread])
    transfer.suspend_auto_start = MagicMock()
    transfer.resume_auto_start = MagicMock()
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = transfer

    MainWindow._stop_all_transfers(window, save_progress=True)

    transfer.suspend_auto_start.assert_called_once_with()
    transfer.resume_auto_start.assert_called_once_with()


def test_save_active_progress_updates_upload_task():
    task = SimpleNamespace(status="上传中", db_task_id="abc", resume_id=None, progress=42)
    db_mock = MagicMock()
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = _make_transfer(upload_tasks=[task])

    with patch.object(MainWindow, "__init__", lambda self: None):
        with patch("src.app.view.main_window.Database") as DbCls:
            DbCls.instance.return_value = db_mock
            MainWindow._save_active_progress(window)

    assert task.status == "已暂停"
    db_mock.update_upload_task.assert_called_once_with("abc", status="已暂停", progress=42)


def test_save_active_progress_updates_download_task():
    task = SimpleNamespace(status="下载中", db_task_id=None, resume_id="xyz", progress=67)
    db_mock = MagicMock()
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = _make_transfer(download_tasks=[task])

    with patch("src.app.view.main_window.Database") as DbCls:
        DbCls.instance.return_value = db_mock
        MainWindow._save_active_progress(window)

    assert task.status == "已暂停"
    db_mock.update_download_task.assert_called_once_with("xyz", status="已暂停", progress=67)


def test_save_active_progress_ignores_non_active_tasks():
    task = SimpleNamespace(status="已完成", db_task_id="done", resume_id=None)
    db_mock = MagicMock()
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = _make_transfer(upload_tasks=[task])

    with patch("src.app.view.main_window.Database") as DbCls:
        DbCls.instance.return_value = db_mock
        MainWindow._save_active_progress(window)

    assert task.status == "已完成"
    db_mock.update_upload_task.assert_not_called()


def test_save_active_progress_swallows_db_exception():
    task = SimpleNamespace(status="上传中", db_task_id="boom", resume_id=None)
    db_mock = MagicMock()
    db_mock.update_upload_task.side_effect = Exception("DB down")
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = _make_transfer(upload_tasks=[task])

    with patch("src.app.view.main_window.Database") as DbCls:
        DbCls.instance.return_value = db_mock
        # 不应抛异常
        MainWindow._save_active_progress(window)

    assert task.status == "已暂停"


def test_stop_all_transfers_with_save_progress_calls_save_active_progress():
    thread = MagicMock()
    task = SimpleNamespace(status="上传中", db_task_id="sid", resume_id=None, thread=thread, progress=55)
    db_mock = MagicMock()
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = _make_transfer(
        upload_threads=[thread],
        upload_tasks=[task],
    )

    with patch("src.app.view.main_window.Database") as DbCls:
        DbCls.instance.return_value = db_mock
        MainWindow._stop_all_transfers(window, save_progress=True)

    db_mock.update_upload_task.assert_called_once_with("sid", status="已暂停", progress=55)


def test_stop_all_transfers_deduplicates_threads_from_tasks():
    thread = MagicMock()
    task = SimpleNamespace(status="上传中", thread=thread, db_task_id=None, resume_id=None)
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = _make_transfer(
        upload_threads=[thread],
        upload_tasks=[task],
    )

    MainWindow._stop_all_transfers(window)

    # 同一个 thread 只被 cancel 一次
    thread.cancel.assert_called_once()


def test_stop_all_transfers_skips_none_and_seen_threads():
    thread = MagicMock()
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = _make_transfer(
        upload_threads=[None, thread, thread],
    )

    MainWindow._stop_all_transfers(window)

    # None 被跳过，重复 thread 只调一次
    thread.cancel.assert_called_once()


def test_stop_all_transfers_extracts_active_threads_from_tasks():
    task_thread = MagicMock()
    task_thread.isRunning.return_value = False
    task = SimpleNamespace(status="上传中", thread=task_thread, db_task_id=None, resume_id=None)
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = _make_transfer(
        upload_threads=[],
        upload_tasks=[task],
    )

    MainWindow._stop_all_transfers(window)

    task_thread.cancel.assert_called_once()
    task_thread.wait.assert_called()


def test_show_relogin_dialog_stops_old_transfers_before_switching_pan():
    old_pan = MagicMock()
    old_pan.on_token_expired = object()
    new_pan = MagicMock()
    events: list[tuple] = []

    message_box = MagicMock()
    login_dialog = MagicMock()
    login_dialog.exec.return_value = QDialog.DialogCode.Accepted
    login_dialog.get_pan.return_value = new_pan

    window = MainWindow.__new__(MainWindow)
    window.pan = old_pan
    window.transfer_interface = MagicMock()
    window.transfer_interface.set_pan.side_effect = lambda pan, force=False: events.append(
        ("set_pan", pan, force)
    )
    window.cloud_interface = MagicMock()
    window.cloud_interface.set_pan.side_effect = lambda pan: events.append(("cloud", pan))
    window.file_interface = MagicMock()
    window.file_interface.reload.side_effect = lambda: events.append(("reload",))
    window._stop_all_transfers = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda save_progress=False: events.append(("stop", save_progress))
    )

    with patch("src.app.view.main_window.MessageBox", return_value=message_box), \
         patch("src.app.view.main_window.LoginDialog", return_value=login_dialog):
        MainWindow._show_relogin_dialog(window)

    assert events[0] == ("stop", True)
    assert events[1] == ("set_pan", new_pan, True)
    assert old_pan.on_token_expired is None
    assert old_pan.close.call_count == 1
    assert new_pan.on_token_expired == window._handle_token_expired  # pylint: disable=comparison-with-callable
    login_dialog.deleteLater.assert_called_once()


def test_force_cleanup_tasks_deletes_active_download_records(tmp_path):
    active_download = SimpleNamespace(
        status="下载中",
        thread=MagicMock(),
        resume_id="resume-active",
    )
    paused_download = SimpleNamespace(
        status="已暂停",
        thread=None,
        resume_id="resume-paused",
    )
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = _make_transfer(
        download_tasks=[active_download, paused_download],
    )

    with patch("src.app.view.main_window.Database") as db_cls, \
         patch("src.app.view.main_window.cleanup_temp_dir") as mock_cleanup:
        db_cls.instance.return_value = MagicMock()
        MainWindow._force_cleanup_tasks(window)

    db_cls.instance.return_value.delete_download_task.assert_called_once_with("resume-active")
    mock_cleanup.assert_called_once_with("resume-active")
    assert active_download.status == "已取消"
    assert paused_download.status == "已取消"


# ==================== 批次 7 追加：构造时序 / 导航切换 / 登出与关闭流转 ====================


def _stub_interface(name, **attrs):
    """构造 MainWindow 导航所需的子页面替身（真实 QWidget，行为经 MagicMock 注入）。"""
    widget = QWidget()
    widget.setObjectName(name)
    for attr, value in attrs.items():
        setattr(widget, attr, value)
    return widget


def _fake_fluent_init(self):
    """绕开 FluentWindow 的 C++ 初始化（offscreen 下段错误），仅保留 MainWindow 依赖的接缝。"""
    # 显式调用 QWidget.__init__ 是刻意为之：super().__init__() 会命中被替换的 FluentWindow.__init__ 导致递归
    QWidget.__init__(self)  # pylint: disable=unnecessary-dunder-call
    self.navigationInterface = MagicMock()
    self.stackedWidget = QStackedWidget(self)


@pytest.fixture
def build_window(qapp, temp_db):
    """工厂 fixture：build_window(...) → (window, stubs)。

    真实执行 MainWindow.__init__ 全流程（含 _startup_login_flow 与 _initNavigation），
    子页面用替身 widget，token 探测/登录弹窗 mock，补丁在测试结束后统一卸载。
    """
    stacks = []

    def _build(probe_result=None, stay_logged_in=None):
        if stay_logged_in is not None:
            temp_db.set_config("stayLoggedIn", stay_logged_in)
        file_if = _stub_interface("file", refresh=MagicMock(), reload=MagicMock())
        transfer_if = _stub_interface(
            "transfer",
            set_pan=MagicMock(),
            upload_threads=[],
            download_threads=[],
            upload_tasks=[],
            download_tasks=[],
            suspend_auto_start=MagicMock(),
            resume_auto_start=MagicMock(),
        )
        setting_if = _stub_interface("setting", refresh_from_db=MagicMock())
        cloud_if = _stub_interface("cloud", set_pan=MagicMock(), logoutRequested=MagicMock())

        stack = ExitStack()
        stacks.append(stack)
        stack.enter_context(patch.object(mw.FluentWindow, "__init__", _fake_fluent_init))
        stack.enter_context(patch.object(mw, "FileInterface", return_value=file_if))
        stack.enter_context(patch.object(mw, "TransferInterface", return_value=transfer_if))
        stack.enter_context(patch.object(mw, "SettingInterface", return_value=setting_if))
        stack.enter_context(patch.object(mw, "CloudInterface", return_value=cloud_if))
        probe = stack.enter_context(
            patch.object(mw, "try_token_probe", return_value=probe_result)
        )
        login_dialog = stack.enter_context(patch.object(mw, "LoginDialog"))

        window = mw.MainWindow()
        stubs = SimpleNamespace(
            file=file_if,
            transfer=transfer_if,
            setting=setting_if,
            cloud=cloud_if,
            probe=probe,
            login_dialog=login_dialog,
        )
        return window, stubs

    yield _build
    for stack in stacks:
        stack.close()


class TestMainWindowConstruction:
    """MainWindow 构造时序与导航初始化。"""

    def test_rejected_login_wires_navigation(self, build_window):
        window, stubs = build_window()

        stubs.probe.assert_called_once()
        stubs.login_dialog.assert_called_once_with(window)
        assert window.pan is None
        assert window.login_success is False
        assert window.file_interface.transfer_interface is window.transfer_interface
        assert window.stackedWidget.widget(0) is stubs.file
        assert window.stackedWidget.widget(1) is stubs.transfer
        assert window.stackedWidget.widget(2) is stubs.cloud
        assert window.stackedWidget.widget(3) is stubs.setting
        window.navigationInterface.setExpandWidth.assert_called_once_with(120)
        window.navigationInterface.setMinimumExpandWidth.assert_called_once_with(0)
        window.navigationInterface.setCollapsible.assert_called_once_with(False)
        window.navigationInterface.setMenuButtonVisible.assert_called_once_with(False)

    def test_probe_success_finishes_login(self, build_window):
        pan = MagicMock()
        window, stubs = build_window(probe_result=pan)

        assert window.pan is pan
        assert window.login_success is True
        stubs.transfer.set_pan.assert_called_once_with(pan)
        stubs.cloud.set_pan.assert_called_once_with(pan)
        stubs.file.reload.assert_called_once()
        assert pan.on_token_expired == window._handle_token_expired
        stubs.cloud.logoutRequested.connect.assert_called_once_with(window.handle_logout)
        stubs.login_dialog.assert_not_called()

    def test_stay_logged_out_skips_probe(self, build_window):
        window, stubs = build_window(stay_logged_in=False)

        stubs.probe.assert_not_called()
        stubs.login_dialog.assert_called_once_with(window)


class TestMainWindowPageChanged:
    """页面切换刷新时序（经由真实 currentChanged 信号）。"""

    def test_file_page_refresh_throttled_and_setting_page_refreshed(self, build_window):
        window, stubs = build_window()
        window.pan = MagicMock()

        window.stackedWidget.setCurrentIndex(1)  # 初始停在文件页，先切走
        stubs.file.refresh.assert_not_called()
        window.stackedWidget.setCurrentIndex(0)
        assert stubs.file.refresh.call_count == 1

        # 30 秒节流窗口内不重复刷新
        window._last_file_refresh_time = time.time()
        window.stackedWidget.setCurrentIndex(1)
        window.stackedWidget.setCurrentIndex(0)
        assert stubs.file.refresh.call_count == 1

        # pan 为空时文件页不刷新
        window.pan = None
        window._last_file_refresh_time = 0.0
        window.stackedWidget.setCurrentIndex(1)
        window.stackedWidget.setCurrentIndex(0)
        assert stubs.file.refresh.call_count == 1

        # 切到设置页触发配置刷新
        window.stackedWidget.setCurrentIndex(3)
        stubs.setting.refresh_from_db.assert_called_once()


class TestMainWindowLoginDialogs:
    """登录弹窗与重登弹窗流转。"""

    def test_show_login_dialog_accepted_finishes_login(self, build_window):
        window, stubs = build_window()
        dialog = MagicMock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        new_pan = MagicMock()
        dialog.get_pan.return_value = new_pan

        with patch.object(mw, "LoginDialog", return_value=dialog):
            window._show_login_dialog()

        assert window.pan is new_pan
        dialog.deleteLater.assert_called_once()
        stubs.transfer.set_pan.assert_called_once_with(new_pan)
        assert new_pan.on_token_expired == window._handle_token_expired

    def test_show_relogin_dialog_rejected_closes_window(self, build_window):
        window, stubs = build_window()
        dialog = stubs.login_dialog.return_value
        dialog.reset_mock()  # 构造期被拒登录已消费一次 mock，清掉历史
        dialog.exec.return_value = QDialog.DialogCode.Rejected

        with patch.object(mw, "MessageBox"), patch.object(window, "close") as mock_close:
            window._show_relogin_dialog()

        mock_close.assert_called_once()
        dialog.deleteLater.assert_called_once()
        stubs.transfer.set_pan.assert_not_called()
        assert window._relogin_pending is False

    def test_show_relogin_dialog_pending_guard(self, build_window):
        window, _ = build_window()
        window._relogin_pending = True

        with patch.object(mw, "MessageBox") as mock_box:
            window._show_relogin_dialog()

        mock_box.assert_not_called()
        # 早退分支不经过 finally，标志位保持 True 以继续抑制后续弹窗
        assert window._relogin_pending is True

    def test_handle_token_expired_schedules_relogin(self, build_window):
        window, _ = build_window()
        with patch.object(mw, "QTimer") as mock_timer:
            window._handle_token_expired()
        mock_timer.singleShot.assert_called_once_with(0, window._show_relogin_dialog)

    def test_close_event_saves_progress_and_closes_pan(self, build_window):
        window, stubs = build_window()
        # 构造期被拒登录的 close() 已触发过一次 closeEvent，清掉 stub 调用历史
        stubs.transfer.suspend_auto_start.reset_mock()
        stubs.transfer.resume_auto_start.reset_mock()
        window.pan = MagicMock()
        event = QCloseEvent()

        with patch.object(mw, "Database") as mock_db:
            window.closeEvent(event)

        assert event.isAccepted()
        window.pan.close.assert_called_once()
        mock_db.reset.assert_called_once()
        stubs.transfer.suspend_auto_start.assert_called_once()
        stubs.transfer.resume_auto_start.assert_called_once()

    def test_clear_login_config_clears_db_and_keyring(self, build_window, temp_db):
        temp_db.set_config("userName", "alice")
        temp_db.set_config("rememberPassword", True)
        window, _ = build_window()

        with patch("src.app.common.credential_store.delete_credential") as mock_delete:
            window.clear_login_config()

        db = Database.instance()  # 构造期 closeEvent 已 reset 过单例，取新实例断言
        assert db.get_config("userName", "") == ""
        assert db.get_config("rememberPassword", True) is False
        mock_delete.assert_any_call("passWord")
        mock_delete.assert_any_call("authorization")


class TestHandleLogout:
    """退出登录三条路径：确认重登 / 重登被拒 / 取消。"""

    @staticmethod
    def _prepare(build_window):
        window, stubs = build_window()
        old_pan = MagicMock()
        window.pan = old_pan
        return window, stubs, old_pan

    def test_confirmed_logout_relogs_in(self, build_window, temp_db):
        window, stubs, old_pan = self._prepare(build_window)
        message = MagicMock()
        message.exec.return_value = True
        dialog = MagicMock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        new_pan = MagicMock()
        dialog.get_pan.return_value = new_pan

        with patch.object(mw, "MessageBox", return_value=message), \
             patch.object(mw, "LoginDialog", return_value=dialog), \
             patch("src.app.common.credential_store.delete_credential"), \
             patch.object(window, "_stop_all_transfers") as mock_stop, \
             patch.object(window, "hide") as mock_hide, \
             patch.object(window, "show") as mock_show, \
             patch.object(window, "close") as mock_close:
            window.handle_logout()

        message.exec.assert_called_once()
        mock_stop.assert_called_once_with()
        assert old_pan.password == ""
        assert old_pan.authorization == ""
        old_pan.close.assert_called_once()
        assert window.pan is new_pan
        assert new_pan.on_token_expired == window._handle_token_expired
        stubs.transfer.set_pan.assert_called_once_with(new_pan, force=True)
        stubs.cloud.set_pan.assert_called_once_with(new_pan)
        stubs.file.reload.assert_called_once()
        mock_hide.assert_called_once()
        mock_show.assert_called_once()
        mock_close.assert_not_called()
        dialog.deleteLater.assert_called_once()
        assert Database.instance().get_config("userName", "") == ""

    def test_confirmed_logout_dialog_rejected_closes(self, build_window, temp_db):
        window, stubs, old_pan = self._prepare(build_window)
        message = MagicMock()
        message.exec.return_value = True
        dialog = MagicMock()
        dialog.exec.return_value = QDialog.DialogCode.Rejected

        with patch.object(mw, "MessageBox", return_value=message), \
             patch.object(mw, "LoginDialog", return_value=dialog), \
             patch("src.app.common.credential_store.delete_credential"), \
             patch.object(window, "hide"), \
             patch.object(window, "close") as mock_close:
            window.handle_logout()

        assert window.pan is None
        old_pan.close.assert_called_once()
        mock_close.assert_called_once()
        dialog.deleteLater.assert_called_once()

    def test_cancelled_logout_does_nothing(self, build_window, temp_db):
        window, _, old_pan = self._prepare(build_window)
        message = MagicMock()
        message.exec.return_value = 0

        with patch.object(mw, "MessageBox", return_value=message), \
             patch.object(window, "_stop_all_transfers") as mock_stop:
            window.handle_logout()

        mock_stop.assert_not_called()
        assert window.pan is old_pan
        old_pan.close.assert_not_called()


class TestStopAllTransfersEdgeCases:
    """_stop_all_transfers 去重/下载任务提取/超时兜底分支。"""

    def test_download_threads_deduped_and_active_download_tasks_stopped(self):
        shared = MagicMock()
        shared.isRunning.return_value = False
        task_thread = MagicMock()
        task_thread.isRunning.return_value = False
        task = SimpleNamespace(status="下载中", thread=task_thread, db_task_id=None, resume_id=None)
        window = MainWindow.__new__(MainWindow)
        window.transfer_interface = _make_transfer(
            upload_threads=[shared],
            download_threads=[shared],
            download_tasks=[task],
        )

        MainWindow._stop_all_transfers(window)

        shared.cancel.assert_called_once()  # 同一线程跨列表只停一次
        task_thread.cancel.assert_called_once()
        task_thread.wait.assert_called()

    def test_wait_loop_breaks_after_deadline(self, monkeypatch):
        thread = MagicMock()
        thread.isRunning.return_value = False
        window = MainWindow.__new__(MainWindow)
        window.transfer_interface = _make_transfer(upload_threads=[thread])
        # 第一次取 deadline，第二次已超过 10 秒窗口 → remaining_ms 为 0 直接 break
        clock = iter([100.0, 10.0 ** 12])
        monkeypatch.setattr(mw, "time", SimpleNamespace(monotonic=lambda: next(clock)))

        MainWindow._stop_all_transfers(window)

        thread.wait.assert_not_called()
        thread.terminate.assert_not_called()

    def test_save_progress_failure_is_swallowed(self):
        thread = MagicMock()
        thread.isRunning.return_value = False
        window = MainWindow.__new__(MainWindow)
        window.transfer_interface = _make_transfer(upload_threads=[thread])

        with patch.object(window, "_save_active_progress", side_effect=Exception("boom")):
            MainWindow._stop_all_transfers(window, save_progress=True)

        thread.pause.assert_called_once()


class TestSaveActiveProgressEdgeCases:
    def test_download_db_error_is_swallowed(self):
        task = SimpleNamespace(status="下载中", db_task_id=None, resume_id="r9", progress=5)
        db_mock = MagicMock()
        db_mock.update_download_task.side_effect = Exception("db down")
        window = MainWindow.__new__(MainWindow)
        window.transfer_interface = _make_transfer(download_tasks=[task])

        with patch("src.app.view.main_window.Database") as db_cls:
            db_cls.instance.return_value = db_mock
            MainWindow._save_active_progress(window)

        assert task.status == "已暂停"


class TestForceCleanupTasksEdgeCases:
    """_force_cleanup_tasks 上传分支与 disconnect 异常兜底。"""

    def test_upload_tasks_cleared_and_disconnect_errors_swallowed(self):
        upload_thread = MagicMock()
        upload_thread.disconnect.side_effect = TypeError("no connections")
        broken_download_thread = MagicMock()
        broken_download_thread.disconnect.side_effect = TypeError("no connections")
        ok_download_thread = MagicMock()
        upload_task = SimpleNamespace(
            status="上传中", thread=upload_thread, db_task_id=None, resume_id=None
        )
        broken_download = SimpleNamespace(
            status="下载中", thread=broken_download_thread, resume_id="r1"
        )
        paused_download = SimpleNamespace(
            status="已暂停", thread=ok_download_thread, resume_id=""
        )
        window = MainWindow.__new__(MainWindow)
        window.transfer_interface = _make_transfer(
            upload_threads=[MagicMock()],
            download_threads=[MagicMock()],
            upload_tasks=[upload_task],
            download_tasks=[broken_download, paused_download],
        )

        with patch("src.app.view.main_window.Database") as db_cls, \
             patch("src.app.view.main_window.cleanup_temp_dir") as mock_cleanup:
            db = MagicMock()
            db_cls.instance.return_value = db
            MainWindow._force_cleanup_tasks(window)

        upload_thread.disconnect.assert_called_once()
        assert upload_task.thread is None
        assert upload_task.status == "已取消"
        broken_download_thread.disconnect.assert_called_once()  # TypeError 被吞掉
        assert broken_download.thread is None
        assert broken_download.status == "已取消"
        db.delete_download_task.assert_called_once_with("r1")
        mock_cleanup.assert_called_once_with("r1")
        assert paused_download.thread is None
        assert paused_download.status == "已取消"
        assert window.transfer_interface.upload_threads == []
        assert window.transfer_interface.download_threads == []
