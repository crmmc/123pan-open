from PySide6.QtCore import Qt, QRunnable, QThreadPool, Signal, QObject, QRect
from PySide6.QtGui import QPen, QColor, QFontMetrics
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QListWidgetItem,
    QSizeGrip,
)

from qfluentwidgets import (
    TitleLabel,
    BodyLabel,
    SearchLineEdit,
    ListWidget,
)
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets.components.widgets.list_view import ListItemDelegate

from ..common.log import get_logger

logger = get_logger(__name__)


class SearchResultDelegate(ListItemDelegate):
    """自定义绘制搜索结果：左侧文件名，右侧灰色路径"""

    PATH_COLOR = QColor(140, 140, 140)
    ICON_SIZE = 20
    PADDING = 8
    GAP = 16

    def paint(self, painter, option, index):
        # 父类绘制背景、hover、选中高亮
        super().paint(painter, option, index)

        data = index.data(Qt.ItemDataRole.UserRole)
        if not data:
            return

        painter.save()

        file_name = data.get("FileName", "")
        file_type = int(data.get("Type", 0))
        paths = data.get("paths")

        # 父类 paint 已经 adjust 了 option.rect（加了 margin），直接用
        rect = option.rect
        x = rect.x() + self.PADDING
        y = rect.y()
        h = rect.height()
        right = rect.right() - self.PADDING

        # 图标
        icon = FIF.FOLDER.icon() if file_type == 1 else FIF.DOCUMENT.icon()
        icon_y = y + (h - self.ICON_SIZE) // 2
        icon.paint(painter, QRect(x, icon_y, self.ICON_SIZE, self.ICON_SIZE))
        x += self.ICON_SIZE + self.PADDING

        fm = QFontMetrics(option.font)
        text_y = y + (h + fm.ascent() - fm.descent()) // 2

        # 路径文本（右侧灰色）
        path_text = ""
        path_width = 0
        if paths:
            full_path = "/".join(paths)
            max_path_width = (right - x) * 2 // 5  # 路径最多占 40% 宽度
            path_text = fm.elidedText(full_path, Qt.TextElideMode.ElideMiddle, max_path_width)
            path_width = fm.horizontalAdvance(path_text)
        elif paths is None:
            path_text = "..."
            path_width = fm.horizontalAdvance(path_text)

        # 文件名（左侧）
        name_max_width = right - x - path_width - self.GAP if path_text else right - x
        elided_name = fm.elidedText(file_name, Qt.TextElideMode.ElideRight, max(name_max_width, 50))
        painter.setPen(option.palette.color(option.palette.ColorRole.Text))
        painter.drawText(x, text_y, elided_name)

        # 路径
        if path_text:
            painter.setPen(QPen(self.PATH_COLOR))
            painter.drawText(right - path_width, text_y, path_text)

        painter.restore()

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        size.setHeight(max(size.height(), 40))
        return size


class SearchDialog(QDialog):
    """搜索文件弹窗"""

    def __init__(self, pan, parent=None):
        super().__init__(parent)
        self.pan = pan
        self._result = None

        self.setWindowTitle("搜索文件")
        self.resize(650, 500)
        self.setMinimumSize(400, 300)
        self.setWindowFlags(
            self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        title = TitleLabel("搜索文件")
        layout.addWidget(title, alignment=Qt.AlignmentFlag.AlignCenter)

        self.searchBar = SearchLineEdit(self)
        self.searchBar.setPlaceholderText("输入文件名搜索")
        layout.addWidget(self.searchBar)

        self.resultList = ListWidget(self)
        self.resultList.setItemDelegate(SearchResultDelegate(self.resultList))
        layout.addWidget(self.resultList, 1)

        self.statusLabel = BodyLabel("", self)
        self.statusLabel.setStyleSheet("font-size: 12px; color: gray;")

        bottomLayout = QHBoxLayout()
        bottomLayout.addWidget(self.statusLabel, 1)
        bottomLayout.addWidget(QSizeGrip(self), 0, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)
        layout.addLayout(bottomLayout)

        self.searchBar.searchSignal.connect(self.__doSearch)
        self.searchBar.returnPressed.connect(self.searchBar.search)
        self.resultList.itemDoubleClicked.connect(self.__onItemDoubleClicked)

    def __doSearch(self, text):
        self.resultList.clear()
        self.statusLabel.setText("搜索中...")

        class SearchSignals(QObject):
            finished = Signal(object, str)

        class SearchTask(QRunnable):
            def __init__(self, pan, text, signals):
                super().__init__()
                self.pan = pan
                self.text = text
                self.signals = signals

            def run(self):
                try:
                    code, items = self.pan.get_dir_by_id(
                        0, save=False, all=True, limit=100, search_data=self.text
                    )
                    self.signals.finished.emit(items if code == 0 else [], "")
                except Exception as e:
                    self.signals.finished.emit([], str(e))

        self._search_signals = SearchSignals()
        self._search_signals.finished.connect(self.__onSearchFinished)
        task = SearchTask(self.pan, text, self._search_signals)
        QThreadPool.globalInstance().start(task)

    def __onSearchFinished(self, items, error):
        if error:
            self.statusLabel.setText(f"搜索失败: {error}")
            return

        self.statusLabel.setText(f"搜索到 {len(items)} 个结果")

        for file_item in items:
            data = {
                "FileId": int(file_item.get("FileId", 0)),
                "FileName": file_item.get("FileName", ""),
                "Type": int(file_item.get("Type", 0)),
                "ParentFileId": int(file_item.get("ParentFileId", 0)),
                "paths": None,
            }
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, data)
            self.resultList.addItem(item)

        if items:
            self.__fetchPaths(items)

    def __fetchPaths(self, items):
        """按 ParentFileId 分组，取代表文件调 file_details 获取完整路径"""
        # 对每个唯一 ParentFileId，取一个代表文件 ID
        pid_to_sample_fid = {}
        for item in items:
            pid = int(item.get("ParentFileId", 0))
            if pid not in pid_to_sample_fid:
                pid_to_sample_fid[pid] = int(item.get("FileId", 0))

        class PathSignals(QObject):
            finished = Signal(object)

        class FetchPathsTask(QRunnable):
            def __init__(self, pan, pid_to_fid, signals):
                super().__init__()
                self.pan = pan
                self.pid_to_fid = pid_to_fid
                self.signals = signals

            def run(self):
                result = {}
                for pid, fid in self.pid_to_fid.items():
                    try:
                        # 对文件本身调 file_details，paths 返回完整父目录链
                        details = self.pan.file_details([fid])
                        if details:
                            path_list = details.get("paths", [])
                            names = [p.get("fileName", "") for p in path_list]
                            result[pid] = names
                    except Exception:
                        pass
                self.signals.finished.emit(result)

        self._path_signals = PathSignals()
        self._path_signals.finished.connect(self.__onPathsFetched)
        task = FetchPathsTask(self.pan, pid_to_sample_fid, self._path_signals)
        QThreadPool.globalInstance().start(task)

    def __onPathsFetched(self, path_map):
        """路径获取完成，更新列表项"""
        for i in range(self.resultList.count()):
            item = self.resultList.item(i)
            data = item.data(Qt.ItemDataRole.UserRole)
            if not data:
                continue
            pid = data.get("ParentFileId", 0)
            if pid in path_map:
                data["paths"] = path_map[pid]
                item.setData(Qt.ItemDataRole.UserRole, data)

        self.resultList.viewport().update()

    def __onItemDoubleClicked(self, item):
        data = item.data(Qt.ItemDataRole.UserRole)
        if data:
            self._result = data
            self.accept()

    def get_result(self):
        return self._result
