import traceback

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QVBoxLayout,
)

import requests
from qfluentwidgets import (
    LineEdit,
    PrimaryPushButton,
    PushButton,
    MessageBox,
    TitleLabel,
)

from ..common.api import Pan123
from ..common.database import Database
from ..common.log import get_logger

logger = get_logger(__name__)


def has_saved_credentials(db):
    user_name = (db.get_config("userName", "") or "").strip()
    pass_word = db.get_config("passWord", "") or ""
    return bool(user_name and pass_word)


def should_auto_login(db):
    return bool(db.get_config("autoLogin", False) and has_saved_credentials(db))


def update_auto_login_setting(enabled):
    Database.instance().set_config("autoLogin", bool(enabled))


def login_with_credentials(user, pwd):
    db = Database.instance()
    saved_user = db.get_config("userName", "")
    read_saved_config = saved_user == user
    pan = Pan123(readfile=read_saved_config, user_name=user, password=pwd)
    code = pan.login()
    if code not in {0, 200}:
        raise RuntimeError(f"登录失败，返回码: {code}")
    return pan


class LoginDialog(QDialog):
    """登录对话框"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("登录123云盘")
        self.resize(460, 320)
        self.setFixedSize(460, 320)
        # self.setWindowFlags(
        #     self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint
        # )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 30, 40, 30)
        layout.setSpacing(20)

        # 标题
        title = TitleLabel("欢迎使用123云盘")
        layout.addWidget(title, alignment=Qt.AlignmentFlag.AlignCenter)

        form = QFormLayout()
        form.setSpacing(15)

        # 用户名输入框
        self.le_user = LineEdit()
        self.le_user.setPlaceholderText("请输入用户名")
        form.addRow("用户名", self.le_user)

        # 密码输入框
        self.le_pass = LineEdit()
        self.le_pass.setPlaceholderText("请输入密码")
        self.le_pass.setEchoMode(LineEdit.EchoMode.Password)
        form.addRow("密码", self.le_pass)

        self.cb_auto_login = QCheckBox("自动登录")
        form.addRow("", self.cb_auto_login)

        layout.addLayout(form)

        h = QHBoxLayout()
        h.addStretch()

        # 登录按钮
        self.btn_ok = PrimaryPushButton("登录")
        self.btn_ok.setMinimumWidth(100)

        # 取消按钮
        self.btn_cancel = PushButton("取消")
        self.btn_cancel.setMinimumWidth(100)

        h.addWidget(self.btn_ok)
        h.addWidget(self.btn_cancel)
        layout.addLayout(h)

        self.btn_ok.clicked.connect(self.on_ok)
        self.btn_cancel.clicked.connect(self.close)

        self.pan = None
        self.login_error = None

        # 从配置文件中加载用户名
        db = Database.instance()
        self.le_user.setText(db.get_config("userName", ""))
        self.le_pass.setText(db.get_config("passWord", ""))
        self.cb_auto_login.setChecked(bool(db.get_config("autoLogin", False)))

    def on_ok(self):
        """登录处理"""

        user = self.le_user.text().strip()
        pwd = self.le_pass.text()
        if not user or not pwd:
            MessageBox("提示", "请输入用户名和密码。", self).exec()
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.pan = login_with_credentials(user, pwd)
        except requests.exceptions.ConnectTimeout as e:
            self.login_error = f"连接超时，服务器无响应: {e}"
            logger.error(self.login_error, exc_info=True)
            QApplication.restoreOverrideCursor()
            MessageBox("登录失败", self.login_error, self).exec()
            return
        except requests.exceptions.ReadTimeout as e:
            self.login_error = f"读取超时，服务器响应过慢: {e}"
            logger.error(self.login_error, exc_info=True)
            QApplication.restoreOverrideCursor()
            MessageBox("登录失败", self.login_error, self).exec()
            return
        except requests.exceptions.ConnectionError as e:
            self.login_error = f"网络连接失败，请检查网络: {e}"
            logger.error(self.login_error, exc_info=True)
            QApplication.restoreOverrideCursor()
            MessageBox("登录失败", self.login_error, self).exec()
            return
        except requests.exceptions.RequestException as e:
            self.login_error = f"请求异常: {e}"
            logger.error(self.login_error, exc_info=True)
            QApplication.restoreOverrideCursor()
            MessageBox("登录失败", self.login_error, self).exec()
            return
        except RuntimeError as e:
            self.login_error = str(e)
            logger.error(self.login_error)
            QApplication.restoreOverrideCursor()
            MessageBox("登录失败", self.login_error, self).exec()
            return
        except Exception as e:
            self.login_error = f"登录时发生未知异常: {type(e).__name__}: {e}"
            logger.error(
                "登录异常:\n%s", traceback.format_exc()
            )
            QApplication.restoreOverrideCursor()
            MessageBox("登录异常", self.login_error, self).exec()
            return
        finally:
            QApplication.restoreOverrideCursor()

        try:
            if hasattr(self.pan, "save_file"):
                self.pan.save_file()
            update_auto_login_setting(self.cb_auto_login.isChecked())
        except (IOError, OSError) as e:
            # 忽略配置文件保存失败,不影响登录流程
            logger.warning(f"保存配置失败: {e}")
        except Exception as e:
            logger.error(f"保存配置时发生未知错误: {e}")
        self.accept()

    def get_pan(self):
        """获取登录成功的Pan对象"""
        return self.pan
