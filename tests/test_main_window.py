import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.modules.setdefault("qrcode", MagicMock())

from src.app.view.main_window import MainWindow, QDialog


def _make_transfer(
    upload_threads=None,
    download_threads=None,
    upload_tasks=None,
    download_tasks=None,
):
    return type(
        "_Transfer",
        (),
        {
            "upload_threads": upload_threads or [],
            "download_threads": download_threads or [],
            "upload_tasks": upload_tasks or [],
            "download_tasks": download_tasks or [],
        },
    )()


def test_stop_all_transfers_uses_thread_lists():
    upload_thread = MagicMock()
    upload_thread.isRunning.return_value = False
    download_thread = MagicMock()
    download_thread.isRunning.return_value = False
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = _make_transfer(
        upload_threads=[upload_thread],
        download_threads=[download_thread],
    )

    MainWindow._stop_all_transfers(window)

    upload_thread.cancel.assert_called_once()
    download_thread.cancel.assert_called_once()
    upload_thread.wait.assert_called()
    download_thread.wait.assert_called()


def test_stop_all_transfers_save_progress_calls_pause():
    thread = MagicMock()
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = _make_transfer(upload_threads=[thread])

    MainWindow._stop_all_transfers(window, save_progress=True)

    thread.pause.assert_called_once()
    thread.cancel.assert_not_called()


def test_stop_all_transfers_default_calls_cancel():
    thread = MagicMock()
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = _make_transfer(upload_threads=[thread])

    MainWindow._stop_all_transfers(window)

    thread.cancel.assert_called_once()
    thread.pause.assert_not_called()


def test_stop_all_transfers_suspends_auto_start_during_wait():
    thread = MagicMock()
    thread.isRunning.return_value = False
    transfer = _make_transfer(upload_threads=[thread])
    transfer.suspend_auto_start = MagicMock()
    transfer.resume_auto_start = MagicMock()
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = transfer

    MainWindow._stop_all_transfers(window, save_progress=True)

    transfer.suspend_auto_start.assert_called_once_with()
    transfer.resume_auto_start.assert_called_once_with()


def test_save_active_progress_updates_upload_task():
    task = SimpleNamespace(status="上传中", db_task_id="abc", resume_id=None, progress=42)
    db_mock = MagicMock()
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = _make_transfer(upload_tasks=[task])

    with patch.object(MainWindow, "__init__", lambda self: None):
        with patch("src.app.view.main_window.Database") as DbCls:
            DbCls.instance.return_value = db_mock
            MainWindow._save_active_progress(window)

    assert task.status == "已暂停"
    db_mock.update_upload_task.assert_called_once_with("abc", status="已暂停", progress=42)


def test_save_active_progress_updates_download_task():
    task = SimpleNamespace(status="下载中", db_task_id=None, resume_id="xyz", progress=67)
    db_mock = MagicMock()
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = _make_transfer(download_tasks=[task])

    with patch("src.app.view.main_window.Database") as DbCls:
        DbCls.instance.return_value = db_mock
        MainWindow._save_active_progress(window)

    assert task.status == "已暂停"
    db_mock.update_download_task.assert_called_once_with("xyz", status="已暂停", progress=67)


def test_save_active_progress_ignores_non_active_tasks():
    task = SimpleNamespace(status="已完成", db_task_id="done", resume_id=None)
    db_mock = MagicMock()
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = _make_transfer(upload_tasks=[task])

    with patch("src.app.view.main_window.Database") as DbCls:
        DbCls.instance.return_value = db_mock
        MainWindow._save_active_progress(window)

    assert task.status == "已完成"
    db_mock.update_upload_task.assert_not_called()


def test_save_active_progress_swallows_db_exception():
    task = SimpleNamespace(status="上传中", db_task_id="boom", resume_id=None)
    db_mock = MagicMock()
    db_mock.update_upload_task.side_effect = Exception("DB down")
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = _make_transfer(upload_tasks=[task])

    with patch("src.app.view.main_window.Database") as DbCls:
        DbCls.instance.return_value = db_mock
        # 不应抛异常
        MainWindow._save_active_progress(window)

    assert task.status == "已暂停"


def test_stop_all_transfers_with_save_progress_calls_save_active_progress():
    thread = MagicMock()
    task = SimpleNamespace(status="上传中", db_task_id="sid", resume_id=None, thread=thread, progress=55)
    db_mock = MagicMock()
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = _make_transfer(
        upload_threads=[thread],
        upload_tasks=[task],
    )

    with patch("src.app.view.main_window.Database") as DbCls:
        DbCls.instance.return_value = db_mock
        MainWindow._stop_all_transfers(window, save_progress=True)

    db_mock.update_upload_task.assert_called_once_with("sid", status="已暂停", progress=55)


def test_stop_all_transfers_deduplicates_threads_from_tasks():
    thread = MagicMock()
    task = SimpleNamespace(status="上传中", thread=thread, db_task_id=None, resume_id=None)
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = _make_transfer(
        upload_threads=[thread],
        upload_tasks=[task],
    )

    MainWindow._stop_all_transfers(window)

    # 同一个 thread 只被 cancel 一次
    thread.cancel.assert_called_once()


def test_stop_all_transfers_skips_none_and_seen_threads():
    thread = MagicMock()
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = _make_transfer(
        upload_threads=[None, thread, thread],
    )

    MainWindow._stop_all_transfers(window)

    # None 被跳过，重复 thread 只调一次
    thread.cancel.assert_called_once()


def test_stop_all_transfers_extracts_active_threads_from_tasks():
    task_thread = MagicMock()
    task_thread.isRunning.return_value = False
    task = SimpleNamespace(status="上传中", thread=task_thread, db_task_id=None, resume_id=None)
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = _make_transfer(
        upload_threads=[],
        upload_tasks=[task],
    )

    MainWindow._stop_all_transfers(window)

    task_thread.cancel.assert_called_once()
    task_thread.wait.assert_called()


def test_show_relogin_dialog_stops_old_transfers_before_switching_pan():
    old_pan = MagicMock()
    old_pan.on_token_expired = object()
    new_pan = MagicMock()
    events = []

    message_box = MagicMock()
    login_dialog = MagicMock()
    login_dialog.exec.return_value = QDialog.DialogCode.Accepted
    login_dialog.get_pan.return_value = new_pan

    window = MainWindow.__new__(MainWindow)
    window.pan = old_pan
    window.transfer_interface = MagicMock()
    window.transfer_interface.set_pan.side_effect = lambda pan, force=False: events.append(
        ("set_pan", pan, force)
    )
    window.cloud_interface = MagicMock()
    window.cloud_interface.set_pan.side_effect = lambda pan: events.append(("cloud", pan))
    window.file_interface = MagicMock()
    window.file_interface.reload.side_effect = lambda: events.append(("reload",))
    window._stop_all_transfers = MagicMock(
        side_effect=lambda save_progress=False: events.append(("stop", save_progress))
    )

    with patch("src.app.view.main_window.MessageBox", return_value=message_box), \
         patch("src.app.view.main_window.LoginDialog", return_value=login_dialog):
        MainWindow._show_relogin_dialog(window)

    assert events[0] == ("stop", True)
    assert events[1] == ("set_pan", new_pan, True)
    assert old_pan.on_token_expired is None
    assert old_pan.close.call_count == 1
    assert new_pan.on_token_expired == window._handle_token_expired
    login_dialog.deleteLater.assert_called_once()


def test_force_cleanup_tasks_deletes_active_download_records(tmp_path):
    active_download = SimpleNamespace(
        status="下载中",
        thread=MagicMock(),
        resume_id="resume-active",
    )
    paused_download = SimpleNamespace(
        status="已暂停",
        thread=None,
        resume_id="resume-paused",
    )
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = _make_transfer(
        download_tasks=[active_download, paused_download],
    )

    with patch("src.app.view.main_window.Database") as db_cls, \
         patch("src.app.view.main_window.cleanup_temp_dir") as mock_cleanup:
        db_cls.instance.return_value = MagicMock()
        MainWindow._force_cleanup_tasks(window)

    db_cls.instance.return_value.delete_download_task.assert_called_once_with("resume-active")
    mock_cleanup.assert_called_once_with("resume-active")
    assert active_download.status == "已取消"
    assert paused_download.status == "已取消"
