import shiboken6
from PySide6.QtCore import Qt, QObject, Signal, QRunnable, QThreadPool
from PySide6.QtWidgets import QVBoxLayout, QHBoxLayout, QDialog, QTreeWidgetItem

from qfluentwidgets import (
    PrimaryPushButton,
    PushButton,
    TitleLabel,
    BodyLabel,
    TreeWidget,
)
from qfluentwidgets import FluentIcon as FIF


class MoveDialog(QDialog):
    """移动文件 - 选择目标文件夹弹窗"""

    class _ExpandSignals(QObject):
        finished = Signal(int, list, str)  # dir_id, items, error

    class _ExpandTask(QRunnable):
        def __init__(self, pan, dir_id, signals):
            super().__init__()
            self.pan = pan
            self.dir_id = dir_id
            self.signals = signals
            self.setAutoDelete(True)

        def run(self):
            try:
                code, items = self.pan.get_dir_by_id(
                    self.dir_id, all=True, limit=100
                )
                if code == 0:
                    self.signals.finished.emit(self.dir_id, items, "")
                else:
                    self.signals.finished.emit(self.dir_id, [], f"API 返回码: {code}")
            except Exception as exc:
                self.signals.finished.emit(self.dir_id, [], str(exc))

    def __init__(self, pan, current_dir_id, parent=None):
        super().__init__(parent)
        self.pan = pan
        self.current_dir_id = current_dir_id
        self.selected_dir_id = None
        self.selected_dir_name = None
        self._pending_signals = []
        self._active_expands = 0
        self._closed = False

        self.setWindowTitle("移动到")
        self.resize(450, 500)
        self.setWindowFlags(
            self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        title = TitleLabel("移动到")
        layout.addWidget(title, alignment=Qt.AlignmentFlag.AlignCenter)

        hint = BodyLabel("选择目标文件夹")
        layout.addWidget(hint, alignment=Qt.AlignmentFlag.AlignCenter)

        self.folderTree = TreeWidget()
        self.folderTree.setHeaderHidden(True)
        self.folderTree.setUniformRowHeights(True)
        self.folderTree.itemClicked.connect(self.__onItemClicked)
        self.folderTree.itemExpanded.connect(self.__onItemExpanded)
        layout.addWidget(self.folderTree, 1)

        button_layout = QHBoxLayout()
        button_layout.addStretch()

        cancel_button = PushButton("取消")
        cancel_button.setMinimumWidth(100)
        cancel_button.clicked.connect(self.reject)

        self.ok_button = PrimaryPushButton("移动到此")
        self.ok_button.setMinimumWidth(100)
        self.ok_button.setEnabled(False)
        self.ok_button.clicked.connect(self.accept)

        button_layout.addWidget(cancel_button)
        button_layout.addWidget(self.ok_button)
        layout.addLayout(button_layout)

        self.__initTree()

    def accept(self):
        self._closed = True
        super().accept()

    def reject(self):
        self._closed = True
        super().reject()

    def closeEvent(self, event):
        self._closed = True
        super().closeEvent(event)

    def __initTree(self):
        root_item = QTreeWidgetItem(["根目录"])
        root_item.setIcon(0, FIF.FOLDER.icon())
        root_item.setData(0, Qt.ItemDataRole.UserRole, 0)
        root_item.setData(0, Qt.ItemDataRole.UserRole + 1, False)
        self.folderTree.addTopLevelItem(root_item)
        self.__addPlaceholder(root_item)
        self.folderTree.expandItem(root_item)

    def __addPlaceholder(self, parent_item):
        placeholder = QTreeWidgetItem(["加载中..."])
        placeholder.setData(0, Qt.ItemDataRole.UserRole, None)
        parent_item.addChild(placeholder)

    def __onItemClicked(self, item):
        dir_id = item.data(0, Qt.ItemDataRole.UserRole)
        if dir_id is None:
            return
        self.selected_dir_id = int(dir_id)
        self.selected_dir_name = item.text(0)
        self.ok_button.setEnabled(True)
        self.ok_button.setText(f"移动到「{self.selected_dir_name}」")

    def __onItemExpanded(self, item):
        loaded = item.data(0, Qt.ItemDataRole.UserRole + 1)
        dir_id = item.data(0, Qt.ItemDataRole.UserRole)
        if loaded or dir_id is None:
            return

        item.takeChildren()
        loading = QTreeWidgetItem(["加载中..."])
        loading.setData(0, Qt.ItemDataRole.UserRole, None)
        item.addChild(loading)

        self._active_expands += 1
        self.folderTree.setEnabled(False)

        signals = self._ExpandSignals()
        self._pending_signals.append(signals)
        signals.finished.connect(lambda *_, sig=signals: (
            self._pending_signals.remove(sig) if sig in self._pending_signals else None
        ))
        signals.finished.connect(lambda d, items, err, i=item: self._onExpandFinished(i, d, items, err))

        task = self._ExpandTask(self.pan, int(dir_id), signals)
        QThreadPool.globalInstance().start(task)

    def _onExpandFinished(self, item, dir_id, items, error):
        if self._closed:
            return
        if not shiboken6.isValid(item):
            return
        # 查找并移除 loading 占位项
        for i in range(item.childCount()):
            child = item.child(i)
            if child.data(0, Qt.ItemDataRole.UserRole) is None:
                item.removeChild(child)
                break

        if not error:
            for file_item in items:
                if int(file_item.get("Type", 0)) != 1:
                    continue
                child = QTreeWidgetItem([file_item.get("FileName", "")])
                child.setIcon(0, FIF.FOLDER.icon())
                child_id = int(file_item.get("FileId", 0))
                child.setData(0, Qt.ItemDataRole.UserRole, child_id)
                child.setData(0, Qt.ItemDataRole.UserRole + 1, False)
                item.addChild(child)
                self.__addPlaceholder(child)
            item.setData(0, Qt.ItemDataRole.UserRole + 1, True)
        else:
            error_item = QTreeWidgetItem(["加载失败"])
            error_item.setData(0, Qt.ItemDataRole.UserRole, None)
            item.addChild(error_item)

        self._active_expands -= 1
        if self._active_expands <= 0:
            self._active_expands = 0
            self.folderTree.setEnabled(True)

    def get_target(self):
        """返回 (dir_id, dir_name)"""
        return self.selected_dir_id, self.selected_dir_name

    def get_ancestor_ids(self):
        """通过 API 查询目标文件夹的完整祖先链。"""
        if self.selected_dir_id in (None, 0):
            return set()
        ids = set()
        try:
            details = self.pan.file_details([self.selected_dir_id])
            paths = details.get("paths", []) if details else []
            for p in paths:
                fid = int(p.get("fileId", 0))
                if fid:
                    ids.add(fid)
        except Exception:
            pass
        return ids
