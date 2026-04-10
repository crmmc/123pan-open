from enum import Enum

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QVBoxLayout, QHBoxLayout, QDialog

from qfluentwidgets import (
    CheckBox,
    PrimaryPushButton,
    PushButton,
    TitleLabel,
    BodyLabel,
)


class ConflictAction(Enum):
    SKIP = 0         # 取消上传（文件）
    KEEP_BOTH = 1    # 保留两者 — 文件加后缀（文件）
    MERGE = 2        # 合并（文件夹）
    RENAME = 3       # 自动重命名（文件夹）


class UploadConflictDialog(QDialog):
    """上传冲突处理弹窗。

    文件冲突: 提供 "保留两者" / "跳过" 选项
    文件夹冲突: 提供 "合并" / "重命名" 选项
    remaining > 0 时显示 "应用到所有冲突" CheckBox
    """

    def __init__(self, name: str, is_folder: bool, remaining: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("上传冲突")
        self.resize(420, 220)
        self.setWindowFlags(
            self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint
        )

        self._result_action: ConflictAction | None = None
        self._apply_all = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 30, 40, 30)
        layout.setSpacing(16)

        title = TitleLabel("名称冲突")
        layout.addWidget(title, alignment=Qt.AlignmentFlag.AlignCenter)

        kind = "文件夹" if is_folder else "文件"
        hint = BodyLabel(f'目标位置已存在同名{kind} "{name}"')
        hint.setWordWrap(True)
        layout.addWidget(hint, alignment=Qt.AlignmentFlag.AlignCenter)

        # "应用到所有冲突" 复选框
        self.applyAllCheckBox: CheckBox | None = None
        if remaining > 0:
            self.applyAllCheckBox = CheckBox(f"对剩余 {remaining} 个冲突项执行相同操作")
            layout.addWidget(self.applyAllCheckBox)

        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        if is_folder:
            merge_btn = PrimaryPushButton("合并")
            merge_btn.setMinimumWidth(100)
            merge_btn.clicked.connect(lambda: self._finish(ConflictAction.MERGE))
            rename_btn = PushButton("重命名")
            rename_btn.setMinimumWidth(100)
            rename_btn.clicked.connect(lambda: self._finish(ConflictAction.RENAME))
            btn_layout.addWidget(rename_btn)
            btn_layout.addWidget(merge_btn)
        else:
            skip_btn = PushButton("跳过")
            skip_btn.setMinimumWidth(100)
            skip_btn.clicked.connect(lambda: self._finish(ConflictAction.SKIP))
            keep_btn = PrimaryPushButton("保留两者")
            keep_btn.setMinimumWidth(100)
            keep_btn.clicked.connect(lambda: self._finish(ConflictAction.KEEP_BOTH))
            btn_layout.addWidget(skip_btn)
            btn_layout.addWidget(keep_btn)

        layout.addLayout(btn_layout)

    def _finish(self, action: ConflictAction):
        self._result_action = action
        self._apply_all = bool(
            self.applyAllCheckBox and self.applyAllCheckBox.isChecked()
        )
        self.accept()

    @property
    def action(self) -> ConflictAction | None:
        return self._result_action

    @property
    def apply_all(self) -> bool:
        return self._apply_all
