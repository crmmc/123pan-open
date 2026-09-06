"""MoveDialog（移动文件弹窗）测试。

覆盖：_ExpandTask 三分支（成功/错误码/异常）、目录树构造与根节点
异步加载、目标选择（含占位项忽略）、accept/reject/closeEvent 关闭
标记、子树懒加载渲染（目录过滤、错误项、并发展开计数）、
get_ancestor_ids 祖先链查询（空选/成功/None/异常）。

沿用批次 4/5 约定：QThreadPool 换成可同步执行的假池，shiboken6.isValid
patch 为 True；pan 为 MagicMock，不触网。
"""
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QDialog, QTreeWidgetItem

from src.app.view import move_window as mw_module
from src.app.view.move_window import MoveDialog

USER_ROLE = Qt.ItemDataRole.UserRole
LOADED_ROLE = Qt.ItemDataRole.UserRole + 1


class _FakeThreadPool:
    """记录提交的 QRunnable，测试内手动同步执行，避免真实线程时序。"""

    def __init__(self):
        self.tasks = []

    def start(self, task, _priority=0):
        self.tasks.append(task)

    def run_pending(self):
        while self.tasks:
            self.tasks.pop(0).run()


@pytest.fixture(autouse=True)
def _bypass_shiboken_valid(monkeypatch):
    """_onExpandFinished 中的 shiboken6.isValid 对测试对象可能误判，统一 bypass。"""
    monkeypatch.setattr("src.app.view.move_window.shiboken6.isValid", lambda _obj: True)


@pytest.fixture
def fake_pool(monkeypatch):
    """把 move_window 引用的 QThreadPool.globalInstance() 换成可同步执行的假池。"""
    pool = _FakeThreadPool()
    monkeypatch.setattr(
        mw_module,
        "QThreadPool",
        MagicMock(globalInstance=MagicMock(return_value=pool)),
    )
    return pool


@pytest.fixture
def pan():
    """mock Pan123：get_dir_by_id 默认返回空目录列表。"""
    mock = MagicMock()
    mock.get_dir_by_id.return_value = (0, [])
    return mock


@pytest.fixture
def dialog(qapp, fake_pool, pan):
    """真实构造 MoveDialog；构造期根节点展开任务在假池中同步收尾。"""
    dlg = MoveDialog(pan, current_dir_id=0)
    fake_pool.run_pending()
    return dlg


def _tree_item(name, dir_id, loaded=False):
    item = QTreeWidgetItem([name])
    item.setData(0, USER_ROLE, dir_id)
    item.setData(0, LOADED_ROLE, loaded)
    return item


class TestExpandTask:
    """_ExpandTask.run 的三分支：成功 / API 错误码 / 异常。"""

    @staticmethod
    def _make_task(pan, dir_id):
        signals = MoveDialog._ExpandSignals()
        received = []
        signals.finished.connect(lambda *args: received.append(args))
        return MoveDialog._ExpandTask(pan, dir_id, signals), received

    def test_run_emits_items_on_success(self, qapp, pan):
        pan.get_dir_by_id.return_value = (0, [{"Type": 1, "FileId": 2}])

        task, received = self._make_task(pan, 7)
        assert task.autoDelete() is True
        task.run()

        assert received == [(7, [{"Type": 1, "FileId": 2}], "")]

    def test_run_emits_error_on_nonzero_code(self, qapp, pan):
        pan.get_dir_by_id.return_value = (403, [])

        task, received = self._make_task(pan, 7)
        task.run()

        assert received == [(7, [], "API 返回码: 403")]

    def test_run_emits_error_message_on_exception(self, qapp, pan):
        pan.get_dir_by_id.side_effect = RuntimeError("boom")

        task, received = self._make_task(pan, 9)
        task.run()

        assert received == [(9, [], "boom")]


class TestDialogInit:
    def test_init_builds_tree_and_loads_root(self, dialog, pan):
        assert dialog.windowTitle() == "移动到"
        assert dialog.selected_dir_id is None
        assert dialog.selected_dir_name is None
        assert dialog.get_target() == (None, None)
        assert not dialog.ok_button.isEnabled()

        root = dialog.folderTree.topLevelItem(0)
        assert root.text(0) == "根目录"
        assert root.data(0, USER_ROLE) == 0
        # 构造期根节点已通过假池同步加载完成：占位符被移除、标记已加载、树恢复可用
        assert root.childCount() == 0
        assert root.data(0, LOADED_ROLE) is True
        assert dialog.folderTree.isEnabled()
        pan.get_dir_by_id.assert_called_once_with(0, all=True, limit=100)


class TestSelection:
    def test_click_valid_item_selects_target(self, dialog):
        item = _tree_item("图片", 42)
        dialog.folderTree.addTopLevelItem(item)

        dialog.folderTree.itemClicked.emit(item, 0)

        assert dialog.selected_dir_id == 42
        assert dialog.selected_dir_name == "图片"
        assert dialog.ok_button.isEnabled()
        assert dialog.ok_button.text() == "移动到「图片」"
        assert dialog.get_target() == (42, "图片")

    def test_click_placeholder_item_is_ignored(self, dialog):
        placeholder = _tree_item("加载中...", None)
        dialog.folderTree.addTopLevelItem(placeholder)

        dialog.folderTree.itemClicked.emit(placeholder, 0)

        assert dialog.selected_dir_id is None
        assert dialog.selected_dir_name is None
        assert not dialog.ok_button.isEnabled()
        assert dialog.ok_button.text() == "移动到此"

    def test_accept_marks_closed_and_sets_result(self, dialog):
        dialog.accept()

        assert dialog._closed is True
        assert dialog.result() == QDialog.DialogCode.Accepted

    def test_reject_marks_closed_and_sets_result(self, dialog):
        dialog.reject()

        assert dialog._closed is True
        assert dialog.result() == QDialog.DialogCode.Rejected

    def test_close_event_marks_closed(self, dialog):
        event = QCloseEvent()

        dialog.closeEvent(event)

        assert dialog._closed is True


class TestTreeExpand:
    def test_expand_loaded_item_returns_early(self, dialog, fake_pool):
        root = dialog.folderTree.topLevelItem(0)
        assert root.data(0, LOADED_ROLE) is True

        dialog.folderTree.itemExpanded.emit(root)

        assert fake_pool.tasks == []
        assert root.childCount() == 0

    def test_expand_placeholder_item_returns_early(self, dialog, fake_pool):
        placeholder = _tree_item("加载中...", None)
        dialog.folderTree.addTopLevelItem(placeholder)

        dialog.folderTree.itemExpanded.emit(placeholder)

        assert fake_pool.tasks == []
        assert placeholder.childCount() == 0

    def test_expand_unloaded_item_renders_folder_children(self, dialog, pan, fake_pool):
        child = _tree_item("docs", 5)
        dialog.folderTree.topLevelItem(0).addChild(child)
        pan.get_dir_by_id.return_value = (0, [
            {"Type": 1, "FileName": "sub", "FileId": 6},
            {"Type": 0, "FileName": "file.txt", "FileId": 7},  # 非目录应被过滤
        ])

        dialog.folderTree.itemExpanded.emit(child)
        assert not dialog.folderTree.isEnabled()  # 加载期间禁用树

        fake_pool.run_pending()

        pan.get_dir_by_id.assert_called_with(5, all=True, limit=100)
        assert child.childCount() == 1  # 只有 sub（file.txt 已被过滤）
        sub = child.child(0)
        assert sub.text(0) == "sub"
        assert sub.data(0, USER_ROLE) == 6
        assert sub.data(0, LOADED_ROLE) is False
        assert sub.childCount() == 1  # sub 自带加载占位
        assert sub.child(0).data(0, USER_ROLE) is None
        assert child.data(0, LOADED_ROLE) is True
        assert dialog.folderTree.isEnabled()  # 完成后恢复

    def test_expand_failure_adds_error_item(self, dialog, pan, fake_pool):
        child = _tree_item("bad", 9)
        dialog.folderTree.topLevelItem(0).addChild(child)
        pan.get_dir_by_id.return_value = (500, [])

        dialog.folderTree.itemExpanded.emit(child)
        fake_pool.run_pending()

        assert child.childCount() == 1
        error_item = child.child(0)
        assert error_item.text(0) == "加载失败"
        assert error_item.data(0, USER_ROLE) is None
        assert child.data(0, LOADED_ROLE) is False  # 失败不标记，可重试
        assert dialog.folderTree.isEnabled()

    def test_concurrent_expands_keep_tree_disabled_until_all_finished(
        self, dialog, pan, fake_pool
    ):
        root = dialog.folderTree.topLevelItem(0)
        first = _tree_item("a", 1)
        second = _tree_item("b", 2)
        root.addChild(first)
        root.addChild(second)

        dialog.folderTree.itemExpanded.emit(first)
        dialog.folderTree.itemExpanded.emit(second)
        assert dialog._active_expands == 2
        assert not dialog.folderTree.isEnabled()

        fake_pool.run_pending()

        assert dialog._active_expands == 0
        assert dialog.folderTree.isEnabled()


class TestOnExpandFinished:
    def test_finish_after_close_is_ignored(self, dialog):
        dialog._closed = True
        root = dialog.folderTree.topLevelItem(0)

        dialog._onExpandFinished(root, 3, [{"Type": 1, "FileName": "x", "FileId": 8}], "")

        assert root.childCount() == 0
        assert root.data(0, LOADED_ROLE) is True  # 未被改动

    def test_finish_with_invalid_item_is_ignored(self, dialog, monkeypatch):
        monkeypatch.setattr(
            "src.app.view.move_window.shiboken6.isValid", lambda _obj: False
        )
        root = dialog.folderTree.topLevelItem(0)

        dialog._onExpandFinished(root, 3, [], "")

        assert root.childCount() == 0


class TestGetAncestorIds:
    def test_no_selection_returns_empty_set(self, dialog, pan):
        assert dialog.get_ancestor_ids() == set()
        pan.file_details.assert_not_called()

    def test_root_selection_returns_empty_set(self, dialog, pan):
        dialog.selected_dir_id = 0

        assert dialog.get_ancestor_ids() == set()
        pan.file_details.assert_not_called()

    def test_collects_nonzero_path_ids(self, dialog, pan):
        dialog.selected_dir_id = 42
        pan.file_details.return_value = {
            "paths": [{"fileId": 1}, {"fileId": 42}, {"fileId": 0}, {}]
        }

        assert dialog.get_ancestor_ids() == {1, 42}
        pan.file_details.assert_called_once_with([42])

    def test_none_details_returns_empty_set(self, dialog, pan):
        dialog.selected_dir_id = 42
        pan.file_details.return_value = None

        assert dialog.get_ancestor_ids() == set()

    def test_api_exception_returns_empty_set(self, dialog, pan):
        dialog.selected_dir_id = 42
        pan.file_details.side_effect = RuntimeError("net down")

        assert dialog.get_ancestor_ids() == set()
