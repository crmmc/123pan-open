"""UploadConflictDialog（上传冲突弹窗）测试。

覆盖：冲突决策矩阵（文件×保留两者/跳过、文件夹×合并/重命名，真实点击
按钮触发 _finish）、apply_all 复选框记忆、无复选框场景与提示文案。
"""
import pytest
from PySide6.QtWidgets import QDialog

from qfluentwidgets import BodyLabel, PushButton

from src.app.view.upload_conflict_dialog import ConflictAction, UploadConflictDialog


def _button_by_text(dialog, text):
    """按钮为局部变量（PrimaryPushButton 是 PushButton 子类），按文本查找。"""
    for btn in dialog.findChildren(PushButton):
        if btn.text() == text:
            return btn
    raise AssertionError(f"未找到按钮: {text}")


def _hint_text(dialog):
    """取提示文案（首个 BodyLabel，即冲突说明行）。"""
    labels = dialog.findChildren(BodyLabel)
    assert labels, "未找到提示标签"
    return labels[0].text()


class TestUploadConflictDialog:
    def test_file_conflict_without_remaining_has_no_checkbox(self, qapp):
        dlg = UploadConflictDialog("a.txt", is_folder=False, remaining=0)

        assert dlg.applyAllCheckBox is None
        assert dlg.action is None
        assert dlg.apply_all is False
        assert '同名文件 "a.txt"' in _hint_text(dlg)

    def test_folder_conflict_hint_and_checkbox_text(self, qapp):
        dlg = UploadConflictDialog("docs", is_folder=True, remaining=3)

        assert dlg.applyAllCheckBox is not None
        assert dlg.applyAllCheckBox.text() == "对剩余 3 个冲突项执行相同操作"
        assert '同名文件夹 "docs"' in _hint_text(dlg)

    @pytest.mark.parametrize(
        ("is_folder", "button_text", "expected_action"),
        [
            (False, "保留两者", ConflictAction.KEEP_BOTH),
            (False, "跳过", ConflictAction.SKIP),
            (True, "合并", ConflictAction.MERGE),
            (True, "重命名", ConflictAction.RENAME),
        ],
        ids=["file-keep-both", "file-skip", "folder-merge", "folder-rename"],
    )
    def test_button_click_records_action_and_accepts(
        self, qapp, is_folder, button_text, expected_action
    ):
        dlg = UploadConflictDialog("demo", is_folder=is_folder, remaining=2)

        _button_by_text(dlg, button_text).click()

        assert dlg.action is expected_action
        assert dlg.apply_all is False  # 复选框存在但未勾选
        assert dlg.result() == QDialog.DialogCode.Accepted

    @pytest.mark.parametrize("checked", [True, False], ids=["checked", "unchecked"])
    def test_apply_all_remembers_checkbox_state(self, qapp, checked):
        dlg = UploadConflictDialog("a.txt", is_folder=False, remaining=5)
        assert dlg.applyAllCheckBox is not None
        dlg.applyAllCheckBox.setChecked(checked)

        _button_by_text(dlg, "跳过").click()

        assert dlg.action is ConflictAction.SKIP
        assert dlg.apply_all is checked

    def test_apply_all_false_without_checkbox(self, qapp):
        dlg = UploadConflictDialog("a.txt", is_folder=False, remaining=0)

        _button_by_text(dlg, "保留两者").click()

        assert dlg.action is ConflictAction.KEEP_BOTH
        assert dlg.apply_all is False

    def test_wrong_button_set_not_present_for_folder(self, qapp):
        """文件夹冲突只提供 合并/重命名，不提供 跳过/保留两者。"""
        dlg = UploadConflictDialog("docs", is_folder=True, remaining=0)
        texts = {btn.text() for btn in dlg.findChildren(PushButton)}

        assert texts == {"合并", "重命名"}

    def test_wrong_button_set_not_present_for_file(self, qapp):
        """文件冲突只提供 跳过/保留两者，不提供 合并/重命名。"""
        dlg = UploadConflictDialog("a.txt", is_folder=False, remaining=0)
        texts = {btn.text() for btn in dlg.findChildren(PushButton)}

        assert texts == {"跳过", "保留两者"}
