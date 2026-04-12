import queue
import threading
from unittest.mock import MagicMock

from src.app.common.concurrency import _ProgressAggregator, slow_start_scheduler


def test_slow_start_scheduler_returns_immediately_when_queue_is_empty():
    part_queue = queue.Queue()
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
    part_queue = queue.Queue()
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
    part_queue = queue.Queue()
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
