import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QDialog

from qfluentwidgets import (
    NavigationItemPosition,
    MessageBox,
    FluentWindow,
)
from qfluentwidgets import FluentIcon as FIF

from .file_interface import FileInterface
from .transfer_interface import (
    DOWNLOAD_ACTIVE_STATUSES,
    UPLOAD_ACTIVE_STATUSES,
    TransferInterface,
)
from .setting_interface import SettingInterface
from .cloud_interface import CloudInterface
from .login_window import LoginDialog, try_token_probe

from ..common import resource  # noqa: F401 -- 触发 qInitResources()
from ..common.database import Database
from ..common.download_resume import cleanup_temp_dir


class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("123pan-open")
        self.resize(900, 600)

        # 导航栏始终展开，不允许收起
        nav = self.navigationInterface
        nav.setExpandWidth(120)
        nav.setMinimumExpandWidth(0)
        nav.setCollapsible(False)
        nav.setMenuButtonVisible(False)
        self._last_file_refresh_time = 0.0
        self.login_success = False

        # 初始化子页面
        self.file_interface = FileInterface(self)
        self.transfer_interface = TransferInterface(self)
        self.setting_interface = SettingInterface(self)
        self.cloud_interface = CloudInterface(self)

        # 传递传输界面引用给文件界面
        self.file_interface.transfer_interface = self.transfer_interface

        self._startup_login_flow()
        self._initNavigation()

    def _initNavigation(self):
        self.addSubInterface(self.file_interface, FIF.FOLDER, "文件")
        self.addSubInterface(self.transfer_interface, FIF.SYNC, "传输")
        self.addSubInterface(
            self.cloud_interface,
            FIF.CLOUD,
            "账户",
            position=NavigationItemPosition.BOTTOM,
        )
        self.addSubInterface(
            self.setting_interface,
            FIF.SETTING,
            "设置",
            position=NavigationItemPosition.BOTTOM,
        )

        self.stackedWidget.currentChanged.connect(self._onPageChanged)

    def _onPageChanged(self, index):
        widget = self.stackedWidget.widget(index)
        if widget is self.file_interface:
            if self.pan is not None:
                now = time.time()
                if now - self._last_file_refresh_time > 30:
                    self.file_interface.refresh()
                    self._last_file_refresh_time = now
        elif widget is self.setting_interface:
            # P1-13: 切换到设置页面时刷新配置值
            self.setting_interface.refresh_from_db()

    def _startup_login_flow(self):
        db = Database.instance()
        self.pan = None

        stay_logged_in = bool(db.get_config("stayLoggedIn", True))
        if stay_logged_in:
            pan = try_token_probe(db)
            if pan is not None:
                self._on_probe_success(pan)
                return
        self._show_login_dialog()

    def _on_probe_success(self, pan):
        self.pan = pan
        self._finish_login()

    def _show_login_dialog(self):
        dlg = LoginDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            dlg.deleteLater()
            self.close()
            return
        self.pan = dlg.get_pan()
        dlg.deleteLater()
        self._finish_login()

    def _finish_login(self):
        self.login_success = True
        self.transfer_interface.set_pan(self.pan)
        self.cloud_interface.set_pan(self.pan)
        self.file_interface.pan = self.pan
        self.file_interface.reload()
        self.pan.on_token_expired = self._handle_token_expired
        self.cloud_interface.logoutRequested.connect(self.handle_logout)

    def _handle_token_expired(self):
        """H1: token 过期时在主线程弹出登录对话框。"""
        QTimer.singleShot(0, self._show_relogin_dialog)

    def _show_relogin_dialog(self):
        if getattr(self, "_relogin_pending", False):
            return
        self._relogin_pending = True
        try:
            msg = MessageBox("登录过期", "登录凭证已过期，请重新登录。", self)
            msg.exec()
            msg.deleteLater()
            dlg = LoginDialog(self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                old_pan = self.pan
                new_pan = dlg.get_pan()
                if old_pan is not None:
                    old_pan.on_token_expired = None
                self._stop_all_transfers(save_progress=True)
                self._force_cleanup_tasks()  # P1-8: 清理旧线程引用
                self.pan = new_pan
                self.pan.on_token_expired = self._handle_token_expired
                self.transfer_interface.set_pan(self.pan, force=True)
                self.cloud_interface.set_pan(self.pan)
                self.file_interface.pan = self.pan
                self.file_interface.reload()
                if old_pan is not None:
                    old_pan.close()
            else:
                self.close()
            dlg.deleteLater()
        finally:
            self._relogin_pending = False

    def _stop_all_transfers(self, save_progress=False):
        """停止所有正在进行的传输任务并等待线程退出。

        Args:
            save_progress: True 时 pause 线程（保留进度），False 时 cancel（丢弃进度）。
        """
        transfer = self.transfer_interface
        suspend_auto_start = getattr(transfer, "suspend_auto_start", None)
        resume_auto_start = getattr(transfer, "resume_auto_start", None)
        if callable(suspend_auto_start):
            suspend_auto_start()
        try:
            threads_to_wait = []
            seen = set()
            stop_fn = "pause" if save_progress else "cancel"
            for thread in list(transfer.upload_threads):
                if thread is None or id(thread) in seen:
                    continue
                getattr(thread, stop_fn)()
                threads_to_wait.append(thread)
                seen.add(id(thread))
            for thread in list(transfer.download_threads):
                if thread is None or id(thread) in seen:
                    continue
                getattr(thread, stop_fn)()
                threads_to_wait.append(thread)
                seen.add(id(thread))
            for task in transfer.upload_tasks:
                if task.thread and id(task.thread) not in seen and task.status in UPLOAD_ACTIVE_STATUSES:
                    getattr(task.thread, stop_fn)()
                    threads_to_wait.append(task.thread)
                    seen.add(id(task.thread))
            for task in transfer.download_tasks:
                if task.thread and id(task.thread) not in seen and task.status in DOWNLOAD_ACTIVE_STATUSES:
                    getattr(task.thread, stop_fn)()
                    threads_to_wait.append(task.thread)
                    seen.add(id(task.thread))
            # M11: 总超时模式，避免串行等待阻塞 UI
            deadline = time.monotonic() + 10
            for thread in threads_to_wait:
                remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
                if remaining_ms <= 0:
                    break
                thread.wait(remaining_ms)

            # P1-21: 超时后强制终止仍在运行的线程，防止 crash
            for thread in threads_to_wait:
                if thread.isRunning():
                    from ..common.log import get_logger as _get_logger
                    _get_logger(__name__).warning("强制终止残留线程: %s", thread)
                    thread.terminate()
                    thread.wait(2000)

            if save_progress:
                try:
                    self._save_active_progress()
                except Exception:
                    from ..common.log import get_logger as _get_logger
                    _get_logger(__name__).warning("保存进度失败（可能因锁已被终止线程持有）", exc_info=True)
        finally:
            if callable(resume_auto_start):
                resume_auto_start()

    def _save_active_progress(self):
        """兜底：对所有仍在活跃状态的任务强制更新 DB 为 "已暂停"。"""
        db = Database.instance()
        for task in self.transfer_interface.upload_tasks:
            if task.status in UPLOAD_ACTIVE_STATUSES:
                task.status = "已暂停"
                if task.db_task_id:
                    try:
                        db.update_upload_task(task.db_task_id, status="已暂停", progress=task.progress)
                    except Exception:
                        pass
        for task in self.transfer_interface.download_tasks:
            if task.status in DOWNLOAD_ACTIVE_STATUSES:
                task.status = "已暂停"
                if task.resume_id:
                    try:
                        db.update_download_task(task.resume_id, status="已暂停", progress=task.progress)
                    except Exception:
                        pass

    def closeEvent(self, event):
        """H6: 关闭窗口时暂停传输线程，保存进度以便下次续传。"""
        self._stop_all_transfers(save_progress=True)
        if self.pan:
            self.pan.close()
        Database.reset()
        event.accept()

    def clear_login_config(self):
        """清除登录配置信息"""
        from ..common.credential_store import delete_credential
        db = Database.instance()
        for key in ("userName", "passWord", "authorization", "deviceType", "osVersion", "loginuuid"):
            db.set_config(key, "")
        db.set_config("rememberPassword", False)
        delete_credential("passWord")
        delete_credential("authorization")

    def handle_logout(self):
        """处理退出登录请求"""
        msg = MessageBox("退出登录", "确定要退出登录吗？", self)
        if msg.exec():
            msg.deleteLater()
            # M8: 退出登录前停止传输
            self._stop_all_transfers()
            self._force_cleanup_tasks()
            if self.pan:
                old_pan = self.pan
                self.pan = None  # P1-11: 先替换为 None，使异步任务 stale 检查能发现变化
                self.file_interface.pan = None
                old_pan.password = ""
                old_pan.authorization = ""
                old_pan.close()
            self.clear_login_config()
            self.hide()
            dlg = LoginDialog(self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self.pan = dlg.get_pan()
                self.pan.on_token_expired = self._handle_token_expired
                self.transfer_interface.set_pan(self.pan, force=True)
                self.cloud_interface.set_pan(self.pan)
                self.file_interface.pan = self.pan
                self.file_interface.reload()
                self.show()
            else:
                self.close()
            dlg.deleteLater()

    def _force_cleanup_tasks(self):
        """M7: 强制清理所有残留任务状态，防止重登后线程冲突。"""
        db = Database.instance()
        for task in self.transfer_interface.upload_tasks:
            if task.thread is not None:
                try:
                    task.thread.disconnect()
                except TypeError:
                    pass
                task.thread = None
            task.status = "已取消"
        for task in self.transfer_interface.download_tasks:
            if task.thread is not None:
                try:
                    task.thread.disconnect()
                except TypeError:
                    pass
                task.thread = None
            if task.status in DOWNLOAD_ACTIVE_STATUSES:
                if getattr(task, "resume_id", ""):
                    db.delete_download_task(task.resume_id)
                    cleanup_temp_dir(task.resume_id)
                task.status = "已取消"
                continue
            task.status = "已取消"
        self.transfer_interface.upload_threads.clear()
        self.transfer_interface.download_threads.clear()
