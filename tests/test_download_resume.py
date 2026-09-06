import hashlib
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.app.common import database as database_module
from src.app.common import download_resume
from src.app.common.database import Database
from src.app.common.download_resume import (
    _build_parts,
    _download_part,
    _get_stop_result,
    _is_task_cancelled,
    _is_task_paused,
    _notify_conn_info,
    _notify_progress,
    _replace_output_file,
    _validate_existing_parts,
    build_resume_id,
    cleanup_temp_dir,
    get_part_path,
    get_temp_dir,
    stream_download_from_url,
)
from src.app.common.download_resume import (  # 批次 3 追加导入（仅新增名字）
    _auto_download_part_size,
    _cleanup_parts,
    _cleanup_stale_single_stream_files,
    _delete_download_resume_state,
    _download_single_stream,
    _download_with_resume,
    _notify_status,
    _prepare_resume_metadata,
    _reset_partial_download,
    _save_download_status,
    _verify_completed_download,
    _verify_remote_file,
    get_merged_path,
)

PART_SIZE = 5 * 1024 * 1024  # 测试用常量，与运行时默认分片大小一致


class _MockResponse:
    def __init__(self, body=b"", status_code=200, headers=None):
        self.body = body
        self.status_code = status_code
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def iter_content(self, chunk_size=8192):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset: offset + chunk_size]


def _use_temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "123pan-open.db"
    monkeypatch.setattr(database_module, "_get_db_path", lambda: db_path)
    monkeypatch.setattr(download_resume, "CONFIG_DIR", tmp_path)
    Database.reset()
    return Database.instance()


def _make_resume_task(out_path, etag):
    return SimpleNamespace(
        resume_id=build_resume_id("alice", 100, str(out_path)),
        account_name="alice",
        file_name=out_path.name,
        file_id=100,
        file_type=0,
        file_size=0,
        save_path=str(out_path),
        current_dir_id=0,
        etag=etag,
        s3key_flag=False,
        status="失败",
        progress=0,
        last_error="",
        metadata_version=2,
    )


def test_download_task_records_are_isolated_by_account(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.save_download_task({
        "resume_id": "task-a",
        "account_name": "alice",
        "file_name": "a.bin",
        "file_id": 1,
        "save_path": str(tmp_path / "a.bin"),
    })
    db.save_download_task({
        "resume_id": "task-b",
        "account_name": "bob",
        "file_name": "b.bin",
        "file_id": 2,
        "save_path": str(tmp_path / "b.bin"),
    })

    assert [task["resume_id"] for task in db.get_download_tasks("alice")] == ["task-a"]
    assert [task["resume_id"] for task in db.get_download_tasks("bob")] == ["task-b"]


def test_stream_download_reuses_good_parts_and_redownloads_bad_part(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.set_config("maxDownloadThreads", 2)
    db.set_config("downloadPartMode", "fixed")

    total_size = PART_SIZE + 512
    content = (b"a" * PART_SIZE) + (b"b" * 512)
    out_path = tmp_path / "target.bin"
    task = _make_resume_task(out_path, download_resume.hashlib.md5(content).hexdigest())
    part0_path = get_part_path(task.resume_id, 0)
    part1_path = get_part_path(task.resume_id, 1)
    part0_path.parent.mkdir(parents=True, exist_ok=True)
    part0_path.write_bytes(content[:PART_SIZE])
    part1_path.write_bytes(b"broken")

    db.save_download_task({
        "resume_id": task.resume_id,
        "account_name": "alice",
        "file_name": out_path.name,
        "file_size": total_size,
        "file_id": task.file_id,
        "file_type": task.file_type,
        "save_path": str(out_path),
        "etag": task.etag,
        "status": "失败",
        "supports_resume": 1,
    })
    db.record_download_part(task.resume_id, {
        "index": 0,
        "start": 0,
        "end": PART_SIZE - 1,
        "expected_size": PART_SIZE,
        "actual_size": PART_SIZE,
        "md5": download_resume.hashlib.md5(content[:PART_SIZE]).hexdigest(),
    })
    db.record_download_part(task.resume_id, {
        "index": 1,
        "start": PART_SIZE,
        "end": total_size - 1,
        "expected_size": 512,
        "actual_size": 6,
        "md5": "bad",
    })

    requested_ranges = []

    def fake_head(url, headers=None, allow_redirects=True, timeout=30):
        return _MockResponse(
            headers={"Content-Length": str(total_size), "Accept-Ranges": "bytes"}
        )

    def fake_get(url, headers=None, stream=True, timeout=30):
        headers = headers or {}
        if "Range" not in headers:
            return _MockResponse(
                body=content,
                headers={"Content-Length": str(total_size), "Accept-Ranges": "bytes"},
            )
        requested_ranges.append(headers["Range"])
        start_text, end_text = headers["Range"].split("=")[1].split("-")
        start = int(start_text)
        end = int(end_text)
        return _MockResponse(body=content[start: end + 1], status_code=206)

    mock_session = MagicMock()
    mock_session.head = fake_head
    mock_session.get = fake_get
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    result = stream_download_from_url(
        "https://example.test/download",
        out_path,
        overwrite=True,
        resume_task=task,
    )

    assert result == out_path
    assert out_path.read_bytes() == content
    assert requested_ranges == [f"bytes={PART_SIZE}-{total_size - 1}"]
    assert db.get_download_task(task.resume_id) is not None


def test_stream_download_raises_on_final_hash_mismatch(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.set_config("maxDownloadThreads", 1)
    db.set_config("downloadPartMode", "fixed")

    total_size = PART_SIZE
    content = b"z" * total_size
    out_path = tmp_path / "broken.bin"
    task = _make_resume_task(out_path, "0000000000000000deadbeef00000000")

    def fake_head(url, headers=None, allow_redirects=True, timeout=30):
        return _MockResponse(
            headers={"Content-Length": str(total_size), "Accept-Ranges": "bytes"}
        )

    def fake_get(url, headers=None, stream=True, timeout=30):
        headers = headers or {}
        if "Range" not in headers:
            return _MockResponse(
                body=content,
                headers={"Content-Length": str(total_size), "Accept-Ranges": "bytes"},
            )
        start_text, end_text = headers["Range"].split("=")[1].split("-")
        start = int(start_text)
        end = int(end_text)
        return _MockResponse(body=content[start: end + 1], status_code=206)

    mock_session = MagicMock()
    mock_session.head = fake_head
    mock_session.get = fake_get
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    with pytest.raises(RuntimeError, match="整文件校验失败"):
        stream_download_from_url(
            "https://example.test/download",
            out_path,
            overwrite=True,
            resume_task=task,
        )

    assert not out_path.exists()
    assert db.get_download_task(task.resume_id) is None


def test_stream_download_failure_keeps_existing_output_when_overwriting(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    out_path = tmp_path / "existing.bin"
    out_path.write_bytes(b"keep-me")

    monkeypatch.setattr(download_resume, "_probe_download", lambda _url: (123, False, False))
    mock_session = MagicMock()
    mock_session.get = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("network boom"))
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    with pytest.raises(RuntimeError, match="network boom"):
        stream_download_from_url(
            "https://example.test/download",
            out_path,
            overwrite=True,
        )

    assert out_path.read_bytes() == b"keep-me"


def test_stream_download_requeues_part_after_retryable_failure(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.set_config("maxDownloadThreads", 1)
    db.set_config("downloadPartMode", "fixed")

    total_size = PART_SIZE + 512
    content = (b"a" * PART_SIZE) + (b"b" * 512)
    out_path = tmp_path / "retryable.bin"
    task = _make_resume_task(out_path, download_resume.hashlib.md5(content).hexdigest())
    part_retry_range = f"bytes={PART_SIZE}-{total_size - 1}"
    attempts = {part_retry_range: 0}
    requested_ranges = []

    def fake_head(url, headers=None, allow_redirects=True, timeout=30):
        return _MockResponse(
            headers={"Content-Length": str(total_size), "Accept-Ranges": "bytes"}
        )

    def fake_get(url, headers=None, stream=True, timeout=30):
        headers = headers or {}
        if "Range" not in headers:
            return _MockResponse(
                body=content,
                headers={"Content-Length": str(total_size), "Accept-Ranges": "bytes"},
            )
        range_header = headers["Range"]
        requested_ranges.append(range_header)
        if range_header == part_retry_range:
            attempts[range_header] += 1
            if attempts[range_header] <= 3:
                raise download_resume.requests.exceptions.ConnectionError("temporary")
        start_text, end_text = range_header.split("=")[1].split("-")
        start = int(start_text)
        end = int(end_text)
        return _MockResponse(body=content[start: end + 1], status_code=206)

    mock_session = MagicMock()
    mock_session.head = fake_head
    mock_session.get = fake_get
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    result = stream_download_from_url(
        "https://example.test/download",
        out_path,
        overwrite=True,
        resume_task=task,
    )

    assert result == out_path
    assert out_path.read_bytes() == content
    assert requested_ranges.count(part_retry_range) == 4
    assert db.get_download_task(task.resume_id) is not None


def test_stream_download_requeues_rate_limited_part(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.set_config("maxDownloadThreads", 1)
    db.set_config("downloadPartMode", "fixed")
    monkeypatch.setattr(download_resume.time, "sleep", lambda *_args, **_kwargs: None)

    total_size = PART_SIZE + 512
    content = (b"a" * PART_SIZE) + (b"b" * 512)
    out_path = tmp_path / "rate-limit.bin"
    task = _make_resume_task(out_path, download_resume.hashlib.md5(content).hexdigest())
    first_range = f"bytes=0-{PART_SIZE - 1}"
    attempts = {first_range: 0}
    requested_ranges = []

    def fake_head(url, headers=None, allow_redirects=True, timeout=30):
        return _MockResponse(
            headers={"Content-Length": str(total_size), "Accept-Ranges": "bytes"}
        )

    def fake_get(url, headers=None, stream=True, timeout=30):
        headers = headers or {}
        if "Range" not in headers:
            return _MockResponse(
                body=content,
                headers={"Content-Length": str(total_size), "Accept-Ranges": "bytes"},
            )
        range_header = headers["Range"]
        requested_ranges.append(range_header)
        if range_header == first_range and attempts[first_range] == 0:
            attempts[first_range] += 1
            return _MockResponse(status_code=429)
        start_text, end_text = range_header.split("=")[1].split("-")
        start = int(start_text)
        end = int(end_text)
        return _MockResponse(body=content[start: end + 1], status_code=206)

    mock_session = MagicMock()
    mock_session.head = fake_head
    mock_session.get = fake_get
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    result = stream_download_from_url(
        "https://example.test/download",
        out_path,
        overwrite=True,
        resume_task=task,
    )

    assert result == out_path
    assert out_path.read_bytes() == content
    assert requested_ranges.count(first_range) == 2
    assert db.get_download_task(task.resume_id) is not None


def test_stream_download_pause_then_resume_from_last_completed_part(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.set_config("maxDownloadThreads", 1)
    db.set_config("downloadPartMode", "fixed")

    total_size = PART_SIZE + 512
    content = (b"a" * PART_SIZE) + (b"b" * 512)
    out_path = tmp_path / "paused.bin"
    task = _make_resume_task(out_path, download_resume.hashlib.md5(content).hexdigest())
    pause_range = f"bytes={PART_SIZE}-{total_size - 1}"
    requested_ranges = []

    class _PauseTask:
        def __init__(self):
            self.pause_requested = False
            self.is_cancelled = False
            self._active_response = None
            self._response_lock = threading.Lock()

    pause_task = _PauseTask()

    class _PauseResponse(_MockResponse):
        def iter_content(self, chunk_size=8192):
            first = True
            for offset in range(0, len(self.body), chunk_size):
                if first:
                    pause_task.pause_requested = True
                    first = False
                yield self.body[offset: offset + chunk_size]

    def fake_head(url, headers=None, allow_redirects=True, timeout=30):
        return _MockResponse(
            headers={"Content-Length": str(total_size), "Accept-Ranges": "bytes"}
        )

    def fake_get(url, headers=None, stream=True, timeout=30):
        headers = headers or {}
        if "Range" not in headers:
            return _MockResponse(
                body=content,
                headers={"Content-Length": str(total_size), "Accept-Ranges": "bytes"},
            )
        range_header = headers["Range"]
        requested_ranges.append(range_header)
        start_text, end_text = range_header.split("=")[1].split("-")
        start = int(start_text)
        end = int(end_text)
        body = content[start: end + 1]
        if range_header == pause_range and not pause_task.is_cancelled:
            return _PauseResponse(body=body, status_code=206)
        return _MockResponse(body=body, status_code=206)

    mock_session = MagicMock()
    mock_session.head = fake_head
    mock_session.get = fake_get
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    paused_result = stream_download_from_url(
        "https://example.test/download",
        out_path,
        overwrite=True,
        resume_task=task,
        task=pause_task,
    )

    assert paused_result == "已暂停"
    saved_task = db.get_download_task(task.resume_id)
    assert saved_task["status"] == "已暂停"
    assert [part["part_index"] for part in db.get_download_parts(task.resume_id)] == [0]

    resume_task = _PauseTask()
    resumed_result = stream_download_from_url(
        "https://example.test/download",
        out_path,
        overwrite=True,
        resume_task=task,
        task=resume_task,
    )

    assert resumed_result == out_path
    assert out_path.read_bytes() == content
    assert requested_ranges.count(f"bytes=0-{PART_SIZE - 1}") == 1
    assert requested_ranges.count(pause_range) == 2
    assert db.get_download_task(task.resume_id) is not None


# ---- _validate_existing_parts 孤立清理测试 ----


def test_validate_existing_parts_cleans_orphan_merged_file(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    resume_id = "test-merged-cleanup"
    temp_dir = get_temp_dir(resume_id)
    temp_dir.mkdir(parents=True, exist_ok=True)

    content = b"a" * 1024
    part0_path = get_part_path(resume_id, 0)
    part0_path.write_bytes(content)
    merged_path = temp_dir / "merged"
    merged_path.write_bytes(b"old-merged-data")

    db.save_download_task({
        "resume_id": resume_id,
        "account_name": "alice",
        "file_name": "f.bin",
        "file_id": 1,
        "save_path": str(tmp_path / "f.bin"),
    })
    db.record_download_part(resume_id, {
        "index": 0,
        "start": 0,
        "end": 1023,
        "expected_size": 1024,
        "actual_size": 1024,
        "md5": hashlib.md5(content).hexdigest(),
    })

    part_plan = [{"index": 0, "start": 0, "end": 1023, "expected_size": 1024}]
    _validate_existing_parts(resume_id, part_plan)

    assert part0_path.exists()
    assert not merged_path.exists()


def test_validate_existing_parts_cleans_orphan_part_files(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    resume_id = "test-orphan-parts"
    temp_dir = get_temp_dir(resume_id)
    temp_dir.mkdir(parents=True, exist_ok=True)

    content = b"a" * 1024
    part0_path = get_part_path(resume_id, 0)
    part0_path.write_bytes(content)
    # part1 和 part2 不在 reusable_indexes 中，应被清理
    part1_path = get_part_path(resume_id, 1)
    part1_path.write_bytes(b"orphan1")
    part2_path = get_part_path(resume_id, 2)
    part2_path.write_bytes(b"orphan2")

    db.save_download_task({
        "resume_id": resume_id,
        "account_name": "alice",
        "file_name": "f.bin",
        "file_id": 1,
        "save_path": str(tmp_path / "f.bin"),
    })
    db.record_download_part(resume_id, {
        "index": 0,
        "start": 0,
        "end": 1023,
        "expected_size": 1024,
        "actual_size": 1024,
        "md5": hashlib.md5(content).hexdigest(),
    })

    part_plan = [{"index": 0, "start": 0, "end": 1023, "expected_size": 1024}]
    _validate_existing_parts(resume_id, part_plan)

    assert part0_path.exists()
    assert not part1_path.exists()
    assert not part2_path.exists()


def test_validate_existing_parts_skips_non_part_files(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    resume_id = "test-non-part"
    temp_dir = get_temp_dir(resume_id)
    temp_dir.mkdir(parents=True, exist_ok=True)

    content = b"a" * 1024
    part0_path = get_part_path(resume_id, 0)
    part0_path.write_bytes(content)
    random_file = temp_dir / "random.txt"
    random_file.write_text("keep me")

    db.save_download_task({
        "resume_id": resume_id,
        "account_name": "alice",
        "file_name": "f.bin",
        "file_id": 1,
        "save_path": str(tmp_path / "f.bin"),
    })
    db.record_download_part(resume_id, {
        "index": 0,
        "start": 0,
        "end": 1023,
        "expected_size": 1024,
        "actual_size": 1024,
        "md5": hashlib.md5(content).hexdigest(),
    })

    part_plan = [{"index": 0, "start": 0, "end": 1023, "expected_size": 1024}]
    _validate_existing_parts(resume_id, part_plan)

    assert part0_path.exists()
    assert random_file.exists()


def test_validate_existing_parts_handles_malformed_part_names(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    resume_id = "test-malformed"
    temp_dir = get_temp_dir(resume_id)
    temp_dir.mkdir(parents=True, exist_ok=True)

    content = b"a" * 1024
    part0_path = get_part_path(resume_id, 0)
    part0_path.write_bytes(content)
    malformed = temp_dir / "part_abc"
    malformed.write_bytes(b"bad-name")

    db.save_download_task({
        "resume_id": resume_id,
        "account_name": "alice",
        "file_name": "f.bin",
        "file_id": 1,
        "save_path": str(tmp_path / "f.bin"),
    })
    db.record_download_part(resume_id, {
        "index": 0,
        "start": 0,
        "end": 1023,
        "expected_size": 1024,
        "actual_size": 1024,
        "md5": hashlib.md5(content).hexdigest(),
    })

    part_plan = [{"index": 0, "start": 0, "end": 1023, "expected_size": 1024}]
    # 不应抛异常
    _validate_existing_parts(resume_id, part_plan)

    assert part0_path.exists()
    # malformed 文件名不以 "part" 开头后跟数字，被忽略
    assert malformed.exists()


# ---- _download_part 内存缓存暂停回滚测试 ----


def test_download_part_memory_buffer_no_partial_file_on_pause(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    resume_id = "test-mem-pause"
    monkeypatch.setattr(download_resume, "CONFIG_DIR", tmp_path)

    db.save_download_task({
        "resume_id": resume_id,
        "account_name": "alice",
        "file_name": "f.bin",
        "file_id": 1,
        "save_path": str(tmp_path / "f.bin"),
    })

    part_size = 2048
    content = b"x" * part_size
    part_path = get_part_path(resume_id, 0)
    part_path.parent.mkdir(parents=True, exist_ok=True)

    part = {"index": 0, "start": 0, "end": part_size - 1, "expected_size": part_size}
    aggregator = MagicMock()
    aggregator.record = MagicMock()

    task = SimpleNamespace(is_cancelled=False, pause_requested=False, _active_response=None, _response_lock=threading.Lock())

    small_chunk = 512
    chunks_yielded = 0

    class _PauseMidChunkResponse:
        status_code = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def raise_for_status(self):
            pass
        def iter_content(self, chunk_size=8192):
            nonlocal chunks_yielded, task
            # 忽略调用方传入的 chunk_size，使用小块以产生多次迭代
            for offset in range(0, len(content), small_chunk):
                if chunks_yielded >= 2:
                    task.pause_requested = True
                yield content[offset:offset + small_chunk]
                chunks_yielded += 1

    def fake_get(url, **kwargs):
        return _PauseMidChunkResponse()

    mock_session = MagicMock()
    mock_session.get = fake_get
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)
    monkeypatch.setattr(download_resume.time, "sleep", lambda *_a, **_kw: None)

    result = _download_part(
        ["https://example.test/file"],
        part, resume_id, aggregator, None, part_size, task,
    )

    assert result == "paused"
    # 磁盘无 part 文件（内存缓存，pause 时不写磁盘）
    assert not part_path.exists()
    # aggregator 进度被回滚（record 调用中有负值）
    records = [call.args[0] for call in aggregator.record.call_args_list]
    assert any(r < 0 for r in records), f"Expected negative record, got: {records}"
    # DB 无该 part 记录
    assert db.get_download_parts(resume_id) == []


def test_download_part_writes_disk_only_after_size_validation(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    resume_id = "test-write-after-validate"
    monkeypatch.setattr(download_resume, "CONFIG_DIR", tmp_path)

    db.save_download_task({
        "resume_id": resume_id,
        "account_name": "alice",
        "file_name": "f.bin",
        "file_id": 1,
        "save_path": str(tmp_path / "f.bin"),
    })

    part_size = 1024
    content = b"y" * part_size
    part_path = get_part_path(resume_id, 0)
    part_path.parent.mkdir(parents=True, exist_ok=True)

    part = {"index": 0, "start": 0, "end": part_size - 1, "expected_size": part_size}
    aggregator = MagicMock()
    task = SimpleNamespace(is_cancelled=False, pause_requested=False, _active_response=None, _response_lock=threading.Lock())

    class _OkResponse:
        status_code = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def raise_for_status(self):
            pass
        def iter_content(self, chunk_size=8192):
            for offset in range(0, len(content), chunk_size):
                yield content[offset:offset + chunk_size]

    def fake_get(url, **kwargs):
        return _OkResponse()

    mock_session = MagicMock()
    mock_session.get = fake_get
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    # part 文件在调用前不存在
    assert not part_path.exists()

    result = _download_part(
        ["https://example.test/file"],
        part, resume_id, aggregator, None, part_size, task,
    )

    assert result == "ok"
    # size 校验通过后才写入磁盘
    assert part_path.exists()
    assert part_path.read_bytes() == content
    # DB 有记录
    parts = db.get_download_parts(resume_id)
    assert len(parts) == 1
    assert parts[0]["part_index"] == 0


def test_download_part_waits_when_refresh_returns_none(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    resume_id = "refresh-none"
    monkeypatch.setattr(download_resume, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(download_resume.time, "sleep", lambda *_args, **_kwargs: None)

    db.save_download_task({
        "resume_id": resume_id,
        "account_name": "alice",
        "file_name": "f.bin",
        "file_id": 1,
        "save_path": str(tmp_path / "f.bin"),
    })

    part = {"index": 0, "start": 0, "end": 3, "expected_size": 4}
    aggregator = MagicMock()
    aggregator.record = MagicMock()
    task = SimpleNamespace(
        is_cancelled=False,
        pause_requested=False,
        _active_response=None,
        _response_lock=threading.Lock(),
    )
    calls = []

    def fake_get(url, **_kwargs):
        calls.append(url)
        if len(calls) <= 2:
            return _MockResponse(status_code=403)
        return _MockResponse(body=b"data", status_code=206)

    refresh_results = iter([None, "https://example.test/refreshed"])
    mock_session = MagicMock()
    mock_session.get = fake_get
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    result = _download_part(
        ["https://example.test/expired"],
        part,
        resume_id,
        aggregator,
        None,
        4,
        task,
        refresh_url_fn=lambda: next(refresh_results),
    )

    assert result == "ok"
    assert calls == [
        "https://example.test/expired",
        "https://example.test/expired",
        "https://example.test/refreshed",
    ]


def test_download_part_sends_minimal_download_headers(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    resume_id = "download-headers"
    monkeypatch.setattr(download_resume, "CONFIG_DIR", tmp_path)

    db.save_download_task({
        "resume_id": resume_id,
        "account_name": "alice",
        "file_name": "f.bin",
        "file_id": 1,
        "save_path": str(tmp_path / "f.bin"),
    })

    part = {"index": 0, "start": 0, "end": 3, "expected_size": 4}
    aggregator = MagicMock()
    aggregator.record = MagicMock()
    task = SimpleNamespace(
        is_cancelled=False,
        pause_requested=False,
        _active_response=None,
        _response_lock=threading.Lock(),
    )
    seen_headers = []

    def fake_get(_url, headers=None, **_kwargs):
        seen_headers.append(headers or {})
        return _MockResponse(body=b"data", status_code=206)

    mock_session = MagicMock()
    mock_session.get = fake_get
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    assert _download_part(
        ["https://example.test/file"],
        part,
        resume_id,
        aggregator,
        None,
        4,
        task,
    ) == "ok"
    assert seen_headers == [{
        "User-Agent": "123pan-open/1.0",
        "Range": "bytes=0-3",
    }]


def test_single_stream_cancel_with_cleanup_deletes_download_record(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    out_path = tmp_path / "single-cancel.bin"
    task = _make_resume_task(out_path, "")
    db.save_download_task({
        "resume_id": task.resume_id,
        "account_name": "alice",
        "file_name": out_path.name,
        "file_size": 4,
        "file_id": task.file_id,
        "save_path": str(out_path),
        "status": "下载中",
        "progress": 50,
    })
    cancel_task = SimpleNamespace(
        is_cancelled=False,
        pause_requested=False,
        cleanup_on_cancel=True,
        _active_response=None,
        _response_lock=threading.Lock(),
    )

    def fake_probe(_url):
        return 4, False, False

    class _CancelMidStreamResponse(_MockResponse):
        def iter_content(self, chunk_size=8192):
            cancel_task.is_cancelled = True
            yield b"data"

    mock_session = MagicMock()
    mock_session.get = lambda *_args, **_kwargs: _CancelMidStreamResponse(status_code=200)
    monkeypatch.setattr(download_resume, "_probe_download", fake_probe)
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    result = stream_download_from_url(
        "https://example.test/download",
        out_path,
        overwrite=True,
        resume_task=task,
        task=cancel_task,
    )

    assert result == "已取消"
    assert db.get_download_task(task.resume_id) is None


def test_single_stream_verifies_md5_before_replacing_output(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    content = b"good-data"
    out_path = tmp_path / "single-ok.bin"
    task = _make_resume_task(out_path, hashlib.md5(content).hexdigest())

    monkeypatch.setattr(download_resume, "_probe_download", lambda _url: (len(content), False, False))
    mock_session = MagicMock()
    mock_session.get = lambda *_args, **_kwargs: _MockResponse(
        body=content,
        status_code=200,
        headers={"Content-Length": str(len(content))},
    )
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    result = stream_download_from_url(
        "https://example.test/download",
        out_path,
        overwrite=True,
        resume_task=task,
    )

    assert result == out_path
    assert out_path.read_bytes() == content


def test_single_stream_md5_mismatch_does_not_replace_output(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    content = b"good-data"
    out_path = tmp_path / "single-bad.bin"
    task = _make_resume_task(out_path, hashlib.md5(b"other-data").hexdigest())

    monkeypatch.setattr(download_resume, "_probe_download", lambda _url: (len(content), False, False))
    mock_session = MagicMock()
    mock_session.get = lambda *_args, **_kwargs: _MockResponse(
        body=content,
        status_code=200,
        headers={"Content-Length": str(len(content))},
    )
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    with pytest.raises(RuntimeError, match="整文件校验失败"):
        stream_download_from_url(
            "https://example.test/download",
            out_path,
            overwrite=True,
            resume_task=task,
        )

    assert not out_path.exists()


def test_single_stream_403_refresh_success(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    content = b"refresh-ok"
    out_path = tmp_path / "single-refresh-ok.bin"
    task = _make_resume_task(out_path, hashlib.md5(content).hexdigest())
    urls = []

    monkeypatch.setattr(download_resume, "_probe_download", lambda _url: (len(content), False, False))

    def fake_get(url, **_kwargs):
        urls.append(url)
        if len(urls) == 1:
            return _MockResponse(status_code=403)
        return _MockResponse(
            body=content,
            status_code=200,
            headers={"Content-Length": str(len(content))},
        )

    mock_session = MagicMock()
    mock_session.get = fake_get
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    result = stream_download_from_url(
        "https://example.test/expired",
        out_path,
        overwrite=True,
        resume_task=task,
        refresh_url_fn=lambda: "https://example.test/refreshed",
    )

    assert result == out_path
    assert urls == [
        "https://example.test/expired",
        "https://example.test/refreshed",
    ]
    assert out_path.read_bytes() == content


def test_single_stream_waits_when_refresh_returns_none(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    content = b"refresh-ok"
    out_path = tmp_path / "single-refresh-none.bin"
    task = _make_resume_task(out_path, hashlib.md5(content).hexdigest())
    urls = []
    monkeypatch.setattr(download_resume.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(download_resume, "_probe_download", lambda _url: (len(content), False, False))

    def fake_get(url, **_kwargs):
        urls.append(url)
        if len(urls) <= 2:
            return _MockResponse(status_code=403)
        return _MockResponse(
            body=content,
            status_code=200,
            headers={"Content-Length": str(len(content))},
        )

    refresh_results = iter([None, "https://example.test/refreshed"])
    mock_session = MagicMock()
    mock_session.get = fake_get
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    result = stream_download_from_url(
        "https://example.test/expired",
        out_path,
        overwrite=True,
        resume_task=task,
        refresh_url_fn=lambda: next(refresh_results),
    )

    assert result == out_path
    assert urls == [
        "https://example.test/expired",
        "https://example.test/expired",
        "https://example.test/refreshed",
    ]


def test_single_stream_records_speed_by_chunk_delta(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    out_path = tmp_path / "single-speed.bin"
    task = _make_resume_task(out_path, "")
    content = b"abcd"
    speed_tracker = MagicMock()

    monkeypatch.setattr(download_resume, "_probe_download", lambda _url: (len(content), False, False))
    mock_session = MagicMock()
    mock_session.get = lambda *_args, **_kwargs: _MockResponse(
        body=content,
        status_code=200,
        headers={"Content-Length": str(len(content))},
    )
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    stream_download_from_url(
        "https://example.test/file",
        out_path,
        overwrite=True,
        resume_task=task,
        speed_tracker=speed_tracker,
    )

    assert [call.args[0] for call in speed_tracker.record.call_args_list] == [4]


def test_single_stream_403_refresh_failure_raises_clear_error(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    out_path = tmp_path / "single-refresh-fail.bin"
    task = _make_resume_task(out_path, "")

    monkeypatch.setattr(download_resume, "_probe_download", lambda _url: (4, False, False))
    mock_session = MagicMock()
    mock_session.get = lambda *_args, **_kwargs: _MockResponse(status_code=403)
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    with pytest.raises(RuntimeError, match="下载链接已过期或刷新失败"):
        stream_download_from_url(
            "https://example.test/expired",
            out_path,
            overwrite=True,
            resume_task=task,
            refresh_url_fn=lambda: "https://example.test/refreshed",
        )

    assert not out_path.exists()


# ---- 3a. _build_parts ----


def test_build_parts_returns_empty_for_zero_total():
    assert _build_parts(0) == []


def test_build_parts_single_part_when_small():
    parts = _build_parts(100, part_size=200)
    assert len(parts) == 1
    assert parts[0] == {"index": 0, "start": 0, "end": 99, "expected_size": 100}


def test_build_parts_exact_multiple():
    parts = _build_parts(100, part_size=50)
    assert len(parts) == 2
    assert parts[0] == {"index": 0, "start": 0, "end": 49, "expected_size": 50}
    assert parts[1] == {"index": 1, "start": 50, "end": 99, "expected_size": 50}


def test_build_parts_with_remainder():
    parts = _build_parts(110, part_size=50)
    assert len(parts) == 3
    assert parts[2] == {"index": 2, "start": 100, "end": 109, "expected_size": 10}


def test_build_parts_default_part_size(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.set_config("downloadPartSizeMB", 5)
    db.set_config("downloadPartMode", "fixed")
    parts = _build_parts(10 * 1024 * 1024)
    assert len(parts) == 2


def test_build_parts_auto_small_file_single_part(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.set_config("downloadPartMode", "auto")

    parts = _build_parts(9 * 1024 * 1024)

    assert len(parts) == 1
    assert parts[0]["expected_size"] == 9 * 1024 * 1024


def test_build_parts_auto_caps_large_part_size(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.set_config("downloadPartMode", "auto")

    parts = _build_parts(1024 * 1024 * 1024)

    assert parts[0]["expected_size"] == 32 * 1024 * 1024


def test_verify_remote_file_false_when_size_differs(monkeypatch):
    mock_session = MagicMock()
    mock_session.head.return_value = _MockResponse(
        status_code=200,
        headers={"Content-Length": "8", "ETag": '"abc"'},
    )
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    assert download_resume._verify_remote_file("https://example.test/file", 4, "abc") is False


def test_verify_remote_file_allows_head_failure(monkeypatch):
    mock_session = MagicMock()
    mock_session.head.side_effect = download_resume.requests.Timeout()
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    assert download_resume._verify_remote_file("https://example.test/file", 4, "abc") is True


def test_stream_download_clears_stored_parts_when_remote_head_differs(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    db.set_config("maxDownloadThreads", 1)
    db.set_config("downloadPartMode", "fixed")

    total_size = PART_SIZE + 512
    content = (b"a" * PART_SIZE) + (b"b" * 512)
    out_path = tmp_path / "remote-changed.bin"
    task = _make_resume_task(out_path, hashlib.md5(content).hexdigest())
    part0_path = get_part_path(task.resume_id, 0)
    part0_path.parent.mkdir(parents=True, exist_ok=True)
    part0_path.write_bytes(content[:PART_SIZE])

    db.save_download_task({
        "resume_id": task.resume_id,
        "account_name": "alice",
        "file_name": out_path.name,
        "file_size": total_size,
        "file_id": task.file_id,
        "file_type": task.file_type,
        "save_path": str(out_path),
        "etag": task.etag,
        "status": "失败",
        "supports_resume": 1,
    })
    db.record_download_part(task.resume_id, {
        "index": 0,
        "start": 0,
        "end": PART_SIZE - 1,
        "expected_size": PART_SIZE,
        "actual_size": PART_SIZE,
        "md5": hashlib.md5(content[:PART_SIZE]).hexdigest(),
    })

    requested_ranges = []
    head_calls: list[None] = []

    def fake_head(_url, **_kwargs):
        head_calls.append(None)
        if len(head_calls) == 1:
            return _MockResponse(headers={"Content-Length": str(total_size), "Accept-Ranges": "bytes"})
        return _MockResponse(headers={"Content-Length": str(total_size + 1), "Accept-Ranges": "bytes"})

    def fake_get(_url, headers=None, **_kwargs):
        headers = headers or {}
        requested_ranges.append(headers["Range"])
        start_text, end_text = headers["Range"].split("=")[1].split("-")
        start = int(start_text)
        end = int(end_text)
        return _MockResponse(body=content[start: end + 1], status_code=206)

    mock_session = MagicMock()
    mock_session.head = fake_head
    mock_session.get = fake_get
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    result = stream_download_from_url(
        "https://example.test/download",
        out_path,
        overwrite=True,
        resume_task=task,
    )

    assert result == out_path
    assert f"bytes=0-{PART_SIZE - 1}" in requested_ranges
    assert f"bytes={PART_SIZE}-{total_size - 1}" in requested_ranges


# ---- 3b. 状态判断 ----


def test_is_task_cancelled_none_returns_false():
    assert _is_task_cancelled(None) is False


def test_is_task_cancelled_true():
    assert _is_task_cancelled(SimpleNamespace(is_cancelled=True)) is True


def test_is_task_paused_true():
    assert _is_task_paused(SimpleNamespace(pause_requested=True)) is True


def test_get_stop_result_cancelled_priority():
    task = SimpleNamespace(is_cancelled=True, pause_requested=True)
    assert _get_stop_result(task) == "cancelled"


# ---- 3c. _replace_output_file ----


def test_replace_output_file_same_device(tmp_path):
    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    src.write_bytes(b"hello")
    _replace_output_file(src, dst)
    assert dst.read_bytes() == b"hello"
    assert not src.exists()


def test_replace_output_file_cross_device(tmp_path, monkeypatch):
    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    src.write_bytes(b"cross")

    import errno as errno_mod
    real_replace = Path.replace

    def fake_replace(self, target):
        if self == src:
            exc = OSError(errno_mod.EXDEV, "cross-device")
            raise exc
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", fake_replace)
    _replace_output_file(src, dst)
    assert dst.read_bytes() == b"cross"


# ---- 3d. signal helpers ----


def test_notify_progress_emits_when_total_nonzero():
    signals = MagicMock()
    _notify_progress(signals, 100, 50)
    signals.progress.emit.assert_called_once_with(50)


def test_notify_progress_skips_when_total_zero():
    signals = MagicMock()
    _notify_progress(signals, 0, 50)
    signals.progress.emit.assert_not_called()


def test_notify_conn_info_skips_when_no_attr():
    signals = MagicMock(spec=[])
    _notify_conn_info(signals, 1, 4)
    # 无 conn_info 属性，不报错即可


# ---- 3e. cleanup_temp_dir ----


def test_cleanup_temp_dir_removes_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(download_resume, "CONFIG_DIR", tmp_path)
    resume_id = "cleanup-test"
    temp = get_temp_dir(resume_id)
    temp.mkdir(parents=True)
    (temp / "part0").write_bytes(b"data")
    cleanup_temp_dir(resume_id)
    assert not temp.exists()


def test_cleanup_temp_dir_silently_handles_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(download_resume, "CONFIG_DIR", tmp_path)
    cleanup_temp_dir("nonexistent-id")  # 不报错即可


# ============================================================
# 批次 3 追加：download_resume.py 覆盖率补齐
# ============================================================


def _make_part_task():
    """标准下载 task 对象（_download_part / _download_single_stream 用）。"""
    return SimpleNamespace(
        is_cancelled=False,
        pause_requested=False,
        _active_response=None,
        _response_lock=threading.Lock(),
    )


def _save_min_download_task(db, resume_id, save_path):
    """写入最小可用的下载任务记录。"""
    db.save_download_task({
        "resume_id": resume_id,
        "account_name": "alice",
        "file_name": "f.bin",
        "file_id": 1,
        "save_path": str(save_path),
    })


def write_part_file(resume_id, index, data):
    """替身写入分片文件（真实 _download_part 会先 mkdir，替身需自行补齐）。"""
    part_path = get_part_path(resume_id, index)
    part_path.parent.mkdir(parents=True, exist_ok=True)
    part_path.write_bytes(data)


def _prepare_resume_flow(tmp_path, monkeypatch, total_size, part_size=5, threads=1):
    """_download_with_resume 公共环境：临时库 + 固定小分片（通过 DB 持久化 part_size）。"""
    db = _use_temp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(download_resume, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(download_resume.time, "sleep", lambda *_a, **_kw: None)
    db.set_config("maxDownloadThreads", threads)
    out_path = tmp_path / "resume-flow.bin"
    task = _make_resume_task(out_path, "")
    db.save_download_task({
        "resume_id": task.resume_id,
        "account_name": task.account_name,
        "file_name": task.file_name,
        "file_id": task.file_id,
        "file_type": task.file_type,
        "file_size": total_size,
        "save_path": str(out_path),
        "etag": task.etag,
        "status": "失败",
        "supports_resume": 1,
        "part_size": part_size,
    })
    return db, task, out_path


class _ScriptedPartDownloader:
    """_download_part 替身：按脚本返回结果；"ok" 时写入分片文件并触发首字节回调。

    分片路径必须经测试模块顶层导入的 get_part_path（原始函数对象），
    以免受测试内 monkeypatch download_resume.get_part_path 的影响。
    """

    def __init__(self, content, script=None):
        self.content = content
        self.script = script or {}
        self.calls = []

    def __call__(self, url_holder, part, resume_id, aggregator, signals, total,
                 task, first_byte_callback=None, refresh_url_fn=None):
        index = int(part["index"])
        self.calls.append(index)
        results = self.script.get(index)
        result = results.pop(0) if results else "ok"
        if result == "ok":
            start = int(part["start"])
            end = int(part["end"])
            write_part_file(resume_id, index, self.content[start:end + 1])
            if first_byte_callback:
                first_byte_callback()
        return result


# ---- _replace_output_file 边界 ----


def test_replace_output_file_reraises_non_exdev_oserror(tmp_path, monkeypatch):
    import errno as errno_mod

    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    src.write_bytes(b"data")
    real_replace = Path.replace

    def fake_replace(self, target):
        if self == src:
            raise OSError(errno_mod.EACCES, "permission denied")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", fake_replace)

    with pytest.raises(OSError, match="permission denied"):
        _replace_output_file(src, dst)
    assert src.exists()


def test_replace_output_file_cross_device_size_mismatch_raises(tmp_path, monkeypatch):
    import errno as errno_mod

    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    src.write_bytes(b"123456")
    real_replace = Path.replace

    def fake_replace(self, target):
        if self == src:
            raise OSError(errno_mod.EXDEV, "cross-device")
        return real_replace(self, target)

    def short_copy2(_src, dst_str):
        Path(dst_str).write_bytes(b"short")

    monkeypatch.setattr(Path, "replace", fake_replace)
    monkeypatch.setattr(download_resume.shutil, "copy2", short_copy2)

    with pytest.raises(OSError, match="跨盘拷贝大小不匹配"):
        _replace_output_file(src, dst)
    assert not (tmp_path / "dst.bin.tmp").exists()


def test_replace_output_file_cross_device_cleanup_failure_still_raises(tmp_path, monkeypatch):
    import errno as errno_mod

    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    src.write_bytes(b"123456")
    real_replace = Path.replace

    def fake_replace(self, target):
        if self == src:
            raise OSError(errno_mod.EXDEV, "cross-device")
        return real_replace(self, target)

    def dir_copy2(_src, dst_str):
        os.mkdir(dst_str)  # 临时文件位置是目录 → 清理 unlink 也失败

    monkeypatch.setattr(Path, "replace", fake_replace)
    monkeypatch.setattr(download_resume.shutil, "copy2", dir_copy2)

    with pytest.raises(OSError):
        _replace_output_file(src, dst)


# ---- signal / task state helpers ----


def test_notify_conn_info_emits_when_attr_present():
    signals = MagicMock()
    _notify_conn_info(signals, 2, 4)
    signals.conn_info.emit.assert_called_once_with(2, 4)


def test_notify_conn_info_skips_for_none_signals():
    _notify_conn_info(None, 2, 4)  # 不报错即可


def test_notify_status_emits_when_attr_present():
    signals = MagicMock()
    _notify_status(signals, "下载中")
    signals.status.emit.assert_called_once_with("下载中")


def test_notify_status_skips_for_none_signals():
    _notify_status(None, "下载中")  # 不报错即可


def test_save_download_status_skips_empty_resume_id():
    _save_download_status("", 100, 50, "下载中")  # 无 resume_id 时不落库


def test_delete_download_resume_state_skips_empty_task():
    _delete_download_resume_state(None)  # 不报错即可


# ---- 分片回滚 / 清理 helpers ----


def test_reset_partial_download_rolls_back_progress_and_files(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    resume_id = "reset-part"
    part_path = get_part_path(resume_id, 2)
    part_path.parent.mkdir(parents=True, exist_ok=True)
    part_path.write_bytes(b"junk")
    aggregator = MagicMock()

    _reset_partial_download(part_path, aggregator, 128, resume_id, 2)

    aggregator.record.assert_called_once_with(-128)
    assert db.get_download_parts(resume_id) == []
    assert not part_path.exists()


def test_reset_partial_download_warns_when_unlink_fails(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    resume_id = "reset-part-dir"
    part_path = get_part_path(resume_id, 1)
    part_path.parent.mkdir(parents=True, exist_ok=True)
    part_path.mkdir()  # 目录使 unlink 抛 OSError
    aggregator = MagicMock()

    _reset_partial_download(part_path, aggregator, 0, resume_id, 1)

    aggregator.record.assert_not_called()  # byte_count=0 不回滚进度
    assert part_path.exists()  # 删除失败仅告警


def test_cleanup_parts_removes_existing_part_files(tmp_path, monkeypatch):
    monkeypatch.setattr(download_resume, "CONFIG_DIR", tmp_path)
    resume_id = "cleanup-parts"
    part0 = get_part_path(resume_id, 0)
    part1 = get_part_path(resume_id, 1)
    part0.parent.mkdir(parents=True, exist_ok=True)
    part0.write_bytes(b"a")
    part1.write_bytes(b"b")

    _cleanup_parts(resume_id, [0, 1, 7])

    assert not part0.exists()
    assert not part1.exists()


def test_cleanup_parts_warns_when_unlink_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(download_resume, "CONFIG_DIR", tmp_path)
    resume_id = "cleanup-parts-dir"
    part0 = get_part_path(resume_id, 0)
    part0.parent.mkdir(parents=True, exist_ok=True)
    part0.mkdir()

    _cleanup_parts(resume_id, [0])  # 删除失败仅告警，不抛异常

    assert part0.exists()


# ---- 完成校验 / 远端校验 helpers ----


def test_verify_completed_download_raises_when_size_mismatch(tmp_path):
    file_path = tmp_path / "f.bin"
    file_path.write_bytes(b"12345")
    resume_task = SimpleNamespace(etag="")

    with pytest.raises(RuntimeError, match="文件大小不匹配"):
        _verify_completed_download(file_path, 100, resume_task)


def test_verify_completed_download_skips_md5_for_multipart_etag(tmp_path):
    file_path = tmp_path / "f.bin"
    file_path.write_bytes(b"12345")
    resume_task = SimpleNamespace(etag='"abc-2"')

    # 含 "-" 的 ETag 不能作为整文件 MD5，仅做大小校验
    _verify_completed_download(file_path, 5, resume_task)


def test_verify_remote_file_true_when_head_status_error(monkeypatch):
    mock_session = MagicMock()
    mock_session.head.return_value = _MockResponse(status_code=404, headers={})
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    assert download_resume._verify_remote_file("https://example.test/file", 4, "abc") is True


def test_verify_remote_file_false_when_etag_differs(monkeypatch):
    mock_session = MagicMock()
    mock_session.head.return_value = _MockResponse(
        status_code=200,
        headers={"Content-Length": "4", "ETag": '"bbb"'},
    )
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    assert download_resume._verify_remote_file("https://example.test/file", 4, "aaa") is False


# ---- 分片大小自适应 ----


@pytest.mark.parametrize(
    "total,expected",
    [
        (10 * 1024 * 1024, 8 * 1024 * 1024),
        (99 * 1024 * 1024, 8 * 1024 * 1024),
        (100 * 1024 * 1024, 16 * 1024 * 1024),
        (511 * 1024 * 1024, 16 * 1024 * 1024),
        (512 * 1024 * 1024, 32 * 1024 * 1024),
    ],
)
def test_auto_download_part_size_mid_ranges(total, expected):
    assert _auto_download_part_size(total) == expected


# ---- _prepare_resume_metadata ----


def test_prepare_resume_metadata_returns_none_without_task(tmp_path):
    assert _prepare_resume_metadata(tmp_path / "out.bin", 100, None, True) is None


# ---- _probe_download 异常路径 ----

_RATE_LIMITED = "rate_limited"


@pytest.mark.parametrize(
    "head_side_effect,get_behavior,expected",
    [
        (_RATE_LIMITED, None, (0, False, False)),
        (download_resume.requests.exceptions.ConnectionError("conn refused"), None, (0, False, True)),
        (download_resume.requests.exceptions.Timeout("head timed out"), None, (0, False, True)),
        (
            download_resume.requests.exceptions.HTTPError("bad request"),
            {"headers": {"Content-Length": "8", "Accept-Ranges": "bytes"}},
            (8, True, False),
        ),
        (
            download_resume.requests.exceptions.HTTPError("bad request"),
            {"status_code": 429},
            (0, False, False),
        ),
        (
            download_resume.requests.exceptions.HTTPError("bad request"),
            download_resume.requests.exceptions.Timeout("get timed out"),
            (0, False, True),
        ),
        (ValueError("unexpected"), None, (0, False, True)),
    ],
)
def test_probe_download_failure_paths(head_side_effect, get_behavior, expected, monkeypatch):
    mock_session = MagicMock()
    if head_side_effect == _RATE_LIMITED:
        mock_session.head.return_value = _MockResponse(status_code=429)
    else:
        mock_session.head.side_effect = head_side_effect
    if isinstance(get_behavior, dict):
        get_kwargs = {"status_code": 200, "headers": {}}
        get_kwargs.update(get_behavior)
        mock_session.get.return_value = _MockResponse(**get_kwargs)
    elif isinstance(get_behavior, BaseException):
        mock_session.get.side_effect = get_behavior
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    assert download_resume._probe_download("https://example.test/file") == expected


# ---- _validate_existing_parts 分支补齐 ----


def test_validate_existing_parts_removes_record_without_file(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    resume_id = "validate-missing-file"
    content = b"a" * 64
    part0_path = get_part_path(resume_id, 0)
    part0_path.parent.mkdir(parents=True, exist_ok=True)
    part0_path.write_bytes(content)
    _save_min_download_task(db, resume_id, tmp_path / "f.bin")
    db.record_download_part(resume_id, {
        "index": 0, "start": 0, "end": 63,
        "expected_size": 64, "actual_size": 64,
        "md5": hashlib.md5(content).hexdigest(),
    })
    # index 1 有 DB 记录但磁盘文件缺失 → 记录应被删除
    db.record_download_part(resume_id, {
        "index": 1, "start": 64, "end": 127,
        "expected_size": 64, "actual_size": 64,
        "md5": hashlib.md5(content).hexdigest(),
    })

    part_plan = [
        {"index": 0, "start": 0, "end": 63, "expected_size": 64},
        {"index": 1, "start": 64, "end": 127, "expected_size": 64},
    ]
    downloaded, reusable = _validate_existing_parts(resume_id, part_plan)

    assert downloaded == 64
    assert reusable == [0]
    assert [p["part_index"] for p in db.get_download_parts(resume_id)] == [0]


def test_validate_existing_parts_removes_record_without_hash(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    resume_id = "validate-no-hash"
    part0_path = get_part_path(resume_id, 0)
    part0_path.parent.mkdir(parents=True, exist_ok=True)
    part0_path.write_bytes(b"a" * 64)
    _save_min_download_task(db, resume_id, tmp_path / "f.bin")
    db.record_download_part(resume_id, {
        "index": 0, "start": 0, "end": 63,
        "expected_size": 64, "actual_size": 64, "md5": "",
    })

    part_plan = [{"index": 0, "start": 0, "end": 63, "expected_size": 64}]
    downloaded, reusable = _validate_existing_parts(resume_id, part_plan)

    assert downloaded == 0
    assert reusable == []
    assert db.get_download_parts(resume_id) == []


def test_validate_existing_parts_warns_when_size_mismatch_unlink_fails(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    resume_id = "validate-size-mismatch-dir"
    temp_dir = get_temp_dir(resume_id)
    temp_dir.mkdir(parents=True, exist_ok=True)
    (temp_dir / "part0").mkdir()  # 目录：大小不匹配且 unlink 失败
    _save_min_download_task(db, resume_id, tmp_path / "f.bin")
    db.record_download_part(resume_id, {
        "index": 0, "start": 0, "end": 1023,
        "expected_size": 1024, "actual_size": 1024, "md5": "whatever",
    })

    part_plan = [{"index": 0, "start": 0, "end": 1023, "expected_size": 1024}]
    downloaded, reusable = _validate_existing_parts(resume_id, part_plan)

    assert downloaded == 0
    assert reusable == []
    assert db.get_download_parts(resume_id) == []
    assert (temp_dir / "part0").exists()  # unlink 失败仅告警


def test_validate_existing_parts_removes_md5_mismatched_part(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    resume_id = "validate-md5-mismatch"
    part0_path = get_part_path(resume_id, 0)
    part0_path.parent.mkdir(parents=True, exist_ok=True)
    part0_path.write_bytes(b"a" * 64)
    _save_min_download_task(db, resume_id, tmp_path / "f.bin")
    db.record_download_part(resume_id, {
        "index": 0, "start": 0, "end": 63,
        "expected_size": 64, "actual_size": 64, "md5": "stale-hash",
    })

    part_plan = [{"index": 0, "start": 0, "end": 63, "expected_size": 64}]
    downloaded, reusable = _validate_existing_parts(resume_id, part_plan)

    assert downloaded == 0
    assert reusable == []
    assert not part0_path.exists()
    assert db.get_download_parts(resume_id) == []


def test_validate_existing_parts_warns_when_md5_mismatch_unlink_fails(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    resume_id = "validate-md5-locked"
    part0_path = get_part_path(resume_id, 0)
    part0_path.parent.mkdir(parents=True, exist_ok=True)
    part0_path.write_bytes(b"a" * 64)
    _save_min_download_task(db, resume_id, tmp_path / "f.bin")
    db.record_download_part(resume_id, {
        "index": 0, "start": 0, "end": 63,
        "expected_size": 64, "actual_size": 64, "md5": "stale-hash",
    })

    real_unlink = Path.unlink

    def fake_unlink(self, missing_ok=False):
        if self.name == "part0":
            raise OSError("file locked")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fake_unlink)

    part_plan = [{"index": 0, "start": 0, "end": 63, "expected_size": 64}]
    downloaded, reusable = _validate_existing_parts(resume_id, part_plan)

    assert downloaded == 0
    assert reusable == []
    assert part0_path.exists()  # unlink 失败仅告警
    assert db.get_download_parts(resume_id) == []


def test_validate_existing_parts_warns_when_merged_unlink_fails(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    resume_id = "validate-merged-dir"
    temp_dir = get_temp_dir(resume_id)
    temp_dir.mkdir(parents=True, exist_ok=True)
    (temp_dir / "merged").mkdir()  # 目录：unlink 失败仅告警
    content = b"a" * 64
    part0_path = get_part_path(resume_id, 0)
    part0_path.write_bytes(content)
    _save_min_download_task(db, resume_id, tmp_path / "f.bin")
    db.record_download_part(resume_id, {
        "index": 0, "start": 0, "end": 63,
        "expected_size": 64, "actual_size": 64,
        "md5": hashlib.md5(content).hexdigest(),
    })

    part_plan = [{"index": 0, "start": 0, "end": 63, "expected_size": 64}]
    downloaded, reusable = _validate_existing_parts(resume_id, part_plan)

    assert downloaded == 64
    assert reusable == [0]
    assert (temp_dir / "merged").exists()


# ---- _download_part 分支补齐 ----


def test_download_part_returns_cancelled_before_starting(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(download_resume, "CONFIG_DIR", tmp_path)
    part = {"index": 0, "start": 0, "end": 3, "expected_size": 4}
    aggregator = MagicMock()
    task = SimpleNamespace(
        is_cancelled=True,
        pause_requested=False,
        _active_response=None,
        _response_lock=threading.Lock(),
    )

    result = _download_part(
        ["https://example.test/file"], part, "part-cancel-early",
        aggregator, None, 4, task,
    )

    assert result == "cancelled"
    aggregator.record.assert_not_called()
    assert db.get_download_parts("part-cancel-early") == []


def test_download_part_gives_up_after_repeated_403_refresh(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(download_resume, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(download_resume.time, "sleep", lambda *_a, **_kw: None)
    part = {"index": 0, "start": 0, "end": 3, "expected_size": 4}
    refresh_calls = []

    def refresh():
        refresh_calls.append(True)
        return "https://example.test/refreshed"

    mock_session = MagicMock()
    mock_session.get = lambda *_a, **_kw: _MockResponse(status_code=403)
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    result = _download_part(
        ["https://example.test/expired"], part, "part-403-exhausted",
        MagicMock(), None, 4, _make_part_task(), refresh_url_fn=refresh,
    )

    assert result == "url_expired"
    assert len(refresh_calls) == 4  # 连续 4 次刷新后放弃


def test_download_part_returns_url_expired_when_refresh_raises(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(download_resume, "CONFIG_DIR", tmp_path)
    part = {"index": 0, "start": 0, "end": 3, "expected_size": 4}

    def refresh():
        raise RuntimeError("refresh boom")

    mock_session = MagicMock()
    mock_session.get = lambda *_a, **_kw: _MockResponse(status_code=403)
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    result = _download_part(
        ["https://example.test/expired"], part, "part-403-error",
        MagicMock(), None, 4, _make_part_task(), refresh_url_fn=refresh,
    )

    assert result == "url_expired"


def test_download_part_skips_empty_chunks(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(download_resume, "CONFIG_DIR", tmp_path)
    _save_min_download_task(db, "part-empty-chunk", tmp_path / "f.bin")
    content = b"data"
    part_path = get_part_path("part-empty-chunk", 0)
    part_path.parent.mkdir(parents=True, exist_ok=True)

    class _EmptyFirstChunkResponse(_MockResponse):
        def iter_content(self, chunk_size=8192):
            yield b""
            yield content

    mock_session = MagicMock()
    mock_session.get = lambda *_a, **_kw: _EmptyFirstChunkResponse(body=content, status_code=206)
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    part = {"index": 0, "start": 0, "end": 3, "expected_size": 4}
    result = _download_part(
        ["https://example.test/file"], part, "part-empty-chunk",
        MagicMock(), None, 4, _make_part_task(),
    )

    assert result == "ok"
    assert part_path.read_bytes() == content


def test_download_part_size_mismatch_rolls_back_and_requeues(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(download_resume, "CONFIG_DIR", tmp_path)
    db.set_config("retryMaxAttempts", 0)
    monkeypatch.setattr(download_resume.time, "sleep", lambda *_a, **_kw: None)
    part_path = get_part_path("part-size-mismatch", 0)
    part_path.parent.mkdir(parents=True, exist_ok=True)
    aggregator = MagicMock()

    mock_session = MagicMock()
    mock_session.get = lambda *_a, **_kw: _MockResponse(body=b"short", status_code=206)
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    part = {"index": 0, "start": 0, "end": 9, "expected_size": 10}
    result = _download_part(
        ["https://example.test/file"], part, "part-size-mismatch",
        aggregator, None, 10, _make_part_task(),
    )

    assert result == "retryable"
    assert part["attempt"] == 1
    records = [call.args[0] for call in aggregator.record.call_args_list]
    assert records == [5, -5]  # 收到 5 字节后大小不匹配，进度回滚
    assert not part_path.exists()
    assert db.get_download_parts("part-size-mismatch") == []


def test_download_part_returns_fatal_after_queue_attempt_limit(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(download_resume, "CONFIG_DIR", tmp_path)
    db.set_config("retryMaxAttempts", 0)
    monkeypatch.setattr(download_resume.time, "sleep", lambda *_a, **_kw: None)
    aggregator = MagicMock()

    mock_session = MagicMock()
    mock_session.get = lambda *_a, **_kw: _MockResponse(body=b"short", status_code=206)
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    part = {"index": 0, "start": 0, "end": 9, "expected_size": 10, "attempt": 2}
    result = _download_part(
        ["https://example.test/file"], part, "part-fatal",
        aggregator, None, 10, _make_part_task(),
    )

    assert result == "fatal"
    assert part["attempt"] == 3


def test_download_part_rolls_back_partial_progress_on_network_error(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(download_resume, "CONFIG_DIR", tmp_path)
    db.set_config("retryMaxAttempts", 0)
    monkeypatch.setattr(download_resume.time, "sleep", lambda *_a, **_kw: None)
    aggregator = MagicMock()

    class _BrokenStreamResponse(_MockResponse):
        def iter_content(self, chunk_size=8192):
            yield b"abc"
            raise download_resume.requests.exceptions.ConnectionError("mid-stream")

    mock_session = MagicMock()
    mock_session.get = lambda *_a, **_kw: _BrokenStreamResponse(body=b"abc", status_code=206)
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    part = {"index": 0, "start": 0, "end": 9, "expected_size": 10}
    result = _download_part(
        ["https://example.test/file"], part, "part-partial-rollback",
        aggregator, None, 10, _make_part_task(),
    )

    assert result == "retryable"
    records = [call.args[0] for call in aggregator.record.call_args_list]
    assert records == [3, -3]  # 中途断开，已计入进度被回滚
    assert db.get_download_parts("part-partial-rollback") == []


def test_download_part_cleans_stale_part_file_on_failure(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(download_resume, "CONFIG_DIR", tmp_path)
    db.set_config("retryMaxAttempts", 0)
    monkeypatch.setattr(download_resume.time, "sleep", lambda *_a, **_kw: None)
    part_path = get_part_path("part-stale-file", 0)
    part_path.parent.mkdir(parents=True, exist_ok=True)
    part_path.write_bytes(b"stale")

    def failing_get(*_args, **_kwargs):
        raise download_resume.requests.exceptions.ConnectionError("down")

    mock_session = MagicMock()
    mock_session.get = failing_get
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    part = {"index": 0, "start": 0, "end": 3, "expected_size": 4}
    result = _download_part(
        ["https://example.test/file"], part, "part-stale-file",
        MagicMock(), None, 4, _make_part_task(),
    )

    assert result == "retryable"
    assert not part_path.exists()  # 失败后清理残留分片文件


def test_download_part_warns_when_stale_part_unlink_fails(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(download_resume, "CONFIG_DIR", tmp_path)
    db.set_config("retryMaxAttempts", 0)
    monkeypatch.setattr(download_resume.time, "sleep", lambda *_a, **_kw: None)
    part_path = get_part_path("part-locked", 0)
    part_path.parent.mkdir(parents=True, exist_ok=True)
    part_path.mkdir()  # 目录使 unlink 失败

    def failing_get(*_args, **_kwargs):
        raise download_resume.requests.exceptions.ConnectionError("down")

    mock_session = MagicMock()
    mock_session.get = failing_get
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    part = {"index": 0, "start": 0, "end": 3, "expected_size": 4}
    result = _download_part(
        ["https://example.test/file"], part, "part-locked",
        MagicMock(), None, 4, _make_part_task(),
    )

    assert result == "retryable"
    assert part_path.exists()  # 删除失败仅告警


# ---- _download_with_resume 状态机（worker 调度 / 合并 / 终态）----


def test_download_with_resume_flushes_database_every_ten_parts(tmp_path, monkeypatch):
    total_size = 50  # part_size=5 → 10 个分片
    db, task, out_path = _prepare_resume_flow(tmp_path, monkeypatch, total_size)
    content = bytes(range(total_size))
    flush_calls = []
    real_flush = Database.flush

    def spy_flush(self):
        flush_calls.append(True)
        real_flush(self)

    monkeypatch.setattr(Database, "flush", spy_flush)
    monkeypatch.setattr(download_resume, "_download_part", _ScriptedPartDownloader(content))

    result = _download_with_resume(
        "https://example.test/download", out_path, total_size,
        None, None, task, None,
    )

    assert result == out_path
    assert out_path.read_bytes() == content
    assert len(flush_calls) == 2  # 第 10 个分片后批量提交 + 收尾统一提交
    assert db.get_download_parts(task.resume_id) == []


def test_download_with_resume_cancelled_task_cleans_up_state(tmp_path, monkeypatch):
    total_size = 10
    db, task, out_path = _prepare_resume_flow(tmp_path, monkeypatch, total_size)
    temp_dir = get_temp_dir(task.resume_id)
    temp_dir.mkdir(parents=True)
    (temp_dir / "part0").write_bytes(b"junk")
    stop_task = SimpleNamespace(is_cancelled=False, pause_requested=False, cleanup_on_cancel=True)

    def fake_download_part(url_holder, part, resume_id, aggregator, signals, total,
                           task_obj, first_byte_callback=None, refresh_url_fn=None):
        write_part_file(resume_id, int(part["index"]), b"partial")
        stop_task.is_cancelled = True  # 首个分片完成后取消，worker 下一轮循环退出
        return "ok"

    monkeypatch.setattr(download_resume, "_download_part", fake_download_part)

    result = _download_with_resume(
        "https://example.test/download", out_path, total_size,
        None, stop_task, task, None,
    )

    assert result == "已取消"
    assert db.get_download_task(task.resume_id) is None
    assert not temp_dir.exists()


def test_download_with_resume_fails_after_repeated_rate_limits(tmp_path, monkeypatch):
    total_size = 10
    db, task, out_path = _prepare_resume_flow(tmp_path, monkeypatch, total_size)
    rate_limit_hits = []

    def fake_download_part(*_args, **_kwargs):
        rate_limit_hits.append(True)
        return "rate_limited"

    monkeypatch.setattr(download_resume, "_download_part", fake_download_part)

    with pytest.raises(RuntimeError, match="分片下载失败"):
        _download_with_resume(
            "https://example.test/download", out_path, total_size,
            None, None, task, None,
        )

    assert len(rate_limit_hits) == download_resume.MAX_RATE_LIMITS + 1
    assert db.get_download_task(task.resume_id)["status"] == "失败"


def test_download_with_resume_requeues_retryable_part_and_recovers(tmp_path, monkeypatch):
    total_size = 10
    db, task, out_path = _prepare_resume_flow(tmp_path, monkeypatch, total_size)
    content = b"0123456789"
    # part0 连续两次 retryable：第一次 probe 自清（639），第二次非 probe 降档（641）
    fake = _ScriptedPartDownloader(content, script={0: ["retryable", "retryable"]})
    monkeypatch.setattr(download_resume, "_download_part", fake)

    result = _download_with_resume(
        "https://example.test/download", out_path, total_size,
        None, None, task, None,
    )

    assert result == out_path
    assert out_path.read_bytes() == content
    # part0 重试期间 part1 会先被取走（重入队尾），最终两者都成功
    assert fake.calls == [0, 1, 0, 0]


def test_download_with_resume_aborts_after_repeated_url_expiry(tmp_path, monkeypatch):
    total_size = 5  # 单分片：避免其他分片成功后重置 url_expired 计数
    db, task, out_path = _prepare_resume_flow(tmp_path, monkeypatch, total_size)
    fake = _ScriptedPartDownloader(
        b"01234",
        script={0: ["url_expired", "url_expired", "url_expired"]},
    )
    monkeypatch.setattr(download_resume, "_download_part", fake)

    with pytest.raises(RuntimeError, match="分片下载失败"):
        _download_with_resume(
            "https://example.test/download", out_path, total_size,
            None, None, task, None,
        )

    assert fake.calls == [0, 0, 0]  # 连续 3 次 url_expired 后终止
    assert db.get_download_task(task.resume_id)["status"] == "失败"


def test_download_with_resume_fails_on_unexpected_part_result(tmp_path, monkeypatch):
    total_size = 10
    db, task, out_path = _prepare_resume_flow(tmp_path, monkeypatch, total_size)
    fake = _ScriptedPartDownloader(b"0123456789", script={0: ["fatal"]})
    monkeypatch.setattr(download_resume, "_download_part", fake)

    with pytest.raises(RuntimeError, match="分片下载失败"):
        _download_with_resume(
            "https://example.test/download", out_path, total_size,
            None, None, task, None,
        )

    assert fake.calls == [0]
    assert db.get_download_task(task.resume_id)["status"] == "失败"


def test_download_with_resume_rate_limit_shrinks_worker_concurrency(tmp_path, monkeypatch):
    total_size = 20  # 4 个分片，2 线程
    db, task, out_path = _prepare_resume_flow(tmp_path, monkeypatch, total_size, threads=2)
    content = b"0123456789abcdefghij"
    real_sleep = time.sleep  # 正确性仅依赖事件同步；短真实 sleep 保证覆盖率判定稳定
    ev_drop = threading.Event()
    index2_calls: list[bool] = []

    def fake_download_part(url_holder, part, resume_id, aggregator, signals, total,
                           task_obj, first_byte_callback=None, refresh_url_fn=None):
        index = int(part["index"])
        if index == 0:
            if first_byte_callback:
                first_byte_callback()  # probe 首字节转正，允许第二个 worker
            write_part_file(resume_id, 0, content[:5])
            return "ok"
        if index == 1:
            # 等待另一个 worker 触发限流降档后再完成，保证 active > allowed 窗口
            ev_drop.wait(timeout=5)
            real_sleep(0.05)
            write_part_file(resume_id, 1, content[5:10])
            return "ok"
        if index == 2:
            if not index2_calls:
                index2_calls.append(True)
                ev_drop.set()
                return "rate_limited"  # 非 probe 限流 → allowed 降档
            write_part_file(resume_id, 2, content[10:15])
            return "ok"
        start = int(part["start"])
        write_part_file(resume_id, index, content[start:start + 5])
        return "ok"

    monkeypatch.setattr(download_resume, "_download_part", fake_download_part)

    result = _download_with_resume(
        "https://example.test/download", out_path, total_size,
        None, None, task, None,
    )

    assert result == out_path
    assert out_path.read_bytes() == content
    assert len(index2_calls) == 1


def test_download_with_resume_removes_stale_merged_file_before_merge(tmp_path, monkeypatch):
    total_size = 10
    db, task, out_path = _prepare_resume_flow(tmp_path, monkeypatch, total_size)
    content = b"0123456789"
    merged_path = get_merged_path(task.resume_id)
    merged_path.parent.mkdir(parents=True, exist_ok=True)
    merged_path.write_bytes(b"stale-merged")
    monkeypatch.setattr(
        download_resume, "_download_part", _ScriptedPartDownloader(content))

    result = _download_with_resume(
        "https://example.test/download", out_path, total_size,
        None, None, task, None,
    )

    assert result == out_path
    assert out_path.read_bytes() == content
    assert db.get_download_task(task.resume_id) is not None


def test_download_with_resume_fails_when_stale_merged_cleanup_fails(tmp_path, monkeypatch):
    total_size = 10
    db, task, out_path = _prepare_resume_flow(tmp_path, monkeypatch, total_size)
    merged_path = get_merged_path(task.resume_id)
    merged_path.parent.mkdir(parents=True, exist_ok=True)
    merged_path.mkdir()  # 目录：旧合并文件清理失败，合并写入也失败
    monkeypatch.setattr(
        download_resume, "_download_part", _ScriptedPartDownloader(b"0123456789"))

    with pytest.raises(RuntimeError, match="合并分片文件失败"):
        _download_with_resume(
            "https://example.test/download", out_path, total_size,
            None, None, task, None,
        )


def test_download_with_resume_cancel_during_merge_cleans_state(tmp_path, monkeypatch):
    total_size = 10
    db, task, out_path = _prepare_resume_flow(tmp_path, monkeypatch, total_size)
    monkeypatch.setattr(
        download_resume, "_download_part", _ScriptedPartDownloader(b"0123456789"))
    stop_task = SimpleNamespace(is_cancelled=False, pause_requested=False, cleanup_on_cancel=True)
    real_get_part_path = get_part_path

    def hook_get_part_path(resume_id, index):
        if index == 0:
            stop_task.is_cancelled = True  # 合并读取首个分片时触发取消
        return real_get_part_path(resume_id, index)

    monkeypatch.setattr(download_resume, "get_part_path", hook_get_part_path)

    result = _download_with_resume(
        "https://example.test/download", out_path, total_size,
        None, stop_task, task, None,
    )

    assert result == "已取消"
    assert db.get_download_task(task.resume_id) is None
    assert not get_temp_dir(task.resume_id).exists()


def test_download_with_resume_pause_during_merge_keeps_merged_file(tmp_path, monkeypatch):
    total_size = 10
    db, task, out_path = _prepare_resume_flow(tmp_path, monkeypatch, total_size)
    monkeypatch.setattr(
        download_resume, "_download_part", _ScriptedPartDownloader(b"0123456789"))
    stop_task = SimpleNamespace(is_cancelled=False, pause_requested=False)
    real_get_part_path = get_part_path

    def hook_get_part_path(resume_id, index):
        if index == 0:
            stop_task.pause_requested = True  # 合并读取首个分片时触发暂停
        return real_get_part_path(resume_id, index)

    monkeypatch.setattr(download_resume, "get_part_path", hook_get_part_path)

    real_unlink = Path.unlink

    def fake_unlink(self, missing_ok=False):
        if self.name == "merged":
            raise OSError("merged locked")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fake_unlink)

    result = _download_with_resume(
        "https://example.test/download", out_path, total_size,
        None, stop_task, task, None,
    )

    assert result == "已暂停"
    assert db.get_download_task(task.resume_id)["status"] == "已暂停"
    assert get_merged_path(task.resume_id).exists()  # 删除失败仅告警，合并文件保留


def test_download_with_resume_records_reused_bytes_in_speed_tracker(tmp_path, monkeypatch):
    total_size = 10
    db, task, out_path = _prepare_resume_flow(tmp_path, monkeypatch, total_size)
    content = b"0123456789"
    part0_path = get_part_path(task.resume_id, 0)
    part0_path.parent.mkdir(parents=True, exist_ok=True)
    part0_path.write_bytes(content[:5])
    db.record_download_part(task.resume_id, {
        "index": 0, "start": 0, "end": 4,
        "expected_size": 5, "actual_size": 5,
        "md5": hashlib.md5(content[:5]).hexdigest(),
    })
    speed_tracker = MagicMock()
    monkeypatch.setattr(
        download_resume, "_download_part", _ScriptedPartDownloader(content))
    # 有存量分片时会先 HEAD 校验远端文件未变更
    mock_session = MagicMock()
    mock_session.head = lambda *_a, **_kw: _MockResponse(
        headers={"Content-Length": str(total_size)})
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    result = _download_with_resume(
        "https://example.test/download", out_path, total_size,
        None, None, task, speed_tracker,
    )

    assert result == out_path
    assert out_path.read_bytes() == content
    assert speed_tracker.record.call_args_list[0].args[0] == 5  # 复用字节计入速度统计


# ---- _download_single_stream 分支补齐 ----


@pytest.mark.parametrize("status_code,message", [
    (403, "下载链接已过期或刷新失败"),
    (429, "下载被限流，请稍后重试"),
])
def test_single_stream_raises_on_error_status_without_refresh(
    tmp_path, monkeypatch, status_code, message,
):
    monkeypatch.setattr(download_resume, "CONFIG_DIR", tmp_path)

    mock_session = MagicMock()
    mock_session.get = lambda *_a, **_kw: _MockResponse(status_code=status_code)
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    with pytest.raises(RuntimeError, match=message):
        _download_single_stream(
            "https://example.test/file", tmp_path / "out.bin", 4,
            None, _make_part_task(), None, None,
        )


def test_single_stream_skips_empty_chunks(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    content = b"stream-data"
    out_path = tmp_path / "out.bin"
    task = _make_resume_task(out_path, hashlib.md5(content).hexdigest())

    class _EmptyFirstChunkResponse(_MockResponse):
        def iter_content(self, chunk_size=8192):
            yield b""
            yield content

    monkeypatch.setattr(
        download_resume, "_probe_download", lambda _url: (len(content), False, False))
    mock_session = MagicMock()
    mock_session.get = lambda *_a, **_kw: _EmptyFirstChunkResponse(body=content, status_code=200)
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    result = stream_download_from_url(
        "https://example.test/file", out_path, overwrite=True, resume_task=task,
    )

    assert result == out_path
    assert out_path.read_bytes() == content


def test_single_stream_pause_after_stream_keeps_temp_file(tmp_path, monkeypatch):
    monkeypatch.setattr(download_resume, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(download_resume.time, "time", lambda: 0.0)  # 循环内不触发节流进度
    content = b"abc"
    out_path = tmp_path / "paused-single.bin"
    task = _make_part_task()
    signals = MagicMock()
    # 最终进度发射时置为暂停：模拟流结束后用户点暂停
    signals.progress.emit.side_effect = lambda _percent: setattr(task, "pause_requested", True)

    class _SingleChunkResponse(_MockResponse):
        def iter_content(self, chunk_size=8192):
            yield content

    mock_session = MagicMock()
    mock_session.get = lambda *_a, **_kw: _SingleChunkResponse(body=content, status_code=200)
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    result = _download_single_stream(
        "https://example.test/file", out_path, 3, signals, task, None, None,
    )

    assert result == "已暂停"
    signals.status.emit.assert_called_with("已暂停")
    temp_files = list((tmp_path / "tmp" / "single_stream").iterdir())
    assert len(temp_files) == 1  # P2-19: 暂停时保留临时文件以便恢复


def test_single_stream_cancel_after_stream_deletes_state(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    content = b"abc"
    out_path = tmp_path / "cancel-single.bin"
    resume_task = _make_resume_task(out_path, "")
    _save_min_download_task(db, resume_task.resume_id, out_path)
    cancel_task = _make_part_task()
    cancel_task.cleanup_on_cancel = True
    signals = MagicMock()
    # 最终进度发射时置为取消：模拟流结束后用户点取消
    signals.progress.emit.side_effect = lambda _percent: setattr(
        cancel_task, "is_cancelled", True)

    class _SingleChunkResponse(_MockResponse):
        def iter_content(self, chunk_size=8192):
            yield content

    mock_session = MagicMock()
    mock_session.get = lambda *_a, **_kw: _SingleChunkResponse(body=content, status_code=200)
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    result = _download_single_stream(
        "https://example.test/file", out_path, 3, signals, cancel_task, resume_task, None,
    )

    assert result == "已取消"
    assert db.get_download_task(resume_task.resume_id) is None
    assert list((tmp_path / "tmp" / "single_stream").iterdir()) == []


def test_single_stream_raises_when_delivered_size_differs(tmp_path, monkeypatch):
    monkeypatch.setattr(download_resume, "CONFIG_DIR", tmp_path)
    out_path = tmp_path / "size-mismatch.bin"

    mock_session = MagicMock()
    mock_session.get = lambda *_a, **_kw: _MockResponse(body=b"12345", status_code=200)
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    with pytest.raises(RuntimeError, match="下载大小不匹配"):
        _download_single_stream(
            "https://example.test/file", out_path, 100,
            None, _make_part_task(), None, None,
        )


def test_single_stream_cleans_temp_file_on_mid_stream_error(tmp_path, monkeypatch):
    monkeypatch.setattr(download_resume, "CONFIG_DIR", tmp_path)
    out_path = tmp_path / "boom.bin"

    class _BrokenStreamResponse(_MockResponse):
        def iter_content(self, chunk_size=8192):
            yield b"partial"
            raise download_resume.requests.exceptions.ConnectionError("mid-stream boom")

    mock_session = MagicMock()
    mock_session.get = lambda *_a, **_kw: _BrokenStreamResponse(body=b"partial", status_code=200)
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    with pytest.raises(
        download_resume.requests.exceptions.ConnectionError, match="mid-stream boom",
    ):
        _download_single_stream(
            "https://example.test/file", out_path, 100,
            None, _make_part_task(), None, None,
        )

    assert list((tmp_path / "tmp" / "single_stream").iterdir()) == []


# ---- 入口函数边界 ----


def test_cleanup_stale_single_stream_files_removes_only_old_files(tmp_path, monkeypatch):
    monkeypatch.setattr(download_resume, "CONFIG_DIR", tmp_path)
    stale_dir = tmp_path / "tmp" / "single_stream"
    stale_dir.mkdir(parents=True)
    old_file = stale_dir / "old.bin"
    old_file.write_bytes(b"old")
    old_time = time.time() - 48 * 3600
    os.utime(old_file, (old_time, old_time))
    fresh_file = stale_dir / "fresh.bin"
    fresh_file.write_bytes(b"fresh")
    nested_dir = stale_dir / "nested"
    nested_dir.mkdir()

    _cleanup_stale_single_stream_files(max_age_hours=24)

    assert not old_file.exists()
    assert fresh_file.exists()
    assert nested_dir.exists()


def test_cleanup_stale_single_stream_files_swallows_entry_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(download_resume, "CONFIG_DIR", tmp_path)
    stale_dir = tmp_path / "tmp" / "single_stream"
    stale_dir.mkdir(parents=True)
    bad_entry = MagicMock()
    bad_entry.is_file.side_effect = OSError("stat boom")
    monkeypatch.setattr(Path, "iterdir", lambda _self: iter([bad_entry]))

    _cleanup_stale_single_stream_files(max_age_hours=24)  # 单个条目异常不中断清理


def test_cleanup_stale_single_stream_files_swallows_iterdir_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(download_resume, "CONFIG_DIR", tmp_path)
    stale_dir = tmp_path / "tmp" / "single_stream"
    stale_dir.mkdir(parents=True)

    def boom_iterdir(_self):
        raise OSError("iterdir boom")

    monkeypatch.setattr(Path, "iterdir", boom_iterdir)

    _cleanup_stale_single_stream_files(max_age_hours=24)  # 目录遍历异常不中断


def test_stream_download_rejects_unwritable_target_dir(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    out_dir = tmp_path / "readonly"
    out_dir.mkdir()
    out_path = out_dir / "file.bin"

    monkeypatch.setattr(download_resume.os, "access", lambda *_a, **_kw: False)

    with pytest.raises(PermissionError, match="目标目录不可写"):
        stream_download_from_url("https://example.test/file", out_path)

    assert not out_path.exists()


def test_stream_download_removes_residual_tmp_file(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    out_path = tmp_path / "residual.bin"
    tmp_residual = tmp_path / "residual.bin.tmp"
    tmp_residual.write_bytes(b"leftover")

    monkeypatch.setattr(download_resume, "_probe_download", lambda _url: (0, False, True))

    with pytest.raises(ConnectionError, match="下载探测失败"):
        stream_download_from_url("https://example.test/file", out_path)

    assert not tmp_residual.exists()


def test_stream_download_swallows_residual_tmp_cleanup_error(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    out_path = tmp_path / "residual-dir.bin"
    tmp_residual = tmp_path / "residual-dir.bin.tmp"
    tmp_residual.mkdir()  # 目录使 unlink 失败

    monkeypatch.setattr(download_resume, "_probe_download", lambda _url: (0, False, True))

    with pytest.raises(ConnectionError):
        stream_download_from_url("https://example.test/file", out_path)


def test_stream_download_raises_when_output_exists_without_overwrite(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    out_path = tmp_path / "exists.bin"
    out_path.write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        stream_download_from_url("https://example.test/file", out_path)


def test_stream_download_warns_when_failure_state_write_fails(tmp_path, monkeypatch):
    db = _use_temp_db(tmp_path, monkeypatch)
    out_path = tmp_path / "db-fail.bin"
    resume_task = _make_resume_task(out_path, "")
    _save_min_download_task(db, resume_task.resume_id, out_path)

    monkeypatch.setattr(download_resume, "_probe_download", lambda _url: (4, False, False))

    def broken_update(self, resume_id, **fields):
        raise RuntimeError("db boom")

    monkeypatch.setattr(Database, "update_download_task", broken_update)

    mock_session = MagicMock()
    mock_session.get = lambda *_a, **_kw: _MockResponse(status_code=429)
    monkeypatch.setattr(download_resume, "_dl_session", mock_session)

    with pytest.raises(RuntimeError, match="下载被限流"):
        stream_download_from_url(
            "https://example.test/file", out_path, overwrite=True, resume_task=resume_task,
        )
