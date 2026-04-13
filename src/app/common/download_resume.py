import errno
import hashlib
import io
import math
import os
import queue
import shutil
import threading
import time
import uuid
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

from .config import CONFIG_DIR
from .concurrency import (
    RATE_LIMIT_CODES, MAX_RATE_LIMITS, RATE_LIMIT_BACKOFF,
    PROGRESS_INTERVAL, slow_start_scheduler, _ProgressAggregator,
)
from .database import Database, get_download_part_size, _safe_int
from .log import get_logger

logger = get_logger(__name__)

MIN_PARALLEL_SIZE = 2 * 1024 * 1024
IO_CHUNK_SIZE = 1024 * 1024
MAX_PART_QUEUE_ATTEMPTS = 3
DEFAULT_MAX_DOWNLOAD_THREADS = 1
SINGLE_STREAM_READ_TIMEOUT = 60

_dl_session = requests.Session()
_dl_session.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=16, max_retries=1))


def build_resume_id(account_name, file_id, save_path):
    raw = f"{account_name}|{file_id}|{Path(save_path).resolve()}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _compute_md5(file_path):
    md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            md5.update(chunk)
    return md5.hexdigest()


def get_temp_dir(resume_id):
    """返回下载临时目录: CONFIG_DIR/tmp/<resume_id>/"""
    return CONFIG_DIR / "tmp" / resume_id


def get_part_path(resume_id, index):
    return get_temp_dir(resume_id) / f"part{index}"


def get_merged_path(resume_id):
    return get_temp_dir(resume_id) / "merged"


def cleanup_temp_dir(resume_id):
    temp_dir = get_temp_dir(resume_id)
    if temp_dir.exists():
        shutil.rmtree(str(temp_dir), ignore_errors=True)


def _replace_output_file(src_path: Path, out_path: Path) -> Path:
    try:
        src_path.replace(out_path)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        # 跨盘：先写临时文件再 rename，避免直接覆盖目标
        tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
        try:
            shutil.copy2(str(src_path), str(tmp_path))
            src_size = src_path.stat().st_size
            tmp_size = tmp_path.stat().st_size
            if tmp_size != src_size:
                tmp_path.unlink(missing_ok=True)
                raise OSError(f"跨盘拷贝大小不匹配: 预期 {src_size}, 实际 {tmp_size}")
            tmp_path.replace(out_path)
            src_path.unlink(missing_ok=True)
        except BaseException:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise
    return out_path


# ---- signal helpers ----

def _notify_progress(signals, total, downloaded):
    if signals and total:
        signals.progress.emit(int(downloaded * 100 / total))


def _notify_conn_info(signals, active_workers, max_workers):
    if signals and hasattr(signals, "conn_info"):
        signals.conn_info.emit(active_workers, max_workers)


def _notify_status(signals, status):
    if signals and hasattr(signals, "status"):
        signals.status.emit(status)


# ---- task state helpers ----

def _is_task_cancelled(task):
    return bool(task and getattr(task, "is_cancelled", False))


def _is_task_paused(task):
    return bool(task and getattr(task, "pause_requested", False))


def _get_stop_result(task):
    if _is_task_cancelled(task):
        return "cancelled"
    if _is_task_paused(task):
        return "paused"
    return None


# ---- DB helpers ----

def _save_download_status(resume_id, total, downloaded, status, error=None):
    if not resume_id:
        return
    progress = int(downloaded * 100 / total) if total else 0
    Database.instance().update_download_task(
        resume_id,
        progress=progress,
        status=status,
        error=error or "",
        file_size=total,
    )


def _delete_download_resume_state(resume_task):
    if not resume_task:
        return
    Database.instance().delete_download_task(resume_task.resume_id)
    cleanup_temp_dir(resume_task.resume_id)


def _reset_partial_download(part_path, aggregator, byte_count, resume_id, index):
    if byte_count:
        aggregator.record(-byte_count)
    Database.instance().remove_download_part(resume_id, index)
    if part_path.exists():
        try:
            part_path.unlink()
        except OSError:
            logger.warning("删除失败的分片文件失败: %s", part_path)


def _cleanup_parts(resume_id, part_indexes):
    for index in part_indexes:
        p = get_part_path(resume_id, index)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                logger.warning("删除分片失败: %s", p)


def _verify_completed_download(file_path, total, resume_task):
    expected_etag = (resume_task.etag or "").strip().strip('"').lower()
    if expected_etag and "-" not in expected_etag:
        actual_etag = _compute_md5(file_path).lower()
        if actual_etag != expected_etag:
            raise RuntimeError("整文件校验失败，需要重新下载")
        return
    actual_size = file_path.stat().st_size
    if total and actual_size != total:
        raise RuntimeError(f"文件大小不匹配: 预期 {total}, 实际 {actual_size}")
    if expected_etag:
        logger.info("ETag 无法作为整文件 MD5，仅执行大小校验: %s", expected_etag)


# ---- part plan ----

def _build_parts(total, part_size=None):
    if total <= 0:
        return []
    ps = part_size or get_download_part_size()
    part_count = math.ceil(total / ps)
    parts = []
    for index in range(part_count):
        start = index * ps
        end = min(start + ps - 1, total - 1)
        parts.append({
            "index": index, "start": start, "end": end,
            "expected_size": end - start + 1,
        })
    return parts


def _validate_existing_parts(resume_id, part_plan):
    db = Database.instance()
    stored = {p["part_index"]: p for p in db.get_download_parts(resume_id)}
    logger.info(
        "断点续传校验: resume_id=%s, DB分片数=%d, 新分片计划数=%d, part_size=%dMB",
        resume_id[:8], len(stored), len(part_plan),
        (part_plan[0]["expected_size"] // 1024 // 1024) if part_plan else 0,
    )
    reusable_indexes = []
    downloaded = 0
    for part in part_plan:
        index = int(part["index"])
        sp = stored.get(index)
        if not sp:
            continue
        p_path = get_part_path(resume_id, index)
        expected_size = int(part["expected_size"])
        expected_hash = sp.get("md5")
        if not expected_hash or not p_path.exists():
            logger.debug("分片 %d 跳过: hash=%s file_exists=%s", index, bool(expected_hash), p_path.exists())
            db.remove_download_part(resume_id, index)
            continue
        actual_size = p_path.stat().st_size
        if actual_size != expected_size:
            logger.info("分片 %d 大小不匹配: disk=%d plan=%d", index, actual_size, expected_size)
            try:
                p_path.unlink()
            except OSError:
                logger.warning("删除大小不匹配的分片失败: %s", p_path)
            db.remove_download_part(resume_id, index)
            continue
        actual_hash = _compute_md5(p_path)
        if actual_hash != expected_hash:
            logger.info("分片 %d MD5不匹配", index)
            try:
                p_path.unlink()
            except OSError:
                logger.warning("删除MD5不匹配的分片失败: %s", p_path)
            db.remove_download_part(resume_id, index)
            continue
        reusable_indexes.append(index)
        downloaded += expected_size

    # 清理孤立分片文件（不在 reusable_indexes 中的 part 和 merged）
    temp_dir = get_temp_dir(resume_id)
    if temp_dir.exists():
        reusable_set = set(reusable_indexes)
        for entry in temp_dir.iterdir():
            name = entry.name
            if name == "merged":
                try:
                    entry.unlink()
                except OSError:
                    logger.warning("删除合并残留文件失败: %s", entry)
                continue
            if name.startswith("part"):
                try:
                    idx = int(name[4:])
                except (ValueError, IndexError):
                    continue
                if idx not in reusable_set:
                    try:
                        entry.unlink()
                    except OSError:
                        logger.warning("删除孤立分片失败: %s", entry)

    return downloaded, reusable_indexes


def _prepare_resume_metadata(out_path, total, resume_task, multi_part_enabled):
    if not resume_task:
        return None
    db = Database.instance()
    # P0-4: 如果 DB 中已有 supports_resume=1 且有已完成分片，保留旧值不覆盖
    effective_supports_resume = int(multi_part_enabled)
    existing = db.get_download_task(resume_task.resume_id)
    if existing and existing.get("supports_resume", 0) == 1:
        stored_parts = db.get_download_parts(resume_task.resume_id)
        if stored_parts:
            effective_supports_resume = 1
    task_data = {
        "resume_id": resume_task.resume_id,
        "account_name": resume_task.account_name,
        "file_name": resume_task.file_name,
        "file_id": resume_task.file_id,
        "file_type": resume_task.file_type,
        "file_size": total or resume_task.file_size,
        "save_path": str(out_path),
        "current_dir_id": getattr(resume_task, "current_dir_id", 0),
        "etag": resume_task.etag,
        "s3key_flag": int(resume_task.s3key_flag),
        "status": resume_task.status,
        "progress": resume_task.progress,
        "error": resume_task.last_error,
        "supports_resume": effective_supports_resume,
        "metadata_version": getattr(resume_task, "metadata_version", 2),
        "part_size": get_download_part_size(),  # P1-9: 持久化分片大小
    }
    db.save_download_task(task_data)
    return task_data


# ---- probe ----

def _probe_download(redirect_url):
    total = 0
    accept_ranges = False
    try:
        head = _dl_session.head(redirect_url, allow_redirects=True, timeout=30)
        if head.status_code in RATE_LIMIT_CODES:
            logger.debug("下载探测 HEAD 被限流: %s", head.status_code)
            return 0, False, False
        head.raise_for_status()
        total = int(head.headers.get("Content-Length", 0) or 0)
        accept_ranges = head.headers.get("Accept-Ranges", "").lower() == "bytes"
    except requests.ConnectionError as exc:
        logger.debug("下载探测连接失败: %s", exc)
        return 0, False, True
    except requests.Timeout:
        logger.debug("下载探测超时")
        return 0, False, True
    except requests.RequestException:
        try:
            with _dl_session.get(redirect_url, stream=True, timeout=30) as r:
                if r.status_code in RATE_LIMIT_CODES:
                    logger.debug("下载探测 GET 被限流: %s", r.status_code)
                    return 0, False, False
                r.raise_for_status()
                total = int(r.headers.get("Content-Length", 0) or 0)
                accept_ranges = r.headers.get("Accept-Ranges", "").lower() == "bytes"
        except requests.RequestException as exc:
            logger.debug("下载探测 GET 失败: %s", exc)
            return 0, False, True
    except Exception as e:
        logger.warning("下载探测失败: %s", e)
        return 0, False, True
    return total, accept_ranges, False


# ---- download part ----

def _download_part(
    url_holder, part, resume_id, aggregator, signals, total, task,
    first_byte_callback=None, refresh_url_fn=None,
):
    index = int(part["index"])
    start = int(part["start"])
    end = int(part["end"])
    part_path = get_part_path(resume_id, index)
    part_path.parent.mkdir(parents=True, exist_ok=True)
    headers = {"Range": f"bytes={start}-{end}"}
    queue_attempt = int(part.get("attempt", 0))
    io_chunk_size = min(IO_CHUNK_SIZE, max(8192, int(part["expected_size"] / 100) or 8192))
    max_retries = _safe_int(
        Database.instance().get_config("retryMaxAttempts", 3), 3, 0, 5
    )

    first_byte_signaled = False
    attempt = 0
    refresh_403_count = 0
    max_refresh_403 = 3
    buf = io.BytesIO()
    while True:
        attempt_downloaded = 0
        stop_result = _get_stop_result(task)
        if stop_result:
            return stop_result
        try:
            md5 = hashlib.md5()
            buf.seek(0)
            buf.truncate()
            try:
                with _dl_session.get(url_holder[0], headers=headers, stream=True, timeout=(5, 600)) as resp:
                    if task is not None:
                        with task._response_lock:
                            task._active_response = resp
                    if resp.status_code == 403:
                        if refresh_url_fn:
                            try:
                                new_url = refresh_url_fn()
                                if new_url and not isinstance(new_url, int):
                                    url_holder[0] = new_url
                                    refresh_403_count += 1
                                    if refresh_403_count > max_refresh_403:
                                        logger.warning("分片 %d 连续 %d 次 403 refresh 仍失败", index, refresh_403_count)
                                        return "url_expired"
                                    logger.debug("分片 %d URL 已刷新，重试 (%d/%d)", index, refresh_403_count, max_refresh_403)
                                    continue
                            except Exception as exc:
                                logger.warning("分片 %d 刷新 URL 失败: %s", index, exc)
                        return "url_expired"
                    if resp.status_code in RATE_LIMIT_CODES:
                        return "rate_limited"
                    resp.raise_for_status()
                    for chunk in resp.iter_content(chunk_size=io_chunk_size):
                        stop_result = _get_stop_result(task)
                        if stop_result:
                            # 丢弃内存 buffer，回滚 aggregator 进度
                            if attempt_downloaded:
                                aggregator.record(-attempt_downloaded)
                            Database.instance().remove_download_part(resume_id, index)
                            return stop_result
                        if not chunk:
                            continue
                        if not first_byte_signaled and first_byte_callback:
                            first_byte_callback()
                            first_byte_signaled = True
                        buf.write(chunk)
                        md5.update(chunk)
                        attempt_downloaded += len(chunk)
                        aggregator.record(len(chunk))
            finally:
                if task is not None:
                    with task._response_lock:
                        task._active_response = None
            # 校验 + 一次性写磁盘
            data = buf.getvalue()
            actual_size = len(data)
            if actual_size != int(part["expected_size"]):
                raise RuntimeError(f"分片 {index} 大小不匹配")
            with open(part_path, "wb") as f:
                f.write(data)
            Database.instance().record_download_part(resume_id, {
                "index": index, "start": start, "end": end,
                "expected_size": int(part["expected_size"]),
                "actual_size": actual_size, "md5": md5.hexdigest(),
            }, commit=False)
            part["attempt"] = 0
            return "ok"
        except (requests.RequestException, RuntimeError, OSError) as exc:
            # 失败时回滚 aggregator 进度，清理 DB 记录
            if attempt_downloaded:
                aggregator.record(-attempt_downloaded)
            Database.instance().remove_download_part(resume_id, index)
            # 清理磁盘上可能存在的旧 part 文件（兜底历史残留）
            if part_path.exists():
                try:
                    part_path.unlink()
                except OSError:
                    logger.warning("删除失败的分片文件失败: %s", part_path)
            logger.warning("分片 %d 下载失败 (attempt=%d/%d): %s", index, attempt, max_retries, exc)
            if attempt >= max_retries:
                queue_attempt += 1
                part["attempt"] = queue_attempt
                if queue_attempt >= MAX_PART_QUEUE_ATTEMPTS:
                    return "fatal"
                return "retryable"
            attempt += 1
            time.sleep(attempt)
    return "fatal"


# ---- multi-part download ----

def _download_with_resume(redirect_url, out_path, total, signals, task, resume_task, speed_tracker, refresh_url_fn=None):
    resume_id = resume_task.resume_id
    db = Database.instance()
    # P1-9: 优先从 DB 读取持久化的 part_size，避免用户修改配置后分片计划不匹配
    stored_task = db.get_download_task(resume_id)
    stored_part_size = None
    if stored_task and stored_task.get("part_size", 0) > 0:
        stored_part_size = stored_task["part_size"]
    else:
        # 兼容旧数据：从已有分片推断
        stored_parts = db.get_download_parts(resume_id)
        if stored_parts:
            for sp in stored_parts:
                if sp["expected_size"] > 0 and sp["part_index"] < len(stored_parts) - 1:
                    stored_part_size = sp["expected_size"]
                    break
            if stored_part_size is None and stored_parts:
                stored_part_size = stored_parts[0]["expected_size"]
    part_plan = _build_parts(total, part_size=stored_part_size)
    _prepare_resume_metadata(out_path, total, resume_task, True)

    reused_bytes, reusable_indexes = _validate_existing_parts(resume_id, part_plan)
    reusable = set(reusable_indexes)
    logger.info(
        "断点续传结果: resume_id=%s total=%d reused=%d reusable_parts=%d/%d",
        resume_id[:8], total, reused_bytes, len(reusable), len(part_plan),
    )
    _save_download_status(resume_id, total, reused_bytes, "校验中")
    _notify_status(signals, "校验中")
    _notify_progress(signals, total, reused_bytes)

    part_queue: queue.Queue[dict] = queue.Queue()
    for part in part_plan:
        if int(part["index"]) not in reusable:
            part_queue.put(part)

    max_workers = min(
        max(1, _safe_int(Database.instance().get_config("maxDownloadThreads", DEFAULT_MAX_DOWNLOAD_THREADS))),
        16,
    )
    url_holder = [redirect_url]
    active_workers = [0]
    allowed_workers = [1]
    failed = [False]
    rate_limit_count = [0]
    url_expired_count = [0]
    parts_since_commit = [0]
    progress_lock = threading.Lock()
    worker_feedback = threading.Event()
    probe_thread_name = [None]

    aggregator = _ProgressAggregator(total, speed_tracker, signals, PROGRESS_INTERVAL)
    aggregator.set_initial(reused_bytes)
    if speed_tracker and reused_bytes:
        speed_tracker.record(reused_bytes)
    aggregator.start()

    def _try_promote_probe():
        """当前线程是 probe 且收到首字节 → 转正。"""
        with progress_lock:
            if threading.current_thread().name == probe_thread_name[0]:
                probe_thread_name[0] = None
                if allowed_workers[0] < max_workers:
                    allowed_workers[0] += 1
        worker_feedback.set()

    def worker():
        with progress_lock:
            active_workers[0] += 1
            _notify_conn_info(signals, active_workers[0], max_workers)
        try:
            while not failed[0]:
                stop_result = _get_stop_result(task)
                if stop_result:
                    return
                with progress_lock:
                    is_probe = threading.current_thread().name == probe_thread_name[0]
                    if not is_probe and active_workers[0] > allowed_workers[0] and active_workers[0] > 1:
                        return
                try:
                    part = part_queue.get_nowait()
                except queue.Empty:
                    return

                result = _download_part(
                    url_holder, part, resume_id, aggregator,
                    signals, total, task,
                    first_byte_callback=_try_promote_probe,
                    refresh_url_fn=refresh_url_fn,
                )
                if result == "ok":
                    url_expired_count[0] = 0
                    parts_since_commit[0] += 1
                    if parts_since_commit[0] >= 10:
                        Database.instance().flush()
                        parts_since_commit[0] = 0
                    _save_download_status(resume_id, total, aggregator.cumulative, "下载中")
                    worker_feedback.set()
                    continue
                if result in ("cancelled", "paused"):
                    return
                if result == "rate_limited":
                    with progress_lock:
                        rate_limit_count[0] += 1
                        if rate_limit_count[0] > MAX_RATE_LIMITS:
                            failed[0] = True
                            return
                        if threading.current_thread().name == probe_thread_name[0]:
                            probe_thread_name[0] = None
                        else:
                            new_limit = max(1, active_workers[0] - 1)
                            if new_limit < allowed_workers[0]:
                                allowed_workers[0] = new_limit
                    part_queue.put(part)
                    worker_feedback.set()
                    time.sleep(RATE_LIMIT_BACKOFF)
                    continue
                if result == "retryable":
                    part_queue.put(part)
                    with progress_lock:
                        if threading.current_thread().name == probe_thread_name[0]:
                            probe_thread_name[0] = None
                        else:
                            allowed_workers[0] = max(1, allowed_workers[0] - 1)
                    worker_feedback.set()
                    return  # worker 退出，调度器补充
                if result == "url_expired":
                    url_expired_count[0] += 1
                    part_queue.put(part)
                    if url_expired_count[0] >= 3:
                        logger.error("连续 %d 次刷新 URL 失败，终止下载", url_expired_count[0])
                        failed[0] = True
                        return
                    with progress_lock:
                        if threading.current_thread().name == probe_thread_name[0]:
                            probe_thread_name[0] = None
                        else:
                            allowed_workers[0] = max(1, allowed_workers[0] - 1)
                    worker_feedback.set()
                    time.sleep(2)
                    return
                failed[0] = True
                worker_feedback.set()
                return
        finally:
            with progress_lock:
                if threading.current_thread().name == probe_thread_name[0]:
                    probe_thread_name[0] = None
                active_workers[0] -= 1
                _notify_conn_info(signals, active_workers[0], max_workers)
            worker_feedback.set()

    _save_download_status(resume_id, total, reused_bytes, "下载中")
    _notify_status(signals, "下载中")

    slow_start_scheduler(
        worker_fn=worker,
        max_workers=max_workers,
        part_queue=part_queue,
        progress_lock=progress_lock,
        active_workers=active_workers,
        allowed_workers=allowed_workers,
        failed=failed,
        probe_thread_name=probe_thread_name,
        worker_feedback=worker_feedback,
        is_stopped_fn=lambda: bool(_get_stop_result(task)),
        notify_conn_fn=lambda a, _al: _notify_conn_info(signals, a, max_workers),
        thread_prefix="dl_worker",
    )

    aggregator.stop()
    aggregator.emit_final()
    _notify_conn_info(signals, 0, max_workers)

    # 统一提交所有分片记录（配合 record_download_part(commit=False)）
    Database.instance().flush()

    if _is_task_paused(task):
        _save_download_status(resume_id, total, aggregator.cumulative, "已暂停", "")
        _notify_status(signals, "已暂停")
        return "已暂停"

    if _is_task_cancelled(task):
        _save_download_status(resume_id, total, aggregator.cumulative, "已取消", "用户取消下载")
        _notify_status(signals, "已取消")
        if getattr(task, "cleanup_on_cancel", False):
            Database.instance().delete_download_task(resume_id)
            cleanup_temp_dir(resume_id)
        return "已取消"

    if failed[0]:
        _save_download_status(resume_id, total, aggregator.cumulative, "失败", "分片下载失败")
        _notify_status(signals, "失败")
        raise RuntimeError("分片下载失败")

    # ---- 合并阶段 ----
    _save_download_status(resume_id, total, aggregator.cumulative, "合并中")
    _notify_status(signals, "合并中")
    merged_path = get_merged_path(resume_id)
    merged_path.parent.mkdir(parents=True, exist_ok=True)
    # 清理旧的合并文件（防止上次合并中断残留）
    if merged_path.exists():
        try:
            merged_path.unlink()
        except OSError:
            logger.warning("删除旧合并文件失败: %s", merged_path)
    try:
        with open(merged_path, "wb") as output:
            for part in part_plan:
                stop_result = _get_stop_result(task)
                if stop_result:
                    break
                pp = get_part_path(resume_id, int(part["index"]))
                with open(pp, "rb") as pf:
                    while True:
                        chunk = pf.read(IO_CHUNK_SIZE)
                        if not chunk:
                            break
                        output.write(chunk)
    except OSError as exc:
        raise RuntimeError(f"合并分片文件失败: {exc}") from exc

    if _is_task_paused(task):
        _save_download_status(resume_id, total, aggregator.cumulative, "已暂停", "")
        _notify_status(signals, "已暂停")
        try:
            merged_path.unlink()
        except OSError:
            logger.warning("暂停时删除合并文件失败: %s", merged_path)
        return "已暂停"

    if _is_task_cancelled(task):
        _save_download_status(resume_id, total, aggregator.cumulative, "已取消", "用户取消下载")
        if getattr(task, "cleanup_on_cancel", False):
            Database.instance().delete_download_task(resume_id)
            cleanup_temp_dir(resume_id)
        return "已取消"

    try:
        _verify_completed_download(merged_path, total, resume_task)
    except RuntimeError:
        cleanup_temp_dir(resume_id)
        Database.instance().delete_download_task(resume_id)
        raise

    # 移动到目标位置
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _replace_output_file(merged_path, out_path)
    Database.instance().update_download_task(resume_id, status="已完成")
    cleanup_temp_dir(resume_id)
    return out_path


# ---- single stream download ----

def _download_single_stream(
    redirect_url, out_path, total, signals, task, resume_task, speed_tracker, refresh_url_fn=None,
):
    temp_dir = CONFIG_DIR / "tmp" / "single_stream"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"{uuid.uuid4().hex}_{out_path.name}"
    current_url = redirect_url
    refresh_403_count = 0
    max_refresh_403 = 3
    _notify_status(signals, "下载中")
    try:
        while True:
            with _dl_session.get(current_url, stream=True, timeout=(5, SINGLE_STREAM_READ_TIMEOUT)) as response:
                if task is not None:
                    with task._response_lock:
                        task._active_response = response
                if response.status_code == 403:
                    if refresh_url_fn:
                        new_url = refresh_url_fn()
                        if new_url and not isinstance(new_url, int):
                            refresh_403_count += 1
                            if refresh_403_count > max_refresh_403:
                                raise RuntimeError("下载链接已过期或刷新失败")
                            current_url = new_url
                            continue
                    raise RuntimeError("下载链接已过期或刷新失败")
                if response.status_code in RATE_LIMIT_CODES:
                    raise RuntimeError("下载被限流，请稍后重试")
                response.raise_for_status()
                done = 0
                last_t = 0.0
                with open(temp_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=256 * 1024):
                        stop_result = _get_stop_result(task)
                        if stop_result:
                            f.close()
                            # P2-19: 暂停时保留临时文件，取消时才删除
                            if stop_result == "cancelled" and temp_path.exists():
                                temp_path.unlink()
                            status = "已暂停" if stop_result == "paused" else "已取消"
                            if stop_result == "cancelled" and getattr(task, "cleanup_on_cancel", False):
                                _delete_download_resume_state(resume_task)
                            _notify_status(signals, status)
                            return status
                        if not chunk:
                            continue
                        f.write(chunk)
                        done += len(chunk)
                        if speed_tracker:
                            speed_tracker.record(done)
                        now = time.time()
                        if now - last_t > PROGRESS_INTERVAL:
                            _notify_progress(signals, total, done)
                            last_t = now
            break
        _notify_progress(signals, total, done)
        if _is_task_paused(task):
            # P2-19: 暂停时保留临时文件以便恢复
            _notify_status(signals, "已暂停")
            return "已暂停"
        if _is_task_cancelled(task):
            if temp_path.exists():
                temp_path.unlink()
            if getattr(task, "cleanup_on_cancel", False):
                _delete_download_resume_state(resume_task)
            _notify_status(signals, "已取消")
            return "已取消"
        # H3: 大小校验
        if total and done != total:
            temp_path.unlink(missing_ok=True)
            raise RuntimeError(f"下载大小不匹配: 预期 {total}, 实际 {done}")
        try:
            _verify_completed_download(temp_path, total, resume_task)
        except RuntimeError:
            temp_path.unlink(missing_ok=True)
            raise
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _replace_output_file(temp_path, out_path)
        return out_path
    except BaseException:
        # M14: 异常时清理临时文件
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        raise
    finally:
        if task is not None:
            with task._response_lock:
                task._active_response = None


# ---- entry point ----

def _cleanup_stale_single_stream_files(max_age_hours=24):
    """清理 single_stream 目录中超过 max_age_hours 小时的临时文件。"""
    stale_dir = CONFIG_DIR / "tmp" / "single_stream"
    if not stale_dir.exists():
        return
    try:
        cutoff = time.time() - max_age_hours * 3600
        for entry in stale_dir.iterdir():
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    entry.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass


def stream_download_from_url(
    redirect_url, out_path, signals=None, task=None,
    overwrite=False, resume_task=None, speed_tracker=None,
    refresh_url_fn=None,
):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not os.access(str(out_path.parent), os.W_OK):
        raise PermissionError(f"目标目录不可写: {out_path.parent}")

    # 清理上次崩溃残留的 .tmp 文件
    tmp_residual = out_path.with_suffix(out_path.suffix + ".tmp")
    if tmp_residual.exists():
        try:
            tmp_residual.unlink(missing_ok=True)
        except OSError:
            pass

    _cleanup_stale_single_stream_files()

    if out_path.exists() and not overwrite:
        raise FileExistsError(str(out_path))

    total, accept_ranges, probe_connection_failed = _probe_download(redirect_url)
    if probe_connection_failed:
        raise ConnectionError("下载探测失败，无法连接服务器")
    multi_part_enabled = bool(accept_ranges and total and total > MIN_PARALLEL_SIZE)

    if resume_task:
        _prepare_resume_metadata(out_path, total, resume_task, multi_part_enabled)

    try:
        if multi_part_enabled:
            return _download_with_resume(
                redirect_url, out_path, total, signals, task,
                resume_task, speed_tracker, refresh_url_fn,
            )
        return _download_single_stream(
            redirect_url, out_path, total, signals, task, resume_task, speed_tracker, refresh_url_fn,
        )
    except Exception as exc:
        _notify_status(signals, "失败")
        if resume_task:
            try:
                db = Database.instance()
                existing = db.get_download_task(resume_task.resume_id)
                progress = existing.get("progress", 0) if existing else 0
                db.update_download_task(
                    resume_task.resume_id,
                    status="失败", progress=progress,
                    error=str(exc), file_size=total or resume_task.file_size,
                )
            except Exception:
                logger.warning("写入下载失败状态时异常: %s", resume_task.resume_id)
        raise
