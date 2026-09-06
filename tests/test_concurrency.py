import queue
import threading
import time
from unittest.mock import MagicMock

from src.app.common.concurrency import _ProgressAggregator, slow_start_scheduler


def test_slow_start_scheduler_returns_immediately_when_queue_is_empty():
    part_queue: queue.Queue[dict] = queue.Queue()
    progress_lock = threading.Lock()
    active_workers = [0]
    allowed_workers = [1]
    failed = [False]
    probe_thread_name = [None]

    class _Event:
        def wait(self, timeout=None):
            raise AssertionError("wait should not be called for an empty queue")

        def clear(self):
            raise AssertionError("clear should not be called for an empty queue")

    worker_feedback = _Event()

    slow_start_scheduler(
        worker_fn=lambda: None,
        max_workers=4,
        part_queue=part_queue,
        progress_lock=progress_lock,
        active_workers=active_workers,
        allowed_workers=allowed_workers,
        failed=failed,
        probe_thread_name=probe_thread_name,
        worker_feedback=worker_feedback,
        is_stopped_fn=lambda: False,
        notify_conn_fn=lambda active, allowed: None,
        thread_prefix="upload_worker",
    )


# ---- 4a. _ProgressAggregator ----


def test_aggregator_cumulative_initial_zero():
    agg = _ProgressAggregator(1000, None, None, 0.1)
    assert agg.cumulative == 0


def test_aggregator_record_queues_values():
    agg = _ProgressAggregator(1000, None, None, 0.1)
    agg.record(100)
    agg.record(200)
    # 值还在队列中，cumulative 尚未更新
    assert agg.cumulative == 0
    agg.emit_final()
    assert agg.cumulative == 300


def test_aggregator_emit_final_drains_and_emits():
    signals = MagicMock()
    agg = _ProgressAggregator(1000, None, signals, 0.1)
    agg.record(500)
    agg.emit_final()
    assert agg.cumulative == 500
    signals.progress.emit.assert_called_once_with(50)


def test_aggregator_emit_final_skips_when_total_zero():
    signals = MagicMock()
    agg = _ProgressAggregator(0, None, signals, 0.1)
    agg.record(100)
    agg.emit_final()
    signals.progress.emit.assert_not_called()


def test_aggregator_set_initial():
    agg = _ProgressAggregator(1000, None, None, 0.1)
    agg.set_initial(500)
    assert agg.cumulative == 500


def test_aggregator_start_stop_lifecycle():
    agg = _ProgressAggregator(1000, None, None, 0.1)
    agg.start()
    agg.record(100)
    agg.stop()
    assert agg.cumulative == 100


def test_aggregator_speed_tracker_called():
    tracker = MagicMock()
    signals = MagicMock()
    agg = _ProgressAggregator(1000, tracker, signals, 0.1)
    agg.record(200)
    agg.emit_final()
    tracker.record.assert_called_with(200)


# ---- 4b. slow_start_scheduler 退出场景 ----


def test_slow_start_scheduler_exits_on_failed_flag():
    part_queue: queue.Queue[dict] = queue.Queue()
    part_queue.put({"index": 0})
    progress_lock = threading.Lock()
    active_workers = [0]
    allowed_workers = [1]
    failed = [True]
    probe_thread_name = [None]
    worker_feedback = threading.Event()

    slow_start_scheduler(
        worker_fn=lambda: None,
        max_workers=4,
        part_queue=part_queue,
        progress_lock=progress_lock,
        active_workers=active_workers,
        allowed_workers=allowed_workers,
        failed=failed,
        probe_thread_name=probe_thread_name,
        worker_feedback=worker_feedback,
        is_stopped_fn=lambda: False,
        notify_conn_fn=lambda active, allowed: None,
    )


def test_slow_start_scheduler_exits_on_is_stopped():
    part_queue: queue.Queue[dict] = queue.Queue()
    part_queue.put({"index": 0})
    progress_lock = threading.Lock()
    active_workers = [0]
    allowed_workers = [1]
    failed = [False]
    probe_thread_name = [None]
    worker_feedback = threading.Event()

    slow_start_scheduler(
        worker_fn=lambda: None,
        max_workers=4,
        part_queue=part_queue,
        progress_lock=progress_lock,
        active_workers=active_workers,
        allowed_workers=allowed_workers,
        failed=failed,
        probe_thread_name=probe_thread_name,
        worker_feedback=worker_feedback,
        is_stopped_fn=lambda: True,
        notify_conn_fn=lambda active, allowed: None,
    )


# ---- 4c. slow_start_scheduler 调度行为（伪线程串行执行，确定性覆盖） ----


class _FakeThread:
    """串行伪线程：start() 立即在当前线程执行 target，join() 可触发延迟动作。

    用于把调度器的事件循环变成确定性顺序执行；deferred 回调模拟真实线程
    在 join 等待窗口内收尾（如把失败分片放回队列）的竞态。
    """

    current: "_FakeThread | None" = None

    def __init__(self, target, name=None, daemon=None):
        self._target = target
        self._deferred = None
        self.name = name or ""

    def defer_until_join(self, fn):
        self._deferred = fn

    def start(self):
        _FakeThread.current = self
        try:
            self._target()
        finally:
            _FakeThread.current = None

    def join(self, timeout=None):
        if self._deferred is not None:
            deferred, self._deferred = self._deferred, None
            deferred()


def _fake_current_name() -> str:
    """当前伪线程名（仅在 start() 执行目标期间调用）。"""
    current = _FakeThread.current
    assert current is not None
    return current.name


def _make_serial_worker(active_workers, allowed_workers, probe_thread_name,
                        part_queue, consumed, worker_feedback, max_workers):
    """构造 worker_fn：probe 收到首字节后转正（allowed+1、清空 probe 槽位）。

    伪线程串行执行，无真实并发，因此不持有 progress_lock。
    """

    def worker_fn():
        active_workers[0] += 1
        if probe_thread_name[0] == _fake_current_name():
            probe_thread_name[0] = None
            allowed_workers[0] = min(allowed_workers[0] + 1, max_workers)
        try:
            item = part_queue.get_nowait()
        except queue.Empty:
            item = None
        if item is not None:
            consumed.append(item["index"])
        active_workers[0] -= 1
        worker_feedback.set()

    return worker_fn


def _run_scheduler(**kwargs):
    defaults = {
        "max_workers": 4,
        "progress_lock": threading.Lock(),
        "active_workers": [0],
        "allowed_workers": [1],
        "failed": [False],
        "probe_thread_name": [None],
        "worker_feedback": threading.Event(),
        "is_stopped_fn": lambda: False,
        "notify_conn_fn": lambda active, allowed: None,
    }
    defaults.update(kwargs)
    slow_start_scheduler(**defaults)


def test_slow_start_scheduler_spawns_workers_and_replaces_probe(monkeypatch):
    """57-66 补充 normal worker；68-82 探测转正后启动新 probe。"""
    monkeypatch.setattr(threading, "Thread", _FakeThread)
    part_queue: queue.Queue[dict] = queue.Queue()
    for index in range(4):
        part_queue.put({"index": index})
    active_workers = [0]
    allowed_workers = [1]
    probe_thread_name = [None]
    worker_feedback = threading.Event()
    consumed: list[int] = []

    _run_scheduler(
        worker_fn=_make_serial_worker(
            active_workers, allowed_workers, probe_thread_name,
            part_queue, consumed, worker_feedback, 4,
        ),
        part_queue=part_queue,
        active_workers=active_workers,
        allowed_workers=allowed_workers,
        probe_thread_name=probe_thread_name,
        worker_feedback=worker_feedback,
    )

    assert sorted(consumed) == [0, 1, 2, 3]
    assert part_queue.empty()
    assert active_workers[0] == 0


def test_slow_start_scheduler_safety_net_restarts_worker_as_probe(monkeypatch):
    """89-100 行：无活跃 worker 但队列非空时安全网补位，并接管 probe 角色。"""
    monkeypatch.setattr(threading, "Thread", _FakeThread)
    part_queue: queue.Queue[dict] = queue.Queue()
    for index in range(5):
        part_queue.put({"index": index})
    active_workers = [0]
    allowed_workers = [1]
    probe_thread_name = [None]
    worker_feedback = threading.Event()
    consumed: list[int] = []

    _run_scheduler(
        worker_fn=_make_serial_worker(
            active_workers, allowed_workers, probe_thread_name,
            part_queue, consumed, worker_feedback, 4,
        ),
        part_queue=part_queue,
        active_workers=active_workers,
        allowed_workers=allowed_workers,
        probe_thread_name=probe_thread_name,
        worker_feedback=worker_feedback,
    )

    assert sorted(consumed) == [0, 1, 2, 3, 4]
    assert part_queue.empty()
    assert active_workers[0] == 0
    assert allowed_workers[0] == 4


def test_slow_start_scheduler_resumes_after_part_requeued_during_join(monkeypatch):
    """102-113 行：join 期间 worker 把失败分片放回队列 → 重新调度而非退出。"""
    monkeypatch.setattr(threading, "Thread", _FakeThread)
    part_queue: queue.Queue[dict] = queue.Queue()
    for index in range(2):
        part_queue.put({"index": index})
    active_workers = [0]
    allowed_workers = [1]
    probe_thread_name = [None]
    worker_feedback = threading.Event()
    consumed = []

    def worker_fn():
        active_workers[0] += 1
        current_name = _fake_current_name()
        is_probe = probe_thread_name[0] == current_name
        if is_probe:
            probe_thread_name[0] = None
            allowed_workers[0] = min(allowed_workers[0] + 1, 4)
        item = part_queue.get_nowait()
        consumed.append(item["index"])
        active_workers[0] -= 1
        worker_feedback.set()
        if is_probe and not item.get("requeued"):
            # 模拟 probe 收尾失败：延迟到调度器 join 时才把分片放回队列
            thread = _FakeThread.current
            assert thread is not None
            thread.defer_until_join(
                lambda: part_queue.put({"index": item["index"], "requeued": True})
            )

    _run_scheduler(
        worker_fn=worker_fn,
        part_queue=part_queue,
        active_workers=active_workers,
        allowed_workers=allowed_workers,
        probe_thread_name=probe_thread_name,
        worker_feedback=worker_feedback,
    )

    assert sorted(consumed) == [0, 0, 1]
    assert part_queue.empty()
    assert active_workers[0] == 0


# ---- 4d. _ProgressAggregator 批量排空与竞态防御 ----


class _RacyEmptyQueue(queue.Queue):
    """empty() 永远返回 False，模拟多线程排空竞态，覆盖 except Empty 分支。"""

    def empty(self):
        return False


def test_aggregator_emit_final_tolerates_empty_race():
    """161-162 行：排空中 get_nowait 抛 Empty 时安全退出。"""
    signals = MagicMock()
    agg = _ProgressAggregator(1000, None, signals, 0.1)
    agg._queue = _RacyEmptyQueue()
    agg.record(250)

    agg.emit_final()

    assert agg.cumulative == 250
    signals.progress.emit.assert_called_once_with(25)


def test_aggregator_run_records_speed_and_emits_progress():
    """179-180 / 184 / 188 行：_run 批量排空、上报速度并按间隔发射进度。"""
    tracker = MagicMock()
    signals = MagicMock()
    agg = _ProgressAggregator(1000, tracker, signals, 0.0)
    agg._queue = _RacyEmptyQueue()
    agg.record(100)

    agg.start()
    deadline = time.monotonic() + 2
    while agg.cumulative != 100 and time.monotonic() < deadline:
        time.sleep(0.005)
    agg.stop()

    assert agg.cumulative == 100
    tracker.record.assert_called_once_with(100)
    signals.progress.emit.assert_called_once_with(10)
