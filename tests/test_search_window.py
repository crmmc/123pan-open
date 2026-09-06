from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import (
    QCloseEvent,
    QImage,
    QPainter,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import QDialog, QListWidgetItem, QStyleOptionViewItem

from src.app.view import search_window as search_module
from src.app.view.search_window import SearchDialog, SearchResultDelegate


class _FakeLabel:
    def __init__(self):
        self.text = None

    def setText(self, text):
        self.text = text


class _FakeViewport:
    def __init__(self):
        self.updated = False

    def update(self):
        self.updated = True


class _FakeListItem:
    def __init__(self):
        self._data = {}

    def setData(self, role, value):
        self._data[role] = value

    def data(self, role):
        return self._data.get(role)


class _FakeResultList:
    def __init__(self):
        self.items = []
        self._viewport = _FakeViewport()

    def clear(self):
        self.items.clear()

    def addItem(self, item):
        self.items.append(item)

    def count(self):
        return len(self.items)

    def item(self, index):
        return self.items[index]

    def viewport(self):
        return self._viewport


def test_search_finished_ignores_stale_result(monkeypatch):
    dialog = SearchDialog.__new__(SearchDialog)
    dialog._search_request_id = 2
    dialog._closed = False
    dialog._pending_signals = []
    dialog.statusLabel = _FakeLabel()
    dialog.resultList = _FakeResultList()
    dialog._SearchDialog__fetchPaths = MagicMock()
    monkeypatch.setattr(search_module, "QListWidgetItem", _FakeListItem)

    SearchDialog._SearchDialog__onSearchFinished(
        dialog,
        items=[{"FileId": 1, "FileName": "old.txt", "Type": 0, "ParentFileId": 0}],
        error="",
        request_id=1,
    )

    assert dialog.statusLabel.text is None
    assert dialog.resultList.items == []
    dialog._SearchDialog__fetchPaths.assert_not_called()


def test_paths_finished_ignores_stale_result():
    dialog = SearchDialog.__new__(SearchDialog)
    dialog._search_request_id = 2
    dialog._closed = False
    dialog._pending_signals = []
    dialog.resultList = _FakeResultList()
    item = _FakeListItem()
    item.setData(
        Qt.ItemDataRole.UserRole,
        {
            "FileId": 1,
            "FileName": "demo.txt",
            "Type": 0,
            "ParentFileId": 7,
            "paths": None,
        },
    )
    dialog.resultList.addItem(item)

    SearchDialog._SearchDialog__onPathsFetched(
        dialog,
        path_map={7: ["旧目录"]},
        request_id=1,
    )

    assert item.data(Qt.ItemDataRole.UserRole)["paths"] is None
    assert dialog.resultList.viewport().updated is False


def test_do_search_reports_business_error_instead_of_zero_results(monkeypatch):
    dialog = SearchDialog.__new__(SearchDialog)
    dialog.pan = MagicMock()
    dialog.pan.get_dir_by_id.return_value = (5001, [])
    dialog._search_request_id = 0
    dialog._closed = False
    dialog._pending_signals = []
    dialog.statusLabel = _FakeLabel()
    dialog.resultList = _FakeResultList()
    dialog._SearchDialog__fetchPaths = MagicMock()

    class _FakeThreadPool:
        def start(self, task):
            task.run()

    monkeypatch.setattr(search_module.QThreadPool, "globalInstance", lambda: _FakeThreadPool())

    SearchDialog._SearchDialog__doSearch(dialog, "demo")

    assert dialog.statusLabel.text == "搜索失败: 搜索失败，返回码: 5001"
    assert dialog.resultList.items == []
    dialog._SearchDialog__fetchPaths.assert_not_called()


# ---------------------------------------------------------------------------
# 批次 8 追加：SearchDialog 集成流 + SearchResultDelegate 绘制
# ---------------------------------------------------------------------------


class _FakeThreadPool:
    """记录提交的 QRunnable，测试内手动同步执行，避免真实线程时序。"""

    def __init__(self):
        self.tasks = []

    def start(self, task, _priority=0):
        self.tasks.append(task)

    def run_next(self):
        if self.tasks:
            self.tasks.pop(0).run()

    def run_pending(self):
        while self.tasks:
            self.tasks.pop(0).run()


@pytest.fixture
def fake_pool(monkeypatch):
    """把 search_window 引用的 QThreadPool.globalInstance() 换成可同步执行的假池。"""
    pool = _FakeThreadPool()
    monkeypatch.setattr(
        search_module,
        "QThreadPool",
        MagicMock(globalInstance=MagicMock(return_value=pool)),
    )
    return pool


@pytest.fixture
def pan():
    """mock Pan123：默认返回空结果、空详情，不触网。"""
    mock = MagicMock()
    mock.get_dir_by_id.return_value = (0, [])
    mock.file_details.return_value = None
    return mock


@pytest.fixture
def dialog(qapp, fake_pool, pan):
    """真实构造 SearchDialog（不启动任何任务）。"""
    return SearchDialog(pan)


@pytest.fixture
def delegate(dialog):
    """从真实列表上取自定义委托。"""
    result = dialog.resultList.itemDelegate()
    assert isinstance(result, SearchResultDelegate)
    return result


def _item_data(dialog, row):
    item = dialog.resultList.item(row)
    assert item is not None
    return item.data(Qt.ItemDataRole.UserRole)


class TestDialogInit:
    def test_init_builds_search_ui(self, dialog, pan):
        assert dialog.windowTitle() == "搜索文件"
        assert dialog.searchBar.placeholderText() == "输入文件名搜索"
        assert isinstance(dialog.resultList.itemDelegate(), SearchResultDelegate)
        assert dialog.statusLabel.text() == ""
        assert dialog._search_request_id == 0
        assert dialog._pending_signals == []
        assert dialog.get_result() is None
        pan.get_dir_by_id.assert_not_called()


class TestSearchFlow:
    def test_search_success_populates_results_and_fetches_paths(self, dialog, pan, fake_pool):
        pan.get_dir_by_id.return_value = (0, [
            {"FileId": 11, "FileName": "report.pdf", "Type": 0, "ParentFileId": 7},
            {"FileId": 12, "FileName": "photos", "Type": 1, "ParentFileId": 7},
        ])
        pan.file_details.return_value = {
            "paths": [{"fileName": "docs"}, {"fileName": "work"}]
        }

        dialog.searchBar.searchSignal.emit("report")
        fake_pool.run_pending()

        pan.get_dir_by_id.assert_called_once_with(
            0, all=True, limit=100, search_data="report"
        )
        pan.file_details.assert_called_once_with([11])  # 同一父目录只取代表文件
        assert dialog.resultList.count() == 2
        assert dialog.statusLabel.text() == "搜索到 2 个结果"
        for row in range(dialog.resultList.count()):
            data = _item_data(dialog, row)
            assert data["paths"] == ["docs", "work"]
        first = dialog.resultList.item(0)
        assert first is not None
        assert _item_data(dialog, 0)["Type"] == 0
        assert _item_data(dialog, 1)["Type"] == 1

    def test_blank_search_is_ignored(self, dialog, pan, fake_pool):
        dialog.searchBar.searchSignal.emit("   ")

        assert dialog.resultList.count() == 0
        assert dialog.statusLabel.text() == ""
        assert fake_pool.tasks == []
        pan.get_dir_by_id.assert_not_called()

    def test_search_error_code_shown_in_status(self, dialog, pan, fake_pool):
        pan.get_dir_by_id.return_value = (5001, [])

        dialog.searchBar.searchSignal.emit("demo")
        fake_pool.run_pending()

        assert dialog.statusLabel.text() == "搜索失败: 搜索失败，返回码: 5001"
        assert dialog.resultList.count() == 0

    def test_search_exception_shown_in_status(self, dialog, pan, fake_pool):
        pan.get_dir_by_id.side_effect = RuntimeError("网络断开")

        dialog.searchBar.searchSignal.emit("demo")
        fake_pool.run_pending()

        assert dialog.statusLabel.text() == "搜索失败: 网络断开"
        assert dialog.resultList.count() == 0

    def test_second_search_invalidates_first_result(self, dialog, pan, fake_pool):
        """旧请求结果按 request_id 丢弃，仅渲染最新一次搜索。"""
        pan.get_dir_by_id.side_effect = [
            (0, [{"FileId": 1, "FileName": "old.txt", "Type": 0, "ParentFileId": 0}]),
            (0, [{"FileId": 2, "FileName": "new.txt", "Type": 0, "ParentFileId": 0}]),
        ]

        dialog.searchBar.searchSignal.emit("first")
        dialog.searchBar.searchSignal.emit("second")
        fake_pool.run_pending()

        assert dialog.resultList.count() == 1
        assert _item_data(dialog, 0)["FileName"] == "new.txt"

    def test_search_task_cancelled_before_run_is_noop(self, dialog, pan, fake_pool):
        dialog.searchBar.searchSignal.emit("demo")
        task = fake_pool.tasks[-1]
        task.signals._cancelled = True

        fake_pool.run_pending()

        pan.get_dir_by_id.assert_not_called()
        assert dialog.resultList.count() == 0
        assert dialog.statusLabel.text() == "搜索中..."

    def test_search_task_cancelled_during_request_drops_result(self, dialog, pan, fake_pool):
        dialog.searchBar.searchSignal.emit("demo")
        sig = fake_pool.tasks[-1].signals

        def _cancel_and_return(*_args, **_kwargs):
            sig._cancelled = True
            return 0, [{"FileId": 1, "FileName": "a.txt", "Type": 0, "ParentFileId": 0}]

        pan.get_dir_by_id.side_effect = _cancel_and_return
        fake_pool.run_pending()

        pan.file_details.assert_not_called()
        assert dialog.resultList.count() == 0
        assert dialog.statusLabel.text() == "搜索中..."


class TestFetchPaths:
    def test_paths_fetch_failure_keeps_items_without_paths(self, dialog, pan, fake_pool):
        pan.get_dir_by_id.return_value = (0, [
            {"FileId": 5, "FileName": "a.txt", "Type": 0, "ParentFileId": 7},
        ])
        pan.file_details.side_effect = RuntimeError("超时")

        dialog.searchBar.searchSignal.emit("a")
        fake_pool.run_pending()

        assert dialog.resultList.count() == 1
        assert _item_data(dialog, 0)["paths"] is None

    def test_paths_fetch_skips_missing_details_and_items_without_data(self, dialog, pan, fake_pool):
        pan.get_dir_by_id.return_value = (0, [
            {"FileId": 5, "FileName": "a.txt", "Type": 0, "ParentFileId": 7},
        ])

        dialog.searchBar.searchSignal.emit("a")
        dialog.resultList.addItem(QListWidgetItem())  # 无数据的条目应被跳过
        fake_pool.run_pending()

        assert dialog.statusLabel.text() == "搜索到 1 个结果"
        assert dialog.resultList.item(0) is not None  # 行 0 为无数据条目
        assert _item_data(dialog, 1)["paths"] is None  # details 为 None 时不更新

    def test_paths_updated_only_for_matching_parent_ids(self, dialog, pan, fake_pool):
        pan.get_dir_by_id.return_value = (0, [
            {"FileId": 5, "FileName": "a.txt", "Type": 0, "ParentFileId": 7},
            {"FileId": 6, "FileName": "b.txt", "Type": 0, "ParentFileId": 8},
        ])

        def _details(file_ids):
            if file_ids == [5]:
                return {"paths": [{"fileName": "docs"}]}
            raise RuntimeError("fid 6 查询失败")

        pan.file_details.side_effect = _details
        dialog.searchBar.searchSignal.emit("x")
        fake_pool.run_pending()

        assert _item_data(dialog, 0)["paths"] == ["docs"]
        assert _item_data(dialog, 1)["paths"] is None

    def test_paths_task_cancelled_before_run_does_nothing(self, dialog, pan, fake_pool):
        pan.get_dir_by_id.return_value = (0, [
            {"FileId": 5, "FileName": "a.txt", "Type": 0, "ParentFileId": 7},
        ])
        dialog.searchBar.searchSignal.emit("a")
        fake_pool.run_next()  # 只跑搜索任务，路径任务排队

        paths_task = fake_pool.tasks[-1]
        paths_task.signals._cancelled = True
        fake_pool.run_next()

        pan.file_details.assert_not_called()
        assert _item_data(dialog, 0)["paths"] is None

    def test_paths_task_cancelled_midway_skips_remaining(self, dialog, pan, fake_pool):
        pan.get_dir_by_id.return_value = (0, [
            {"FileId": 5, "FileName": "a.txt", "Type": 0, "ParentFileId": 7},
            {"FileId": 6, "FileName": "b.txt", "Type": 0, "ParentFileId": 8},
        ])
        dialog.searchBar.searchSignal.emit("x")
        fake_pool.run_next()  # 搜索任务完成后路径任务排队
        sig = fake_pool.tasks[-1].signals

        def _cancel_on_first_call(_file_ids):
            sig._cancelled = True
            return {"paths": [{"fileName": "docs"}]}

        pan.file_details.side_effect = _cancel_on_first_call
        fake_pool.run_next()

        pan.file_details.assert_called_once()  # 第二轮循环前已取消
        assert _item_data(dialog, 0)["paths"] is None  # finished 未发射
        assert _item_data(dialog, 1)["paths"] is None


class TestDialogClose:
    def test_accept_marks_closed_and_cancels_pending(self, dialog, fake_pool):
        dialog.searchBar.searchSignal.emit("demo")
        sig = fake_pool.tasks[-1].signals
        assert sig._cancelled is False

        dialog.accept()

        assert dialog._closed is True
        assert sig._cancelled is True
        assert dialog.result() == QDialog.DialogCode.Accepted

    def test_reject_marks_closed_and_cancels_pending(self, dialog, fake_pool):
        dialog.searchBar.searchSignal.emit("demo")
        sig = fake_pool.tasks[-1].signals

        dialog.reject()

        assert dialog._closed is True
        assert sig._cancelled is True
        assert dialog.result() == QDialog.DialogCode.Rejected

    def test_close_event_marks_closed_and_cancels_pending(self, dialog, fake_pool):
        dialog.searchBar.searchSignal.emit("demo")
        sig = fake_pool.tasks[-1].signals

        dialog.closeEvent(QCloseEvent())

        assert dialog._closed is True
        assert sig._cancelled is True

    def test_finished_after_close_is_ignored(self, dialog, pan, fake_pool):
        dialog.searchBar.searchSignal.emit("demo")
        dialog._closed = True

        fake_pool.run_pending()

        assert dialog.resultList.count() == 0

    def test_paths_after_close_are_ignored(self, dialog, pan, fake_pool):
        pan.get_dir_by_id.return_value = (0, [
            {"FileId": 5, "FileName": "a.txt", "Type": 0, "ParentFileId": 7},
        ])
        dialog.searchBar.searchSignal.emit("a")
        fake_pool.run_next()  # 搜索完成、条目已加入，路径任务排队
        dialog._closed = True

        fake_pool.run_next()

        assert _item_data(dialog, 0)["paths"] is None


class TestItemSelection:
    def test_double_click_accepts_dialog_with_result(self, dialog, pan, fake_pool):
        pan.get_dir_by_id.return_value = (0, [
            {"FileId": 11, "FileName": "report.pdf", "Type": 0, "ParentFileId": 7},
        ])
        pan.file_details.return_value = {"paths": [{"fileName": "docs"}]}
        dialog.searchBar.searchSignal.emit("report")
        fake_pool.run_pending()

        item = dialog.resultList.item(0)
        assert item is not None
        dialog.resultList.itemDoubleClicked.emit(item)

        assert dialog.result() == QDialog.DialogCode.Accepted
        result = dialog.get_result()
        assert result is not None
        assert result["FileId"] == 11
        assert dialog._closed is True

    def test_double_click_item_without_data_is_ignored(self, dialog):
        dialog.resultList.addItem(QListWidgetItem())
        item = dialog.resultList.item(0)
        assert item is not None

        dialog.resultList.itemDoubleClicked.emit(item)

        assert dialog.get_result() is None


class TestSearchResultDelegate:
    @staticmethod
    def _paint(delegate, data):
        model = QStandardItemModel()
        item = QStandardItem()
        if data is not None:
            item.setData(data, Qt.ItemDataRole.UserRole)
        model.appendRow(item)
        index = model.index(0, 0)

        image = QImage(360, 48, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.white)
        painter = QPainter(image)
        try:
            option = QStyleOptionViewItem()
            option.rect = QRect(0, 0, 360, 48)
            delegate.paint(painter, option, index)
        finally:
            painter.end()
        return image

    @pytest.mark.parametrize(
        "data",
        [
            {"FileId": 1, "FileName": "report.pdf", "Type": 0, "paths": ["docs", "work"]},
            {"FileId": 2, "FileName": "photos", "Type": 1, "paths": None},
            {"FileId": 3, "FileName": "empty.txt", "Type": 0, "paths": []},
            {
                "FileId": 4,
                "FileName": "长文件名" * 40,
                "Type": 0,
                "paths": ["a" * 80, "b" * 80],
            },
        ],
        ids=["file-paths", "folder-no-paths", "empty-paths", "long-elided"],
    )
    def test_paint_renders_item_data(self, delegate, data):
        image = self._paint(delegate, data)
        assert not image.isNull()

    def test_paint_without_item_data_returns_early(self, delegate):
        image = self._paint(delegate, None)
        assert not image.isNull()

    def test_size_hint_enforces_minimum_height(self, delegate):
        model = QStandardItemModel()
        model.appendRow(QStandardItem())
        index = model.index(0, 0)

        size = delegate.sizeHint(QStyleOptionViewItem(), index)

        assert size.height() >= 40
