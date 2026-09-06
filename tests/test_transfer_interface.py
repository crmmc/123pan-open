import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtWidgets import QHeaderView

from src.app.common import database as database_module
from src.app.common.api import format_file_size
from src.app.common.database import Database
from src.app.common.download_metadata import LEGACY_RESUME_TASK_ERROR
from src.app.common.download_resume import get_merged_path, get_part_path
from src.app.view import transfer_interface as tw_module
from src.app.view.transfer_interface import (
    BUTTON_CLICK_HANDLER_ATTR,
    COL_ACTION,
    COL_CONN,
    COL_ETA,
    COL_NAME,
    COL_PERCENT,
    COL_SIZE,
    COL_SPEED,
    COL_STATUS,
    DownloadTask,
    DownloadThread,
    TransferInterface,
    UploadTask,
    UploadThread,
    _UploadTaskStore,
    format_eta,
    format_speed,
)


class _FakeSignal:
    def __init__(self):
        self.connected = []
        self.disconnected = []

    def connect(self, handler):
        self.connected.append(handler)

    def disconnect(self, handler):
        self.disconnected.append(handler)
        if handler in self.connected:
            self.connected.remove(handler)


class _FakeButton:
    def __init__(self):
        self.clicked = _FakeSignal()
        self.text = None
        self.enabled = None
        self.icon = None

    def setText(self, text):
        self.text = text

    def setEnabled(self, enabled):
        self.enabled = enabled

    def setIcon(self, icon):
        self.icon = icon


def _use_temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "123pan-open.db"
    monkeypatch.setattr(database_module, "_get_db_path", lambda: db_path)
    Database.reset()
    return Database.instance()


def _make_interface(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    interface = TransferInterface.__new__(TransferInterface)
    interface.download_tasks = []
    interface.upload_tasks = []
    interface.upload_threads = []
    interface.download_threads = []
    interface.download_status_filter = "全部"
    interface.upload_status_filter = "全部"
    interface.current_account_name = "alice"
    interface.pan = None
    interface._upload_store = _UploadTaskStore()
    interface._TransferInterface__update_download_table = lambda: None
    interface._TransferInterface__update_upload_table = lambda: None
    interface._TransferInterface__try_start_pending_downloads = lambda: None
    interface._TransferInterface__try_start_pending_uploads = lambda: None
    interface.downloadTable = type(
        "_Table",
        (),
        {"currentRow": lambda self: 0},
    )()
    monkeypatch.setattr("src.app.view.transfer_interface.Database.instance", lambda: db)
    return interface, db


def test_upload_task_store_create_task_persists_db_record(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    task = UploadTask("demo.bin", 12, str(tmp_path / "demo.bin"), 7)
    store = _UploadTaskStore()

    store.create_task(task, "alice")

    assert task.db_task_id
    stored = db.get_upload_task(task.db_task_id)
    assert stored is not None
    assert stored["account_name"] == "alice"
    assert stored["file_name"] == "demo.bin"
    assert stored["status"] == "等待中"
    assert stored["delete_requested"] == 0


def test_upload_task_store_update_session_persists_resume_fields(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    task = UploadTask("demo.bin", 12, str(tmp_path / "demo.bin"), 7)
    store = _UploadTaskStore()
    store.create_task(task, "alice")
    task.bucket = "bucket"
    task.storage_node = "node"
    task.upload_key = "key"
    task.upload_id_s3 = "upload-id"
    task.up_file_id = 123
    task.total_parts = 4
    task.block_size = 5
    task.etag = "etag"
    task.file_mtime = 123.4

    store.update_session(task)

    stored = db.get_upload_task(task.db_task_id)
    assert stored is not None
    assert stored["bucket"] == "bucket"
    assert stored["storage_node"] == "node"
    assert stored["upload_key"] == "key"
    assert stored["upload_id_s3"] == "upload-id"
    assert stored["up_file_id"] == 123
    assert stored["total_parts"] == 4
    assert stored["block_size"] == 5
    assert stored["etag"] == "etag"
    assert stored["file_mtime"] == 123.4


def test_upload_task_store_reset_session_clears_parts_and_keeps_progress_when_requested(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    task = UploadTask("demo.bin", 12, str(tmp_path / "demo.bin"), 7)
    store = _UploadTaskStore()
    store.create_task(task, "alice")
    task.progress = 66
    task.bucket = "bucket"
    task.storage_node = "node"
    task.upload_key = "key"
    task.upload_id_s3 = "upload-id"
    task.up_file_id = 123
    task.total_parts = 4
    task.block_size = 5
    task.etag = "etag"
    db.record_upload_part(task.db_task_id, 1, "etag-1")

    store.reset_session(task, clear_progress=False)

    stored = db.get_upload_task(task.db_task_id)
    assert stored is not None
    assert stored["progress"] == 66
    assert stored["bucket"] == ""
    assert stored["upload_key"] == ""
    assert stored["upload_id_s3"] == ""
    assert stored["up_file_id"] == 0
    assert stored["total_parts"] == 0
    assert stored["block_size"] == task.block_size
    assert stored["etag"] == ""
    assert db.get_upload_parts(task.db_task_id) == []


def test_upload_task_store_delete_task_discards_pending_parts(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    task = UploadTask("demo.bin", 12, str(tmp_path / "demo.bin"), 7)
    store = _UploadTaskStore()
    store.create_task(task, "alice")
    task_id = task.db_task_id
    task._pending_upload_parts = [(1, "etag-1")]

    store.delete_task(task)

    assert task.db_task_id is None
    assert task._pending_upload_parts == []
    assert db.get_upload_task(task_id) is None


def test_task_finished_deletes_download_record(tmp_path, monkeypatch):
    interface, db = _make_interface(tmp_path, monkeypatch)
    task = DownloadTask(
        file_name="demo.bin",
        file_size=10,
        file_id=1,
        save_path=str(tmp_path / "demo.bin"),
        account_name="alice",
    )
    db.save_download_task({
        "resume_id": task.resume_id,
        "account_name": "alice",
        "file_name": task.file_name,
        "file_id": task.file_id,
        "save_path": task.save_path,
    })

    # 模拟真实信号流：__update_task_status("已完成") 先于 __task_finished
    task.status = "已完成"
    interface._TransferInterface__task_finished(task, "download")

    assert task.status == "已完成"
    assert db.get_download_task(task.resume_id) is None


def test_get_filtered_download_tasks_by_status(tmp_path, monkeypatch):
    interface, _db = _make_interface(tmp_path, monkeypatch)
    interface.download_tasks = [
        DownloadTask("a.bin", 1, 1, str(tmp_path / "a.bin"), account_name="alice"),
        DownloadTask("b.bin", 1, 2, str(tmp_path / "b.bin"), account_name="alice"),
    ]
    interface.download_tasks[0].status = "下载中"
    interface.download_tasks[1].status = "失败"
    interface.download_status_filter = "失败"

    filtered = interface._TransferInterface__get_filtered_download_tasks()

    assert [task.file_name for task in filtered] == ["b.bin"]


def test_resolve_download_folder_prefers_selected_visible_task(tmp_path, monkeypatch):
    interface, _db = _make_interface(tmp_path, monkeypatch)
    selected = DownloadTask(
        "a.bin",
        1,
        1,
        str(tmp_path / "custom" / "a.bin"),
        account_name="alice",
    )
    interface.download_tasks = [selected]

    folder = interface._TransferInterface__resolve_download_folder()

    assert folder == str(tmp_path / "custom")


def test_resolve_download_folder_falls_back_to_default_setting(tmp_path, monkeypatch):
    interface, db = _make_interface(tmp_path, monkeypatch)
    interface.downloadTable = type(
        "_Table",
        (),
        {"currentRow": lambda self: -1},
    )()
    db.set_config("defaultDownloadPath", str(tmp_path / "downloads"))

    folder = interface._TransferInterface__resolve_download_folder()

    assert folder == str(tmp_path / "downloads")


def test_add_download_task_persists_record(tmp_path, monkeypatch):
    interface, db = _make_interface(tmp_path, monkeypatch)

    task = interface.add_download_task(
        "demo.bin",
        12,
        99,
        str(tmp_path / "demo.bin"),
        current_dir_id=7,
        file_type=0,
        etag="etag-1",
        s3key_flag=True,
    )

    stored = db.get_download_task(task.resume_id)
    assert stored is not None
    assert stored["file_name"] == "demo.bin"
    assert stored["file_id"] == 99
    assert stored["current_dir_id"] == 7
    assert stored["etag"] == "etag-1"


def test_reload_upload_tasks_pauses_active_verifying_tasks(tmp_path, monkeypatch):
    interface, db = _make_interface(tmp_path, monkeypatch)
    db.save_upload_task({
        "task_id": "upload-1",
        "account_name": "alice",
        "file_name": "demo.bin",
        "file_size": 12,
        "local_path": str(tmp_path / "demo.bin"),
        "target_dir_id": 7,
        "status": "校验中",
        "progress": 34,
    })

    interface._TransferInterface__reload_upload_tasks()

    assert len(interface.upload_tasks) == 1
    assert interface.upload_tasks[0].status == "已暂停"
    stored = db.get_upload_task("upload-1")
    assert stored is not None
    assert stored["status"] == "已暂停"


def test_reload_download_tasks_marks_verifying_tasks_failed(tmp_path, monkeypatch):
    interface, db = _make_interface(tmp_path, monkeypatch)
    db.save_download_task({
        "resume_id": "download-1",
        "account_name": "alice",
        "file_name": "demo.bin",
        "file_size": 12,
        "file_id": 7,
        "save_path": str(tmp_path / "demo.bin"),
        "status": "校验中",
        "progress": 34,
    })

    interface._TransferInterface__reload_download_tasks()

    assert len(interface.download_tasks) == 1
    assert interface.download_tasks[0].status == "失败"
    assert interface.download_tasks[0].last_error == "下载中断，等待重试"
    assert interface._TransferInterface__active_download_count() == 0


def test_active_upload_count_includes_verifying_tasks(tmp_path, monkeypatch):
    interface, _db = _make_interface(tmp_path, monkeypatch)
    interface.upload_tasks = [
        type("_Task", (), {"status": "校验中"})(),
        type("_Task", (), {"status": "上传中"})(),
        type("_Task", (), {"status": "等待中"})(),
    ]

    assert interface._TransferInterface__active_upload_count() == 2


def test_active_upload_count_includes_starting_thread(tmp_path, monkeypatch):
    interface, _db = _make_interface(tmp_path, monkeypatch)
    interface.upload_tasks = [
        type("_Task", (), {"status": "等待中", "thread": object()})(),
        type("_Task", (), {"status": "等待中", "thread": None})(),
    ]

    assert interface._TransferInterface__active_upload_count() == 1


def test_try_start_pending_uploads_respects_thread_occupied_slots(tmp_path, monkeypatch):
    interface, db = _make_interface(tmp_path, monkeypatch)
    db.set_config("maxConcurrentUploads", 1)
    started = []

    class _Task:
        def __init__(self, name):
            self.file_name = name
            self.status = "等待中"
            self.thread = None
            self.delete_requested = False

    task_a = _Task("a")
    task_b = _Task("b")
    interface.upload_tasks = [task_a, task_b]

    def fake_start(task):
        started.append(task.file_name)
        task.thread = object()

    interface._TransferInterface__start_upload_task = fake_start

    TransferInterface._TransferInterface__try_start_pending_uploads(interface)

    assert started == ["a"]
    assert task_b.thread is None


def test_remove_active_upload_defers_delete_until_terminal_status(tmp_path, monkeypatch):
    interface, db = _make_interface(tmp_path, monkeypatch)
    task = interface.add_upload_task(
        "demo.bin",
        12,
        str(tmp_path / "demo.bin"),
        7,
    )
    task.thread = MagicMock()
    task_id = task.db_task_id

    interface._TransferInterface__remove_task(task, "upload")

    assert task in interface.upload_tasks
    assert task.delete_requested is True
    stored = db.get_upload_task(task_id)
    assert stored is not None
    assert stored["delete_requested"] == 1
    assert stored["status"] == "已取消"

    task.thread = None
    interface._TransferInterface__update_task_status(task, "已取消")

    assert task not in interface.upload_tasks
    assert task.db_task_id is None
    assert db.get_upload_task(task_id) is None


def test_remove_active_download_defers_delete_until_terminal_status(tmp_path, monkeypatch):
    interface, db = _make_interface(tmp_path, monkeypatch)
    task = interface.add_download_task(
        "demo.bin",
        12,
        99,
        str(tmp_path / "demo.bin"),
    )
    task.status = "下载中"
    thread = MagicMock()
    task.thread = thread
    interface.download_threads.append(thread)
    resume_id = task.resume_id

    interface._TransferInterface__remove_task(task, "download")

    assert task in interface.download_tasks
    assert task.delete_requested is True
    assert task.cleanup_on_cancel is True
    assert thread.cancel.call_count == 1
    assert interface._TransferInterface__active_download_count() == 1
    assert db.get_download_task(resume_id) is not None

    interface._TransferInterface__update_task_status(task, "已取消")

    assert task not in interface.download_tasks
    assert interface._TransferInterface__active_download_count() == 0
    assert db.get_download_task(resume_id) is None


def test_remove_active_download_persists_cancelled_status_before_thread_exit(tmp_path, monkeypatch):
    interface, db = _make_interface(tmp_path, monkeypatch)
    task = interface.add_download_task(
        "demo.bin",
        12,
        99,
        str(tmp_path / "demo.bin"),
    )
    task.status = "下载中"
    task.thread = MagicMock()

    interface._TransferInterface__remove_task(task, "download")

    stored = db.get_download_task(task.resume_id)
    assert stored is not None
    assert stored["status"] == "已取消"
    assert stored["error"] == "用户删除任务"


def test_start_download_task_clears_pause_and_cancel_flags(tmp_path, monkeypatch):
    interface, db = _make_interface(tmp_path, monkeypatch)
    interface.pan = object()
    task = DownloadTask(
        file_name="demo.bin",
        file_size=12,
        file_id=1,
        save_path=str(tmp_path / "demo.bin"),
        account_name="alice",
    )
    task.pause_requested = True
    task.is_cancelled = True

    created_threads = []

    class _FakeThread:
        def __init__(self, task_arg, pan_arg):
            created_threads.append((task_arg.pause_requested, task_arg.is_cancelled, pan_arg))
            self.progress_updated = MagicMock()
            self.status_updated = MagicMock()
            self.conn_info_updated = MagicMock()
            self.finished = MagicMock()
            self.error = MagicMock()

        def start(self):
            return None

    monkeypatch.setattr("src.app.view.transfer_interface.DownloadThread", _FakeThread)
    interface._ensure_speed_timer = lambda: None

    interface._TransferInterface__start_download_task(task)

    assert task.pause_requested is False
    assert task.is_cancelled is False
    assert created_threads == [(False, False, interface.pan)]
    assert task.thread is not None


def test_download_thread_cancel_closes_active_response(tmp_path):
    task = DownloadTask(
        file_name="demo.bin",
        file_size=12,
        file_id=1,
        save_path=str(tmp_path / "demo.bin"),
        account_name="alice",
    )
    active_response = MagicMock()
    task._active_response = active_response
    thread = DownloadThread(task, pan=MagicMock())

    thread.cancel()

    assert task.is_cancelled is True
    assert task.pause_requested is False
    active_response.close.assert_called_once()


def test_pause_non_resumable_download_resets_progress(tmp_path, monkeypatch):
    interface, db = _make_interface(tmp_path, monkeypatch)
    task = DownloadTask(
        file_name="demo.bin",
        file_size=12,
        file_id=1,
        save_path=str(tmp_path / "demo.bin"),
        account_name="alice",
    )
    task.status = "下载中"
    task.progress = 67
    task.supports_resume = False
    task.thread = MagicMock()
    db.save_download_task({
        "resume_id": task.resume_id,
        "account_name": "alice",
        "file_name": task.file_name,
        "file_id": task.file_id,
        "save_path": task.save_path,
        "status": "下载中",
        "progress": 67,
        "supports_resume": 0,
    })

    interface._TransferInterface__toggle_pause(task)

    assert task.progress == 0
    stored = db.get_download_task(task.resume_id)
    assert stored is not None
    assert stored["progress"] == 0


def test_retry_non_resumable_download_resets_progress(tmp_path, monkeypatch):
    interface, db = _make_interface(tmp_path, monkeypatch)
    task = DownloadTask(
        file_name="demo.bin",
        file_size=12,
        file_id=1,
        save_path=str(tmp_path / "demo.bin"),
        account_name="alice",
    )
    task.status = "已暂停"
    task.progress = 67
    task.supports_resume = False
    db.save_download_task({
        "resume_id": task.resume_id,
        "account_name": "alice",
        "file_name": task.file_name,
        "file_id": task.file_id,
        "save_path": task.save_path,
        "status": "已暂停",
        "progress": 67,
        "supports_resume": 0,
    })

    interface._TransferInterface__retry_download(task)

    assert task.progress == 0
    stored = db.get_download_task(task.resume_id)
    assert stored is not None
    assert stored["progress"] == 0


def test_try_start_pending_downloads_respects_auto_start_suppression(tmp_path, monkeypatch):
    interface, _db = _make_interface(tmp_path, monkeypatch)
    interface._auto_start_suppressed = True
    task = DownloadTask(
        file_name="demo.bin",
        file_size=12,
        file_id=1,
        save_path=str(tmp_path / "demo.bin"),
        account_name="alice",
    )
    task.status = "等待中"
    interface.download_tasks = [task]
    interface._TransferInterface__start_download_task = MagicMock()

    TransferInterface._TransferInterface__try_start_pending_downloads(interface)

    interface._TransferInterface__start_download_task.assert_not_called()


def test_resolve_download_detail_clears_old_parts_when_remote_version_changes(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    task = DownloadTask(
        file_name="demo.bin",
        file_size=12,
        file_id=1,
        save_path=str(tmp_path / "demo.bin"),
        current_dir_id=7,
        etag="old-etag-2",
        s3key_flag=False,
        account_name="alice",
    )
    db.save_download_task({
        "resume_id": task.resume_id,
        "account_name": "alice",
        "file_name": task.file_name,
        "file_size": 12,
        "file_id": task.file_id,
        "file_type": 0,
        "save_path": task.save_path,
        "current_dir_id": task.current_dir_id,
        "etag": "old-etag-2",
        "s3key_flag": 0,
        "status": "已暂停",
        "progress": 66,
    })
    db.record_download_part(task.resume_id, {
        "index": 0,
        "start": 0,
        "end": 11,
        "expected_size": 12,
        "actual_size": 12,
        "md5": "part-md5",
    })
    part_path = get_part_path(task.resume_id, 0)
    part_path.parent.mkdir(parents=True, exist_ok=True)
    part_path.write_bytes(b"old-part-data")
    merged_path = get_merged_path(task.resume_id)
    merged_path.write_bytes(b"old-merged-data")

    current_detail = {
        "FileId": task.file_id,
        "FileName": task.file_name,
        "Type": 0,
        "Size": 12,
        "Etag": "new-etag-2",
        "S3KeyFlag": False,
    }
    monkeypatch.setattr(
        "src.app.view.transfer_interface.resolve_download_file_detail",
        lambda *_args, **_kwargs: current_detail,
    )

    thread = DownloadThread(task, pan=MagicMock())
    resolved = thread._resolve_download_detail()

    assert resolved == current_detail
    assert task.progress == 0
    assert db.get_download_parts(task.resume_id) == []
    assert not part_path.exists()
    assert not merged_path.exists()
    stored = db.get_download_task(task.resume_id)
    assert stored is not None
    assert stored["progress"] == 0
    assert stored["etag"] == "new-etag-2"


def test_download_thread_refresh_url_uses_cooldown_for_failure_and_success(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    task = DownloadTask(
        file_name="demo.bin",
        file_size=12,
        file_id=1,
        save_path=str(tmp_path / "demo.bin"),
        account_name="alice",
    )
    file_detail = {
        "FileId": task.file_id,
        "FileName": task.file_name,
        "Type": 0,
        "Size": 12,
        "Etag": "etag",
        "S3KeyFlag": False,
    }
    monkeypatch.setattr(
        "src.app.view.transfer_interface.resolve_download_file_detail",
        lambda *_args, **_kwargs: file_detail,
    )
    pan = MagicMock()
    pan.link_by_fileDetail.side_effect = [
        "https://example.test/initial",
        403,
        "https://example.test/refreshed",
    ]
    refresh_callbacks = []

    def fake_stream_download_from_url(*_args, **kwargs):
        refresh_callbacks.append(kwargs["refresh_url_fn"])
        return "已取消"

    monkeypatch.setattr(
        "src.app.view.transfer_interface._stream_download_from_url",
        fake_stream_download_from_url,
    )
    now = [100.0]
    monkeypatch.setattr(
        "src.app.view.transfer_interface.time.monotonic",
        lambda: now[0],
    )

    thread = DownloadThread(task, pan=pan)
    thread.run()
    refresh_url = refresh_callbacks[0]

    assert refresh_url() is None
    assert refresh_url() is None
    assert pan.link_by_fileDetail.call_count == 2

    now[0] += 31
    assert refresh_url() == "https://example.test/refreshed"
    assert refresh_url() == "https://example.test/refreshed"
    assert pan.link_by_fileDetail.call_count == 3


def test_reload_download_tasks_drops_cancelled_records(tmp_path, monkeypatch):
    interface, db = _make_interface(tmp_path, monkeypatch)
    db.save_download_task({
        "resume_id": "resume-cancelled",
        "account_name": "alice",
        "file_name": "demo.bin",
        "file_id": 1,
        "save_path": str(tmp_path / "demo.bin"),
        "status": "已取消",
    })

    TransferInterface._TransferInterface__reload_download_tasks(interface)

    assert interface.download_tasks == []
    assert db.get_download_task("resume-cancelled") is None


def test_upload_task_error_persists_error_message(tmp_path, monkeypatch):
    monkeypatch.setattr("src.app.view.transfer_interface.InfoBar.error", lambda **_kwargs: None)
    interface, db = _make_interface(tmp_path, monkeypatch)
    task = interface.add_upload_task(
        "demo.bin",
        12,
        str(tmp_path / "demo.bin"),
        7,
    )

    interface._TransferInterface__task_error(task, "boom")

    stored = db.get_upload_task(task.db_task_id)
    assert stored is not None
    assert stored["status"] == "失败"
    assert stored["error"] == "boom"
    assert task.last_error == "boom"


def test_upload_progress_db_updates_are_throttled(tmp_path, monkeypatch):
    interface, db = _make_interface(tmp_path, monkeypatch)
    task = interface.add_upload_task(
        "demo.bin",
        12,
        str(tmp_path / "demo.bin"),
        7,
    )
    interface._TransferInterface__partial_refresh = lambda _task: None

    calls = []

    def fake_update(task_id, **fields):
        calls.append((task_id, fields))

    monkeypatch.setattr(db, "update_upload_task", fake_update)
    times = iter([100.0, 100.1, 100.2, 102.3])
    monkeypatch.setattr("src.app.view.transfer_interface.time.time", lambda: next(times))

    interface._TransferInterface__update_task_progress(task, 10)
    interface._TransferInterface__update_task_progress(task, 10)
    interface._TransferInterface__update_task_progress(task, 10)
    interface._TransferInterface__update_task_progress(task, 10)

    assert [fields["progress"] for _task_id, fields in calls] == [10, 10]


def test_upload_part_done_is_buffered_until_flush(tmp_path, monkeypatch):
    interface, db = _make_interface(tmp_path, monkeypatch)
    task = interface.add_upload_task(
        "demo.bin",
        12,
        str(tmp_path / "demo.bin"),
        7,
    )
    calls = []

    def fake_record(task_id, part_index, etag="", *, commit=True):
        calls.append((task_id, part_index, etag, commit))

    monkeypatch.setattr(db, "record_upload_part", fake_record)

    interface._TransferInterface__on_upload_part_done(task, 1, "etag-1")

    assert calls == []
    assert task._pending_upload_parts == [(1, "etag-1")]


def test_tick_speed_flushes_buffered_upload_parts(tmp_path, monkeypatch):
    interface, db = _make_interface(tmp_path, monkeypatch)
    task = interface.add_upload_task(
        "demo.bin",
        12,
        str(tmp_path / "demo.bin"),
        7,
    )
    task.status = "上传中"
    task._pending_upload_parts = [(1, "etag-1"), (2, "etag-2")]
    interface.download_tasks = []
    interface.uploadTable = type("_Table", (), {"rowCount": lambda self: 0})()
    interface._upload_batch_btns = {"speed": type("_Label", (), {"setText": lambda self, text: None})()}

    records = []
    flushes = []
    monkeypatch.setattr(
        db,
        "record_upload_part",
        lambda task_id, part_index, etag="", *, commit=True:
        records.append((task_id, part_index, etag, commit)),
    )
    monkeypatch.setattr(db, "flush", lambda: flushes.append(True))

    interface._TransferInterface__tick_speed()

    assert records == [
        (task.db_task_id, 1, "etag-1", False),
        (task.db_task_id, 2, "etag-2", False),
    ]
    assert flushes == [True]
    assert task._pending_upload_parts == []


def test_upload_terminal_status_flushes_buffered_parts(tmp_path, monkeypatch):
    interface, db = _make_interface(tmp_path, monkeypatch)
    task = interface.add_upload_task(
        "demo.bin",
        12,
        str(tmp_path / "demo.bin"),
        7,
    )
    task._pending_upload_parts = [(1, "etag-1")]
    records = []
    monkeypatch.setattr(
        db,
        "record_upload_part",
        lambda task_id, part_index, etag="", *, commit=True:
        records.append((task_id, part_index, etag, commit)),
    )
    monkeypatch.setattr(db, "flush", lambda: None)
    monkeypatch.setattr("src.app.view.transfer_interface.InfoBar.error", lambda **_kwargs: None)

    interface._TransferInterface__update_task_status(task, "失败")

    assert records == [(task.db_task_id, 1, "etag-1", False)]
    assert task._pending_upload_parts == []


def test_cancelled_upload_discards_buffered_parts(tmp_path, monkeypatch):
    interface, db = _make_interface(tmp_path, monkeypatch)
    task = interface.add_upload_task(
        "demo.bin",
        12,
        str(tmp_path / "demo.bin"),
        7,
    )
    task._pending_upload_parts = [(1, "etag-1")]
    records = []
    monkeypatch.setattr(
        db,
        "record_upload_part",
        lambda task_id, part_index, etag="", *, commit=True:
        records.append((task_id, part_index, etag, commit)),
    )

    interface._TransferInterface__update_task_status(task, "已取消")

    assert records == []
    assert task._pending_upload_parts == []


def test_download_task_error_keeps_thread_until_terminal_cleanup(tmp_path, monkeypatch):
    interface, db = _make_interface(tmp_path, monkeypatch)
    task = DownloadTask(
        file_name="demo.bin",
        file_size=12,
        file_id=1,
        save_path=str(tmp_path / "demo.bin"),
        account_name="alice",
    )
    db.save_download_task({
        "resume_id": task.resume_id,
        "account_name": "alice",
        "file_name": task.file_name,
        "file_id": task.file_id,
        "save_path": task.save_path,
    })

    class _FakeThread:
        def __init__(self):
            self.disconnected = False
            self.delete_later_called = False

        def disconnect(self):
            self.disconnected = True

        def deleteLater(self):
            self.delete_later_called = True

    thread = _FakeThread()
    task.thread = thread
    interface.download_threads.append(thread)
    monkeypatch.setattr("src.app.view.transfer_interface.InfoBar.error", lambda **_kwargs: None)

    interface._TransferInterface__task_error(task, "boom")

    assert task.thread is thread
    assert task.last_error == "boom"

    interface._TransferInterface__update_task_status(task, "失败")

    assert thread.disconnected is True
    assert thread.delete_later_called is True
    assert interface.download_threads == []
    stored = db.get_download_task(task.resume_id)
    assert stored is not None
    assert stored["status"] == "失败"
    assert stored["error"] == "boom"


def test_retry_upload_clears_stale_session_and_parts(tmp_path, monkeypatch):
    interface, db = _make_interface(tmp_path, monkeypatch)
    task = interface.add_upload_task(
        "demo.bin",
        12,
        str(tmp_path / "demo.bin"),
        7,
    )
    task.status = "失败"
    task.bucket = "bucket"
    task.storage_node = "node"
    task.upload_key = "key"
    task.upload_id_s3 = "upload-id"
    task.up_file_id = 123
    task.total_parts = 4
    task.block_size = 5
    task.etag = "etag"
    task.progress = 66
    db.update_upload_task(
        task.db_task_id,
        bucket=task.bucket,
        storage_node=task.storage_node,
        upload_key=task.upload_key,
        upload_id_s3=task.upload_id_s3,
        up_file_id=task.up_file_id,
        total_parts=task.total_parts,
        block_size=task.block_size,
        etag=task.etag,
        progress=task.progress,
        status="失败",
        error="boom",
    )
    db.record_upload_part(task.db_task_id, 1, "etag-1")

    interface._TransferInterface__retry_upload(task)

    stored = db.get_upload_task(task.db_task_id)
    assert stored is not None
    assert stored["status"] == "等待中"
    assert stored["progress"] == 66
    # P1-7: S3 session 有效时保留，复用已上传分片
    assert stored["bucket"] == "bucket"
    assert stored["upload_key"] == "key"
    assert stored["upload_id_s3"] == "upload-id"
    assert stored["error"] == ""


def test_reload_upload_tasks_drops_persisted_delete_requested_items(tmp_path, monkeypatch):
    interface, db = _make_interface(tmp_path, monkeypatch)
    db.save_upload_task({
        "task_id": "upload-1",
        "account_name": "alice",
        "file_name": "demo.bin",
        "file_size": 12,
        "local_path": str(tmp_path / "demo.bin"),
        "target_dir_id": 7,
        "status": "已取消",
        "delete_requested": 1,
    })

    interface._TransferInterface__reload_upload_tasks()

    assert interface.upload_tasks == []
    assert db.get_upload_task("upload-1") is None


def test_bind_button_disconnects_previous_handler_only():
    interface = TransferInterface.__new__(TransferInterface)
    button = _FakeButton()

    def first() -> None:
        """占位处理器：仅作为身份参与断言。"""

    def second() -> None:
        """占位处理器：仅作为身份参与断言。"""

    interface._TransferInterface__bind_button(button, first)
    interface._TransferInterface__bind_button(button, second)

    assert button.clicked.disconnected == [first]
    assert button.clicked.connected == [second]


def test_configure_upload_actions_disables_primary_button_without_receivers_call():
    interface = TransferInterface.__new__(TransferInterface)
    primary = _FakeButton()
    secondary = _FakeButton()
    primary._transfer_click_handler = lambda: None
    primary.receivers = MagicMock(side_effect=AssertionError("receivers should not be used"))
    widget = type(
        "_Widget",
        (),
        {"primary_button": primary, "secondary_button": secondary},
    )()
    task = type("_Task", (), {"status": "已完成"})()

    interface._TransferInterface__get_or_create_actions = lambda *_args: widget
    interface._TransferInterface__remove_task = lambda *_args: None
    interface.uploadTable = object()

    interface._TransferInterface__configure_upload_actions(0, task)

    assert primary.text == ""
    assert primary.enabled is False
    assert primary.clicked.disconnected


def test_upload_task_terminal_cleanup_calls_deleteLater(tmp_path, monkeypatch):
    """上传任务终态时 disconnect + deleteLater + 从 upload_threads 移除。"""
    interface, db = _make_interface(tmp_path, monkeypatch)
    task = interface.add_upload_task(
        "demo.bin",
        12,
        str(tmp_path / "demo.bin"),
        7,
    )

    class _FakeThread:
        def __init__(self):
            self.disconnected = False
            self.delete_later_called = False

        def disconnect(self):
            self.disconnected = True

        def deleteLater(self):
            self.delete_later_called = True

    thread = _FakeThread()
    task.thread = thread
    interface.upload_threads.append(thread)
    monkeypatch.setattr("src.app.view.transfer_interface.InfoBar.error", lambda **_kwargs: None)
    monkeypatch.setattr("src.app.view.transfer_interface.InfoBar.success", lambda **_kwargs: None)

    interface._TransferInterface__update_task_status(task, "已完成")

    assert thread.disconnected is True
    assert thread.delete_later_called is True
    assert task.thread is None
    assert interface.upload_threads == []


# ---------------------------------------------------------------------------
# 批次 5 追加：UI 构建、线程 run、批量操作、表格渲染与生命周期
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mute_infobar(monkeypatch):
    """InfoBar 通知统一 mock：headless 下无副作用，且各测试可断言调用。"""
    monkeypatch.setattr(tw_module, "InfoBar", MagicMock())


@pytest.fixture
def ti(qapp, temp_db):
    """真实构造 TransferInterface：数据库指向临时库、pan=None 不触网。"""
    return TransferInterface()


class _LiveSignal:
    """带 emit 的假信号：emit 时同步调用已连接处理器（模拟 DirectConnection）。"""

    def __init__(self):
        self.connected = []

    def connect(self, handler):
        self.connected.append(handler)

    def disconnect(self, handler=None):
        if handler is None:
            self.connected.clear()
        elif handler in self.connected:
            self.connected.remove(handler)

    def emit(self, *args):
        for handler in list(self.connected):
            handler(*args)


class _FakeThread:
    """线程替身：记录 start/deleteLater，信号可同步 emit。"""

    signal_names: tuple

    def __init__(self, task, pan):
        self.task = task
        self.pan = pan
        self.started = False
        self.delete_later_called = False
        for name in self.signal_names:
            setattr(self, name, _LiveSignal())

    def start(self):
        self.started = True

    def disconnect(self, *args):
        for name in self.signal_names:
            getattr(self, name).disconnect()

    def deleteLater(self):
        self.delete_later_called = True


class _FakeUploadThread(_FakeThread):
    signal_names = (
        "progress_updated", "status_updated", "finished", "error",
        "conn_info_updated", "session_info", "part_done",
    )


class _FakeDownloadThread(_FakeThread):
    signal_names = (
        "progress_updated", "status_updated", "finished", "error",
        "conn_info_updated",
    )


def _select_rows(table, rows):
    """以行选择模式选中表格指定行，并把当前行锚定在首行。"""
    if rows:
        table.setCurrentCell(rows[0], 0)
    for row in rows:
        index = table.model().index(row, 0)
        table.selectionModel().select(
            index,
            QItemSelectionModel.SelectionFlag.Select
            | QItemSelectionModel.SelectionFlag.Rows,
        )


def _download_detail(file_id=1, size=12, etag="e"):
    return {
        "FileId": file_id,
        "FileName": "demo.bin",
        "Type": 0,
        "Size": size,
        "Etag": etag,
        "S3KeyFlag": False,
    }


class TestFormatHelpers:
    @pytest.mark.parametrize("bps,expected", [
        (0, "--"),
        (-3.2, "--"),
        (512, "512 B/s"),
        (2048, "2.0 KB/s"),
        (5 * 1048576, "5.0 MB/s"),
        (2 * 1073741824, "2.0 GB/s"),
    ])
    def test_format_speed_branches(self, bps, expected):
        assert format_speed(bps) == expected

    @pytest.mark.parametrize("seconds,expected", [
        (0, "--"),
        (-1.0, "--"),
        (30.4, "30秒"),
        (90, "1分30秒"),
        (3661, "1时1分"),
    ])
    def test_format_eta_branches(self, seconds, expected):
        assert format_eta(seconds) == expected


class TestUploadTaskStoreGuards:
    @staticmethod
    def _make_task(tmp_path):
        return UploadTask("demo.bin", 12, str(tmp_path / "demo.bin"), 7)

    def test_update_progress_without_db_task_id_is_noop(self, tmp_path):
        task = self._make_task(tmp_path)
        _UploadTaskStore().update_progress(task, 5)
        assert task._last_db_progress == -1

    def test_update_status_without_db_task_id_is_noop(self, tmp_path):
        task = self._make_task(tmp_path)
        _UploadTaskStore().update_status(task, "失败", error="x", delete_requested=True)
        assert task.db_task_id is None

    def test_update_session_without_db_task_id_is_noop(self, tmp_path):
        task = self._make_task(tmp_path)
        _UploadTaskStore().update_session(task)

    def test_flush_parts_without_db_task_id_keeps_buffer(self, tmp_path):
        task = self._make_task(tmp_path)
        store = _UploadTaskStore()
        store.buffer_part(task, 1, "e1")
        store.flush_parts(task)
        assert task._pending_upload_parts == [(1, "e1")]

    def test_reset_session_clears_progress_when_requested(self, tmp_path):
        task = self._make_task(tmp_path)
        task.progress = 66
        _UploadTaskStore().reset_session(task, clear_progress=True)
        assert task.progress == 0
        assert task.bucket == ""
        assert task.upload_id_s3 == ""

    def test_reset_session_without_db_task_id_still_resets_fields(self, tmp_path):
        task = self._make_task(tmp_path)
        task.bucket = "bucket"
        task.total_parts = 4
        _UploadTaskStore().reset_session(task, clear_progress=False)
        assert task.bucket == ""
        assert task.total_parts == 0

    def test_delete_task_without_db_task_id_is_noop(self, tmp_path):
        task = self._make_task(tmp_path)
        _UploadTaskStore().delete_task(task)
        assert task.db_task_id is None


class TestUploadThreadRun:
    @staticmethod
    def _make_thread(tmp_path, pan):
        task = UploadTask("demo.bin", 12, str(tmp_path / "demo.bin"), 7)
        return UploadThread(task, pan), task

    @staticmethod
    def _recorder(thread):
        statuses: list[str] = []
        errors: list[str] = []
        progress: list[int] = []
        thread.status_updated.connect(statuses.append)
        thread.error.connect(errors.append)
        thread.progress_updated.connect(progress.append)
        return statuses, errors, progress

    def test_run_success_emits_progress_and_completed(self, tmp_path, temp_db):
        pan = MagicMock()
        pan.upload_file_stream.return_value = "ok"
        thread, task = self._make_thread(tmp_path, pan)
        statuses, errors, progress = self._recorder(thread)

        thread.run()

        assert statuses == ["上传中", "已完成"]
        assert progress == [100]
        assert errors == []
        kwargs = pan.upload_file_stream.call_args.kwargs
        assert kwargs["task"] is task
        assert kwargs["parent_id"] == 7
        assert kwargs["resume_info"] is None
        assert kwargs["speed_tracker"] is task.speed_tracker
        assert kwargs["file_name_override"] == "demo.bin"

    def test_run_exposes_signal_adapter_proxies(self, tmp_path, temp_db):
        pan = MagicMock()
        pan.upload_file_stream.return_value = "ok"
        thread, _task = self._make_thread(tmp_path, pan)
        statuses: list[str] = []
        thread.status_updated.connect(statuses.append)
        adapter_box = []

        original = pan.upload_file_stream

        def capture(*args, **kwargs):
            adapter_box.append(kwargs["signals"])
            return original(*args, **kwargs)

        pan.upload_file_stream.side_effect = capture
        thread.run()

        adapter = adapter_box[0]
        adapter.status.emit("probe")
        assert "probe" in statuses
        adapter.progress.emit(55)
        adapter.conn_info.emit(1, 4)
        adapter.part_done.emit(1, "e1")

    def test_run_cancelled_emits_cancelled_status(self, tmp_path, temp_db):
        pan = MagicMock()
        pan.upload_file_stream.return_value = "已取消"
        thread, _task = self._make_thread(tmp_path, pan)
        statuses, _errors, progress = self._recorder(thread)

        thread.run()

        assert statuses == ["上传中", "已取消"]
        assert progress == []

    def test_run_paused_emits_paused_status(self, tmp_path, temp_db):
        pan = MagicMock()
        pan.upload_file_stream.return_value = "已暂停"
        thread, _task = self._make_thread(tmp_path, pan)
        statuses, _errors, _progress = self._recorder(thread)

        thread.run()

        assert statuses == ["上传中", "已暂停"]

    def test_run_exception_emits_error_and_failed(self, tmp_path, temp_db):
        pan = MagicMock()
        pan.upload_file_stream.side_effect = RuntimeError("boom")
        thread, _task = self._make_thread(tmp_path, pan)
        statuses, errors, _progress = self._recorder(thread)

        thread.run()

        assert errors == ["boom"]
        assert statuses == ["上传中", "失败"]

    def test_run_tolerates_db_flush_failure(self, tmp_path, temp_db, monkeypatch):
        pan = MagicMock()
        pan.upload_file_stream.return_value = "ok"
        monkeypatch.setattr(
            temp_db, "flush",
            MagicMock(side_effect=RuntimeError("db busy")),
        )
        thread, _task = self._make_thread(tmp_path, pan)
        statuses, _errors, _progress = self._recorder(thread)

        thread.run()

        assert statuses == ["上传中", "已完成"]

    def test_run_builds_resume_info_from_db_parts(self, tmp_path, temp_db):
        task = UploadTask("demo.bin", 12, str(tmp_path / "demo.bin"), 7)
        _UploadTaskStore().create_task(task, "alice")
        task.bucket = "bucket"
        task.storage_node = "node"
        task.upload_key = "key"
        task.upload_id_s3 = "upload-id"
        task.up_file_id = 5
        task.total_parts = 8
        task.block_size = 6
        task.etag = "etag"
        task.file_mtime = 9.5
        temp_db.record_upload_part(task.db_task_id, 2, "e2")
        pan = MagicMock()
        pan.upload_file_stream.return_value = "ok"
        thread = UploadThread(task, pan)
        statuses, _errors, _progress = self._recorder(thread)

        thread.run()

        kwargs = pan.upload_file_stream.call_args.kwargs
        resume_info = kwargs["resume_info"]
        assert resume_info["done_parts"] == {2}
        assert resume_info["bucket"] == "bucket"
        assert resume_info["upload_id"] == "upload-id"
        assert resume_info["block_size"] == 6
        assert resume_info["up_file_id"] == 5
        assert statuses == ["上传中", "已完成"]

    def test_build_resume_info_returns_none_without_session(self, tmp_path):
        _task = UploadTask("demo.bin", 12, str(tmp_path / "demo.bin"), 7)
        thread = UploadThread(_task, MagicMock())
        assert thread._build_resume_info() is None

    def test_pause_and_cancel_toggle_request_flags(self, tmp_path):
        _task = UploadTask("demo.bin", 12, str(tmp_path / "demo.bin"), 7)
        thread = UploadThread(_task, MagicMock())

        thread.pause()
        assert _task.pause_requested is True

        thread.cancel()
        assert _task.is_cancelled is True
        assert _task.pause_requested is False


class TestDownloadThreadRun:
    @staticmethod
    def _make_task(tmp_path):
        return DownloadTask(
            "demo.bin", 12, 1, str(tmp_path / "demo.bin"), account_name="alice",
        )

    def test_run_success_emits_completed(self, tmp_path, temp_db, monkeypatch):
        monkeypatch.setattr(
            "src.app.view.transfer_interface.resolve_download_file_detail",
            lambda *_args, **_kwargs: _download_detail(),
        )
        pan = MagicMock()
        pan.link_by_fileDetail.return_value = "https://example.test/f"
        captured = {}

        def fake_stream(url, path, **kwargs):
            captured["url"] = url
            captured["path"] = path
            captured.update(kwargs)
            return "ok"

        monkeypatch.setattr(
            "src.app.view.transfer_interface._stream_download_from_url", fake_stream,
        )
        task = self._make_task(tmp_path)
        thread = DownloadThread(task, pan)
        statuses: list[str] = []
        progress: list[int] = []
        conn: list[tuple[int, int]] = []
        thread.status_updated.connect(statuses.append)
        thread.progress_updated.connect(progress.append)
        thread.conn_info_updated.connect(lambda a, m: conn.append((a, m)))

        thread.run()

        assert statuses == ["已完成"]
        assert progress == [100]
        assert captured["url"] == "https://example.test/f"
        assert captured["path"] == Path(task.save_path)
        assert captured["overwrite"] is True
        assert captured["resume_task"] is task
        assert captured["speed_tracker"] is task.speed_tracker
        captured["signals"].status.emit("probe")
        captured["signals"].conn_info.emit(2, 8)
        assert conn == [(2, 8)]

    def test_run_paused_stops_without_progress(self, tmp_path, temp_db, monkeypatch):
        monkeypatch.setattr(
            "src.app.view.transfer_interface.resolve_download_file_detail",
            lambda *_args, **_kwargs: _download_detail(),
        )
        pan = MagicMock()
        pan.link_by_fileDetail.return_value = "https://example.test/f"
        monkeypatch.setattr(
            "src.app.view.transfer_interface._stream_download_from_url",
            lambda *_args, **_kwargs: "已暂停",
        )
        task = self._make_task(tmp_path)
        thread = DownloadThread(task, pan)
        statuses: list[str] = []
        progress: list[int] = []
        thread.status_updated.connect(statuses.append)
        thread.progress_updated.connect(progress.append)

        thread.run()

        assert statuses == ["已暂停"]
        assert progress == []

    def test_run_link_return_code_raises_error(self, tmp_path, temp_db, monkeypatch):
        monkeypatch.setattr(
            "src.app.view.transfer_interface.resolve_download_file_detail",
            lambda *_args, **_kwargs: _download_detail(),
        )
        pan = MagicMock()
        pan.link_by_fileDetail.return_value = 401
        stream_mock = MagicMock()
        monkeypatch.setattr(
            "src.app.view.transfer_interface._stream_download_from_url", stream_mock,
        )
        task = self._make_task(tmp_path)
        thread = DownloadThread(task, pan)
        statuses: list[str] = []
        errors: list[str] = []
        thread.status_updated.connect(statuses.append)
        thread.error.connect(errors.append)

        thread.run()

        assert errors == ["获取下载链接失败，返回码: 401"]
        assert statuses == ["失败"]
        stream_mock.assert_not_called()

    def test_run_stream_exception_emits_error(self, tmp_path, temp_db, monkeypatch):
        monkeypatch.setattr(
            "src.app.view.transfer_interface.resolve_download_file_detail",
            lambda *_args, **_kwargs: _download_detail(),
        )
        pan = MagicMock()
        pan.link_by_fileDetail.return_value = "https://example.test/f"

        def broken_stream(*_args, **_kwargs):
            raise ValueError("disk full")

        monkeypatch.setattr(
            "src.app.view.transfer_interface._stream_download_from_url", broken_stream,
        )
        task = self._make_task(tmp_path)
        thread = DownloadThread(task, pan)
        statuses: list[str] = []
        errors: list[str] = []
        thread.status_updated.connect(statuses.append)
        thread.error.connect(errors.append)

        thread.run()

        assert errors == ["disk full"]
        assert statuses == ["失败"]

    def test_pause_sets_pause_requested(self, tmp_path):
        task = self._make_task(tmp_path)
        DownloadThread(task, MagicMock()).pause()
        assert task.pause_requested is True

    def test_cancel_tolerates_response_close_failure(self, tmp_path):
        task = self._make_task(tmp_path)
        response = MagicMock()
        response.close.side_effect = RuntimeError("already closed")
        task._active_response = response

        DownloadThread(task, MagicMock()).cancel()

        assert task.is_cancelled is True
        assert task.pause_requested is False
        response.close.assert_called_once()


class TestInterfaceConstruction:
    def test_init_builds_tables_toolbars_and_header(self, ti):
        assert ti.uploadTable.columnCount() == 8
        assert ti.downloadTable.columnCount() == 8
        assert ti.uploadFrame.isHidden() is False
        assert ti.downloadFrame.isHidden() is True
        assert ti.openDownloadFolderButton.isHidden() is True
        assert ti.downloadFilterLabel.isHidden() is True
        assert set(ti._upload_batch_btns) == {
            "select_all", "invert", "pause", "resume", "delete", "count", "speed",
        }
        assert ti._upload_batch_btns["count"].text() == "已选 0 项"
        assert ti._upload_batch_btns["speed"].text() == "总速度: --"
        header = ti.uploadTable.horizontalHeader()
        assert header.sectionResizeMode(0) == QHeaderView.ResizeMode.Stretch
        assert header.sectionResizeMode(1) == QHeaderView.ResizeMode.ResizeToContents

    def test_set_pan_reloads_tasks_for_account(self, ti, temp_db):
        temp_db.save_upload_task({
            "task_id": "upload-bob",
            "account_name": "bob",
            "file_name": "bob.bin",
            "file_size": 12,
            "local_path": "/tmp/bob.bin",
            "target_dir_id": 7,
            "status": "已暂停",
        })
        pan = MagicMock()
        pan.user_name = "bob"

        ti.set_pan(pan)

        assert ti.pan is pan
        assert ti.current_account_name == "bob"
        assert [t.file_name for t in ti.upload_tasks] == ["bob.bin"]

    def test_set_pan_same_account_skips_reload(self, ti, temp_db):
        pan = MagicMock()
        pan.user_name = "alice"

        ti.set_pan(pan)
        ti.upload_tasks.append("sentinel")
        ti.set_pan(pan)

        assert ti.upload_tasks == ["sentinel"]

    def test_set_pan_force_reloads_same_account(self, ti, temp_db):
        pan = MagicMock()
        pan.user_name = "alice"

        ti.set_pan(pan)
        ti.upload_tasks.append("sentinel")
        ti.set_pan(pan, force=True)

        assert ti.upload_tasks == []

    def test_suspend_and_resume_auto_start(self, ti):
        ti.suspend_auto_start()
        assert ti._auto_start_suppressed is True
        ti.resume_auto_start()
        assert ti._auto_start_suppressed is False


class TestViewSwitching:
    def test_on_segment_changed_toggles_frames_and_filters(self, ti):
        ti._TransferInterface__onSegmentChanged("download")
        assert ti.uploadFrame.isHidden() is True
        assert ti.downloadFrame.isHidden() is False
        assert ti.uploadFilterLabel.isHidden() is True
        assert ti.downloadFilterLabel.isHidden() is False
        assert ti.openDownloadFolderButton.isHidden() is False

        ti._TransferInterface__onSegmentChanged("upload")
        assert ti.uploadFrame.isHidden() is False
        assert ti.downloadFrame.isHidden() is True
        assert ti.uploadFilterLabel.isHidden() is False
        assert ti.downloadFilterLabel.isHidden() is True
        assert ti.openDownloadFolderButton.isHidden() is True

    def test_on_upload_filter_changed_updates_table(self, ti, temp_db, tmp_path):
        first = ti.add_upload_task("a.bin", 1, str(tmp_path / "a.bin"), 1)
        first.status = "失败"
        ti.add_upload_task("b.bin", 1, str(tmp_path / "b.bin"), 1)

        ti._TransferInterface__onUploadFilterChanged("失败")

        assert ti.upload_status_filter == "失败"
        assert ti.uploadTable.rowCount() == 1
        assert ti.uploadTable.item(0, COL_NAME).text() == "a.bin"

        ti._TransferInterface__onUploadFilterChanged("全部")
        assert ti.uploadTable.rowCount() == 2

    def test_on_download_filter_changed_updates_table(self, ti, temp_db, tmp_path):
        first = ti.add_download_task("a.bin", 1, 1, str(tmp_path / "a.bin"))
        first.status = "失败"
        ti.add_download_task("b.bin", 1, 2, str(tmp_path / "b.bin"))

        ti._TransferInterface__onDownloadFilterChanged("失败")

        assert ti.download_status_filter == "失败"
        assert ti.downloadTable.rowCount() == 1

        ti._TransferInterface__onDownloadFilterChanged("全部")
        assert ti.downloadTable.rowCount() == 2

    def test_open_download_folder_opens_existing_folder(self, ti, temp_db, tmp_path, monkeypatch):
        temp_db.set_config("defaultDownloadPath", str(tmp_path))
        open_url = MagicMock()
        monkeypatch.setattr(tw_module, "QDesktopServices", MagicMock(openUrl=open_url))

        ti._TransferInterface__open_download_folder()

        open_url.assert_called_once()
        assert open_url.call_args.args[0].toLocalFile() == str(tmp_path)

    def test_open_download_folder_uses_selected_task_path(
        self, ti, temp_db, tmp_path, monkeypatch,
    ):
        folder = tmp_path / "custom"
        folder.mkdir()
        task = DownloadTask(
            "a.bin", 1, 1, str(folder / "a.bin"), account_name="alice",
        )
        ti.download_tasks = [task]
        ti._TransferInterface__update_download_table()
        _select_rows(ti.downloadTable, [0])
        open_url = MagicMock()
        monkeypatch.setattr(tw_module, "QDesktopServices", MagicMock(openUrl=open_url))

        ti._TransferInterface__open_download_folder()

        assert open_url.call_args.args[0].toLocalFile() == str(folder)

    def test_open_download_folder_missing_shows_error(self, ti, temp_db, tmp_path, monkeypatch):
        temp_db.set_config("defaultDownloadPath", str(tmp_path / "nope"))
        open_url = MagicMock()
        monkeypatch.setattr(tw_module, "QDesktopServices", MagicMock(openUrl=open_url))

        ti._TransferInterface__open_download_folder()

        open_url.assert_not_called()
        tw_module.InfoBar.error.assert_called_once()


class TestConcurrencyLimits:
    def test_max_concurrent_downloads_clamped_to_range(self, ti, temp_db):
        assert ti._TransferInterface__max_concurrent_downloads() == 5
        temp_db.set_config("maxConcurrentDownloads", 99)
        assert ti._TransferInterface__max_concurrent_downloads() == 5
        temp_db.set_config("maxConcurrentDownloads", 0)
        assert ti._TransferInterface__max_concurrent_downloads() == 1

    def test_try_start_pending_downloads_starts_until_limit(self, ti, temp_db):
        temp_db.set_config("maxConcurrentDownloads", 1)
        started = []

        class _Task:
            def __init__(self, name):
                self.file_name = name
                self.status = "等待中"
                self.thread = None

        task_a, task_b = _Task("a"), _Task("b")
        ti.download_tasks = [task_a, task_b]

        def fake_start(task):
            started.append(task.file_name)
            task.thread = object()

        ti._TransferInterface__start_download_task = fake_start
        ti._TransferInterface__try_start_pending_downloads()

        assert started == ["a"]
        assert task_b.thread is None

    def test_try_start_pending_downloads_breaks_when_slots_full(self, ti, temp_db):
        temp_db.set_config("maxConcurrentDownloads", 1)
        ti._TransferInterface__start_download_task = MagicMock()

        class _Task:
            def __init__(self, name, status):
                self.file_name = name
                self.status = status
                self.thread = None

        ti.download_tasks = [_Task("busy", "下载中"), _Task("next", "等待中")]

        ti._TransferInterface__try_start_pending_downloads()

        ti._TransferInterface__start_download_task.assert_not_called()

    def test_try_start_pending_downloads_suppressed_in_batch_rebuild(self, ti):
        ti._batch_rebuild_suppressed = True
        ti._TransferInterface__start_download_task = MagicMock()

        ti._TransferInterface__try_start_pending_downloads()

        ti._TransferInterface__start_download_task.assert_not_called()

    def test_try_start_pending_uploads_suppressed_in_batch_rebuild(self, ti):
        ti._batch_rebuild_suppressed = True
        ti._TransferInterface__start_upload_task = MagicMock()

        ti._TransferInterface__try_start_pending_uploads()

        ti._TransferInterface__start_upload_task.assert_not_called()

    def test_try_start_pending_uploads_suppressed_by_auto_start(self, ti):
        ti._auto_start_suppressed = True
        ti._TransferInterface__start_upload_task = MagicMock()

        ti._TransferInterface__try_start_pending_uploads()

        ti._TransferInterface__start_upload_task.assert_not_called()

    def test_try_start_pending_uploads_skips_delete_requested(self, ti, temp_db):
        started = []

        class _Task:
            def __init__(self, name, delete_requested=False):
                self.file_name = name
                self.status = "等待中"
                self.thread = None
                self.delete_requested = delete_requested

        task_a, task_b = _Task("a", delete_requested=True), _Task("b")
        ti.upload_tasks = [task_a, task_b]

        def fake_start(task):
            started.append(task.file_name)
            task.thread = object()

        ti._TransferInterface__start_upload_task = fake_start
        ti._TransferInterface__try_start_pending_uploads()

        assert started == ["b"]


class TestAddAndStartTasks:
    def test_add_upload_task_dedup_returns_existing(self, ti, temp_db, tmp_path):
        path = str(tmp_path / "a.bin")
        first = ti.add_upload_task("a.bin", 1, path, 7)
        again = ti.add_upload_task("a.bin", 1, path, 7)

        assert again is first
        assert len(ti.upload_tasks) == 1

    def test_add_upload_task_dedup_ignores_completed(self, ti, temp_db, tmp_path):
        path = str(tmp_path / "a.bin")
        first = ti.add_upload_task("a.bin", 1, path, 7)
        first.status = "已完成"
        second = ti.add_upload_task("a.bin", 1, path, 7)

        assert second is not first
        assert len(ti.upload_tasks) == 2

    def test_add_upload_task_with_pan_starts_thread(self, ti, temp_db, tmp_path, monkeypatch):
        monkeypatch.setattr(tw_module, "UploadThread", _FakeUploadThread)
        ti.pan = object()

        task = ti.add_upload_task("a.bin", 1, str(tmp_path / "a.bin"), 7)

        thread = task.thread
        assert isinstance(thread, _FakeUploadThread)
        assert thread.started is True
        assert thread.pan is ti.pan
        assert thread.task is task
        assert len(thread.status_updated.connected) == 1
        assert len(thread.session_info.connected) == 1
        assert len(thread.part_done.connected) == 1
        assert ti._speed_timer.isActive()

    def test_start_upload_task_skips_without_pan(self, ti, tmp_path):
        task = UploadTask("a.bin", 1, str(tmp_path / "a.bin"), 7)

        ti._TransferInterface__start_upload_task(task)

        assert task.thread is None

    def test_start_upload_task_skips_when_delete_requested(self, ti, tmp_path):
        ti.pan = object()
        task = UploadTask("a.bin", 1, str(tmp_path / "a.bin"), 7)
        task.delete_requested = True

        ti._TransferInterface__start_upload_task(task)

        assert task.thread is None

    def test_start_download_task_skips_without_pan(self, ti, tmp_path):
        task = DownloadTask(
            "a.bin", 1, 1, str(tmp_path / "a.bin"), account_name="alice",
        )

        ti._TransferInterface__start_download_task(task)

        assert task.thread is None

    def test_start_download_task_skips_when_thread_running(self, ti, tmp_path):
        ti.pan = object()
        task = DownloadTask(
            "a.bin", 1, 1, str(tmp_path / "a.bin"), account_name="alice",
        )
        task.thread = MagicMock()

        ti._TransferInterface__start_download_task(task)

        assert not isinstance(task.thread, _FakeDownloadThread)

    def test_start_download_task_marks_legacy_metadata_failed(self, ti, temp_db, tmp_path):
        ti.pan = object()
        task = DownloadTask(
            "a.bin", 1, 1, str(tmp_path / "a.bin"),
            account_name="alice", resume_metadata_valid=False,
        )
        task.status = "等待中"
        temp_db.save_download_task({
            "resume_id": task.resume_id,
            "account_name": "alice",
            "file_name": task.file_name,
            "file_id": task.file_id,
            "save_path": task.save_path,
            "status": "等待中",
        })
        ti.download_tasks = [task]

        ti._TransferInterface__start_download_task(task)

        assert task.status == "失败"
        assert task.thread is None
        assert task.last_error == LEGACY_RESUME_TASK_ERROR
        stored = temp_db.get_download_task(task.resume_id)
        assert stored is not None
        assert stored["status"] == "失败"
        tw_module.InfoBar.error.assert_called_once()

    def test_add_download_task_returns_existing_for_same_resume(self, ti, temp_db, tmp_path):
        path = str(tmp_path / "a.bin")
        first = ti.add_download_task("a.bin", 1, 9, path)
        again = ti.add_download_task("a.bin", 1, 9, path)

        assert again is first
        assert len(ti.download_tasks) == 1

    def test_setup_transfer_header_tolerates_missing_header(self, ti):
        table = type("_Table", (), {"horizontalHeader": lambda self: None})()

        ti._TransferInterface__setup_transfer_header(table)


class TestTaskLifecycleSignals:
    def test_upload_thread_signals_drive_task_state(self, ti, temp_db, tmp_path, monkeypatch):
        monkeypatch.setattr(tw_module, "UploadThread", _FakeUploadThread)
        ti.pan = object()

        task = ti.add_upload_task("a.bin", 100, str(tmp_path / "a.bin"), 1)
        thread = task.thread
        assert isinstance(thread, _FakeUploadThread)

        thread.progress_updated.emit(50)
        assert task.progress == 50

        thread.conn_info_updated.emit(2, 4)
        assert task.active_workers == 2
        assert task.max_workers == 4

        thread.status_updated.emit("已完成")

        assert task.status == "已完成"
        assert task.thread is None
        assert thread.delete_later_called is True
        assert ti.upload_threads == []
        assert temp_db.get_upload_task(task.db_task_id) is None
        tw_module.InfoBar.success.assert_called_once()

    def test_download_thread_signals_drive_task_state(self, ti, temp_db, tmp_path, monkeypatch):
        monkeypatch.setattr(tw_module, "DownloadThread", _FakeDownloadThread)
        ti.pan = object()

        task = ti.add_download_task("d.bin", 100, 11, str(tmp_path / "d.bin"))
        thread = task.thread
        assert isinstance(thread, _FakeDownloadThread)

        thread.progress_updated.emit(30)
        assert task.progress == 30

        thread.status_updated.emit("下载中")
        assert task.status == "下载中"
        stored = temp_db.get_download_task(task.resume_id)
        assert stored is not None
        assert stored["status"] == "下载中"

        thread.error.emit("boom")
        assert task.last_error == "boom"
        assert task.status == "失败"
        assert task.thread is not None

        thread.status_updated.emit("失败")

        assert task.thread is None
        assert thread.delete_later_called is True
        assert ti.download_threads == []
        stored = temp_db.get_download_task(task.resume_id)
        assert stored is not None
        assert stored["status"] == "失败"
        assert stored["error"] == "boom"


class TestProgressStatusSlots:
    def test_update_task_progress_download_throttles_db_writes(self, ti, temp_db, tmp_path, monkeypatch):
        task = ti.add_download_task("d.bin", 100, 3, str(tmp_path / "d.bin"))
        ti._TransferInterface__partial_refresh = lambda _task: None
        calls = []

        def fake_update(resume_id, **fields):
            calls.append((resume_id, fields))

        monkeypatch.setattr(temp_db, "update_download_task", fake_update)
        times = iter([100.0, 100.1, 100.2, 103.0])
        monkeypatch.setattr(tw_module.time, "time", lambda: next(times))

        for _ in range(4):
            ti._TransferInterface__update_task_progress(task, 10)

        assert [fields["progress"] for _rid, fields in calls] == [10, 10]

    def test_update_task_conn_info_updates_cells(self, ti, temp_db, tmp_path):
        task = DownloadTask(
            "d.bin", 100, 3, str(tmp_path / "d.bin"), account_name="alice",
        )
        ti.download_tasks = [task]
        ti._TransferInterface__update_download_table()

        ti._TransferInterface__update_task_conn_info(task, 2, 4)

        assert task.active_workers == 2
        assert task.max_workers == 4
        assert ti.downloadTable.item(0, COL_CONN).text() == "2/4"

    def test_update_task_status_swallows_disconnect_type_error(self, ti, temp_db, tmp_path):
        task = DownloadTask(
            "d.bin", 100, 3, str(tmp_path / "d.bin"), account_name="alice",
        )
        temp_db.save_download_task({
            "resume_id": task.resume_id,
            "account_name": "alice",
            "file_name": task.file_name,
            "file_id": task.file_id,
            "save_path": task.save_path,
        })

        class _Thread:
            def __init__(self):
                self.deleted = False

            def disconnect(self):
                raise TypeError("disconnect failed")

            def deleteLater(self):
                self.deleted = True

        thread = _Thread()
        task.thread = thread
        ti.download_tasks.append(task)

        ti._TransferInterface__update_task_status(task, "失败")

        assert thread.deleted is True
        assert task.thread is None

    def test_update_task_status_non_resumable_paused_resets_progress(self, ti, temp_db, tmp_path):
        task = DownloadTask(
            "d.bin", 100, 3, str(tmp_path / "d.bin"), account_name="alice",
        )
        temp_db.save_download_task({
            "resume_id": task.resume_id,
            "account_name": "alice",
            "file_name": task.file_name,
            "file_id": task.file_id,
            "save_path": task.save_path,
            "status": "下载中",
            "progress": 40,
        })
        task.supports_resume = False
        task.progress = 40
        task.status = "下载中"

        class _Thread:
            def disconnect(self):
                pass

            def deleteLater(self):
                pass

        task.thread = _Thread()

        ti._TransferInterface__update_task_status(task, "已暂停")

        assert task.progress == 0
        stored = temp_db.get_download_task(task.resume_id)
        assert stored is not None
        assert stored["progress"] == 0
        assert stored["status"] == "已暂停"

    def test_download_supports_resume_falls_back_to_db_record(self, ti, temp_db):
        task_missing = type("_Task", (), {"resume_id": "resume-missing"})()
        assert ti._TransferInterface__download_supports_resume(task_missing) is False

        temp_db.save_download_task({
            "resume_id": "resume-9",
            "account_name": "alice",
            "file_name": "x.bin",
            "file_id": 9,
            "save_path": "/tmp/x.bin",
            "supports_resume": 1,
        })
        task_with_id = type("_Task", (), {"resume_id": "resume-9"})()
        assert ti._TransferInterface__download_supports_resume(task_with_id) is True


class TestTickSpeed:
    def test_ensure_speed_timer_is_idempotent(self, ti):
        ti._ensure_speed_timer()
        timer = ti._speed_timer
        ti._ensure_speed_timer()

        assert ti._speed_timer is timer
        assert timer.interval() == 1000
        assert timer.isActive()

    def test_tick_speed_updates_active_rows_and_totals(self, ti, temp_db, tmp_path):
        up = ti.add_upload_task("u.bin", 1000, str(tmp_path / "u.bin"), 1)
        up.status = "上传中"
        down = ti.add_download_task("d.bin", 1000, 3, str(tmp_path / "d.bin"))
        down.status = "下载中"
        up.speed_tracker = MagicMock(
            speed=MagicMock(return_value=2048.0), eta=MagicMock(return_value=9.0),
        )
        down.speed_tracker = MagicMock(
            speed=MagicMock(return_value=512.0), eta=MagicMock(return_value=-1.0),
        )
        ti._ensure_speed_timer()

        ti._TransferInterface__tick_speed()

        assert up.speed_bps == 2048.0
        assert up.eta_seconds == 9.0
        assert down.speed_bps == 512.0
        assert down.eta_seconds == -1.0
        assert ti.uploadTable.item(0, COL_SPEED).text() == "2.0 KB/s"
        assert ti.uploadTable.item(0, COL_ETA).text() == "9秒"
        assert ti.downloadTable.item(0, COL_SPEED).text() == "512 B/s"
        assert ti._upload_batch_btns["speed"].text() == f"总速度: {format_speed(2048.0)}"
        assert ti._download_batch_btns["speed"].text() == f"总速度: {format_speed(512.0)}"
        assert ti._speed_timer.isActive()

    def test_tick_speed_resets_idle_speed_rows(self, ti, temp_db, tmp_path):
        up = ti.add_upload_task("u.bin", 1000, str(tmp_path / "u.bin"), 1)
        up.status = "已暂停"
        up.speed_bps = 500.0
        down = ti.add_download_task("d.bin", 1000, 3, str(tmp_path / "d.bin"))
        down.status = "失败"
        down.speed_bps = 300.0
        ti._ensure_speed_timer()

        ti._TransferInterface__tick_speed()

        assert up.speed_bps == 0.0
        assert up.eta_seconds == -1.0
        assert down.speed_bps == 0.0
        assert down.eta_seconds == -1.0
        assert ti.uploadTable.item(0, COL_SPEED).text() == "--"
        assert ti.downloadTable.item(0, COL_ETA).text() == "--"

    def test_tick_speed_stops_timer_without_active_tasks(self, ti, temp_db, tmp_path):
        ti.add_upload_task("u.bin", 1000, str(tmp_path / "u.bin"), 1)
        ti._ensure_speed_timer()

        ti._TransferInterface__tick_speed()

        assert ti._speed_timer.isActive() is False


class TestUploadSessionSlots:
    def test_on_upload_session_info_persists_fields(self, ti, temp_db, tmp_path):
        task = ti.add_upload_task("u.bin", 100, str(tmp_path / "u.bin"), 1)
        info = {
            "bucket": "bucket", "storage_node": "node", "upload_key": "key",
            "upload_id": "upload-id", "up_file_id": 3, "total_parts": 9,
            "block_size": 7, "etag": "etag", "file_mtime": 12.5,
        }

        ti._TransferInterface__on_upload_session_info(task, info)

        assert task.bucket == "bucket"
        assert task.storage_node == "node"
        assert task.upload_key == "key"
        assert task.upload_id_s3 == "upload-id"
        assert task.up_file_id == 3
        assert task.total_parts == 9
        assert task.block_size == 7
        assert task.etag == "etag"
        assert task.file_mtime == 12.5
        stored = temp_db.get_upload_task(task.db_task_id)
        assert stored is not None
        assert stored["upload_id_s3"] == "upload-id"
        assert stored["total_parts"] == 9

    def test_on_upload_part_done_buffers_part(self, ti, temp_db, tmp_path):
        task = ti.add_upload_task("u.bin", 100, str(tmp_path / "u.bin"), 1)

        ti._TransferInterface__on_upload_part_done(task, 1, "e1")

        assert task._pending_upload_parts == [(1, "e1")]
        assert temp_db.get_upload_parts(task.db_task_id) == []

    def test_flush_upload_parts_records_buffered_parts(self, ti, temp_db, tmp_path):
        task = ti.add_upload_task("u.bin", 100, str(tmp_path / "u.bin"), 1)
        task._pending_upload_parts = [(1, "e1"), (2, "e2")]

        ti._TransferInterface__flush_upload_parts(task)

        assert [r["part_index"] for r in temp_db.get_upload_parts(task.db_task_id)] == [1, 2]
        assert task._pending_upload_parts == []

    def test_flush_upload_parts_ignores_download_task(self, ti, temp_db, tmp_path):
        task = DownloadTask(
            "d.bin", 100, 3, str(tmp_path / "d.bin"), account_name="alice",
        )

        ti._TransferInterface__flush_upload_parts(task)

    def test_reset_upload_session_clears_db_session(self, ti, temp_db, tmp_path):
        task = ti.add_upload_task("u.bin", 100, str(tmp_path / "u.bin"), 1)
        task.bucket = "bucket"
        task.upload_id_s3 = "upload-id"
        task.progress = 40
        temp_db.update_upload_task(
            task.db_task_id, bucket="bucket", upload_id_s3="upload-id", progress=40,
        )
        temp_db.record_upload_part(task.db_task_id, 1, "e1")

        ti._TransferInterface__reset_upload_session(task, clear_progress=True)

        stored = temp_db.get_upload_task(task.db_task_id)
        assert stored is not None
        assert stored["bucket"] == ""
        assert stored["upload_id_s3"] == ""
        assert stored["progress"] == 0
        assert temp_db.get_upload_parts(task.db_task_id) == []
        assert task.progress == 0


class TestTaskFinished:
    def test_finish_guard_prevents_reentry_for_download(self, ti, temp_db, tmp_path):
        task = ti.add_download_task("d.bin", 100, 3, str(tmp_path / "d.bin"))
        temp_db.save_download_task({
            "resume_id": task.resume_id,
            "account_name": "alice",
            "file_name": task.file_name,
            "file_id": task.file_id,
            "save_path": task.save_path,
        })
        task.status = "已完成"

        ti._TransferInterface__task_finished(task, "download")
        ti._TransferInterface__task_finished(task, "download")

        assert temp_db.get_download_task(task.resume_id) is None
        tw_module.InfoBar.success.assert_not_called()

    def test_upload_completed_records_duration_and_cleans_record(self, ti, temp_db, tmp_path):
        task = ti.add_upload_task("u.bin", 100, str(tmp_path / "u.bin"), 1)
        task.status = "已完成"
        task.start_time = time.monotonic() - 10

        ti._TransferInterface__task_finished(task, "upload")

        assert 9.0 < task.finish_duration < 12.0
        assert task.finish_avg_speed > 0
        assert temp_db.get_upload_task(task.db_task_id) is None
        tw_module.InfoBar.success.assert_called_once()

    def test_upload_finished_with_delete_requested_returns_early(self, ti, temp_db, tmp_path):
        task = ti.add_upload_task("u.bin", 100, str(tmp_path / "u.bin"), 1)
        task.status = "已完成"
        task.delete_requested = True
        task.start_time = time.monotonic() - 10

        ti._TransferInterface__task_finished(task, "upload")

        assert task.finish_duration > 0
        assert temp_db.get_upload_task(task.db_task_id) is not None
        tw_module.InfoBar.success.assert_not_called()

    def test_upload_failed_returns_early_without_cleanup(self, ti, temp_db, tmp_path):
        task = ti.add_upload_task("u.bin", 100, str(tmp_path / "u.bin"), 1)
        task.status = "失败"

        ti._TransferInterface__task_finished(task, "upload")

        assert task.finish_duration == -1.0
        assert temp_db.get_upload_task(task.db_task_id) is not None
        tw_module.InfoBar.success.assert_not_called()


class TestUploadPauseAndRetry:
    def test_toggle_pause_upload_resumes_paused_task(self, ti, temp_db, tmp_path):
        task = ti.add_upload_task("u.bin", 100, str(tmp_path / "u.bin"), 1)
        task.status = "已暂停"
        ti._TransferInterface__start_upload_task = MagicMock()

        ti._TransferInterface__toggle_pause_upload(task)

        assert task.status == "等待中"
        ti._TransferInterface__start_upload_task.assert_called_once()

    def test_toggle_pause_upload_skips_when_thread_exiting(self, ti, temp_db, tmp_path):
        task = ti.add_upload_task("u.bin", 100, str(tmp_path / "u.bin"), 1)
        task.status = "已暂停"
        task.thread = MagicMock()

        ti._TransferInterface__toggle_pause_upload(task)

        assert task.status == "已暂停"

    def test_toggle_pause_upload_pauses_active_task(self, ti, temp_db, tmp_path):
        task = ti.add_upload_task("u.bin", 100, str(tmp_path / "u.bin"), 1)
        task.status = "上传中"
        thread = MagicMock()
        task.thread = thread
        task.active_workers = 3
        task.max_workers = 4

        ti._TransferInterface__toggle_pause_upload(task)

        thread.pause.assert_called_once()
        assert task.status == "已暂停"
        assert task.active_workers == 0
        assert task.max_workers == 0
        stored = temp_db.get_upload_task(task.db_task_id)
        assert stored is not None
        assert stored["status"] == "已暂停"

    def test_toggle_pause_upload_ignores_task_without_thread(self, ti, temp_db, tmp_path):
        task = ti.add_upload_task("u.bin", 100, str(tmp_path / "u.bin"), 1)
        task.status = "等待中"

        ti._TransferInterface__toggle_pause_upload(task)

        assert task.status == "等待中"

    def test_retry_upload_skips_when_thread_active(self, ti, temp_db, tmp_path):
        task = ti.add_upload_task("u.bin", 100, str(tmp_path / "u.bin"), 1)
        task.status = "失败"
        task.thread = MagicMock()

        ti._TransferInterface__retry_upload(task)

        assert task.status == "失败"

    def test_retry_upload_resets_invalid_session(self, ti, temp_db, tmp_path):
        task = ti.add_upload_task("u.bin", 100, str(tmp_path / "u.bin"), 1)
        task.status = "失败"
        temp_db.update_upload_task(task.db_task_id, bucket="", upload_id_s3="")
        temp_db.record_upload_part(task.db_task_id, 1, "e1")

        ti._TransferInterface__retry_upload(task)

        assert task.status == "等待中"
        stored = temp_db.get_upload_task(task.db_task_id)
        assert stored is not None
        assert stored["status"] == "等待中"
        assert stored["error"] == ""
        assert stored["upload_id_s3"] == ""
        assert temp_db.get_upload_parts(task.db_task_id) == []


class TestDownloadPauseAndRetry:
    def _save_task(self, db, task, **extra):
        db.save_download_task({
            "resume_id": task.resume_id,
            "account_name": task.account_name,
            "file_name": task.file_name,
            "file_id": task.file_id,
            "save_path": task.save_path,
            **extra,
        })

    def test_toggle_pause_resumes_paused_task(self, ti, temp_db, tmp_path, monkeypatch):
        task = DownloadTask(
            "d.bin", 100, 3, str(tmp_path / "d.bin"), account_name="alice",
        )
        self._save_task(temp_db, task, status="已暂停", progress=40)
        task.status = "已暂停"
        task.progress = 40
        ti.download_tasks = [task]
        started = []
        monkeypatch.setattr(
            ti, "_TransferInterface__start_download_task",
            lambda _task: started.append(_task),
        )

        ti._TransferInterface__toggle_pause(task)

        assert task.status == "等待中"
        assert task.progress == 40
        assert started == [task]
        # 注意：恢复分支不回写 db 状态，等线程启动后由 status_updated 落库
        stored = temp_db.get_download_task(task.resume_id)
        assert stored is not None
        assert stored["progress"] == 40

    def test_toggle_pause_resume_resets_progress_for_non_resumable(self, ti, temp_db, tmp_path):
        task = DownloadTask(
            "d.bin", 100, 3, str(tmp_path / "d.bin"), account_name="alice",
        )
        self._save_task(temp_db, task, status="已暂停", progress=40)
        task.status = "已暂停"
        task.progress = 40
        task.supports_resume = False

        ti._TransferInterface__toggle_pause(task)

        assert task.progress == 0
        stored = temp_db.get_download_task(task.resume_id)
        assert stored is not None
        assert stored["progress"] == 0

    def test_toggle_pause_skips_when_thread_exiting(self, ti, temp_db, tmp_path):
        task = DownloadTask(
            "d.bin", 100, 3, str(tmp_path / "d.bin"), account_name="alice",
        )
        task.status = "已暂停"
        task.thread = MagicMock()

        ti._TransferInterface__toggle_pause(task)

        assert task.status == "已暂停"

    def test_toggle_pause_ignores_task_without_thread(self, ti, temp_db, tmp_path):
        task = DownloadTask(
            "d.bin", 100, 3, str(tmp_path / "d.bin"), account_name="alice",
        )
        task.status = "等待中"

        ti._TransferInterface__toggle_pause(task)

        assert task.status == "等待中"

    def test_retry_download_skips_when_thread_active(self, ti, temp_db, tmp_path):
        task = DownloadTask(
            "d.bin", 100, 3, str(tmp_path / "d.bin"), account_name="alice",
        )
        task.status = "失败"
        task.thread = MagicMock()

        ti._TransferInterface__retry_download(task)

        assert task.status == "失败"

    def test_retry_download_marks_legacy_metadata_failed(self, ti, temp_db, tmp_path):
        task = DownloadTask(
            "d.bin", 100, 3, str(tmp_path / "d.bin"),
            account_name="alice", resume_metadata_valid=False,
        )
        task.status = "失败"

        ti._TransferInterface__retry_download(task)

        assert task.status == "失败"
        assert task.last_error == LEGACY_RESUME_TASK_ERROR
        tw_module.InfoBar.error.assert_called_once()

    def test_retry_download_keeps_progress_when_resumable(self, ti, temp_db, tmp_path):
        task = DownloadTask(
            "d.bin", 100, 3, str(tmp_path / "d.bin"), account_name="alice",
        )
        self._save_task(temp_db, task, status="失败", progress=40)
        task.status = "失败"
        task.progress = 40

        ti._TransferInterface__retry_download(task)

        assert task.status == "等待中"
        assert task.progress == 40
        stored = temp_db.get_download_task(task.resume_id)
        assert stored is not None
        assert stored["status"] == "等待中"
        assert stored["progress"] == 40


class TestRemoveTask:
    def test_remove_idle_upload_deletes_record_and_list_entry(self, ti, temp_db, tmp_path):
        task = ti.add_upload_task("u.bin", 100, str(tmp_path / "u.bin"), 1)
        ti._TransferInterface__try_start_pending_uploads = MagicMock()

        ti._TransferInterface__remove_task(task, "upload")

        assert task not in ti.upload_tasks
        assert temp_db.get_upload_task(task.db_task_id) is None
        ti._TransferInterface__try_start_pending_uploads.assert_called_once()

    def test_remove_upload_without_db_id_removes_from_list(self, ti, temp_db, tmp_path):
        task = UploadTask("u.bin", 100, str(tmp_path / "u.bin"), 1)
        ti.upload_tasks = [task]

        ti._TransferInterface__remove_task(task, "upload")

        assert ti.upload_tasks == []

    def test_remove_idle_download_deletes_record_and_temp_dir(self, ti, temp_db, tmp_path, monkeypatch):
        cleanup_calls = []
        monkeypatch.setattr(
            tw_module, "cleanup_temp_dir", lambda resume_id: cleanup_calls.append(resume_id),
        )
        task = ti.add_download_task("d.bin", 100, 3, str(tmp_path / "d.bin"))
        ti._TransferInterface__try_start_pending_downloads = MagicMock()

        ti._TransferInterface__remove_task(task, "download")

        assert task not in ti.download_tasks
        assert temp_db.get_download_task(task.resume_id) is None
        assert cleanup_calls == [task.resume_id]
        ti._TransferInterface__try_start_pending_downloads.assert_called_once()

    def test_remove_failed_download_with_thread_deletes_record(self, ti, temp_db, tmp_path, monkeypatch):
        cleanup_calls = []
        monkeypatch.setattr(
            tw_module, "cleanup_temp_dir", lambda resume_id: cleanup_calls.append(resume_id),
        )
        task = ti.add_download_task("d.bin", 100, 3, str(tmp_path / "d.bin"))
        task.status = "失败"
        task.thread = MagicMock()

        ti._TransferInterface__remove_task(task, "download")

        assert task.thread.cancel.call_count == 0
        assert task not in ti.download_tasks
        assert temp_db.get_download_task(task.resume_id) is None
        assert cleanup_calls == [task.resume_id]


class TestBatchOperations:
    def test_update_batch_bar_shows_selection_count(self, ti, temp_db, tmp_path):
        ti.add_upload_task("a.bin", 1, str(tmp_path / "a.bin"), 1)
        ti.add_upload_task("b.bin", 1, str(tmp_path / "b.bin"), 1)
        _select_rows(ti.uploadTable, [0, 1])

        ti._TransferInterface__update_batch_bar(ti._upload_batch_btns, ti.uploadTable)

        assert ti._upload_batch_btns["count"].text() == "已选 2 项"

    def test_get_selected_tasks_filters_out_of_range_rows(self, temp_db):
        class _Index:
            def __init__(self, row):
                self._row = row

            def row(self):
                return self._row

        table = type(
            "_Table",
            (),
            {"selectionModel": lambda self: type(
                "_Selection", (), {"selectedRows": lambda self: [_Index(0), _Index(9)]},
            )()},
        )()
        tasks = [UploadTask("a.bin", 1, "/tmp/a.bin", 1)]

        selected = TransferInterface._TransferInterface__get_selected_tasks(table, tasks)

        assert selected == tasks

    def test_select_all_selects_every_row(self, ti, temp_db, tmp_path):
        ti.add_upload_task("a.bin", 1, str(tmp_path / "a.bin"), 1)
        ti.add_upload_task("b.bin", 1, str(tmp_path / "b.bin"), 1)

        ti._TransferInterface__select_all(ti.uploadTable, ti.upload_tasks)

        assert len(ti.uploadTable.selectionModel().selectedRows()) == 2

    def test_invert_selection_flips_selected_rows(self, ti, temp_db, tmp_path):
        ti.add_upload_task("a.bin", 1, str(tmp_path / "a.bin"), 1)
        ti.add_upload_task("b.bin", 1, str(tmp_path / "b.bin"), 1)
        _select_rows(ti.uploadTable, [0])

        ti._TransferInterface__invert_selection(ti.uploadTable, ti.upload_tasks)

        selected = {idx.row() for idx in ti.uploadTable.selectionModel().selectedRows()}
        assert selected == {1}

    def test_batch_pause_without_selection_warns(self, ti, temp_db, tmp_path):
        ti.add_upload_task("a.bin", 1, str(tmp_path / "a.bin"), 1)

        ti._TransferInterface__batch_pause(ti.uploadTable, ti.upload_tasks, "upload")

        tw_module.InfoBar.warning.assert_called_once()

    def test_batch_pause_upload_pauses_active_tasks(self, ti, temp_db, tmp_path):
        task = ti.add_upload_task("a.bin", 1, str(tmp_path / "a.bin"), 1)
        task.status = "上传中"
        thread = MagicMock()
        task.thread = thread
        _select_rows(ti.uploadTable, [0])

        ti._TransferInterface__batch_pause(ti.uploadTable, ti.upload_tasks, "upload")

        thread.pause.assert_called_once()
        assert task.status == "已暂停"
        tw_module.InfoBar.success.assert_called_once()

    def test_batch_pause_download_pauses_active_tasks(self, ti, temp_db, tmp_path):
        task = ti.add_download_task("a.bin", 1, 1, str(tmp_path / "a.bin"))
        task.status = "下载中"
        thread = MagicMock()
        task.thread = thread
        _select_rows(ti.downloadTable, [0])

        ti._TransferInterface__batch_pause(ti.downloadTable, ti.download_tasks, "download")

        thread.pause.assert_called_once()
        assert task.status == "已暂停"
        tw_module.InfoBar.success.assert_called_once()

    def test_batch_pause_without_pausable_tasks_is_silent(self, ti, temp_db, tmp_path):
        ti.add_upload_task("a.bin", 1, str(tmp_path / "a.bin"), 1)
        _select_rows(ti.uploadTable, [0])

        ti._TransferInterface__batch_pause(ti.uploadTable, ti.upload_tasks, "upload")

        tw_module.InfoBar.success.assert_not_called()
        tw_module.InfoBar.warning.assert_not_called()

    def test_batch_resume_upload_paused_creates_task_when_missing_db_id(
        self, ti, temp_db, tmp_path,
    ):
        task = UploadTask("a.bin", 1, str(tmp_path / "a.bin"), 1)
        task.status = "已暂停"
        ti.upload_tasks = [task]
        ti._TransferInterface__update_upload_table()
        _select_rows(ti.uploadTable, [0])

        ti._TransferInterface__batch_resume(ti.uploadTable, ti.upload_tasks, "upload")

        assert task.status == "等待中"
        assert task.db_task_id
        stored = temp_db.get_upload_task(task.db_task_id)
        assert stored is not None
        assert stored["status"] == "等待中"
        tw_module.InfoBar.success.assert_called_once()

    def test_batch_resume_upload_paused_updates_existing_db_record(
        self, ti, temp_db, tmp_path,
    ):
        task = ti.add_upload_task("a.bin", 1, str(tmp_path / "a.bin"), 1)
        task.status = "已暂停"
        _select_rows(ti.uploadTable, [0])

        ti._TransferInterface__batch_resume(ti.uploadTable, ti.upload_tasks, "upload")

        assert task.status == "等待中"
        stored = temp_db.get_upload_task(task.db_task_id)
        assert stored is not None
        assert stored["status"] == "等待中"
        tw_module.InfoBar.success.assert_called_once()

    def test_batch_resume_upload_failed_retries_task(self, ti, temp_db, tmp_path):
        task = ti.add_upload_task("a.bin", 1, str(tmp_path / "a.bin"), 1)
        task.status = "失败"
        _select_rows(ti.uploadTable, [0])

        ti._TransferInterface__batch_resume(ti.uploadTable, ti.upload_tasks, "upload")

        assert task.status == "等待中"
        tw_module.InfoBar.success.assert_called_once()

    def test_batch_resume_download_paused_updates_db(self, ti, temp_db, tmp_path):
        task = ti.add_download_task("a.bin", 1, 1, str(tmp_path / "a.bin"))
        task.status = "已暂停"
        _select_rows(ti.downloadTable, [0])

        ti._TransferInterface__batch_resume(ti.downloadTable, ti.download_tasks, "download")

        assert task.status == "等待中"
        stored = temp_db.get_download_task(task.resume_id)
        assert stored is not None
        assert stored["status"] == "等待中"
        tw_module.InfoBar.success.assert_called_once()

    def test_batch_resume_download_failed_retries_task(self, ti, temp_db, tmp_path):
        task = ti.add_download_task("a.bin", 1, 1, str(tmp_path / "a.bin"))
        task.status = "失败"
        _select_rows(ti.downloadTable, [0])

        ti._TransferInterface__batch_resume(ti.downloadTable, ti.download_tasks, "download")

        assert task.status == "等待中"
        tw_module.InfoBar.success.assert_called_once()

    def test_batch_resume_without_selection_warns(self, ti, temp_db, tmp_path):
        ti.add_upload_task("a.bin", 1, str(tmp_path / "a.bin"), 1)

        ti._TransferInterface__batch_resume(ti.uploadTable, ti.upload_tasks, "upload")

        tw_module.InfoBar.warning.assert_called_once()

    def test_batch_resume_without_resumable_tasks_is_silent(self, ti, temp_db, tmp_path):
        task = ti.add_download_task("a.bin", 1, 1, str(tmp_path / "a.bin"))
        task.status = "下载中"
        task.thread = MagicMock()
        _select_rows(ti.downloadTable, [0])

        ti._TransferInterface__batch_resume(ti.downloadTable, ti.download_tasks, "download")

        tw_module.InfoBar.success.assert_not_called()
        tw_module.InfoBar.warning.assert_not_called()

    def test_batch_delete_confirmed_removes_upload_tasks(self, ti, temp_db, tmp_path, monkeypatch):
        monkeypatch.setattr(tw_module, "MessageBox", MagicMock())
        tw_module.MessageBox.return_value.exec.return_value = 1
        task = ti.add_upload_task("a.bin", 1, str(tmp_path / "a.bin"), 1)
        _select_rows(ti.uploadTable, [0])

        ti._TransferInterface__batch_delete(ti.uploadTable, ti.upload_tasks, "upload")

        assert task not in ti.upload_tasks
        assert temp_db.get_upload_task(task.db_task_id) is None
        assert ti._batch_rebuild_suppressed is False
        tw_module.InfoBar.success.assert_called_once()
        tw_module.MessageBox.assert_called_once()

    def test_batch_delete_confirmed_removes_download_tasks(self, ti, temp_db, tmp_path, monkeypatch):
        monkeypatch.setattr(tw_module, "MessageBox", MagicMock())
        tw_module.MessageBox.return_value.exec.return_value = 1
        task = ti.add_download_task("a.bin", 1, 1, str(tmp_path / "a.bin"))
        _select_rows(ti.downloadTable, [0])

        ti._TransferInterface__batch_delete(ti.downloadTable, ti.download_tasks, "download")

        assert task not in ti.download_tasks
        assert temp_db.get_download_task(task.resume_id) is None
        assert ti._batch_rebuild_suppressed is False
        tw_module.InfoBar.success.assert_called_once()

    def test_batch_delete_aborted_keeps_tasks(self, ti, temp_db, tmp_path, monkeypatch):
        monkeypatch.setattr(tw_module, "MessageBox", MagicMock())
        tw_module.MessageBox.return_value.exec.return_value = 0
        task = ti.add_upload_task("a.bin", 1, str(tmp_path / "a.bin"), 1)
        _select_rows(ti.uploadTable, [0])

        ti._TransferInterface__batch_delete(ti.uploadTable, ti.upload_tasks, "upload")

        assert task in ti.upload_tasks
        assert temp_db.get_upload_task(task.db_task_id) is not None
        assert getattr(ti, "_batch_rebuild_suppressed", False) is False

    def test_batch_delete_without_selection_warns(self, ti, temp_db, tmp_path, monkeypatch):
        monkeypatch.setattr(tw_module, "MessageBox", MagicMock())
        ti.add_upload_task("a.bin", 1, str(tmp_path / "a.bin"), 1)

        ti._TransferInterface__batch_delete(ti.uploadTable, ti.upload_tasks, "upload")

        tw_module.InfoBar.warning.assert_called_once()
        tw_module.MessageBox.assert_not_called()


class TestTableHelpers:
    def test_set_table_item_text_creates_updates_and_skips_same(self, ti):
        table = ti.uploadTable
        table.setRowCount(1)

        item = ti._TransferInterface__set_table_item_text(table, 0, 0, "a", Qt.AlignmentFlag.AlignCenter)
        assert item.text() == "a"
        assert item.textAlignment() & Qt.AlignmentFlag.AlignHorizontal_Mask

        same = ti._TransferInterface__set_table_item_text(table, 0, 0, "a")
        assert same is item

        ti._TransferInterface__set_table_item_text(table, 0, 0, "b")
        assert table.item(0, 0).text() == "b"

    def test_clear_button_handler_silences_disconnect_errors(self):
        interface = TransferInterface.__new__(TransferInterface)

        class _ExplodingSignal(_FakeSignal):
            def disconnect(self, handler):
                raise RuntimeError("C++ object deleted")

        button = _FakeButton()
        button.clicked = _ExplodingSignal()

        interface._TransferInterface__bind_button(button, lambda: None)
        interface._TransferInterface__clear_button_handler(button)

        assert getattr(button, BUTTON_CLICK_HANDLER_ATTR) is None

    def test_get_or_create_actions_reuses_widget(self, ti):
        ti.uploadTable.setRowCount(1)

        first = ti._TransferInterface__get_or_create_actions(ti.uploadTable, 0, COL_ACTION)
        second = ti._TransferInterface__get_or_create_actions(ti.uploadTable, 0, COL_ACTION)

        assert first is second
        assert hasattr(first, "primary_button")
        assert hasattr(first, "secondary_button")

    def test_find_task_row_returns_negative_when_missing(self, ti):
        assert ti._TransferInterface__find_task_row(object(), []) == -1

    def test_refresh_task_cells_skips_invalid_rows(self, ti, temp_db, tmp_path):
        task = UploadTask("u.bin", 100, str(tmp_path / "u.bin"), 1)

        ti._TransferInterface__refresh_task_cells(ti.uploadTable, -1, task)
        ti._TransferInterface__refresh_task_cells(ti.uploadTable, 5, task)

    def test_refresh_task_cells_shows_finish_summary_for_completed(self, ti, temp_db, tmp_path):
        task = DownloadTask(
            "d.bin", 100, 3, str(tmp_path / "d.bin"), account_name="alice",
        )
        task.status = "已完成"
        task.progress = 100
        task.finish_avg_speed = 2048.0
        task.finish_duration = 90
        ti.download_tasks = [task]
        ti._TransferInterface__update_download_table()

        ti._TransferInterface__refresh_task_cells(ti.downloadTable, 0, task)

        assert ti.downloadTable.item(0, COL_SPEED).text() == "均速 2.0 KB/s"
        assert ti.downloadTable.item(0, COL_ETA).text() == "耗时 1分30秒"

    def test_partial_refresh_skips_filtered_out_task(self, ti, temp_db, tmp_path):
        task = ti.add_upload_task("a.bin", 1, str(tmp_path / "a.bin"), 1)
        task.status = "失败"
        ti._TransferInterface__onUploadFilterChanged("已完成")

        ti._TransferInterface__partial_refresh(task)


class TestSelectionPersistence:
    def test_update_table_restores_selected_upload_row(self, ti, temp_db, tmp_path):
        first = ti.add_upload_task("a.bin", 1, str(tmp_path / "a.bin"), 1)
        ti.add_upload_task("b.bin", 1, str(tmp_path / "b.bin"), 1)
        _select_rows(ti.uploadTable, [0])

        ti._TransferInterface__update_upload_table()

        selected = {idx.row() for idx in ti.uploadTable.selectionModel().selectedRows()}
        assert selected == {0}
        assert first.db_task_id

    def test_update_table_restores_selected_download_row(self, ti, temp_db, tmp_path):
        first = ti.add_download_task("a.bin", 1, 1, str(tmp_path / "a.bin"))
        ti.add_download_task("b.bin", 1, 2, str(tmp_path / "b.bin"))
        _select_rows(ti.downloadTable, [1])
        saved = ti._TransferInterface__save_selection(ti.downloadTable)

        assert saved == {ti.download_tasks[1].resume_id}
        ti._TransferInterface__update_download_table()

        selected = {idx.row() for idx in ti.downloadTable.selectionModel().selectedRows()}
        assert selected == {1}
        assert first.resume_id


class TestTableUpdate:
    def test_update_upload_table_renders_columns_and_buttons(self, ti, temp_db, tmp_path):
        active = ti.add_upload_task("a.bin", 100, str(tmp_path / "a.bin"), 1)
        active.status = "上传中"
        active.thread = MagicMock()
        paused = ti.add_upload_task("b.bin", 200, str(tmp_path / "b.bin"), 1)
        paused.status = "已暂停"
        failed = ti.add_upload_task("c.bin", 300, str(tmp_path / "c.bin"), 1)
        failed.status = "失败"
        ti.add_upload_task("d.bin", 400, str(tmp_path / "d.bin"), 1)

        ti._TransferInterface__update_upload_table()

        assert ti.uploadTable.rowCount() == 4
        assert ti.uploadTable.item(0, COL_NAME).text() == "a.bin"
        assert ti.uploadTable.item(0, COL_SIZE).text() == format_file_size(100)
        assert ti.uploadTable.item(0, COL_PERCENT).text() == "0%"
        assert ti.uploadTable.item(0, COL_STATUS).text() == "上传中"
        assert ti.uploadTable.item(0, COL_CONN).text() == "-"
        row0 = ti.uploadTable.cellWidget(0, 7)
        assert row0.primary_button.text() == "暂停"
        assert row0.primary_button.isEnabled()
        assert row0.secondary_button.text() == "取消"
        row1 = ti.uploadTable.cellWidget(1, 7)
        assert row1.primary_button.text() == "继续"
        row2 = ti.uploadTable.cellWidget(2, 7)
        assert row2.primary_button.text() == "重试"
        assert row2.secondary_button.text() == "删除"
        row3 = ti.uploadTable.cellWidget(3, 7)
        assert row3.primary_button.text() == ""
        assert not row3.primary_button.isEnabled()
        assert row3.secondary_button.text() == "删除"

    def test_update_upload_table_shows_unknown_name_placeholder(self, ti, temp_db, tmp_path):
        ti.add_upload_task("", 100, str(tmp_path / "anon.bin"), 1)

        ti._TransferInterface__update_upload_table()

        assert ti.uploadTable.item(0, COL_NAME).text() == "(未知)"

    def test_update_download_table_renders_columns_and_buttons(self, ti, temp_db, tmp_path):
        active = ti.add_download_task("a.bin", 100, 1, str(tmp_path / "a.bin"))
        active.status = "下载中"
        active.thread = MagicMock()
        paused = ti.add_download_task("b.bin", 200, 2, str(tmp_path / "b.bin"))
        paused.status = "已暂停"
        failed = ti.add_download_task("c.bin", 300, 3, str(tmp_path / "c.bin"))
        failed.status = "失败"
        cancelled = ti.add_download_task("d.bin", 400, 4, str(tmp_path / "d.bin"))
        cancelled.status = "已取消"

        ti._TransferInterface__update_download_table()

        assert ti.downloadTable.rowCount() == 4
        assert ti.downloadTable.item(0, COL_SIZE).text() == format_file_size(100)
        assert ti.downloadTable.item(0, COL_STATUS).text() == "下载中"
        row0 = ti.downloadTable.cellWidget(0, 7)
        assert row0.primary_button.text() == "暂停"
        assert row0.secondary_button.text() == "取消"
        row1 = ti.downloadTable.cellWidget(1, 7)
        assert row1.primary_button.text() == "继续"
        row2 = ti.downloadTable.cellWidget(2, 7)
        assert row2.primary_button.text() == "重试"
        row3 = ti.downloadTable.cellWidget(3, 7)
        assert row3.primary_button.text() == "已取消"
        assert not row3.primary_button.isEnabled()
        assert row3.secondary_button.text() == "删除"

    def test_update_tables_skip_when_batch_rebuild_suppressed(self, ti, temp_db, tmp_path):
        ti._batch_rebuild_suppressed = True
        ti.add_upload_task("a.bin", 1, str(tmp_path / "a.bin"), 1)
        ti.add_download_task("a.bin", 1, 1, str(tmp_path / "a.bin"))

        assert ti.uploadTable.rowCount() == 0
        assert ti.downloadTable.rowCount() == 0


class TestReloadTasks:
    def test_reload_download_tasks_marks_legacy_metadata_failed(self, ti, temp_db):
        ti.current_account_name = "alice"
        temp_db.save_download_task({
            "resume_id": "resume-legacy",
            "account_name": "alice",
            "file_name": "demo.bin",
            "file_id": 1,
            "save_path": "/tmp/demo.bin",
            "status": "校验中",
            "metadata_version": 1,
        })

        ti._TransferInterface__reload_download_tasks()

        assert len(ti.download_tasks) == 1
        task = ti.download_tasks[0]
        assert task.status == "失败"
        assert task.last_error == LEGACY_RESUME_TASK_ERROR
        assert task.resume_metadata_valid is False
        stored = temp_db.get_download_task("resume-legacy")
        assert stored is not None
        assert stored["status"] == "失败"
        assert stored["error"] == LEGACY_RESUME_TASK_ERROR

    def test_reload_upload_tasks_drops_completed_records(self, ti, temp_db):
        ti.current_account_name = "alice"
        temp_db.save_upload_task({
            "task_id": "upload-done",
            "account_name": "alice",
            "file_name": "demo.bin",
            "file_size": 12,
            "local_path": "/tmp/demo.bin",
            "target_dir_id": 7,
            "status": "已完成",
        })

        ti._TransferInterface__reload_upload_tasks()

        assert ti.upload_tasks == []
        assert temp_db.get_upload_task("upload-done") is None
