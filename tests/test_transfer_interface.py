from pathlib import Path
from unittest.mock import MagicMock

from src.app.common import database as database_module
from src.app.common.database import Database
from src.app.common.download_resume import get_merged_path, get_part_path
from src.app.view.transfer_interface import DownloadTask, DownloadThread, TransferInterface, UploadTask


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
    interface.current_account_name = "alice"
    interface.pan = None
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

    first = lambda: None
    second = lambda: None

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
