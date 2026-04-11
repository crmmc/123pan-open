import math
from unittest.mock import patch

from src.app.common.speed_tracker import SpeedTracker


def test_speed_returns_zero_initially():
    tracker = SpeedTracker()
    assert tracker.speed() == 0.0


def test_first_record_initializes_without_speed():
    tracker = SpeedTracker()
    with patch("src.app.common.speed_tracker.time.monotonic", return_value=100.0):
        tracker.record(100)
    assert tracker._initialized is True
    assert tracker._last_time == 100.0
    assert tracker._last_cumulative == 100
    assert tracker.speed() == 0.0


def test_record_computes_ema_speed():
    tracker = SpeedTracker()
    with patch("src.app.common.speed_tracker.time.monotonic", return_value=100.0):
        tracker.record(100)
    with patch("src.app.common.speed_tracker.time.monotonic", return_value=101.0):
        tracker.record(200)
    dt = 1.0
    alpha = 1.0 - math.exp(-dt / SpeedTracker.TAU)
    expected = alpha * 100.0
    assert abs(tracker.speed() - expected) < 0.01


def test_record_skips_if_dt_below_min_dt():
    tracker = SpeedTracker()
    with patch("src.app.common.speed_tracker.time.monotonic", return_value=100.0):
        tracker.record(100)
    # 第二次 record 间隔 < MIN_DT
    with patch("src.app.common.speed_tracker.time.monotonic", return_value=100.01):
        tracker.record(200)
    assert tracker.speed() == 0.0


def test_record_skips_negative_delta():
    tracker = SpeedTracker()
    with patch("src.app.common.speed_tracker.time.monotonic", return_value=100.0):
        tracker.record(200)
    with patch("src.app.common.speed_tracker.time.monotonic", return_value=101.0):
        tracker.record(100)  # delta_bytes < 0
    assert tracker.speed() == 0.0


def test_record_caps_instant_speed_at_max():
    tracker = SpeedTracker()
    with patch("src.app.common.speed_tracker.time.monotonic", return_value=100.0):
        tracker.record(0)
    # 极大瞬时速度：10GB / 0.1s = 100GB/s，远超 MAX_INSTANT_SPEED
    with patch("src.app.common.speed_tracker.time.monotonic", return_value=100.1):
        tracker.record(10_000_000_000)
    # 瞬时速度被 cap 到 MAX_INSTANT_SPEED，EMA 不超过 MAX_INSTANT_SPEED
    assert tracker.speed() <= SpeedTracker.MAX_INSTANT_SPEED
    assert tracker.speed() > 0


def test_flush_does_nothing_when_uninitialized():
    tracker = SpeedTracker()
    with patch("src.app.common.speed_tracker.time.monotonic", return_value=100.0):
        tracker.flush()
    assert tracker.speed() == 0.0


def test_flush_does_not_decay_within_3_seconds():
    tracker = SpeedTracker()
    with patch("src.app.common.speed_tracker.time.monotonic", return_value=100.0):
        tracker.record(100)
    with patch("src.app.common.speed_tracker.time.monotonic", return_value=101.0):
        tracker.record(200)
    speed_before = tracker.speed()
    # flush 距上次 record < 3s，不衰减
    with patch("src.app.common.speed_tracker.time.monotonic", return_value=102.0):
        tracker.flush()
    assert tracker.speed() == speed_before


def test_flush_decays_speed_after_3_seconds_stall():
    tracker = SpeedTracker()
    with patch("src.app.common.speed_tracker.time.monotonic", return_value=100.0):
        tracker.record(100)
    with patch("src.app.common.speed_tracker.time.monotonic", return_value=101.0):
        tracker.record(200)
    speed_before = tracker.speed()
    assert speed_before > 0
    # flush 距上次 record > 3s，衰减
    stall_time = 105.0  # 距 101.0 过了 4s
    with patch("src.app.common.speed_tracker.time.monotonic", return_value=stall_time):
        tracker.flush()
    dt = stall_time - 101.0
    expected = speed_before * math.exp(-dt / SpeedTracker.TAU)
    assert abs(tracker.speed() - expected) < 0.01
    assert tracker.speed() < speed_before


def test_eta_returns_negative_one_when_no_speed():
    tracker = SpeedTracker()
    assert tracker.eta(1000) == -1.0


def test_eta_returns_negative_one_when_no_remaining():
    tracker = SpeedTracker()
    with patch("src.app.common.speed_tracker.time.monotonic", return_value=100.0):
        tracker.record(100)
    with patch("src.app.common.speed_tracker.time.monotonic", return_value=101.0):
        tracker.record(200)
    assert tracker.eta(0) == -1.0
    assert tracker.eta(-1) == -1.0


def test_eta_computes_remaining_over_speed():
    tracker = SpeedTracker()
    with patch("src.app.common.speed_tracker.time.monotonic", return_value=100.0):
        tracker.record(0)
    with patch("src.app.common.speed_tracker.time.monotonic", return_value=101.0):
        tracker.record(100)
    speed = tracker.speed()
    assert speed > 0
    remaining = 500.0
    assert abs(tracker.eta(remaining) - remaining / speed) < 0.01


def test_reset_clears_all_state():
    tracker = SpeedTracker()
    with patch("src.app.common.speed_tracker.time.monotonic", return_value=100.0):
        tracker.record(100)
    with patch("src.app.common.speed_tracker.time.monotonic", return_value=101.0):
        tracker.record(200)
    assert tracker.speed() > 0
    tracker.reset()
    assert tracker._initialized is False
    assert tracker._ema_speed == 0.0
    assert tracker._last_time is None
    assert tracker._last_cumulative == 0
