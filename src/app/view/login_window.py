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


def login_with_credentials(user, pwd):
    db = Database.instance()
    saved_user = db.get_config("userName", "")
    read_saved_config = saved_user == user
    pan = Pan123(readfile=read_saved_config, user_name=user, password=pwd)
    code = pan.login()
    if code not in {0, 200}:
        raise RuntimeError(f"登录失败，返回码: {code}")
    return pan


def try_token_probe(db):
    """用已有 token 调 user_info API 探测有效性。
    成功返回 Pan123 对象，失败返回 None。
    """
    token = db.get_config("authorization", "")
    if not token:
        return None
    try:
        pan = Pan123(readfile=True, user_name="", password="", authorization=token)
        user_data = pan.user_info()
        if user_data is not None:
            logger.info("Token 探测成功，跳过登录")
            return pan
    except Exception as exc:
        logger.warning(f"Token 探测异常: {exc}")
    # token 无效或过期，清除
    db.set_config("authorization", "")
    return None


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

        self.cb_remember_password = QCheckBox("记住密码")
        self.cb_stay_logged_in = QCheckBox("保持登录")
        checkbox_layout = QHBoxLayout()
        checkbox_layout.addWidget(self.cb_remember_password)
        checkbox_layout.addWidget(self.cb_stay_logged_in)
        form.addRow("", checkbox_layout)

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
        self.cb_remember_password.setChecked(bool(db.get_config("rememberPassword", False)))
        self.cb_stay_logged_in.setChecked(bool(db.get_config("stayLoggedIn", True)))
        self.cb_remember_password.stateChanged.connect(self._on_remember_password_changed)

    def _on_remember_password_changed(self, state):
        if not state:
            Database.instance().set_config("passWord", "")

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
            MessageBox("登录失败", self.login_error, self).exec()
            return
        except requests.exceptions.ReadTimeout as e:
            self.login_error = f"读取超时，服务器响应过慢: {e}"
            logger.error(self.login_error, exc_info=True)
            MessageBox("登录失败", self.login_error, self).exec()
            return
        except requests.exceptions.ConnectionError as e:
            self.login_error = f"网络连接失败，请检查网络: {e}"
            logger.error(self.login_error, exc_info=True)
            MessageBox("登录失败", self.login_error, self).exec()
            return
        except requests.exceptions.RequestException as e:
            self.login_error = f"请求异常: {e}"
            logger.error(self.login_error, exc_info=True)
            MessageBox("登录失败", self.login_error, self).exec()
            return
        except RuntimeError as e:
            self.login_error = str(e)
            logger.error(self.login_error)
            MessageBox("登录失败", self.login_error, self).exec()
            return
        except Exception as e:
            self.login_error = f"登录时发生未知异常: {type(e).__name__}: {e}"
            logger.error(
                "登录异常:\n%s", traceback.format_exc()
            )
            MessageBox("登录异常", self.login_error, self).exec()
            return
        finally:
            QApplication.restoreOverrideCursor()

        try:
            db = Database.instance()
            # 始终保存 userName 和设备信息
            db.set_config("userName", user)
            db.set_config("deviceType", self.pan.devicetype)
            db.set_config("osVersion", self.pan.osversion)
            db.set_config("loginuuid", self.pan.loginuuid)

            # 按用户选择保存密码
            if self.cb_remember_password.isChecked():
                db.set_config("passWord", pwd)
            else:
                db.set_config("passWord", "")

            # 按用户选择保存 token
            if self.cb_stay_logged_in.isChecked():
                db.set_config("authorization", self.pan.authorization)
            else:
                db.set_config("authorization", "")

            # 保存 checkbox 状态
            db.set_config("rememberPassword", self.cb_remember_password.isChecked())
            db.set_config("stayLoggedIn", self.cb_stay_logged_in.isChecked())
        except (IOError, OSError) as e:
            logger.warning(f"保存配置失败: {e}")
        except Exception as e:
            logger.error(f"保存配置时发生未知错误: {e}")
        self.accept()

    def get_pan(self):
        """获取登录成功的Pan对象"""
        return self.pan
