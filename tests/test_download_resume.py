import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.app.common import database as database_module
from src.app.common import download_resume
from src.app.common.database import Database
from src.app.common.download_resume import (
    _download_part,
    _validate_existing_parts,
    build_resume_id,
    get_part_path,
    get_temp_dir,
    stream_download_from_url,
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

    def fake_head(url, allow_redirects=True, timeout=30):
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

    monkeypatch.setattr(download_resume.requests, "head", fake_head)
    monkeypatch.setattr(download_resume.requests, "get", fake_get)

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

    total_size = PART_SIZE
    content = b"z" * total_size
    out_path = tmp_path / "broken.bin"
    task = _make_resume_task(out_path, "0000000000000000deadbeef00000000")

    def fake_head(url, allow_redirects=True, timeout=30):
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

    monkeypatch.setattr(download_resume.requests, "head", fake_head)
    monkeypatch.setattr(download_resume.requests, "get", fake_get)

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

    monkeypatch.setattr(download_resume, "_probe_download", lambda _url: (123, False))
    monkeypatch.setattr(
        download_resume.requests,
        "get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("network boom")),
    )

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

    total_size = PART_SIZE + 512
    content = (b"a" * PART_SIZE) + (b"b" * 512)
    out_path = tmp_path / "retryable.bin"
    task = _make_resume_task(out_path, download_resume.hashlib.md5(content).hexdigest())
    part_retry_range = f"bytes={PART_SIZE}-{total_size - 1}"
    attempts = {part_retry_range: 0}
    requested_ranges = []

    def fake_head(url, allow_redirects=True, timeout=30):
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

    monkeypatch.setattr(download_resume.requests, "head", fake_head)
    monkeypatch.setattr(download_resume.requests, "get", fake_get)

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
    monkeypatch.setattr(download_resume.time, "sleep", lambda *_args, **_kwargs: None)

    total_size = PART_SIZE + 512
    content = (b"a" * PART_SIZE) + (b"b" * 512)
    out_path = tmp_path / "rate-limit.bin"
    task = _make_resume_task(out_path, download_resume.hashlib.md5(content).hexdigest())
    first_range = f"bytes=0-{PART_SIZE - 1}"
    attempts = {first_range: 0}
    requested_ranges = []

    def fake_head(url, allow_redirects=True, timeout=30):
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

    monkeypatch.setattr(download_resume.requests, "head", fake_head)
    monkeypatch.setattr(download_resume.requests, "get", fake_get)

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

    pause_task = _PauseTask()

    class _PauseResponse(_MockResponse):
        def iter_content(self, chunk_size=8192):
            first = True
            for offset in range(0, len(self.body), chunk_size):
                if first:
                    pause_task.pause_requested = True
                    first = False
                yield self.body[offset: offset + chunk_size]

    def fake_head(url, allow_redirects=True, timeout=30):
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

    monkeypatch.setattr(download_resume.requests, "head", fake_head)
    monkeypatch.setattr(download_resume.requests, "get", fake_get)

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

    task = SimpleNamespace(is_cancelled=False, pause_requested=False)

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

    monkeypatch.setattr(download_resume.requests, "get", fake_get)
    monkeypatch.setattr(download_resume.time, "sleep", lambda *_a, **_kw: None)

    result = _download_part(
        "https://example.test/file",
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
    task = SimpleNamespace(is_cancelled=False, pause_requested=False)

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

    monkeypatch.setattr(download_resume.requests, "get", fake_get)

    # part 文件在调用前不存在
    assert not part_path.exists()

    result = _download_part(
        "https://example.test/file",
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
