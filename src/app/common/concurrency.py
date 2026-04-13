"""上传/下载共用的并发控制常量与慢启动调度工具。"""
import queue
import threading
import time

from .log import get_logger

logger = get_logger(__name__)

# ---- 并发控制常量 ----
RATE_LIMIT_CODES = frozenset({429, 503})
MAX_RATE_LIMITS = 50
RATE_LIMIT_BACKOFF = 2
PROGRESS_INTERVAL = 0.1


def slow_start_scheduler(
    worker_fn, max_workers, part_queue, progress_lock,
    active_workers, allowed_workers, failed,
    probe_thread_name, worker_feedback,
    is_stopped_fn, notify_conn_fn, thread_prefix="worker",
):
    """持续调度器：维持 allowed 个 normal worker + 1 个 probe worker。

    probe 收到首字节后转正（allowed += 1），立即启动下一个 probe。
    到达 max_workers 后停止 probe，仅维持 normal workers。
    """
    threads = []

    # 启动第 1 个 worker（即 probe）
    if part_queue.empty() or failed[0] or is_stopped_fn():
        return
    allowed_workers[0] = 1
    t = threading.Thread(target=worker_fn, name=f"{thread_prefix}_0", daemon=True)
    with progress_lock:
        probe_thread_name[0] = t.name
    threads.append(t)
    t.start()
    notify_conn_fn(1, 1)
    logger.debug("[调度器] 启动首个 probe: %s", t.name)

    # 事件驱动监控循环
    while True:
        worker_feedback.wait(timeout=5)
        # P2-18: 先 clear 再处理，避免吞掉并发 set() 通知
        # （最坏情况下延迟一个 timeout 周期，不会丢失）
        worker_feedback.clear()

        if failed[0] or is_stopped_fn():
            break

        with progress_lock:
            active = active_workers[0]
            allowed = allowed_workers[0]

        # 1. 补充 normal workers 到 allowed 水位
        while active < allowed and not part_queue.empty() and not failed[0]:
            t = threading.Thread(
                target=worker_fn,
                name=f"{thread_prefix}_{len(threads)}",
                daemon=True,
            )
            threads.append(t)
            t.start()
            active += 1
            logger.debug("[调度器] 补充 worker: active=%s, allowed=%s", active, allowed)

        # 2. 尝试启动 probe（无 probe + 未到上限 + 队列有活）
        with progress_lock:
            no_probe = probe_thread_name[0] is None
            can_probe = allowed_workers[0] < max_workers
        if no_probe and can_probe and not part_queue.empty() and not failed[0]:
            t = threading.Thread(
                target=worker_fn,
                name=f"{thread_prefix}_{len(threads)}",
                daemon=True,
            )
            threads.append(t)
            with progress_lock:
                probe_thread_name[0] = t.name
            t.start()
            logger.debug("[调度器] 启动 probe: %s, allowed=%s/%s", t.name, allowed_workers[0], max_workers)

        # 3. 完成 / 安全网
        with progress_lock:
            if active_workers[0] == 0 and part_queue.empty():
                # P0-1: 释放锁后 join 所有线程，再检查队列是否为空
                pass  # fall through to join-and-recheck below
            elif active_workers[0] == 0 and not part_queue.empty() and allowed_workers[0] >= 1:
                t = threading.Thread(
                    target=worker_fn,
                    name=f"{thread_prefix}_{len(threads)}",
                    daemon=True,
                )
                threads.append(t)
                # 安全网 worker 作为 probe 启动，恢复并发扩展能力
                if probe_thread_name[0] is None and allowed_workers[0] < max_workers:
                    probe_thread_name[0] = t.name
                t.start()
                logger.debug("[调度器] 安全网触发: 无活跃 worker 但队列非空, probe=%s", probe_thread_name[0])

        # P0-1: 疑似完成时，join 所有线程后再检查队列
        with progress_lock:
            maybe_done = active_workers[0] == 0 and part_queue.empty()
        if maybe_done:
            for t in threads:
                t.join(timeout=5)
            # worker finally 中可能将失败的 part 放回队列
            with progress_lock:
                if part_queue.empty():
                    break
                # 队列非空，继续循环
                logger.debug("[调度器] join 后队列非空，继续调度")

    for t in threads:
        t.join()
    logger.debug("[调度器] 调度结束, 共创建 %s 线程", len(threads))


class _ProgressAggregator:
    """Worker 线程推送增量字节，Aggregator 线程负责累加、速度追踪、UI 进度信号。"""

    def __init__(self, total, speed_tracker, signals, progress_interval):
        self._queue: queue.Queue[int] = queue.Queue()
        self._total = total
        self._speed_tracker = speed_tracker
        self._signals = signals
        self._progress_interval = progress_interval
        self._cumulative = 0        # 仅 Aggregator 线程写
        self._last_emit_time = 0.0  # 仅 Aggregator 线程写
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def cumulative(self) -> int:
        return self._cumulative  # CPython int 赋值原子，Worker 可安全读

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def set_initial(self, value: int):
        """设置初始累计值（仅在 start() 之前调用）。"""
        self._cumulative = value

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def record(self, n: int):
        """Worker 调用: 正值增量 / 负值回滚。"""
        self._queue.put(n)

    def emit_final(self):
        """排空队列后发射最终进度值。"""
        while not self._queue.empty():
            try:
                self._cumulative += self._queue.get_nowait()
            except queue.Empty:
                break
        if self._speed_tracker:
            self._speed_tracker.record(self._cumulative)
        if self._signals and self._total:
            self._signals.progress.emit(int(self._cumulative * 100 / self._total))

    def _run(self):
        while not self._stop_event.is_set():
            try:
                n = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            # 批量排空
            batch = [n]
            while not self._queue.empty():
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            delta = sum(batch)
            self._cumulative += delta
            if self._speed_tracker:
                self._speed_tracker.record(self._cumulative)
            now = time.monotonic()
            if now - self._last_emit_time >= self._progress_interval:
                if self._signals and self._total:
                    self._signals.progress.emit(
                        int(self._cumulative * 100 / self._total)
                    )
                self._last_emit_time = now
