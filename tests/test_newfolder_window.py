"""NewFolderDialog（新建文件夹弹窗）测试。

覆盖：真实构造（默认名称/全选）、校验接受与空白拒绝（确定按钮、
回车键、直接调用三种入口）、get_new_name 去首尾空白。
"""
from PySide6.QtWidgets import QDialog

from qfluentwidgets import PushButton

from src.app.view.newfolder_window import NewFolderDialog


def _button_by_text(dialog, text):
    """弹窗按钮为局部变量，按文本查找真实按钮实例。"""
    for btn in dialog.findChildren(PushButton):
        if btn.text() == text:
            return btn
    raise AssertionError(f"未找到按钮: {text}")


class TestNewFolderDialog:
    def test_init_prefills_default_name(self, qapp):
        dlg = NewFolderDialog()

        assert dlg.windowTitle() == "新建文件夹"
        assert dlg.name_input.text() == "新建文件夹"
        assert dlg.get_new_name() == "新建文件夹"

    def test_validate_accepts_non_blank_name(self, qapp):
        dlg = NewFolderDialog()
        dlg.name_input.setText("  电影  ")

        dlg._validate_and_accept()

        assert dlg.result() == QDialog.DialogCode.Accepted
        assert dlg.get_new_name() == "电影"

    def test_validate_rejects_blank_name(self, qapp):
        dlg = NewFolderDialog()
        dlg.name_input.setText("   ")

        dlg._validate_and_accept()

        assert dlg.result() != QDialog.DialogCode.Accepted

    def test_return_pressed_triggers_validate(self, qapp):
        dlg = NewFolderDialog()
        dlg.name_input.setText("notes")

        dlg.name_input.returnPressed.emit()

        assert dlg.result() == QDialog.DialogCode.Accepted

    def test_return_pressed_with_blank_name_not_accepted(self, qapp):
        dlg = NewFolderDialog()
        dlg.name_input.setText("  ")

        dlg.name_input.returnPressed.emit()

        assert dlg.result() != QDialog.DialogCode.Accepted

    def test_ok_button_click_accepts(self, qapp):
        dlg = NewFolderDialog()

        _button_by_text(dlg, "确定").click()

        assert dlg.result() == QDialog.DialogCode.Accepted

    def test_cancel_button_click_rejects(self, qapp):
        dlg = NewFolderDialog()

        _button_by_text(dlg, "取消").click()

        assert dlg.result() == QDialog.DialogCode.Rejected
