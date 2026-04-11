import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.modules.setdefault("qrcode", MagicMock())

from src.app.view.main_window import MainWindow


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
    download_thread = MagicMock()
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = _make_transfer(
        upload_threads=[upload_thread],
        download_threads=[download_thread],
    )

    MainWindow._stop_all_transfers(window)

    upload_thread.cancel.assert_called_once()
    upload_thread.wait.assert_called_once()
    download_thread.cancel.assert_called_once()
    download_thread.wait.assert_called_once()


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


def test_save_active_progress_updates_upload_task():
    task = SimpleNamespace(status="上传中", db_task_id="abc", resume_id=None)
    db_mock = MagicMock()
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = _make_transfer(upload_tasks=[task])

    with patch.object(MainWindow, "__init__", lambda self: None):
        with patch("src.app.view.main_window.Database") as DbCls:
            DbCls.instance.return_value = db_mock
            MainWindow._save_active_progress(window)

    assert task.status == "已暂停"
    db_mock.update_upload_task.assert_called_once_with("abc", status="已暂停")


def test_save_active_progress_updates_download_task():
    task = SimpleNamespace(status="下载中", db_task_id=None, resume_id="xyz")
    db_mock = MagicMock()
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = _make_transfer(download_tasks=[task])

    with patch("src.app.view.main_window.Database") as DbCls:
        DbCls.instance.return_value = db_mock
        MainWindow._save_active_progress(window)

    assert task.status == "已暂停"
    db_mock.update_download_task.assert_called_once_with("xyz", status="已暂停", error="")


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
    task = SimpleNamespace(status="上传中", db_task_id="sid", resume_id=None, thread=thread)
    db_mock = MagicMock()
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = _make_transfer(
        upload_threads=[thread],
        upload_tasks=[task],
    )

    with patch("src.app.view.main_window.Database") as DbCls:
        DbCls.instance.return_value = db_mock
        MainWindow._stop_all_transfers(window, save_progress=True)

    db_mock.update_upload_task.assert_called_once_with("sid", status="已暂停")


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
    task = SimpleNamespace(status="上传中", thread=task_thread, db_task_id=None, resume_id=None)
    window = MainWindow.__new__(MainWindow)
    window.transfer_interface = _make_transfer(
        upload_threads=[],
        upload_tasks=[task],
    )

    MainWindow._stop_all_transfers(window)

    task_thread.cancel.assert_called_once()
    task_thread.wait.assert_called_once()
