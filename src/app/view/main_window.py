from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QDialog

from qfluentwidgets import (
    NavigationItemPosition,
    MessageBox,
    FluentWindow,
)
from qfluentwidgets import FluentIcon as FIF

from .file_interface import FileInterface
from .transfer_interface import TransferInterface
from .setting_interface import SettingInterface
from .cloud_interface import CloudInterface
from .login_window import LoginDialog, login_with_credentials, should_auto_login

from ..common import resource
from ..common.database import Database


class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("123pan")
        self.resize(900, 600)

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
        if widget is self.file_interface and hasattr(self, "pan"):
            self.file_interface.refresh()

    def _startup_login_flow(self):
        db = Database.instance()
        auto_login_error = None
        if should_auto_login(db):
            try:
                self.pan = login_with_credentials(
                    db.get_config("userName", ""),
                    db.get_config("passWord", ""),
                )
            except Exception as exc:
                auto_login_error = str(exc)

        if auto_login_error or not hasattr(self, "pan"):
            dlg = LoginDialog(self)
            if auto_login_error:
                MessageBox(
                    "自动登录失败",
                    f"{auto_login_error}\n请手动重新登录。",
                    self,
                ).exec()
            if dlg.exec() != QDialog.DialogCode.Accepted:
                # QMessageBox.information(self, "提示", "未登录，程序将退出。")
                QTimer.singleShot(0, self.close)
                return
            self.pan = dlg.get_pan()

        # 将 pan 对象传递给 file_interface 并刷新文件列表
        self.file_interface.pan = self.pan
        self.file_interface.reload()

        # 将 pan 对象传递给 transfer_interface
        self.transfer_interface.set_pan(self.pan)

        # 将 pan 对象传递给 cloud_interface
        self.cloud_interface.set_pan(self.pan)

        # 连接退出登录信号
        self.cloud_interface.logoutRequested.connect(self.handle_logout)

    def clear_login_config(self):
        """清除登录配置信息"""
        db = Database.instance()
        for key in ("userName", "passWord", "authorization", "deviceType", "osVersion", "loginuuid"):
            db.set_config(key, "")

    def handle_logout(self):
        """处理退出登录请求"""
        # 确认对话框
        msg = MessageBox("退出登录", "确定要退出登录吗？", self)
        if msg.exec():
            # 清除登录配置
            self.clear_login_config()
            # 显示登录对话框
            dlg = LoginDialog(self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                # 登录成功，更新 pan 对象
                self.pan = dlg.get_pan()
                self.file_interface.pan = self.pan
                self.file_interface.reload()
                self.transfer_interface.set_pan(self.pan)
                self.cloud_interface.set_pan(self.pan)
            else:
                # 用户取消登录，关闭程序
                self.close()
