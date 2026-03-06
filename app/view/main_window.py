from PyQt6.QtCore import Qt, pyqtSignal, QEasingCurve, QUrl, QSize, QTimer
from PyQt6.QtGui import QIcon, QDesktopServices, QColor
from PyQt6.QtWidgets import QApplication, QHBoxLayout, QFrame, QWidget

from qfluentwidgets import (NavigationAvatarWidget, NavigationItemPosition, MessageBox, FluentWindow,
                            SplashScreen, SystemThemeListener, isDarkTheme)
from qfluentwidgets import FluentIcon as FIF

from .file_interface import FileInterface
from .transfer_interface import TransferInterface
from .setting_interface import SettingInterface

from ..common import resource

class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("123pan")
        self.resize(900, 600)

        # 初始化子页面
        self.file_interface = FileInterface(self)
        self.transfer_interface = TransferInterface(self)
        self.setting_interface = SettingInterface(self)

        self._initNavigation()

    def _initNavigation(self):
        self.addSubInterface(self.file_interface, FIF.FOLDER, "文件")
        self.addSubInterface(self.transfer_interface, FIF.SYNC, "传输")
        self.addSubInterface(self.setting_interface, FIF.SETTING, "设置",
                             position=NavigationItemPosition.BOTTOM)