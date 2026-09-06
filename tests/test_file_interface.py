from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QEvent, QPoint, QSize, Qt, QMimeData, QUrl
from PySide6.QtGui import (
    QAction,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QDialog,
    QTableWidgetSelectionRange,
    QTableWidgetItem,
    QTreeWidgetItem,
    QWidget,
)

from src.app.common.api import format_file_size
from src.app.view import file_interface as fi_module
from src.app.view.file_interface import FileInterface
from src.app.view.upload_conflict_dialog import ConflictAction


@pytest.fixture(autouse=True)
def _bypass_shiboken_valid(monkeypatch):
    """测试中 FileInterface.__new__ 创建的对象没有 C++ 侧初始化，
    shiboken6.isValid 会返回 False。这里 patch 掉以避免误判。"""
    monkeypatch.setattr("src.app.view.file_interface.shiboken6.isValid", lambda _obj: True)


class _FakeUrl:
    def __init__(self, local_file="", is_local=True):
        self._local_file = local_file
        self._is_local = is_local

    def isLocalFile(self):
        return self._is_local

    def toLocalFile(self):
        return self._local_file


class _FakeMimeData:
    def __init__(self, urls):
        self._urls = urls

    def hasUrls(self):
        return bool(self._urls)

    def urls(self):
        return self._urls


def test_extract_local_paths_filters_non_local_and_duplicates(tmp_path):
    file_path = tmp_path / "a.txt"
    file_path.write_text("a", encoding="utf-8")
    folder_path = tmp_path / "folder"
    folder_path.mkdir()

    mime_data = _FakeMimeData(
        [
            _FakeUrl(str(file_path)),
            _FakeUrl(str(file_path)),
            _FakeUrl(str(folder_path)),
            _FakeUrl("https://example.com/demo", is_local=False),
            _FakeUrl(""),
        ]
    )

    paths = FileInterface._FileInterface__extractLocalPaths(mime_data)

    assert paths == [file_path, folder_path]


def test_build_upload_summary_handles_empty_folder_upload():
    summary = FileInterface._FileInterface__buildUploadSummary(0, 3)

    assert summary == "已创建 3 个文件夹"


@patch("src.app.view.file_interface.QFileDialog.getExistingDirectory")
def test_upload_folder_calls_prepare_with_selected_path(mock_dialog):
    mock_dialog.return_value = "/some/folder"
    mock_prepare = MagicMock()

    fi = MagicMock()
    fi._FileInterface__prepareLocalUploads = mock_prepare
    FileInterface._FileInterface__uploadFolder(fi)

    mock_prepare.assert_called_once_with([Path("/some/folder")])


@patch("src.app.view.file_interface.QFileDialog.getExistingDirectory")
def test_upload_folder_cancel_does_not_call_prepare(mock_dialog):
    mock_dialog.return_value = ""
    mock_prepare = MagicMock()

    fi = MagicMock()
    fi._FileInterface__prepareLocalUploads = mock_prepare
    FileInterface._FileInterface__uploadFolder(fi)

    mock_prepare.assert_not_called()


def test_drag_highlight_sets_stylesheet_on_enter():
    fi = MagicMock()
    fi.fileTable = MagicMock()
    viewport_mock = MagicMock()
    fi.fileTable.viewport.return_value = viewport_mock
    fi._FileInterface__acceptLocalDrop = MagicMock(return_value=True)

    event = MagicMock()
    event.type.return_value = QEvent.Type.DragEnter

    result = FileInterface._FileInterface__handleDropEvent(fi, event)

    assert result is True
    viewport_mock.setStyleSheet.assert_called_once_with(
        "border: 2px dashed #0078d4; border-radius: 8px;"
    )


def test_drag_highlight_clears_on_leave():
    fi = MagicMock()
    fi.fileTable = MagicMock()
    viewport_mock = MagicMock()
    fi.fileTable.viewport.return_value = viewport_mock

    event = MagicMock()
    event.type.return_value = QEvent.Type.DragLeave

    result = FileInterface._FileInterface__handleDropEvent(fi, event)

    assert result is False
    viewport_mock.setStyleSheet.assert_called_once_with("")


def test_drag_highlight_clears_on_drop():
    fi = MagicMock()
    fi.fileTable = MagicMock()
    viewport_mock = MagicMock()
    fi.fileTable.viewport.return_value = viewport_mock
    fi._FileInterface__dropLocalPaths = MagicMock(return_value=True)

    event = MagicMock()
    event.type.return_value = QEvent.Type.Drop

    result = FileInterface._FileInterface__handleDropEvent(fi, event)

    assert result is True
    viewport_mock.setStyleSheet.assert_called_once_with("")


def test_prepare_upload_task_skips_failed_file_but_keeps_other_uploads(tmp_path):
    ok_file = tmp_path / "ok.txt"
    ok_file.write_text("ok", encoding="utf-8")
    missing_file = tmp_path / "missing.txt"
    pan = MagicMock()
    pan._get_dir_items_by_id.return_value = []
    task = FileInterface.PrepareUploadTask(
        pan=pan,
        target_dir_id=9,
        local_paths=[str(ok_file), str(missing_file)],
    )
    results = []
    task.signals.finished.connect(lambda entries, ef, efld, error: results.append(
        (entries, ef, efld, error)
    ))

    task.run()

    entries, existing_files, existing_folders, error = results[0]
    assert len(entries) == 1
    assert entries[0]["path"] == ok_file
    assert entries[0]["is_dir"] is False
    assert entries[0]["conflict"] is False
    assert entries[0]["file_size"] == 2
    assert existing_files == set()
    assert existing_folders == set()
    assert "missing.txt" in error


def test_prepare_upload_task_stops_when_folder_creation_fails(tmp_path):
    folder = tmp_path / "folder"
    folder.mkdir()
    pan = MagicMock()
    pan._get_dir_items_by_id.return_value = []
    task = FileInterface.PrepareUploadTask(
        pan=pan,
        target_dir_id=9,
        local_paths=[str(folder)],
    )
    results = []
    task.signals.finished.connect(lambda entries, ef, efld, error: results.append(
        (entries, ef, efld, error)
    ))

    task.run()

    entries, existing_files, existing_folders, error = results[0]
    assert len(entries) == 1
    assert entries[0]["is_dir"] is True
    assert entries[0]["conflict"] is False
    assert existing_files == set()
    assert existing_folders == set()
    assert error == ""


def test_prepare_upload_finished_drops_stale_cross_account_result():
    fi = MagicMock()
    fi.pan = object()
    fi.transfer_interface = MagicMock()
    fi.transfer_interface.current_account_name = "current"
    fi.current_dir_id = 7
    fi._FileInterface__updateTreeUI = MagicMock()
    fi._FileInterface__refreshFileList = MagicMock()

    FileInterface._FileInterface__onPrepareUploadFinished(
        fi,
        entries=[{
            "path": Path("/tmp/a.txt"),
            "is_dir": False,
            "conflict": False,
            "file_size": 1,
        }],
        existing_file_names=set(),
        existing_folder_names=set(),
        error="",
        context={
            "pan": object(),
            "account_name": "old",
            "target_dir_id": 7,
        },
    )

    fi.transfer_interface.add_upload_task.assert_not_called()
    fi._FileInterface__updateTreeUI.assert_not_called()
    fi._FileInterface__refreshFileList.assert_not_called()


def test_jump_finished_drops_stale_result():
    fi = FileInterface.__new__(FileInterface)
    fi._jump_request_id = 2
    fi.path_stack = [(0, "根目录")]
    fi.current_dir_id = 0
    fi._FileInterface__updateBreadcrumb = MagicMock()

    FileInterface._FileInterface__onJumpFinished(
        fi,
        detail_paths=[{"fileId": 7, "fileName": "docs"}],
        target_dir_id=7,
        select_file_id=None,
        error="",
        request_id=1,
    )

    assert fi.path_stack == [(0, "根目录")]
    assert fi.current_dir_id == 0
    fi._FileInterface__updateBreadcrumb.assert_not_called()


def test_create_folder_finished_drops_stale_dir_result():
    fi = FileInterface.__new__(FileInterface)
    fi.pan = object()
    fi.current_dir_id = 9
    fi.transfer_interface = MagicMock(current_account_name="alice")
    fi._FileInterface__updateFileListUI = MagicMock()
    fi._FileInterface__updateTreeUI = MagicMock()

    FileInterface._FileInterface__onCreateFolderFinished(
        fi,
        result=True,
        folder_name="docs",
        error="",
        file_items=[{"FileId": 1, "FileName": "docs"}],
        folder_items=[{"FileId": 1, "FileName": "docs"}],
        context={
            "pan": fi.pan,
            "account_name": "alice",
            "dir_id": 7,
            "request_id": None,
        },
    )

    fi._FileInterface__updateFileListUI.assert_not_called()
    fi._FileInterface__updateTreeUI.assert_not_called()


def test_move_finished_drops_stale_account_result():
    fi = FileInterface.__new__(FileInterface)
    fi.pan = object()
    fi.current_dir_id = 7
    fi.transfer_interface = MagicMock(current_account_name="current")
    fi._FileInterface__refreshFileList = MagicMock()

    FileInterface._FileInterface__onMoveFilesFinished(
        fi,
        success=True,
        count=2,
        target_name="目标目录",
        error="",
        context={
            "pan": fi.pan,
            "account_name": "old",
            "dir_id": 7,
            "request_id": None,
        },
    )

    fi._FileInterface__refreshFileList.assert_not_called()


@patch("src.app.view.file_interface.MessageBox", side_effect=AssertionError("should not open"))
def test_file_details_finished_drops_stale_request(_mock_message_box):
    fi = FileInterface.__new__(FileInterface)
    fi.pan = object()
    fi.transfer_interface = MagicMock(current_account_name="alice")
    fi._file_details_request_id = 2

    FileInterface._FileInterface__onFileDetailsFinished(
        fi,
        file_name="demo.txt",
        data={"paths": [], "fileNum": 1, "dirNum": 0, "totalSize": 1},
        error="",
        context={
            "pan": fi.pan,
            "account_name": "alice",
            "dir_id": None,
            "request_id": 1,
        },
    )


def test_async_context_helper_marks_stale_storage_result():
    fi = FileInterface.__new__(FileInterface)
    fi.pan = object()
    fi.transfer_interface = MagicMock(current_account_name="alice")
    fi.current_dir_id = 7

    stale = FileInterface._FileInterface__isAsyncContextStale(
        fi,
        {
            "pan": object(),
            "account_name": "alice",
            "dir_id": None,
            "request_id": None,
        },
    )

    assert stale is True


def test_folder_prepare_done_drops_stale_account_result(monkeypatch):
    monkeypatch.setattr("src.app.view.file_interface.InfoBar.success", lambda **_kwargs: None)
    monkeypatch.setattr("src.app.view.file_interface.InfoBar.warning", lambda **_kwargs: None)
    fi = FileInterface.__new__(FileInterface)
    fi.pan = object()
    fi.current_dir_id = 7
    fi.transfer_interface = MagicMock(current_account_name="current")
    fi._FileInterface__updateTreeUI = MagicMock()
    fi._FileInterface__refreshFileList = MagicMock()
    fi._FileInterface__buildUploadSummary = MagicMock(return_value="summary")

    FileInterface._FileInterface__onFolderPrepareDone(
        fi,
        folder_uploads=[{
            "file_name": "a.txt",
            "file_size": 1,
            "local_path": "/tmp/a.txt",
            "target_dir_id": 7,
        }],
        folder_items=[{"FileId": 1, "FileName": "docs"}],
        created_dir_count=1,
        folder_error="",
        context={
            "pan": fi.pan,
            "account_name": "old",
            "dir_id": 7,
            "request_id": None,
        },
        added_count=0,
        error="",
        should_refresh_current_dir=True,
    )

    fi.transfer_interface.add_upload_task.assert_not_called()
    fi._FileInterface__updateTreeUI.assert_not_called()
    fi._FileInterface__refreshFileList.assert_not_called()


def test_folder_prepare_done_keeps_enqueue_when_only_directory_changes(monkeypatch):
    monkeypatch.setattr("src.app.view.file_interface.InfoBar.success", lambda **_kwargs: None)
    monkeypatch.setattr("src.app.view.file_interface.InfoBar.warning", lambda **_kwargs: None)
    fi = FileInterface.__new__(FileInterface)
    fi.pan = object()
    fi.current_dir_id = 9
    fi.transfer_interface = MagicMock(current_account_name="alice")
    fi._FileInterface__updateTreeUI = MagicMock()
    fi._FileInterface__refreshFileList = MagicMock()
    fi._FileInterface__buildUploadSummary = MagicMock(return_value="summary")

    FileInterface._FileInterface__onFolderPrepareDone(
        fi,
        folder_uploads=[{
            "file_name": "a.txt",
            "file_size": 1,
            "local_path": "/tmp/a.txt",
            "target_dir_id": 7,
        }],
        folder_items=[{"FileId": 1, "FileName": "docs"}],
        created_dir_count=1,
        folder_error="",
        context={
            "pan": fi.pan,
            "account_name": "alice",
            "dir_id": 7,
            "request_id": None,
        },
        added_count=0,
        error="",
        should_refresh_current_dir=True,
    )

    fi.transfer_interface.add_upload_task.assert_called_once_with(
        "a.txt", 1, "/tmp/a.txt", 7,
    )
    fi._FileInterface__updateTreeUI.assert_not_called()
    fi._FileInterface__refreshFileList.assert_not_called()


def test_execute_upload_entries_reserves_names_across_batch():
    with patch("src.app.view.file_interface.InfoBar.success", lambda **_kwargs: None):
        fi = FileInterface.__new__(FileInterface)
        fi.current_dir_id = 7
        fi.transfer_interface = MagicMock()
        fi._FileInterface__refreshFileList = MagicMock()
        fi._FileInterface__buildUploadSummary = MagicMock(return_value="summary")

        FileInterface._FileInterface__executeUploadEntries(
            fi,
            entries=[
                {"path": Path("/tmp/a.txt"), "is_dir": False, "rename": False, "file_size": 1},
                {"path": Path("/tmp/a.txt"), "is_dir": False, "rename": False, "file_size": 1},
            ],
            existing_file_names=set(),
            error="",
            context={"target_dir_id": 7},
            should_refresh_current_dir=False,
        )

        calls = fi.transfer_interface.add_upload_task.call_args_list
        assert calls[0].args == ("a.txt", 1, "/tmp/a.txt", 7)
        assert calls[1].args == ("a(1).txt", 1, "/tmp/a.txt", 7)


def test_folder_prepare_done_reserves_names_within_same_target_dir(monkeypatch):
    monkeypatch.setattr("src.app.view.file_interface.InfoBar.success", lambda **_kwargs: None)
    monkeypatch.setattr("src.app.view.file_interface.InfoBar.warning", lambda **_kwargs: None)
    fi = FileInterface.__new__(FileInterface)
    fi.pan = object()
    fi.current_dir_id = 7
    fi.transfer_interface = MagicMock(current_account_name="alice")
    fi._FileInterface__updateTreeUI = MagicMock()
    fi._FileInterface__refreshFileList = MagicMock()
    fi._FileInterface__buildUploadSummary = MagicMock(return_value="summary")

    FileInterface._FileInterface__onFolderPrepareDone(
        fi,
        folder_uploads=[
            {"file_name": "a.txt", "file_size": 1, "local_path": "/tmp/a1.txt", "target_dir_id": 7},
            {"file_name": "a.txt", "file_size": 2, "local_path": "/tmp/a2.txt", "target_dir_id": 7},
        ],
        folder_items=[],
        created_dir_count=0,
        folder_error="",
        context={"pan": fi.pan, "account_name": "alice", "dir_id": 7, "request_id": None},
        added_count=0,
        error="",
        should_refresh_current_dir=False,
    )

    calls = fi.transfer_interface.add_upload_task.call_args_list
    assert calls[0].args == ("a.txt", 1, "/tmp/a1.txt", 7)
    assert calls[1].args == ("a(1).txt", 2, "/tmp/a2.txt", 7)


def test_update_file_list_ui_refreshes_cached_file_list():
    fi = FileInterface.__new__(FileInterface)
    fi._cached_file_list = []
    fi.fileTable = MagicMock()
    file_items = [{"FileId": 1, "FileName": "a.txt", "Type": 0, "Size": 1}]

    FileInterface._FileInterface__updateFileListUI(fi, file_items)

    assert fi._cached_file_list == file_items


def test_create_upload_button_group_uses_split_push_button(monkeypatch):
    created_actions = []

    class _FakeMenu:
        def addAction(self, action):
            created_actions.append(action)

    class _FakeDropButton:
        def setToolTip(self, text):
            self.tooltip = text

    class _FakeSplitButton:
        def __init__(self, text, parent, icon):
            self.text = text
            self.parent = parent
            self.icon = icon
            self.flyout = None
            self.drop_icon = None
            self.height = None
            self.dropButton = _FakeDropButton()

        def setFlyout(self, flyout):
            self.flyout = flyout

        def setDropIcon(self, icon):
            self.drop_icon = icon

    monkeypatch.setattr("src.app.view.file_interface.RoundMenu", lambda parent=None: _FakeMenu())
    monkeypatch.setattr("src.app.view.file_interface.SplitPushButton", _FakeSplitButton)
    monkeypatch.setattr(
        "src.app.view.file_interface.Action",
        lambda icon, text, triggered=None: {
            "icon": icon,
            "text": text,
            "triggered": triggered,
        },
    )

    fi = MagicMock()
    fi.topBarFrame = object()
    fi._FileInterface__uploadFile = MagicMock()
    fi._FileInterface__uploadFolder = MagicMock()

    FileInterface._FileInterface__createUploadButtonGroup(fi)

    assert fi.uploadButton.text == "上传文件"
    assert fi.uploadButton.drop_icon == fi_module.FIF.DOWN
    assert fi.uploadButton.dropButton.tooltip == "更多上传方式"
    assert [action["text"] for action in created_actions] == ["上传文件", "上传文件夹"]
    assert fi.uploadButtonGroup is fi.uploadButton


# ==========================================================================
# 以下为批次 4 追加：src/app/view/file_interface.py 覆盖补齐测试。
# 沿用既有约定：Qt 组件 patch 到模块路径；真实 FileInterface（pan=None 构造期
# 不触网、不提交线程任务）；QRunnable 用假线程池同步执行；InfoBar/弹窗统一 mock。
# ==========================================================================


class _FakeThreadPool:
    """记录提交的 QRunnable，测试内手动同步执行，避免真实线程时序。"""

    def __init__(self):
        self.tasks = []

    def start(self, task, _priority=0):
        self.tasks.append(task)

    def run_pending(self):
        while self.tasks:
            self.tasks.pop(0).run()


@pytest.fixture
def fake_pool(monkeypatch):
    """把 file_interface 引用的 QThreadPool.globalInstance() 换成可同步执行的假池。"""
    pool = _FakeThreadPool()
    monkeypatch.setattr(
        fi_module,
        "QThreadPool",
        MagicMock(globalInstance=MagicMock(return_value=pool)),
    )
    return pool


@pytest.fixture(autouse=True)
def _mute_infobar(monkeypatch):
    """InfoBar 通知弹窗统一 mock：headless 下无副作用，且各测试可断言调用。"""
    monkeypatch.setattr(fi_module, "InfoBar", MagicMock())


@pytest.fixture
def fi(qapp, fake_pool):
    """真实构造 FileInterface。pan=None 时构造期不触网。

    构造末尾 expandItem 会触发根节点的异步树加载任务；假池已接管，
    这里同步收尾，保证每个测试拿到干净且无后台竞态的初始状态。
    """
    widget = FileInterface()
    fake_pool.run_pending()
    return widget


def _accept_dialog(mock_dialog_cls):
    """让 patched 对话框 exec() 返回 Accepted，并补齐 dialog.DialogCode.Accepted 比较对象。"""
    mock_dialog_cls.return_value.exec.return_value = QDialog.DialogCode.Accepted
    mock_dialog_cls.return_value.DialogCode.Accepted = QDialog.DialogCode.Accepted


def _add_row(fi, row, name, file_id, file_type, size=10, meta=None):
    """向真实 fileTable 添加第 row 行的名称单元格，数据角色与生产代码保持一致。"""
    if fi.fileTable.rowCount() <= row:
        fi.fileTable.setRowCount(row + 1)
    item = QTableWidgetItem(name)
    item.setData(Qt.ItemDataRole.UserRole, file_id)
    item.setData(Qt.ItemDataRole.UserRole + 1, file_type)
    item.setData(
        Qt.ItemDataRole.UserRole + 2,
        meta if meta is not None else {"Size": size},
    )
    fi.fileTable.setItem(row, 0, item)
    return item


def _tree_child(parent, name, dir_id):
    child = QTreeWidgetItem([name])
    child.setData(0, Qt.ItemDataRole.UserRole, dir_id)
    parent.addChild(child)
    return child


def _ctx(pan, account="alice", dir_id=0, request_id=None):
    """构造与 __buildAsyncContext 一致的异步上下文。"""
    return {
        "pan": pan,
        "account_name": account,
        "dir_id": dir_id,
        "request_id": request_id,
    }


def _prepare_pan(pan, items):
    """配置 MagicMock pan：get_dir_by_id 始终返回 (0, items)，user_info 返回合法数据。"""
    pan.get_dir_by_id.return_value = (0, items)
    pan.user_info.return_value = {"SpaceUsed": 0, "SpacePermanent": 100}
    return pan


class TestModuleHelpers:
    def test_generate_keep_both_name_increments_until_free(self):
        assert fi_module._generate_keep_both_name("a.txt", {"a(1).txt"}) == "a(2).txt"

    def test_generate_keep_both_name_without_suffix(self):
        assert fi_module._generate_keep_both_name("README", set()) == "README(1)"

    def test_assign_reserved_name_renames_when_forced(self):
        reserved: set[str] = set()
        assigned = fi_module._assign_reserved_file_name(
            "a.txt", reserved, force_rename=True
        )
        assert assigned == "a(1).txt"
        assert reserved == {"a(1).txt"}

    def test_assign_reserved_name_renames_when_conflict(self):
        reserved = {"b.txt", "a.txt"}
        assigned = fi_module._assign_reserved_file_name(
            "a.txt", reserved, force_rename=False
        )
        assert assigned == "a(1).txt"
        assert reserved == {"b.txt", "a.txt", "a(1).txt"}

    def test_assign_reserved_name_keeps_when_free(self):
        reserved = {"b.txt"}
        assigned = fi_module._assign_reserved_file_name(
            "a.txt", reserved, force_rename=False
        )
        assert assigned == "a.txt"
        assert reserved == {"b.txt", "a.txt"}


class TestExtractLocalPaths:
    def test_extract_returns_empty_for_mime_without_urls(self):
        assert FileInterface._FileInterface__extractLocalPaths(_FakeMimeData([])) == []

    def test_extract_returns_empty_for_none_mime(self):
        assert FileInterface._FileInterface__extractLocalPaths(None) == []


class TestConstructionAndInit:
    def test_constructor_initializes_state(self, fi):
        assert fi.objectName() == "FileInterface"
        assert fi.pan is None
        assert fi.path_stack == [(0, "根目录")]
        assert fi.sort_mode == 0
        assert fi.sort_ascending is True
        assert fi.current_dir_id == 0
        assert fi.fileTable.rowCount() == 0
        assert fi.folderTree.topLevelItemCount() == 1
        assert fi.breadcrumbBar.count() == 1
        assert fi.fileTable.viewport().acceptDrops() is True

    def test_resize_event_updates_list_min_width(self, fi):
        fi.resize(1000, 600)
        fi.resizeEvent(QResizeEvent(QSize(1000, 600), QSize(400, 300)))
        assert fi.listFrame.minimumWidth() == 500

    def test_reload_and_refresh_delegate_to_private_methods(self, fi, monkeypatch):
        calls = []
        monkeypatch.setattr(
            fi, "_FileInterface__loadPanAndData", lambda: calls.append("reload")
        )
        monkeypatch.setattr(
            fi, "_FileInterface__refreshFileList", lambda: calls.append("refresh")
        )
        fi.reload()
        fi.refresh()
        assert calls == ["reload", "refresh"]


def _drag_event(event_cls, paths):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
    event = event_cls(
        QPoint(0, 0),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButtons.LeftButton,
        Qt.KeyboardModifiers.NoModifier,
    )
    # Qt 不持有 mimeData 所有权：挂到事件包装器上防止 Python 侧提前 GC（否则 C++ 悬垂指针段错误）
    event._test_mime = mime
    return event


class TestDragDrop:
    def test_event_filter_handles_drag_enter_on_viewport(self, fi):
        event = _drag_event(QDragEnterEvent, ["/tmp/a.txt"])
        assert fi.eventFilter(fi.fileTable.viewport(), event) is True
        assert fi.fileTable.viewport().styleSheet() != ""

    def test_event_filter_passes_other_watchers_through(self, fi):
        other = QWidget()
        assert fi.eventFilter(other, QEvent(QEvent.Type.HoverMove)) is False

    def test_drag_enter_without_paths_falls_back_to_super(self, fi):
        fi.dragEnterEvent(_drag_event(QDragEnterEvent, []))
        assert fi.fileTable.viewport().styleSheet() == ""

    def test_drag_enter_with_paths_highlights_viewport(self, fi):
        fi.dragEnterEvent(_drag_event(QDragEnterEvent, ["/tmp/a.txt"]))
        assert fi.fileTable.viewport().styleSheet() != ""

    def test_drag_move_with_paths_highlights_viewport(self, fi):
        fi.dragMoveEvent(_drag_event(QDragMoveEvent, ["/tmp/a.txt"]))
        assert fi.fileTable.viewport().styleSheet() != ""

    def test_drop_with_paths_starts_upload_and_dedups(self, fi, monkeypatch):
        prepare = MagicMock()
        monkeypatch.setattr(fi, "_FileInterface__prepareLocalUploads", prepare)
        fi.dropEvent(_drag_event(QDropEvent, ["/tmp/a.txt", "/tmp/a.txt"]))
        prepare.assert_called_once_with([Path("/tmp/a.txt")])

    def test_drop_without_paths_falls_back_to_super(self, fi, monkeypatch):
        prepare = MagicMock()
        monkeypatch.setattr(fi, "_FileInterface__prepareLocalUploads", prepare)
        fi.dropEvent(_drag_event(QDropEvent, []))
        prepare.assert_not_called()

    def test_handle_drop_event_returns_false_for_other_event_types(self, fi):
        assert fi._FileInterface__handleDropEvent(QEvent(QEvent.Type.HoverMove)) is False


class TestShortcutsAndNav:
    def test_delete_shortcut_ignored_when_search_focused(self, fi, monkeypatch):
        monkeypatch.setattr(fi.searchBar, "hasFocus", lambda: True)
        delete = MagicMock()
        monkeypatch.setattr(fi, "_FileInterface__deleteFile", delete)
        fi._FileInterface__onDeleteShortcut()
        delete.assert_not_called()

    def test_delete_shortcut_triggers_delete(self, fi, monkeypatch):
        monkeypatch.setattr(fi.searchBar, "hasFocus", lambda: False)
        delete = MagicMock()
        monkeypatch.setattr(fi, "_FileInterface__deleteFile", delete)
        fi._FileInterface__onDeleteShortcut()
        delete.assert_called_once_with()

    def test_rename_shortcut_ignored_when_search_focused(self, fi, monkeypatch):
        monkeypatch.setattr(fi.searchBar, "hasFocus", lambda: True)
        rename = MagicMock()
        monkeypatch.setattr(fi, "_FileInterface__renameFile", rename)
        fi._FileInterface__onRenameShortcut()
        rename.assert_not_called()

    def test_rename_shortcut_triggers_rename(self, fi, monkeypatch):
        monkeypatch.setattr(fi.searchBar, "hasFocus", lambda: False)
        rename = MagicMock()
        monkeypatch.setattr(fi, "_FileInterface__renameFile", rename)
        fi._FileInterface__onRenameShortcut()
        rename.assert_called_once_with()

    def test_backspace_shortcut_ignored_when_search_focused(self, fi, monkeypatch):
        monkeypatch.setattr(fi.searchBar, "hasFocus", lambda: True)
        go_up = MagicMock()
        monkeypatch.setattr(fi, "_FileInterface__goUpToParent", go_up)
        fi._FileInterface__onBackspaceShortcut()
        go_up.assert_not_called()

    def test_backspace_shortcut_triggers_go_up(self, fi, monkeypatch):
        monkeypatch.setattr(fi.searchBar, "hasFocus", lambda: False)
        go_up = MagicMock()
        monkeypatch.setattr(fi, "_FileInterface__goUpToParent", go_up)
        fi._FileInterface__onBackspaceShortcut()
        go_up.assert_called_once_with()

    def test_go_up_noop_at_root(self, fi, monkeypatch):
        load = MagicMock()
        monkeypatch.setattr(fi, "_FileInterface__loadCurrentList", load)
        fi._FileInterface__goUpToParent()
        load.assert_not_called()

    def test_go_up_pops_stack_and_selects_tree_item(self, fi, monkeypatch):
        load = MagicMock()
        monkeypatch.setattr(fi, "_FileInterface__loadCurrentList", load)
        root = fi.folderTree.topLevelItem(0)
        child = _tree_child(root, "docs", 5)
        fi.folderTree.setCurrentItem(child)
        fi.path_stack = [(0, "根目录"), (5, "docs")]
        fi.current_dir_id = 5
        fi._FileInterface__goUpToParent()
        assert fi.current_dir_id == 0
        assert fi.path_stack == [(0, "根目录")]
        load.assert_called_once()
        assert fi.folderTree.currentItem() is root


class TestBreadcrumb:
    def test_set_error_breadcrumb(self, fi):
        fi._FileInterface__setErrorBreadcrumb("boom")
        assert fi.breadcrumbBar.count() == 1

    def test_load_pan_and_data_shows_error_breadcrumb_on_failure(self, fi, monkeypatch):
        def boom():
            raise RuntimeError("net down")

        monkeypatch.setattr(fi, "_FileInterface__initTree", boom)
        fi.reload()
        assert fi.breadcrumbBar.count() == 1

    def test_breadcrumb_change_ignored_while_updating(self, fi, monkeypatch):
        fi.is_updating_breadcrumb = True
        load = MagicMock()
        monkeypatch.setattr(fi, "_FileInterface__loadCurrentList", load)
        fi._FileInterface__onBreadcrumbItemChanged("0")
        load.assert_not_called()

    def test_breadcrumb_change_ignores_invalid_route_key(self, fi, monkeypatch):
        load = MagicMock()
        monkeypatch.setattr(fi, "_FileInterface__loadCurrentList", load)
        fi._FileInterface__onBreadcrumbItemChanged("abc")
        load.assert_not_called()

    def test_breadcrumb_change_ignores_unknown_route(self, fi, monkeypatch):
        load = MagicMock()
        monkeypatch.setattr(fi, "_FileInterface__loadCurrentList", load)
        fi._FileInterface__onBreadcrumbItemChanged("99")
        load.assert_not_called()

    def test_breadcrumb_change_truncates_stack_and_selects_tree(self, fi, monkeypatch):
        load = MagicMock()
        monkeypatch.setattr(fi, "_FileInterface__loadCurrentList", load)
        root = fi.folderTree.topLevelItem(0)
        child = _tree_child(root, "docs", 5)
        fi.path_stack = [(0, "根目录"), (5, "docs"), (7, "sub")]
        fi._FileInterface__onBreadcrumbItemChanged("5")
        assert fi.path_stack == [(0, "根目录"), (5, "docs")]
        assert fi.current_dir_id == 5
        assert fi.folderTree.currentItem() is child
        load.assert_called_once()
        assert fi.breadcrumbBar.count() == 2


class TestTreeOps:
    @staticmethod
    def _unloaded_parent(fi):
        """构造一个未加载的树节点（根节点在 fixture 收尾后已标记为已加载）。"""
        root = fi.folderTree.topLevelItem(0)
        parent = QTreeWidgetItem(["folder"])
        parent.setData(0, Qt.ItemDataRole.UserRole, 77)
        parent.setData(0, Qt.ItemDataRole.UserRole + 1, False)
        root.addChild(parent)
        return parent

    def test_ensure_tree_children_skips_while_loading(self, fi):
        fi.is_loading_tree = True
        parent = self._unloaded_parent(fi)
        fi._FileInterface__ensureTreeChildrenLoaded(parent)
        assert fi.is_loading_tree is True
        assert parent.childCount() == 0

    def test_ensure_tree_children_skips_placeholder_node(self, fi):
        placeholder = QTreeWidgetItem([""])
        placeholder.setData(0, Qt.ItemDataRole.UserRole, None)
        fi._FileInterface__ensureTreeChildrenLoaded(placeholder)

    def test_ensure_tree_children_loaded_success(self, fi, fake_pool):
        fi.pan = MagicMock()
        fi.pan.get_dir_by_id.return_value = (
            0,
            [
                {"FileId": 11, "FileName": "docs", "Type": 1},
                {"FileId": 12, "FileName": "a.txt", "Type": 0},
            ],
        )
        parent = self._unloaded_parent(fi)
        fi._FileInterface__ensureTreeChildrenLoaded(parent)
        assert fi.is_loading_tree is True
        assert parent.childCount() == 1
        assert parent.child(0).text(0) == "加载中..."
        fake_pool.run_pending()
        assert fi.is_loading_tree is False
        # 只有文件夹成为子节点
        assert parent.childCount() == 1
        child = parent.child(0)
        assert child.text(0) == "docs"
        assert child.data(0, Qt.ItemDataRole.UserRole) == 11
        assert child.childCount() == 1  # 新节点带占位符
        assert parent.data(0, Qt.ItemDataRole.UserRole + 1) is True
        # 已加载的节点再次触发时直接跳过
        fi._FileInterface__ensureTreeChildrenLoaded(parent)
        assert fake_pool.tasks == []

    def test_ensure_tree_children_load_failure_keeps_placeholder(self, fi, fake_pool):
        fi.pan = MagicMock()
        fi.pan.get_dir_by_id.side_effect = RuntimeError("offline")
        parent = self._unloaded_parent(fi)
        fi._FileInterface__ensureTreeChildrenLoaded(parent)
        fake_pool.run_pending()
        assert fi.is_loading_tree is False
        assert parent.data(0, Qt.ItemDataRole.UserRole + 1) is False
        # 回调先 takeChildren 再判错误，失败后占位符被清空且不重试（仅记录 warning）
        assert parent.childCount() == 0

    def test_on_tree_item_expanded_delegates(self, fi, monkeypatch):
        ensure = MagicMock()
        monkeypatch.setattr(fi, "_FileInterface__ensureTreeChildrenLoaded", ensure)
        root = fi.folderTree.topLevelItem(0)
        fi._FileInterface__onTreeItemExpanded(root)
        ensure.assert_called_once_with(root)

    def test_on_tree_item_clicked_placeholder_returns_early(self, fi):
        placeholder = QTreeWidgetItem([""])
        placeholder.setData(0, Qt.ItemDataRole.UserRole, None)
        fi._FileInterface__onTreeItemClicked(placeholder)
        assert fi.current_dir_id == 0

    def test_on_tree_item_clicked_loads_dir_and_updates_breadcrumb(self, fi, monkeypatch):
        load = MagicMock()
        monkeypatch.setattr(fi, "_FileInterface__loadCurrentList", load)
        root = fi.folderTree.topLevelItem(0)
        child = _tree_child(root, "docs", 5)
        fi._FileInterface__onTreeItemClicked(child)
        assert fi.current_dir_id == 5
        assert fi.path_stack == [(0, "根目录"), (5, "docs")]
        load.assert_called_once()
        assert fi.breadcrumbBar.count() == 2

    def test_build_path_stack_from_tree_walks_parents(self, fi):
        root = fi.folderTree.topLevelItem(0)
        child = _tree_child(root, "docs", 5)
        sub = _tree_child(child, "sub", 7)
        assert fi._FileInterface__buildPathStackFromTree(sub) == [
            (0, "根目录"),
            (5, "docs"),
            (7, "sub"),
        ]

    def test_find_tree_item_by_id(self, fi):
        root = fi.folderTree.topLevelItem(0)
        child = _tree_child(root, "docs", 5)
        assert fi._FileInterface__findTreeItemById(5) is child
        assert fi._FileInterface__findTreeItemById(999) is None

    def test_double_click_without_name_item_returns_early(self, fi):
        fi.fileTable.setRowCount(1)
        clicked = MagicMock()
        clicked.row.return_value = 0
        fi._FileInterface__onTableItemDoubleClicked(clicked)
        assert fi.current_dir_id == 0

    def test_double_click_on_file_is_ignored(self, fi):
        _add_row(fi, 0, "a.txt", 12, 0)
        fi._FileInterface__onTableItemDoubleClicked(fi.fileTable.item(0, 0))
        assert fi.current_dir_id == 0

    def test_double_click_on_folder_enters_and_selects_tree(self, fi, monkeypatch):
        load = MagicMock()
        monkeypatch.setattr(fi, "_FileInterface__loadCurrentList", load)
        _add_row(fi, 0, "docs", 12, 1)
        root = fi.folderTree.topLevelItem(0)
        child = _tree_child(root, "docs", 12)
        fi._FileInterface__onTableItemDoubleClicked(fi.fileTable.item(0, 0))
        assert fi.current_dir_id == 12
        assert fi.path_stack[-1] == (12, "docs")
        load.assert_called_once()
        assert fi.folderTree.currentItem() is child


class TestFileListLoad:
    def test_load_current_list_noop_without_pan(self, fi):
        fi._FileInterface__loadCurrentList()
        assert fi.fileTable.rowCount() == 0

    def test_fetch_dir_list_without_pan_returns_empty(self, fi):
        assert fi._FileInterface__fetchDirList(0) == []

    def test_fetch_dir_list_raises_on_error_code(self, fi):
        fi.pan = MagicMock()
        fi.pan.get_dir_by_id.return_value = (1, [])
        with pytest.raises(RuntimeError, match="返回码"):
            fi._FileInterface__fetchDirList(0)

    def test_fetch_dir_list_propagates_exception(self, fi):
        fi.pan = MagicMock()
        fi.pan.get_dir_by_id.side_effect = RuntimeError("offline")
        with pytest.raises(RuntimeError, match="offline"):
            fi._FileInterface__fetchDirList(0)

    def test_load_list_task_success_emits_items(self):
        task = FileInterface.LoadListTask(lambda dir_id, search="": [{"FileId": 1}], 3)
        results = []
        task.signals.finished.connect(lambda items, err: results.append((items, err)))
        task.run()
        assert results == [([{"FileId": 1}], "")]

    def test_load_list_task_error_emits_message(self):
        def boom(dir_id, search=""):
            raise RuntimeError("offline")

        task = FileInterface.LoadListTask(boom, 3)
        results = []
        task.signals.finished.connect(lambda items, err: results.append((items, err)))
        task.run()
        assert results == [([], "offline")]

    def test_load_current_list_populates_table_sorted(self, fi, fake_pool):
        fi.pan = MagicMock()
        fi.pan.get_dir_by_id.return_value = (
            0,
            [
                {"FileId": 2, "FileName": "b.txt", "Type": 0, "Size": 2048},
                {"FileId": 1, "FileName": "docs", "Type": 1, "Size": 0},
            ],
        )
        fi.current_dir_id = 5
        fi._FileInterface__loadCurrentList()
        assert len(fake_pool.tasks) == 1
        fake_pool.run_pending()
        assert fi.fileTable.rowCount() == 2
        assert fi.fileTable.item(0, 0).text() == "docs"
        assert fi.fileTable.item(0, 0).data(Qt.ItemDataRole.UserRole) == 1
        assert fi.fileTable.item(1, 0).text() == "b.txt"
        assert fi.fileTable.item(1, 2).text() == format_file_size(2048)
        assert fi._pending_signals == []

    def test_on_load_list_finished_drops_stale_request_id(self, fi):
        fi._load_request_id = 5
        fi._FileInterface__onLoadListFinished([{"FileId": 1}], "", request_id=4)
        assert fi.fileTable.rowCount() == 0
        fi_module.InfoBar.error.assert_not_called()

    def test_on_load_list_finished_shows_error(self, fi):
        fi._FileInterface__onLoadListFinished([], "offline", request_id=None)
        assert fi_module.InfoBar.error.call_args.kwargs["title"] == "加载失败"
        assert fi.statusLabel.text() == "共 0 个"

    def test_update_file_list_ui_renders_rows_and_meta(self, fi):
        items = [
            {"FileId": 1, "FileName": "docs", "Type": 1, "Size": 0},
            {"FileId": 2, "FileName": "a.txt", "Type": 0, "Size": 2048},
        ]
        fi._FileInterface__updateFileListUI(items)
        assert fi.fileTable.rowCount() == 2
        name_item = fi.fileTable.item(0, 0)
        assert name_item.data(Qt.ItemDataRole.UserRole + 1) == 1
        assert name_item.data(Qt.ItemDataRole.UserRole + 2) == items[0]
        assert fi.fileTable.item(0, 1).text() == "文件夹"
        assert fi.fileTable.item(1, 1).text() == "文件"
        assert fi.fileTable.item(1, 2).text() == format_file_size(2048)


class TestSort:
    @staticmethod
    def _sample():
        return [
            {"FileId": 1, "FileName": "b.txt", "Type": 0, "Size": 30},
            {"FileId": 2, "FileName": "a.txt", "Type": 0, "Size": 10},
            {"FileId": 3, "FileName": "docs", "Type": 1, "Size": 0},
        ]

    @pytest.mark.parametrize(
        "mode,ascending,expected",
        [
            (0, True, ["docs", "a.txt", "b.txt"]),
            (0, False, ["docs", "b.txt", "a.txt"]),
            (2, True, ["docs", "a.txt", "b.txt"]),
            (2, False, ["docs", "b.txt", "a.txt"]),
        ],
    )
    def test_sort_file_list_folders_always_first(self, fi, mode, ascending, expected):
        fi.sort_mode = mode
        fi.sort_ascending = ascending
        result = fi._FileInterface__sortFileList(self._sample())
        assert [item["FileName"] for item in result] == expected

    def test_header_sort_indicator_switches_to_size_column(self, fi):
        fi._cached_file_list = self._sample()
        header = fi.fileTable.horizontalHeader()
        header.sortIndicatorChanged.emit(2, Qt.SortOrder.DescendingOrder)
        assert fi.sort_mode == 2
        assert fi.sort_ascending is False
        assert [fi.fileTable.item(r, 0).text() for r in range(3)] == [
            "docs",
            "b.txt",
            "a.txt",
        ]

    def test_header_sort_indicator_toggles_and_switches_back(self, fi):
        fi._cached_file_list = self._sample()
        fi._FileInterface__onHeaderSortIndicatorChanged(0, Qt.SortOrder.DescendingOrder)
        assert fi.sort_ascending is False
        assert fi.fileTable.item(1, 0).text() == "b.txt"
        fi._FileInterface__onHeaderSortIndicatorChanged(0, Qt.SortOrder.DescendingOrder)
        assert fi.sort_ascending is True
        assert fi.fileTable.item(1, 0).text() == "a.txt"
        # 从大小列切回名称列时默认升序
        fi._FileInterface__onHeaderSortIndicatorChanged(2, Qt.SortOrder.DescendingOrder)
        fi._FileInterface__onHeaderSortIndicatorChanged(0, Qt.SortOrder.DescendingOrder)
        assert fi.sort_mode == 0
        assert fi.sort_ascending is True

    def test_header_sort_ignores_middle_column(self, fi):
        fi._cached_file_list = self._sample()
        fi._FileInterface__onHeaderSortIndicatorChanged(1, Qt.SortOrder.AscendingOrder)
        assert fi.sort_mode == 0
        assert fi.fileTable.rowCount() == 0  # 未触发重绘


class TestUpdateTree:
    def test_update_tree_replaces_missing_children(self, fi):
        root = fi.folderTree.topLevelItem(0)
        _tree_child(root, "old", 1)
        placeholder = QTreeWidgetItem([""])
        placeholder.setData(0, Qt.ItemDataRole.UserRole, None)
        root.addChild(placeholder)
        fi.current_dir_id = 0
        fi._FileInterface__updateTreeUI([{"FileId": 2, "FileName": "new"}])
        # 占位符与不在新列表中的节点都被移除
        assert root.childCount() == 1
        child = root.child(0)
        assert child.text(0) == "new"
        assert child.data(0, Qt.ItemDataRole.UserRole) == 2
        assert child.childCount() == 1  # 新节点带占位符

    def test_update_tree_keeps_children_when_remove_missing_false(self, fi):
        root = fi.folderTree.topLevelItem(0)
        _tree_child(root, "old", 1)
        fi.current_dir_id = 0
        fi._FileInterface__updateTreeUI(
            [{"FileId": 2, "FileName": "new"}], remove_missing=False
        )
        texts = [root.child(i).text(0) for i in range(root.childCount())]
        assert "old" in texts
        assert "new" in texts

    def test_update_tree_keeps_existing_child(self, fi):
        root = fi.folderTree.topLevelItem(0)
        kept = _tree_child(root, "docs", 1)
        fi.current_dir_id = 0
        fi._FileInterface__updateTreeUI([{"FileId": 1, "FileName": "docs"}])
        assert root.child(0) is kept

    def test_update_tree_returns_without_current_item(self, fi):
        fi.current_dir_id = 404
        before = fi.folderTree.topLevelItem(0).childCount()
        fi._FileInterface__updateTreeUI([{"FileId": 2, "FileName": "new"}])
        # 找不到当前目录节点时直接返回，树保持原样
        assert fi.folderTree.topLevelItem(0).childCount() == before


class TestStatusLabel:
    def test_status_label_without_selection(self, fi):
        _add_row(fi, 0, "a.txt", 1, 0, size=100)
        fi._FileInterface__updateStatusLabel()
        assert fi.statusLabel.text() == "共 1 个"

    def test_status_label_with_selection_size(self, fi):
        _add_row(fi, 0, "a.txt", 1, 0, size=1024)
        _add_row(fi, 1, "b.txt", 2, 0, size=10)
        fi.fileTable.selectRow(0)
        fi._FileInterface__updateStatusLabel()
        assert "已选中 1 个" in fi.statusLabel.text()
        assert "总大小" in fi.statusLabel.text()

    def test_status_label_selection_without_size(self, fi):
        _add_row(fi, 0, "a.txt", 1, 0, size=0)
        fi.fileTable.selectRow(0)
        fi._FileInterface__updateStatusLabel()
        assert fi.statusLabel.text() == "已选中 1 个，共 1 个"


class TestNewFolder:
    @patch("src.app.view.file_interface.NewFolderDialog")
    def test_create_new_folder_dialog_rejected(self, mock_dlg, fi, fake_pool):
        mock_dlg.return_value.exec.return_value = QDialog.DialogCode.Rejected
        fi.pan = MagicMock()
        fi._FileInterface__createNewFolder()
        assert fake_pool.tasks == []

    @patch("src.app.view.file_interface.NewFolderDialog")
    def test_create_new_folder_empty_name_warns(self, mock_dlg, fi, fake_pool):
        _accept_dialog(mock_dlg)
        mock_dlg.return_value.get_new_name.return_value = "   "
        fi._FileInterface__createNewFolder()
        assert fi_module.InfoBar.warning.call_args.kwargs["content"] == "请输入文件夹名称"
        assert fake_pool.tasks == []

    @patch("src.app.view.file_interface.NewFolderDialog")
    def test_create_new_folder_illegal_name_warns(self, mock_dlg, fi, fake_pool):
        _accept_dialog(mock_dlg)
        mock_dlg.return_value.get_new_name.return_value = "a/b"
        fi._FileInterface__createNewFolder()
        assert "非法字符" in fi_module.InfoBar.warning.call_args.kwargs["content"]
        assert fake_pool.tasks == []

    @patch("src.app.view.file_interface.NewFolderDialog")
    def test_create_new_folder_success_updates_ui(self, mock_dlg, fi, fake_pool):
        _accept_dialog(mock_dlg)
        mock_dlg.return_value.get_new_name.return_value = "docs"
        fi.pan = MagicMock()
        fi.pan._create_directory.return_value = True
        items = [
            {"FileId": 6, "FileName": "a.txt", "Type": 0},
            {"FileId": 5, "FileName": "docs", "Type": 1},
        ]
        _prepare_pan(fi.pan, items)
        fi.current_dir_id = 0
        fi._FileInterface__createNewFolder()
        fake_pool.run_pending()
        assert fi.pan._create_directory.call_args.args == (0, "docs")
        assert fi.fileTable.rowCount() == 2
        root = fi.folderTree.topLevelItem(0)
        assert root.childCount() == 1
        assert root.child(0).text(0) == "docs"
        assert fi_module.InfoBar.success.call_args.kwargs["title"] == "创建成功"

    @patch("src.app.view.file_interface.NewFolderDialog")
    def test_create_new_folder_directory_listing_fails_still_succeeds(
        self, mock_dlg, fi, fake_pool
    ):
        _accept_dialog(mock_dlg)
        mock_dlg.return_value.get_new_name.return_value = "docs"
        fi.pan = MagicMock()
        fi.pan._create_directory.return_value = True
        fi.pan.get_dir_by_id.return_value = (1, [])  # code != 0 → folder_items 为空
        fi.current_dir_id = 0
        fi._FileInterface__createNewFolder()
        fake_pool.run_pending()
        assert fi.fileTable.rowCount() == 0  # items 为空列表
        fi_module.InfoBar.success.assert_called_once()

    @patch("src.app.view.file_interface.NewFolderDialog")
    def test_create_new_folder_create_fails(self, mock_dlg, fi, fake_pool):
        _accept_dialog(mock_dlg)
        mock_dlg.return_value.get_new_name.return_value = "docs"
        fi.pan = MagicMock()
        fi.pan._create_directory.return_value = False
        fi.current_dir_id = 0
        fi._FileInterface__createNewFolder()
        fake_pool.run_pending()
        assert fi_module.InfoBar.error.call_args.kwargs["content"] == "创建文件夹失败"

    @patch("src.app.view.file_interface.NewFolderDialog")
    def test_create_new_folder_create_exception(self, mock_dlg, fi, fake_pool):
        _accept_dialog(mock_dlg)
        mock_dlg.return_value.get_new_name.return_value = "docs"
        fi.pan = MagicMock()
        fi.pan._create_directory.side_effect = RuntimeError("boom")
        fi.current_dir_id = 0
        fi._FileInterface__createNewFolder()
        fake_pool.run_pending()
        assert "boom" in fi_module.InfoBar.error.call_args.kwargs["content"]

    def test_on_create_folder_finished_without_file_items(self, fi, monkeypatch):
        update_list = MagicMock()
        monkeypatch.setattr(fi, "_FileInterface__updateFileListUI", update_list)
        fi.pan = object()
        fi.current_dir_id = 0
        fi._FileInterface__onCreateFolderFinished(
            True,
            "docs",
            "",
            None,
            [],
            _ctx(fi.pan, account="", dir_id=0),
        )
        update_list.assert_not_called()

    def test_on_create_folder_finished_failure_without_error(self, fi):
        fi.pan = object()
        fi.current_dir_id = 0
        fi._FileInterface__onCreateFolderFinished(
            False, "docs", "", [], [], _ctx(fi.pan, account="", dir_id=0)
        )
        assert fi_module.InfoBar.error.call_args.kwargs["content"] == "创建文件夹失败"


class TestUploadPreparation:
    def test_prepare_local_uploads_without_pan(self, fi):
        fi._FileInterface__prepareLocalUploads([Path("/tmp/a.txt")])
        assert fi_module.InfoBar.error.call_args.kwargs["content"] == "当前未登录"

    def test_prepare_local_uploads_without_transfer_interface(self, fi):
        fi.pan = MagicMock()
        fi._FileInterface__prepareLocalUploads([Path("/tmp/a.txt")])
        assert (
            fi_module.InfoBar.error.call_args.kwargs["content"] == "传输页面未初始化"
        )

    def test_prepare_local_uploads_empty_paths_noop(self, fi):
        fi.pan = MagicMock()
        fi.transfer_interface = MagicMock()
        fi._FileInterface__prepareLocalUploads([])
        fi_module.InfoBar.error.assert_not_called()

    def test_prepare_upload_task_outer_exception(self):
        pan = MagicMock()
        pan._get_dir_items_by_id.side_effect = RuntimeError("db down")
        task = FileInterface.PrepareUploadTask(
            pan=pan, target_dir_id=1, local_paths=["/tmp/a.txt"]
        )
        results = []
        task.signals.finished.connect(
            lambda entries, ef, efld, error: results.append((entries, error))
        )
        task.run()
        assert results == [([], "db down")]

    @patch("src.app.view.file_interface.QFileDialog.getOpenFileNames")
    def test_upload_file_cancelled(self, mock_dlg, fi, fake_pool):
        mock_dlg.return_value = ([], "")
        fi.pan = MagicMock()
        fi.transfer_interface = MagicMock()
        fi._FileInterface__uploadFile()
        assert fake_pool.tasks == []

    @patch("src.app.view.file_interface.QFileDialog.getOpenFileNames")
    def test_upload_file_full_flow(self, mock_dlg, fi, fake_pool, tmp_path):
        target = tmp_path / "a.txt"
        target.write_text("data", encoding="utf-8")
        mock_dlg.return_value = ([str(target)], "")
        fi.pan = MagicMock()
        fi.pan._get_dir_items_by_id.return_value = []
        _prepare_pan(fi.pan, [])
        fi.transfer_interface = MagicMock(current_account_name="alice")
        fi.current_dir_id = 3
        fi._FileInterface__uploadFile()
        fake_pool.run_pending()
        fi.transfer_interface.add_upload_task.assert_called_once_with(
            "a.txt", 4, str(target), 3
        )
        assert fi.storageProgressBar.value() == 0  # StorageTask 同步收尾


class TestUploadConflicts:
    @patch("src.app.view.file_interface.UploadConflictDialog")
    def test_file_conflict_keep_both_renames(self, mock_dlg, fi, fake_pool):
        dlg = mock_dlg.return_value
        dlg.exec.return_value = QDialog.DialogCode.Accepted
        dlg.action = ConflictAction.KEEP_BOTH
        dlg.apply_all = False
        fi.pan = MagicMock()
        _prepare_pan(fi.pan, [])
        fi.transfer_interface = MagicMock(current_account_name="alice")
        fi.current_dir_id = 3
        fi._FileInterface__onPrepareUploadFinished(
            entries=[
                {
                    "path": Path("/tmp/a.txt"),
                    "is_dir": False,
                    "conflict": True,
                    "file_size": 2,
                }
            ],
            existing_file_names={"a.txt"},
            existing_folder_names=set(),
            error="",
            context=None,
        )
        fi.transfer_interface.add_upload_task.assert_called_once_with(
            "a(1).txt", 2, "/tmp/a.txt", 3
        )
        fi_module.InfoBar.success.assert_called_once()

    @patch("src.app.view.file_interface.UploadConflictDialog")
    def test_file_conflict_skipped_by_action(self, mock_dlg, fi, fake_pool):
        dlg = mock_dlg.return_value
        dlg.exec.return_value = QDialog.DialogCode.Accepted
        dlg.action = ConflictAction.SKIP
        dlg.apply_all = False
        fi.pan = MagicMock()
        _prepare_pan(fi.pan, [])
        fi.transfer_interface = MagicMock(current_account_name="alice")
        fi.current_dir_id = 3
        fi._FileInterface__onPrepareUploadFinished(
            entries=[
                {
                    "path": Path("/tmp/a.txt"),
                    "is_dir": False,
                    "conflict": True,
                    "file_size": 2,
                }
            ],
            existing_file_names={"a.txt"},
            existing_folder_names=set(),
            error="",
            context=None,
        )
        fi.transfer_interface.add_upload_task.assert_not_called()
        fi_module.InfoBar.success.assert_not_called()

    @patch("src.app.view.file_interface.UploadConflictDialog")
    def test_file_conflict_dialog_rejected_skips_entry(self, mock_dlg, fi, fake_pool):
        mock_dlg.return_value.exec.return_value = QDialog.DialogCode.Rejected
        fi.pan = MagicMock()
        _prepare_pan(fi.pan, [])
        fi.transfer_interface = MagicMock(current_account_name="alice")
        fi.current_dir_id = 3
        fi._FileInterface__onPrepareUploadFinished(
            entries=[
                {
                    "path": Path("/tmp/a.txt"),
                    "is_dir": False,
                    "conflict": True,
                    "file_size": 2,
                }
            ],
            existing_file_names={"a.txt"},
            existing_folder_names=set(),
            error="",
            context=None,
        )
        fi.transfer_interface.add_upload_task.assert_not_called()

    @patch("src.app.view.file_interface.UploadConflictDialog")
    def test_file_conflict_apply_all_caches_decision(self, mock_dlg, fi, fake_pool):
        dlg = mock_dlg.return_value
        dlg.exec.return_value = QDialog.DialogCode.Accepted
        dlg.action = ConflictAction.KEEP_BOTH
        dlg.apply_all = True
        fi.pan = MagicMock()
        _prepare_pan(fi.pan, [])
        fi.transfer_interface = MagicMock(current_account_name="alice")
        fi.current_dir_id = 3
        fi._FileInterface__onPrepareUploadFinished(
            entries=[
                {
                    "path": Path("/tmp/a.txt"),
                    "is_dir": False,
                    "conflict": True,
                    "file_size": 1,
                },
                {
                    "path": Path("/tmp/b.txt"),
                    "is_dir": False,
                    "conflict": True,
                    "file_size": 2,
                },
            ],
            existing_file_names={"a.txt", "b.txt"},
            existing_folder_names=set(),
            error="",
            context=None,
        )
        assert dlg.exec.call_count == 1  # 应用到所有后不再重复弹窗
        calls = fi.transfer_interface.add_upload_task.call_args_list
        assert calls[0].args == ("a(1).txt", 1, "/tmp/a.txt", 3)
        assert calls[1].args == ("b(1).txt", 2, "/tmp/b.txt", 3)

    @patch("src.app.view.file_interface.UploadConflictDialog")
    def test_prepare_finished_error_without_entries(self, mock_dlg, fi):
        fi.pan = MagicMock()
        fi.transfer_interface = MagicMock(current_account_name="alice")
        fi._FileInterface__onPrepareUploadFinished(
            entries=[],
            existing_file_names=set(),
            existing_folder_names=set(),
            error="boom",
            context=None,
        )
        assert fi_module.InfoBar.error.call_args.kwargs["title"] == "上传准备失败"

    @patch("src.app.view.file_interface.UploadConflictDialog")
    def test_prepare_finished_error_with_entries_warns_later(
        self, mock_dlg, fi, fake_pool, monkeypatch
    ):
        fi.pan = MagicMock()
        _prepare_pan(fi.pan, [])
        fi.transfer_interface = MagicMock(current_account_name="alice")
        fi.current_dir_id = 3
        refresh = MagicMock()
        monkeypatch.setattr(fi, "_FileInterface__refreshFileList", refresh)
        fi._FileInterface__onPrepareUploadFinished(
            entries=[
                {
                    "path": Path("/tmp/a.txt"),
                    "is_dir": False,
                    "conflict": False,
                    "file_size": 1,
                }
            ],
            existing_file_names=set(),
            existing_folder_names=set(),
            error="bad.txt: gone",
            context=None,
        )
        fi.transfer_interface.add_upload_task.assert_called_once()
        assert (
            fi_module.InfoBar.warning.call_args.kwargs["content"] == "bad.txt: gone"
        )

    @patch("src.app.view.file_interface.UploadConflictDialog")
    def test_prepare_finished_stale_dir_skips_refresh(self, mock_dlg, fi, monkeypatch):
        fi.pan = object()
        fi.transfer_interface = MagicMock(current_account_name="alice")
        fi.current_dir_id = 7
        refresh = MagicMock()
        monkeypatch.setattr(fi, "_FileInterface__refreshFileList", refresh)
        fi._FileInterface__onPrepareUploadFinished(
            entries=[
                {
                    "path": Path("/tmp/a.txt"),
                    "is_dir": False,
                    "conflict": False,
                    "file_size": 1,
                }
            ],
            existing_file_names=set(),
            existing_folder_names=set(),
            error="",
            context={"pan": fi.pan, "account_name": "alice", "target_dir_id": 9},
        )
        fi.transfer_interface.add_upload_task.assert_called_once_with(
            "a.txt", 1, "/tmp/a.txt", 9
        )
        refresh.assert_not_called()

    @patch("src.app.view.file_interface.UploadConflictDialog")
    def test_folder_conflict_merge_runs_folder_task(self, mock_dlg, fi, fake_pool):
        dlg = mock_dlg.return_value
        dlg.exec.return_value = QDialog.DialogCode.Accepted
        dlg.action = ConflictAction.MERGE
        dlg.apply_all = False
        fi.pan = MagicMock()
        fi.pan.prepare_folder_upload.return_value = {
            "file_targets": [
                {
                    "file_name": "a.txt",
                    "file_size": 1,
                    "local_path": "/tmp/docs/a.txt",
                    "target_dir_id": 9,
                }
            ],
            "created_dir_count": 1,
            "root_dir_id": 9,
            "root_dir_name": "docs",
        }
        _prepare_pan(fi.pan, [])
        fi.transfer_interface = MagicMock(current_account_name="alice")
        fi.current_dir_id = 3
        fi._FileInterface__onPrepareUploadFinished(
            entries=[
                {"path": Path("/tmp/docs"), "is_dir": True, "conflict": True}
            ],
            existing_file_names=set(),
            existing_folder_names={"docs"},
            error="",
            context=None,
        )
        fake_pool.run_pending()
        assert fi.pan.prepare_folder_upload.call_args.kwargs["merge"] is True
        fi.transfer_interface.add_upload_task.assert_called_once_with(
            "a.txt", 1, "/tmp/docs/a.txt", 9
        )

    @patch("src.app.view.file_interface.UploadConflictDialog")
    def test_folder_conflict_apply_all_caches_folder_decision(
        self, mock_dlg, fi, fake_pool
    ):
        dlg = mock_dlg.return_value
        dlg.exec.return_value = QDialog.DialogCode.Accepted
        dlg.action = ConflictAction.MERGE
        dlg.apply_all = True
        fi.pan = MagicMock()
        fi.pan.prepare_folder_upload.return_value = {
            "file_targets": [],
            "created_dir_count": 1,
            "root_dir_id": 9,
            "root_dir_name": "docs",
        }
        _prepare_pan(fi.pan, [])
        fi.transfer_interface = MagicMock(current_account_name="alice")
        fi.current_dir_id = 3
        fi._FileInterface__onPrepareUploadFinished(
            entries=[
                {"path": Path("/tmp/docs"), "is_dir": True, "conflict": True},
                {"path": Path("/tmp/pics"), "is_dir": True, "conflict": True},
            ],
            existing_file_names=set(),
            existing_folder_names={"docs", "pics"},
            error="",
            context=None,
        )
        fake_pool.run_pending()
        assert dlg.exec.call_count == 1  # 文件夹冲突决策被缓存复用
        assert fi.pan.prepare_folder_upload.call_count == 2

    @patch("src.app.view.file_interface.UploadConflictDialog")
    def test_folder_conflict_rename_passes_merge_false(self, mock_dlg, fi, fake_pool):
        dlg = mock_dlg.return_value
        dlg.exec.return_value = QDialog.DialogCode.Accepted
        dlg.action = ConflictAction.RENAME
        dlg.apply_all = False
        fi.pan = MagicMock()
        fi.pan.prepare_folder_upload.return_value = {
            "file_targets": [],
            "created_dir_count": 1,
            "root_dir_id": 9,
            "root_dir_name": "docs",
        }
        _prepare_pan(fi.pan, [])
        fi.transfer_interface = MagicMock(current_account_name="alice")
        fi.current_dir_id = 3
        fi._FileInterface__onPrepareUploadFinished(
            entries=[
                {"path": Path("/tmp/docs"), "is_dir": True, "conflict": True}
            ],
            existing_file_names=set(),
            existing_folder_names={"docs"},
            error="",
            context=None,
        )
        fake_pool.run_pending()
        assert fi.pan.prepare_folder_upload.call_args.kwargs["merge"] is False
        fi_module.InfoBar.success.assert_called_once()  # 创建了 1 个文件夹


class TestFolderUploadTask:
    def test_folder_upload_prepare_task_success(self):
        pan = MagicMock()
        pan.prepare_folder_upload.return_value = {
            "file_targets": [
                {
                    "file_name": "a.txt",
                    "file_size": 1,
                    "local_path": "/tmp/docs/a.txt",
                    "target_dir_id": 5,
                }
            ],
            "created_dir_count": 2,
            "root_dir_id": 9,
            "root_dir_name": "docs",
        }
        task = FileInterface._FolderUploadPrepareTask(
            pan, [{"path": Path("/tmp/docs"), "merge": True}], 5
        )
        results = []
        task.signals.finished.connect(
            lambda uploads, items, count, error: results.append(
                (uploads, items, count, error)
            )
        )
        task.run()
        uploads, folder_items, created, error = results[0]
        assert uploads[0]["file_name"] == "a.txt"
        assert folder_items == [{"FileId": 9, "FileName": "docs"}]
        assert created == 2
        assert error == ""
        assert pan.prepare_folder_upload.call_args.kwargs["merge"] is True

    def test_folder_upload_prepare_task_defaults_merge_false(self):
        pan = MagicMock()
        pan.prepare_folder_upload.return_value = {
            "file_targets": [],
            "created_dir_count": 0,
            "root_dir_id": 9,
            "root_dir_name": "docs",
        }
        task = FileInterface._FolderUploadPrepareTask(
            pan, [{"path": Path("/tmp/docs")}], 5
        )
        task.signals.finished.connect(lambda *args: None)
        task.run()
        assert pan.prepare_folder_upload.call_args.kwargs["merge"] is False

    def test_folder_upload_prepare_task_collects_errors(self):
        pan = MagicMock()
        pan.prepare_folder_upload.side_effect = RuntimeError("boom")
        task = FileInterface._FolderUploadPrepareTask(
            pan, [{"path": Path("/tmp/docs")}], 5
        )
        results = []
        task.signals.finished.connect(
            lambda uploads, items, count, error: results.append(
                (uploads, items, count, error)
            )
        )
        task.run()
        uploads, folder_items, created, error = results[0]
        assert uploads == []
        assert folder_items == []
        assert created == 0
        assert error == "docs: boom"


class TestFolderUploadDone:
    def test_execute_upload_entries_runs_folder_task(self, fi, fake_pool):
        fi.pan = MagicMock()
        fi.pan.prepare_folder_upload.return_value = {
            "file_targets": [
                {
                    "file_name": "a.txt",
                    "file_size": 1,
                    "local_path": "/tmp/docs/a.txt",
                    "target_dir_id": 9,
                }
            ],
            "created_dir_count": 1,
            "root_dir_id": 9,
            "root_dir_name": "docs",
        }
        _prepare_pan(fi.pan, [])
        fi.transfer_interface = MagicMock(current_account_name="alice")
        fi.current_dir_id = 5
        fi._FileInterface__executeUploadEntries(
            entries=[{"path": Path("/tmp/docs"), "is_dir": True, "merge": True}],
            existing_file_names=set(),
            error="",
            context={"target_dir_id": 5},
            should_refresh_current_dir=True,
        )
        assert len(fake_pool.tasks) == 1
        fake_pool.run_pending()
        fi.transfer_interface.add_upload_task.assert_called_once_with(
            "a.txt", 1, "/tmp/docs/a.txt", 9
        )
        assert (
            fi_module.InfoBar.success.call_args.kwargs["content"]
            == "已添加 1 个上传任务，创建 1 个文件夹"
        )

    def test_execute_upload_entries_folder_task_with_error(self, fi, fake_pool):
        fi.pan = MagicMock()
        _prepare_pan(fi.pan, [])
        fi.transfer_interface = MagicMock(current_account_name="alice")
        fi.current_dir_id = 5
        fi._FileInterface__executeUploadEntries(
            entries=[{"path": Path("/tmp/docs"), "is_dir": True}],
            existing_file_names=set(),
            error="bad.txt: gone",
            context={"target_dir_id": 5},
            should_refresh_current_dir=False,
        )
        fake_pool.run_pending()
        assert (
            fi_module.InfoBar.warning.call_args.kwargs["content"] == "bad.txt: gone"
        )

    def test_on_folder_prepare_done_warns_for_errors(self, fi, monkeypatch):
        fi.pan = object()
        fi.transfer_interface = MagicMock(current_account_name="alice")
        fi.current_dir_id = 5
        fi._FileInterface__onFolderPrepareDone(
            folder_uploads=[],
            folder_items=[],
            created_dir_count=0,
            folder_error="boom；",
            context=_ctx(fi.pan, dir_id=5),
            added_count=0,
            error="skipped: bad.txt",
            should_refresh_current_dir=True,
        )
        warnings = [
            call.kwargs["content"] for call in fi_module.InfoBar.warning.call_args_list
        ]
        assert "skipped: bad.txt" in warnings
        assert "boom" in warnings
        fi_module.InfoBar.success.assert_not_called()

    def test_on_folder_prepare_done_updates_tree_and_refreshes(
        self, fi, fake_pool, monkeypatch
    ):
        fi.pan = object()
        fi.transfer_interface = MagicMock(current_account_name="alice")
        fi.current_dir_id = 0
        refresh = MagicMock()
        monkeypatch.setattr(fi, "_FileInterface__refreshFileList", refresh)
        fi._FileInterface__onFolderPrepareDone(
            folder_uploads=[],
            folder_items=[{"FileId": 9, "FileName": "docs"}],
            created_dir_count=1,
            folder_error="",
            context=_ctx(fi.pan, dir_id=0),
            added_count=0,
            error="",
            should_refresh_current_dir=True,
        )
        refresh.assert_called_once()
        root = fi.folderTree.topLevelItem(0)
        assert root.childCount() == 1
        assert root.child(0).text(0) == "docs"


class TestBuildUploadSummary:
    def test_summary_with_both_counts(self):
        summary = FileInterface._FileInterface__buildUploadSummary(3, 2)
        assert summary == "已添加 3 个上传任务，创建 2 个文件夹"

    def test_summary_with_files_only(self):
        summary = FileInterface._FileInterface__buildUploadSummary(3, 0)
        assert summary == "已添加 3 个上传任务"


def _stub_download_configs(monkeypatch, db, *, ask=True, path="/tmp"):
    configs = {"askDownloadLocation": ask, "defaultDownloadPath": path}
    monkeypatch.setattr(
        db, "get_config", lambda key, default=None: configs.get(key, default)
    )


class TestDownload:
    def test_download_without_selection_warns(self, fi, temp_db, monkeypatch):
        _stub_download_configs(monkeypatch, temp_db)
        fi._FileInterface__downloadFile()
        assert (
            fi_module.InfoBar.warning.call_args.kwargs["content"]
            == "请选择要下载的文件"
        )

    @patch("src.app.view.file_interface.QFileDialog.getExistingDirectory")
    def test_download_multi_cancel_dir_dialog(
        self, mock_dlg, fi, temp_db, monkeypatch
    ):
        _stub_download_configs(monkeypatch, temp_db)
        _add_row(fi, 0, "a.txt", 1, 0, size=5)
        _add_row(fi, 1, "b.txt", 2, 0, size=6)
        fi.fileTable.setRangeSelected(QTableWidgetSelectionRange(0, 0, 1, 0), True)
        mock_dlg.return_value = ""
        fi.transfer_interface = MagicMock()
        fi._FileInterface__downloadFile()
        fi_module.InfoBar.success.assert_not_called()
        fi.transfer_interface.add_download_task.assert_not_called()

    @patch("src.app.view.file_interface.QFileDialog.getExistingDirectory")
    def test_download_multi_dedups_against_dir_and_tasks(
        self, mock_dlg, fi, temp_db, monkeypatch, tmp_path
    ):
        dl = tmp_path / "dl"
        dl.mkdir()
        (dl / "a.txt").write_text("x", encoding="utf-8")
        _stub_download_configs(monkeypatch, temp_db, path=str(dl))
        _add_row(fi, 0, "a.txt", 1, 0, meta={"Size": 5, "Etag": "etag1", "S3KeyFlag": True})
        _add_row(fi, 1, "a.txt", 2, 0, meta={"Size": 6})
        fi.fileTable.setRangeSelected(QTableWidgetSelectionRange(0, 0, 1, 0), True)
        mock_dlg.return_value = str(dl)
        running = MagicMock()
        running.status = "下载中"
        running.save_path = str(dl / "c.txt")
        fi.transfer_interface = MagicMock()
        fi.transfer_interface.download_tasks = [running]
        fi.current_dir_id = 7
        fi._FileInterface__downloadFile()
        calls = fi.transfer_interface.add_download_task.call_args_list
        assert calls[0].args[0] == "a (1).txt"  # 与目录中已有 a.txt 去重
        assert calls[1].args[0] == "a (2).txt"  # 与同批次第一个任务去重
        assert calls[0].args[1:] == (5, 1, str(dl / "a (1).txt"), 7)
        assert calls[0].kwargs["etag"] == "etag1"
        assert calls[0].kwargs["s3key_flag"] is True
        assert (
            fi_module.InfoBar.success.call_args.kwargs["content"]
            == "已添加 2 个下载任务"
        )

    @patch("src.app.view.file_interface.QFileDialog.getSaveFileName")
    def test_download_single_cancel_save_dialog(
        self, mock_dlg, fi, temp_db, monkeypatch
    ):
        _stub_download_configs(monkeypatch, temp_db)
        _add_row(fi, 0, "a.txt", 1, 0, size=5)
        fi.fileTable.selectRow(0)
        mock_dlg.return_value = ("", "")
        fi.transfer_interface = MagicMock()
        fi._FileInterface__downloadFile()
        fi_module.InfoBar.success.assert_not_called()
        fi.transfer_interface.add_download_task.assert_not_called()

    @patch("src.app.view.file_interface.QFileDialog.getSaveFileName")
    def test_download_single_folder_uses_chosen_path(
        self, mock_dlg, fi, temp_db, monkeypatch
    ):
        _stub_download_configs(monkeypatch, temp_db, path="/dl")
        _add_row(fi, 0, "docs", 3, 1, meta={"Size": 5})
        fi.fileTable.selectRow(0)
        mock_dlg.return_value = ("/dl/docs.zip", "Filter")
        fi.transfer_interface = MagicMock()
        fi.current_dir_id = 0
        fi._FileInterface__downloadFile()
        call = fi.transfer_interface.add_download_task.call_args
        assert call.args == ("docs.zip", 5, 3, "/dl/docs.zip", 0)
        assert call.kwargs["file_type"] == 1

    def test_download_no_ask_uses_default_path(self, fi, temp_db, monkeypatch, tmp_path):
        dl = tmp_path / "dl2"
        dl.mkdir()
        _stub_download_configs(monkeypatch, temp_db, ask=False, path=str(dl))
        _add_row(fi, 0, "a.txt", 1, 0, size=5)
        fi.fileTable.selectRow(0)
        fi.transfer_interface = MagicMock()
        fi.transfer_interface.download_tasks = []
        fi.current_dir_id = 0
        fi._FileInterface__downloadFile()
        call = fi.transfer_interface.add_download_task.call_args
        assert call.args[0] == "a.txt"
        assert call.args[3] == str(dl / "a.txt")

    def test_download_without_transfer_interface_skips_tasks(
        self, fi, temp_db, monkeypatch
    ):
        _stub_download_configs(monkeypatch, temp_db, ask=False)
        _add_row(fi, 0, "a.txt", 1, 0, size=5)
        fi.fileTable.selectRow(0)
        fi._FileInterface__downloadFile()
        fi_module.InfoBar.success.assert_not_called()


class TestSearchAndJump:
    @patch("src.app.view.file_interface.SearchDialog")
    def test_on_search_without_pan_returns(self, mock_dlg, fi):
        fi._FileInterface__onSearch("kw")
        mock_dlg.assert_not_called()

    @patch("src.app.view.file_interface.SearchDialog")
    def test_on_search_dialog_rejected(self, mock_dlg, fi, fake_pool):
        fi.pan = MagicMock()
        mock_dlg.return_value.exec.return_value = QDialog.DialogCode.Rejected
        fi._FileInterface__onSearch("kw")
        mock_dlg.return_value.searchBar.setText.assert_called_once_with("kw")
        mock_dlg.return_value.searchBar.search.assert_called_once_with()
        assert fake_pool.tasks == []

    @patch("src.app.view.file_interface.SearchDialog")
    def test_on_search_empty_result(self, mock_dlg, fi, fake_pool):
        fi.pan = MagicMock()
        _accept_dialog(mock_dlg)
        mock_dlg.return_value.get_result.return_value = None
        fi._FileInterface__onSearch("kw")
        assert fake_pool.tasks == []

    @patch("src.app.view.file_interface.SearchDialog")
    def test_search_jump_to_file_selects_row(self, mock_dlg, fi, fake_pool):
        fi.pan = MagicMock()
        fi.pan.file_details.return_value = {
            "paths": [{"fileId": 7, "fileName": "docs"}]
        }
        _prepare_pan(
            fi.pan, [{"FileId": 12, "FileName": "a.txt", "Type": 0, "Size": 1}]
        )
        _accept_dialog(mock_dlg)
        mock_dlg.return_value.get_result.return_value = {
            "FileId": 12,
            "Type": 0,
            "ParentFileId": 7,
            "FileName": "a.txt",
        }
        root = fi.folderTree.topLevelItem(0)
        child = _tree_child(root, "docs", 7)
        fi._FileInterface__onSearch("kw")
        fake_pool.run_pending()
        assert fi.current_dir_id == 7
        assert fi.path_stack == [(0, "根目录"), (7, "docs")]
        assert fi.fileTable.currentRow() == 0
        assert fi.folderTree.currentItem() is child

    @patch("src.app.view.file_interface.SearchDialog")
    def test_search_jump_to_folder_appends_target_name(self, mock_dlg, fi, fake_pool):
        fi.pan = MagicMock()
        fi.pan.file_details.return_value = {
            "paths": [{"fileId": 7, "fileName": "docs"}]
        }
        _prepare_pan(fi.pan, [])
        _accept_dialog(mock_dlg)
        mock_dlg.return_value.get_result.return_value = {
            "FileId": 9,
            "Type": 1,
            "ParentFileId": 7,
            "FileName": "docs",
        }
        fi._FileInterface__onSearch("kw")
        fake_pool.run_pending()
        assert fi.current_dir_id == 9
        assert fi.path_stack == [(0, "根目录"), (7, "docs"), (9, "docs")]

    @patch("src.app.view.file_interface.SearchDialog")
    def test_search_jump_error_shows_infobar(self, mock_dlg, fi, fake_pool):
        fi.pan = MagicMock()
        fi.pan.file_details.side_effect = RuntimeError("offline")
        _accept_dialog(mock_dlg)
        mock_dlg.return_value.get_result.return_value = {
            "FileId": 12,
            "Type": 0,
            "ParentFileId": 7,
            "FileName": "a.txt",
        }
        fi._FileInterface__onSearch("kw")
        fake_pool.run_pending()
        assert fi.current_dir_id == 0
        fi_module.InfoBar.error.assert_called_once()

    def test_jump_finished_falls_back_to_detail_paths(self, fi, fake_pool):
        fi.pan = MagicMock()
        _prepare_pan(fi.pan, [])
        fi._jump_request_id = 3
        fi._FileInterface__onJumpFinished(
            detail_paths=[{"fileId": 7, "fileName": "docs"}],
            target_dir_id=7,
            select_file_id=None,
            error="",
            target_name="",
            request_id=3,
        )
        fake_pool.run_pending()
        assert fi.path_stack == [(0, "根目录"), (7, "docs")]

    def test_jump_finished_without_target_in_paths_keeps_stack(self, fi, fake_pool):
        fi.pan = MagicMock()
        _prepare_pan(fi.pan, [])
        fi._jump_request_id = 3
        fi._FileInterface__onJumpFinished(
            detail_paths=[{"fileId": 7, "fileName": "docs"}],
            target_dir_id=9,
            select_file_id=None,
            error="",
            target_name="",
            request_id=3,
        )
        fake_pool.run_pending()
        # detail_paths 中的路径项全部入栈；target 不在其中且无 target_name，不再追加
        assert fi.path_stack == [(0, "根目录"), (7, "docs")]

    def test_jump_finished_with_explicit_target_name_appends(self, fi, fake_pool):
        fi.pan = MagicMock()
        _prepare_pan(fi.pan, [])
        fi._jump_request_id = 3
        fi._FileInterface__onJumpFinished(
            detail_paths=[{"fileId": 7, "fileName": "docs"}],
            target_dir_id=9,
            select_file_id=None,
            error="",
            target_name="sub",
            request_id=3,
        )
        fake_pool.run_pending()
        assert fi.path_stack == [(0, "根目录"), (7, "docs"), (9, "sub")]


class TestDeleteFiles:
    @patch("src.app.view.file_interface.MessageBox")
    def test_delete_by_args_single_confirmed(self, mock_mb, fi, fake_pool, monkeypatch):
        mock_mb.return_value.exec.return_value = True
        fi.pan = MagicMock()
        items = [
            {"FileId": 5, "FileName": "a.txt", "Type": 0},
            {"FileId": 6, "FileName": "docs", "Type": 1},
        ]
        fi.pan.get_dir_by_id.return_value = (0, items)
        fi.current_dir_id = 0
        refresh = MagicMock()
        monkeypatch.setattr(fi, "_FileInterface__refreshFileList", refresh)
        fi._FileInterface__deleteFile(5, "a.txt")
        fake_pool.run_pending()
        assert mock_mb.call_args.args[0] == "确认删除"
        assert '确定要删除 "a.txt" 吗？' in mock_mb.call_args.args[1]
        fi.pan.delete_file.assert_called_once_with(items[0], operation=True)
        assert fi.fileTable.rowCount() == 2
        assert fi_module.InfoBar.success.call_args.kwargs["content"] == "已成功删除 1 个文件"

    @patch("src.app.view.file_interface.MessageBox")
    def test_delete_confirm_cancelled_aborts(self, mock_mb, fi):
        mock_mb.return_value.exec.return_value = False
        fi.pan = MagicMock()
        _add_row(fi, 0, "a.txt", 5, 0)
        fi.fileTable.selectRow(0)
        fi._FileInterface__deleteFile()
        fi.pan.delete_file.assert_not_called()

    def test_delete_without_selection_warns(self, fi):
        fi._FileInterface__deleteFile()
        assert (
            fi_module.InfoBar.warning.call_args.kwargs["content"]
            == "请选择要删除的文件"
        )

    @patch("src.app.view.file_interface.MessageBox")
    def test_delete_multi_reports_partial_errors(
        self, mock_mb, fi, fake_pool, monkeypatch
    ):
        mock_mb.return_value.exec.return_value = True
        fi.pan = MagicMock()
        for i in range(2):
            _add_row(fi, i, f"f{i}.txt", 10 + i, 0)
        fi.fileTable.setRangeSelected(QTableWidgetSelectionRange(0, 0, 1, 0), True)
        items = [{"FileId": 10 + i, "FileName": f"f{i}.txt", "Type": 0} for i in range(2)]
        remaining = [{"FileId": 99, "FileName": "keep.txt", "Type": 0}]
        fi.pan.get_dir_by_id.side_effect = (
            lambda dir_id, all=True, limit=100, search_data="": (
                (0, items) if limit == 1000 else (0, remaining)
            )
        )
        fi.pan.delete_file.side_effect = [RuntimeError("boom"), None]
        fi.current_dir_id = 0
        refresh = MagicMock()
        monkeypatch.setattr(fi, "_FileInterface__refreshFileList", refresh)
        fi._FileInterface__deleteFile()
        content = mock_mb.call_args.args[1]
        assert "f0.txt" in content
        assert "共 2 个文件" in content
        fake_pool.run_pending()
        assert fi.pan.delete_file.call_count == 2
        assert "f0.txt: boom" in fi_module.InfoBar.error.call_args.kwargs["content"]
        assert fi_module.InfoBar.success.call_args.kwargs["content"] == "已成功删除 1 个文件"
        assert fi.fileTable.item(0, 0).text() == "keep.txt"

    @patch("src.app.view.file_interface.MessageBox")
    def test_delete_more_than_five_names_truncated_and_refreshes(
        self, mock_mb, fi, fake_pool, monkeypatch
    ):
        mock_mb.return_value.exec.return_value = True
        fi.pan = MagicMock()
        for i in range(6):
            _add_row(fi, i, f"f{i}.txt", 10 + i, 0)
        fi.fileTable.setRangeSelected(QTableWidgetSelectionRange(0, 0, 5, 0), True)
        items = [{"FileId": 10 + i, "FileName": f"f{i}.txt", "Type": 0} for i in range(6)]
        fi.pan.get_dir_by_id.side_effect = (
            lambda dir_id, all=True, limit=100, search_data="": (
                (0, items) if limit == 1000 else (1, [])  # 第二次拉取失败
            )
        )
        fi.current_dir_id = 0
        refresh = MagicMock()
        monkeypatch.setattr(fi, "_FileInterface__refreshFileList", refresh)
        fi._FileInterface__deleteFile()
        assert "等共 6 个文件" in mock_mb.call_args.args[1]
        fake_pool.run_pending()
        assert fi.pan.delete_file.call_count == 6
        assert fi_module.InfoBar.error.call_args is None or not fi_module.InfoBar.error.called
        assert fi_module.InfoBar.success.call_args.kwargs["content"] == "已成功删除 6 个文件"
        refresh.assert_called_once()

    @patch("src.app.view.file_interface.MessageBox")
    def test_delete_task_dir_fetch_fails(self, mock_mb, fi, fake_pool):
        mock_mb.return_value.exec.return_value = True
        fi.pan = MagicMock()
        fi.pan.get_dir_by_id.return_value = (1, [])
        _add_row(fi, 0, "a.txt", 5, 0)
        fi.fileTable.selectRow(0)
        fi.current_dir_id = 0
        fi._FileInterface__deleteFile()
        fake_pool.run_pending()
        assert (
            "获取目录失败" in fi_module.InfoBar.error.call_args.kwargs["content"]
        )

    @patch("src.app.view.file_interface.MessageBox")
    def test_delete_task_outer_exception(self, mock_mb, fi, fake_pool):
        mock_mb.return_value.exec.return_value = True
        fi.pan = MagicMock()
        fi.pan.get_dir_by_id.side_effect = RuntimeError("net down")
        _add_row(fi, 0, "a.txt", 5, 0)
        fi.fileTable.selectRow(0)
        fi.current_dir_id = 0
        fi._FileInterface__deleteFile()
        fake_pool.run_pending()
        assert (
            "net down" in fi_module.InfoBar.error.call_args.kwargs["content"]
        )

    def test_on_delete_files_finished_noop_when_nothing_deleted(self, fi, monkeypatch):
        fi.pan = object()
        fi.transfer_interface = MagicMock(current_account_name="alice")
        fi.current_dir_id = 0
        refresh = MagicMock()
        monkeypatch.setattr(fi, "_FileInterface__refreshFileList", refresh)
        fi._FileInterface__onDeleteFilesFinished(
            0, 1, "", [], [], context=_ctx(fi.pan, dir_id=0)
        )
        fi_module.InfoBar.success.assert_not_called()
        refresh.assert_not_called()


class TestRenameFile:
    def test_rename_without_selection_warns(self, fi):
        fi._FileInterface__renameFile()
        assert (
            fi_module.InfoBar.warning.call_args.kwargs["content"]
            == "请选择要重命名的文件"
        )

    @patch("src.app.view.file_interface.RenameDialog")
    def test_rename_dialog_rejected(self, mock_dlg, fi):
        _add_row(fi, 0, "a.txt", 1, 0)
        fi.fileTable.selectRow(0)
        mock_dlg.return_value.exec.return_value = QDialog.DialogCode.Rejected
        fi._FileInterface__renameFile()
        fi_module.InfoBar.warning.assert_not_called()

    @pytest.mark.parametrize(
        "new_name,expected",
        [
            ("", "名称不能为空"),
            ("a.txt", "新名称与旧名称相同"),
            ("a/b.txt", "名称不能包含以下字符"),
        ],
    )
    @patch("src.app.view.file_interface.RenameDialog")
    def test_rename_rejects_invalid_names(
        self, mock_dlg, new_name, expected, fi, fake_pool
    ):
        _add_row(fi, 0, "a.txt", 1, 0)
        fi.fileTable.selectRow(0)
        _accept_dialog(mock_dlg)
        mock_dlg.return_value.get_new_name.return_value = new_name
        fi._FileInterface__renameFile()
        assert expected in fi_module.InfoBar.warning.call_args.kwargs["content"]
        assert fake_pool.tasks == []

    @patch("src.app.view.file_interface.RenameDialog")
    def test_rename_success_updates_list(self, mock_dlg, fi, fake_pool):
        _add_row(fi, 0, "a.txt", 1, 0)
        fi.fileTable.selectRow(0)
        _accept_dialog(mock_dlg)
        mock_dlg.return_value.get_new_name.return_value = "b.txt"
        fi.pan = MagicMock()
        fi.pan.rename_file.return_value = True
        _prepare_pan(
            fi.pan,
            [
                {"FileId": 1, "FileName": "b.txt", "Type": 0},
                {"FileId": 2, "FileName": "docs", "Type": 1},
            ],
        )
        fi.current_dir_id = 0
        fi._FileInterface__renameFile()
        fake_pool.run_pending()
        assert fi.pan.rename_file.call_args.args == (1, "b.txt")
        # 文件夹始终排在前面
        assert fi.fileTable.item(0, 0).text() == "docs"
        assert fi.fileTable.item(1, 0).text() == "b.txt"
        # 树同步更新出 docs 节点（folder_items 由任务内筛选 Type==1 得到）
        root = fi.folderTree.topLevelItem(0)
        assert any(
            root.child(i).text(0) == "docs" for i in range(root.childCount())
        )
        assert "已成功重命名为" in fi_module.InfoBar.success.call_args.kwargs["content"]

    @patch("src.app.view.file_interface.RenameDialog")
    def test_rename_task_reports_failure(self, mock_dlg, fi, fake_pool):
        _add_row(fi, 0, "a.txt", 1, 0)
        fi.fileTable.selectRow(0)
        _accept_dialog(mock_dlg)
        mock_dlg.return_value.get_new_name.return_value = "b.txt"
        fi.pan = MagicMock()
        fi.pan.rename_file.return_value = False
        fi.current_dir_id = 0
        fi._FileInterface__renameFile()
        fake_pool.run_pending()
        assert fi_module.InfoBar.error.call_args.kwargs["content"] == "重命名文件时发生错误: 重命名失败"

    @patch("src.app.view.file_interface.RenameDialog")
    def test_rename_task_exception(self, mock_dlg, fi, fake_pool):
        _add_row(fi, 0, "a.txt", 1, 0)
        fi.fileTable.selectRow(0)
        _accept_dialog(mock_dlg)
        mock_dlg.return_value.get_new_name.return_value = "b.txt"
        fi.pan = MagicMock()
        fi.pan.rename_file.side_effect = RuntimeError("boom")
        fi.current_dir_id = 0
        fi._FileInterface__renameFile()
        fake_pool.run_pending()
        assert "boom" in fi_module.InfoBar.error.call_args.kwargs["content"]

    def test_on_rename_finished_without_error_detail(self, fi):
        fi.pan = object()
        fi.transfer_interface = MagicMock(current_account_name="alice")
        fi.current_dir_id = 0
        fi._FileInterface__onRenameFileFinished(
            False, "a.txt", "b.txt", "", [], [], context=_ctx(fi.pan, dir_id=0)
        )
        assert fi_module.InfoBar.error.call_args.kwargs["content"] == "重命名失败"


class TestMoveFiles:
    @staticmethod
    def _select_single(fi, name="docs", file_id=5, file_type=1):
        _add_row(fi, 0, name, file_id, file_type)
        fi.fileTable.selectRow(0)

    def test_move_without_selection_warns(self, fi):
        fi._FileInterface__moveFile()
        assert (
            fi_module.InfoBar.warning.call_args.kwargs["content"]
            == "请选择要移动的文件"
        )

    @patch("src.app.view.file_interface.MoveDialog")
    def test_move_dialog_rejected(self, mock_dlg, fi):
        self._select_single(fi)
        mock_dlg.return_value.exec.return_value = QDialog.DialogCode.Rejected
        fi._FileInterface__moveFile()
        fi_module.InfoBar.warning.assert_not_called()

    @patch("src.app.view.file_interface.MoveDialog")
    def test_move_target_none_returns(self, mock_dlg, fi):
        self._select_single(fi)
        _accept_dialog(mock_dlg)
        mock_dlg.return_value.get_target.return_value = (None, "")
        fi._FileInterface__moveFile()
        fi_module.InfoBar.warning.assert_not_called()

    @patch("src.app.view.file_interface.MoveDialog")
    def test_move_target_same_dir_warns(self, mock_dlg, fi):
        self._select_single(fi)
        _accept_dialog(mock_dlg)
        mock_dlg.return_value.get_target.return_value = (0, "根目录")
        fi.current_dir_id = 0
        fi._FileInterface__moveFile()
        assert (
            fi_module.InfoBar.warning.call_args.kwargs["content"]
            == "目标文件夹与当前文件夹相同"
        )

    @patch("src.app.view.file_interface.MoveDialog")
    def test_move_folder_into_itself_warns(self, mock_dlg, fi):
        self._select_single(fi, name="docs", file_id=5, file_type=1)
        _accept_dialog(mock_dlg)
        mock_dlg.return_value.get_target.return_value = (5, "docs")
        fi.current_dir_id = 0
        fi._FileInterface__moveFile()
        assert (
            "不能将文件夹移动到自身内部"
            in fi_module.InfoBar.warning.call_args.kwargs["content"]
        )

    @patch("src.app.view.file_interface.MoveDialog")
    def test_move_folder_into_descendant_warns(self, mock_dlg, fi):
        self._select_single(fi, name="docs", file_id=5, file_type=1)
        _accept_dialog(mock_dlg)
        mock_dlg.return_value.get_target.return_value = (9, "sub")
        mock_dlg.return_value.get_ancestor_ids.return_value = {5}
        fi.current_dir_id = 0
        fi._FileInterface__moveFile()
        assert (
            "不能将文件夹移动到其子文件夹中"
            in fi_module.InfoBar.warning.call_args.kwargs["content"]
        )

    @patch("src.app.view.file_interface.MoveDialog")
    def test_move_success_refreshes(self, mock_dlg, fi, fake_pool, monkeypatch):
        _add_row(fi, 0, "a.txt", 1, 0)
        _add_row(fi, 1, "docs", 2, 1)
        fi.fileTable.setRangeSelected(QTableWidgetSelectionRange(0, 0, 1, 0), True)
        _accept_dialog(mock_dlg)
        mock_dlg.return_value.get_target.return_value = (9, "target")
        mock_dlg.return_value.get_ancestor_ids.return_value = set()
        fi.pan = MagicMock()
        fi.pan.move_file.return_value = True
        fi.current_dir_id = 0
        refresh = MagicMock()
        monkeypatch.setattr(fi, "_FileInterface__refreshFileList", refresh)
        fi._FileInterface__moveFile()
        fake_pool.run_pending()
        fi.pan.move_file.assert_called_once_with([1, 2], 9)
        assert (
            fi_module.InfoBar.success.call_args.kwargs["content"]
            == "已将 2 个文件移动到「target」"
        )
        refresh.assert_called_once()

    @patch("src.app.view.file_interface.MoveDialog")
    def test_move_task_failure(self, mock_dlg, fi, fake_pool, monkeypatch):
        self._select_single(fi, name="a.txt", file_id=1, file_type=0)
        _accept_dialog(mock_dlg)
        mock_dlg.return_value.get_target.return_value = (9, "target")
        mock_dlg.return_value.get_ancestor_ids.return_value = set()
        fi.pan = MagicMock()
        fi.pan.move_file.return_value = False
        fi.current_dir_id = 0
        refresh = MagicMock()
        monkeypatch.setattr(fi, "_FileInterface__refreshFileList", refresh)
        fi._FileInterface__moveFile()
        fake_pool.run_pending()
        assert fi_module.InfoBar.error.call_args.kwargs["content"] == "移动文件失败"
        refresh.assert_not_called()

    @patch("src.app.view.file_interface.MoveDialog")
    def test_move_task_exception(self, mock_dlg, fi, fake_pool):
        self._select_single(fi, name="a.txt", file_id=1, file_type=0)
        _accept_dialog(mock_dlg)
        mock_dlg.return_value.get_target.return_value = (9, "target")
        mock_dlg.return_value.get_ancestor_ids.return_value = set()
        fi.pan = MagicMock()
        fi.pan.move_file.side_effect = RuntimeError("boom")
        fi.current_dir_id = 0
        fi._FileInterface__moveFile()
        fake_pool.run_pending()
        assert "boom" in fi_module.InfoBar.error.call_args.kwargs["content"]

    def test_on_move_finished_error_returns_before_refresh(self, fi, monkeypatch):
        fi.pan = object()
        fi.transfer_interface = MagicMock(current_account_name="alice")
        fi.current_dir_id = 0
        refresh = MagicMock()
        monkeypatch.setattr(fi, "_FileInterface__refreshFileList", refresh)
        fi._FileInterface__onMoveFilesFinished(
            True, 1, "t", "boom", context=_ctx(fi.pan, dir_id=0)
        )
        assert "boom" in fi_module.InfoBar.error.call_args.kwargs["content"]
        refresh.assert_not_called()


class TestFileDetails:
    def test_show_details_without_selection_returns(self, fi, fake_pool):
        fi._FileInterface__showFileDetails()
        assert fake_pool.tasks == []

    def test_show_details_flow_renders_message_box(self, fi, fake_pool):
        _add_row(fi, 0, "docs", 5, 1)
        fi.fileTable.selectRow(0)
        fi.pan = MagicMock()
        fi.pan.file_details.return_value = {
            "fileNum": 2,
            "dirNum": 1,
            "totalSize": 1536,
            "paths": [{"fileName": "root"}, {"fileName": "docs"}],
        }
        fi.current_dir_id = 0
        with patch("src.app.view.file_interface.MessageBox") as mock_mb:
            fi._FileInterface__showFileDetails()
            fake_pool.run_pending()
            title, body = mock_mb.call_args.args[0], mock_mb.call_args.args[1]
            assert "docs" in title
            assert "路径：root / docs" in body
            assert "文件夹数：1" in body
            assert "文件数：2" in body
            assert "总大小" in body
            mock_mb.return_value.cancelButton.hide.assert_called_once()
            mock_mb.return_value.exec.assert_called_once()

    def test_show_details_task_error_shows_infobar(self, fi, fake_pool):
        _add_row(fi, 0, "docs", 5, 1)
        fi.fileTable.selectRow(0)
        fi.pan = MagicMock()
        fi.pan.file_details.side_effect = RuntimeError("offline")
        fi.current_dir_id = 0
        fi._FileInterface__showFileDetails()
        fake_pool.run_pending()
        assert fi_module.InfoBar.error.call_args.kwargs["content"] == "offline"

    def test_details_finished_with_none_data_shows_error(self, fi):
        fi.pan = object()
        fi.transfer_interface = MagicMock(current_account_name="alice")
        fi._file_details_request_id = 1
        with patch(
            "src.app.view.file_interface.MessageBox",
            side_effect=AssertionError("should not open"),
        ):
            fi._FileInterface__onFileDetailsFinished(
                "a.txt", None, "", context=_ctx(fi.pan, dir_id=None, request_id=1)
            )
        assert fi_module.InfoBar.error.call_args.kwargs["content"] == "未知错误"

    def test_details_finished_stale_context_returns_early(self, fi):
        fi.pan = object()
        fi.transfer_interface = MagicMock(current_account_name="alice")
        fi._file_details_request_id = 1
        with patch(
            "src.app.view.file_interface.MessageBox",
            side_effect=AssertionError("should not open"),
        ):
            fi._FileInterface__onFileDetailsFinished(
                "a.txt", {"paths": []}, "", context=_ctx(object(), dir_id=None)
            )
        fi_module.InfoBar.error.assert_not_called()


class TestFileContextMenu:
    @staticmethod
    def _install_menu_factory(monkeypatch, created):
        class _Menu:
            def __init__(self, parent=None):
                self.actions = []
                self.exec_calls = []
                created.append(self)

            def addAction(self, action):
                self.actions.append(action)

            def addSeparator(self):
                self.actions.append(None)

            def exec(self, pos):
                self.exec_calls.append(pos)

        monkeypatch.setattr(fi_module, "QMenu", _Menu)

    @staticmethod
    def _fake_index(row, valid=True):
        idx = MagicMock()
        idx.isValid.return_value = valid
        idx.row.return_value = row
        return idx

    def test_context_menu_invalid_index_returns(self, fi, monkeypatch):
        monkeypatch.setattr(
            fi.fileTable, "indexAt", lambda pos: self._fake_index(0, valid=False)
        )
        created: list = []
        self._install_menu_factory(monkeypatch, created)
        fi._FileInterface__onFileTableContextMenu(QPoint(5, 5))
        assert created == []

    def test_context_menu_single_row_shows_rename_and_details(self, fi, monkeypatch):
        _add_row(fi, 0, "a.txt", 1, 0)
        _add_row(fi, 1, "b.txt", 2, 0)
        fi.fileTable.selectRow(0)
        monkeypatch.setattr(
            fi.fileTable, "indexAt", lambda pos: self._fake_index(0)
        )
        created: list = []
        self._install_menu_factory(monkeypatch, created)
        fi._FileInterface__onFileTableContextMenu(QPoint(5, 5))
        texts = [action.text() for action in created[0].actions if action is not None]
        assert texts == ["下载", "重命名", "移动到", "删除", "详情"]
        assert created[0].exec_calls

    def test_context_menu_multi_row_hides_single_only_actions(self, fi, monkeypatch):
        _add_row(fi, 0, "a.txt", 1, 0)
        _add_row(fi, 1, "b.txt", 2, 0)
        fi.fileTable.setRangeSelected(QTableWidgetSelectionRange(0, 0, 1, 0), True)
        monkeypatch.setattr(
            fi.fileTable, "indexAt", lambda pos: self._fake_index(0)
        )
        created: list = []
        self._install_menu_factory(monkeypatch, created)
        fi._FileInterface__onFileTableContextMenu(QPoint(5, 5))
        texts = [action.text() for action in created[0].actions if action is not None]
        assert texts == ["下载", "移动到", "删除"]

    def test_context_menu_click_outside_selection_reselects_row(self, fi, monkeypatch):
        _add_row(fi, 0, "a.txt", 1, 0)
        _add_row(fi, 1, "b.txt", 2, 0)
        fi.fileTable.selectRow(0)
        monkeypatch.setattr(
            fi.fileTable, "indexAt", lambda pos: self._fake_index(1)
        )
        created: list = []
        self._install_menu_factory(monkeypatch, created)
        fi._FileInterface__onFileTableContextMenu(QPoint(5, 5))
        assert fi.fileTable.currentRow() == 1
        texts = [action.text() for action in created[0].actions if action is not None]
        assert "重命名" in texts


class TestStorage:
    def test_update_storage_info(self, fi):
        fi.update_storage_info((50, 200))
        assert fi.storageProgressBar.value() == 25
        assert fi.storageValueLabel.text() == (
            f"{format_file_size(50)} / {format_file_size(200)}"
        )

    def test_update_storage_info_zero_total(self, fi):
        fi.update_storage_info((0, 0))
        assert fi.storageProgressBar.value() == 0

    def test_storage_task_success(self):
        pan = MagicMock()
        pan.user_info.return_value = {"SpaceUsed": 10, "SpacePermanent": 100}
        task = FileInterface.StorageTask(pan)
        results = []
        task.signals.finished.connect(lambda info: results.append(info))
        task.run()
        assert results == [(10, 100)]

    def test_storage_task_empty_data(self):
        pan = MagicMock()
        pan.user_info.return_value = None
        task = FileInterface.StorageTask(pan)
        results = []
        task.signals.finished.connect(lambda info: results.append(info))
        task.run()
        assert results == [(0, 0)]

    def test_storage_task_exception(self):
        pan = MagicMock()
        pan.user_info.side_effect = RuntimeError("offline")
        task = FileInterface.StorageTask(pan)
        results = []
        task.signals.finished.connect(lambda info: results.append(info))
        task.run()
        assert results == [(0, 0)]

    def test_load_and_update_storage_info_without_pan(self, fi):
        fi.load_and_update_storage_info()

    def test_load_and_update_storage_info_flow(self, fi, fake_pool):
        fi.pan = MagicMock()
        fi.pan.user_info.return_value = {"SpaceUsed": 25, "SpacePermanent": 100}
        fi.load_and_update_storage_info()
        fake_pool.run_pending()
        assert fi.storageProgressBar.value() == 25
        assert fi._pending_signals == []


class TestAsyncHelpers:
    def test_current_account_name_prefers_transfer_interface(self, fi):
        fi.transfer_interface = MagicMock(current_account_name="alice")
        assert fi._FileInterface__currentAccountName() == "alice"

    def test_current_account_name_falls_back_to_pan(self, fi):
        fi.transfer_interface = None
        fi.pan = MagicMock(user_name="bob")
        assert fi._FileInterface__currentAccountName() == "bob"

    def test_current_account_name_empty_without_both(self, fi):
        fi.transfer_interface = None
        fi.pan = None
        assert fi._FileInterface__currentAccountName() == ""

    def test_build_async_context_fields(self, fi):
        fi.pan = MagicMock()
        fi.transfer_interface = MagicMock(current_account_name="alice")
        fi.current_dir_id = 3
        context = fi._FileInterface__buildAsyncContext(dir_id=3, request_id=7)
        assert context == {
            "pan": fi.pan,
            "account_name": "alice",
            "dir_id": 3,
            "request_id": 7,
        }

    def test_async_context_stale_when_widget_invalid(self, fi, monkeypatch):
        monkeypatch.setattr(fi_module.shiboken6, "isValid", lambda _obj: False)
        assert fi._FileInterface__isAsyncContextStale({"pan": object()}) is True

    def test_async_context_not_stale_without_context(self, fi):
        assert fi._FileInterface__isAsyncContextStale(None) is False

    def test_async_context_stale_on_dir_change_only_when_required(self, fi):
        fi.pan = MagicMock()
        fi.transfer_interface = MagicMock(current_account_name="alice")
        fi.current_dir_id = 1
        context = _ctx(fi.pan, dir_id=9)
        assert fi._FileInterface__isAsyncContextStale(context, require_same_dir=True) is True
        assert fi._FileInterface__isAsyncContextStale(context) is False

    def test_cleanup_stale_signals_trims_to_last_ten(self, fi):
        sigs = [MagicMock() for _ in range(51)]
        fi._pending_signals = list(sigs)
        fi._cleanup_stale_signals()
        assert fi._pending_signals == sigs[-10:]
        for sig in sigs[:-10]:
            sig.deleteLater.assert_called_once_with()

    def test_cleanup_stale_signals_tolerates_runtime_error(self, fi):
        bad = MagicMock()
        bad.deleteLater.side_effect = RuntimeError("already deleted")
        fi._pending_signals = [bad] + [MagicMock() for _ in range(50)]
        fi._cleanup_stale_signals()
        assert len(fi._pending_signals) == 10


class TestInvalidWidgetGuards:
    """shiboken6.isValid 为 False 时的各异步回调兜底分支。"""

    def test_tree_load_callback_skips_invalid_widget(self, fi, fake_pool, monkeypatch):
        monkeypatch.setattr(fi_module.shiboken6, "isValid", lambda _obj: False)
        fi.pan = MagicMock()
        fi.pan.get_dir_by_id.return_value = (0, [{"FileId": 11, "FileName": "docs", "Type": 1}])
        root = fi.folderTree.topLevelItem(0)
        parent = QTreeWidgetItem(["folder"])
        parent.setData(0, Qt.ItemDataRole.UserRole, 77)
        parent.setData(0, Qt.ItemDataRole.UserRole + 1, False)
        root.addChild(parent)
        fi._FileInterface__ensureTreeChildrenLoaded(parent)
        fake_pool.run_pending()
        # 回调在清理 loading 标记后、触碰 UI 前直接返回
        assert fi.is_loading_tree is False
        assert parent.childCount() == 1  # 占位符未被 takeChildren
        assert parent.data(0, Qt.ItemDataRole.UserRole + 1) is False

    def test_load_list_callback_skips_invalid_widget(self, fi, fake_pool, monkeypatch):
        monkeypatch.setattr(fi_module.shiboken6, "isValid", lambda _obj: False)
        fi.pan = MagicMock()
        _prepare_pan(fi.pan, [{"FileId": 1, "FileName": "a.txt", "Type": 0, "Size": 1}])
        fi._FileInterface__loadCurrentList()
        fake_pool.run_pending()
        assert fi.fileTable.rowCount() == 0
        # 回调在移除 pending signal 前返回，sig 仍被持有
        assert len(fi._pending_signals) == 1

    def test_jump_select_row_callback_skips_invalid_widget(
        self, fi, fake_pool, monkeypatch
    ):
        monkeypatch.setattr(fi_module.shiboken6, "isValid", lambda _obj: False)
        fi.pan = MagicMock()
        _prepare_pan(fi.pan, [{"FileId": 12, "FileName": "a.txt", "Type": 0, "Size": 1}])
        fi._jump_request_id = 1
        fi._FileInterface__onJumpFinished(
            detail_paths=[{"fileId": 7, "fileName": "docs"}],
            target_dir_id=7,
            select_file_id=12,
            error="",
            target_name="",
            request_id=1,
        )
        fake_pool.run_pending()
        assert fi.fileTable.rowCount() == 0

    def test_jump_finished_fallback_appends_target_from_detail_paths(
        self, fi, fake_pool
    ):
        fi.pan = MagicMock()
        _prepare_pan(fi.pan, [])
        fi._jump_request_id = 3
        # target(7) 不在路径末尾时，回退从 detail_paths 中定位并追加
        fi._FileInterface__onJumpFinished(
            detail_paths=[
                {"fileId": 9, "fileName": "a"},
                {"fileId": 7, "fileName": "docs"},
                {"fileId": 3, "fileName": "b"},
            ],
            target_dir_id=7,
            select_file_id=None,
            error="",
            target_name="",
            request_id=3,
        )
        fake_pool.run_pending()
        assert fi.path_stack[-1] == (7, "docs")


class TestStaleContextGuards:
    def test_on_delete_files_finished_stale_context_returns(self, fi, monkeypatch):
        fi.pan = object()
        fi.transfer_interface = MagicMock(current_account_name="alice")
        fi.current_dir_id = 0
        refresh = MagicMock()
        monkeypatch.setattr(fi, "_FileInterface__refreshFileList", refresh)
        fi._FileInterface__onDeleteFilesFinished(
            1, 1, "", [{"FileId": 1}], [], context=_ctx(object(), dir_id=0)
        )
        fi_module.InfoBar.success.assert_not_called()
        refresh.assert_not_called()

    def test_on_rename_file_finished_stale_context_returns(self, fi, monkeypatch):
        fi.pan = object()
        fi.transfer_interface = MagicMock(current_account_name="alice")
        fi.current_dir_id = 0
        fi._FileInterface__onRenameFileFinished(
            False, "a.txt", "b.txt", "boom", [], [], context=_ctx(object(), dir_id=0)
        )
        fi_module.InfoBar.error.assert_not_called()


class TestDragMoveFallback:
    def test_drag_move_without_paths_falls_back_to_super(self, fi):
        fi.dragMoveEvent(_drag_event(QDragMoveEvent, []))
        assert fi.fileTable.viewport().styleSheet() == ""
