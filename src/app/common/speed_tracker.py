import time
from collections import deque


class SpeedTracker:
    """滑动窗口速度计算器。

    worker 线程调 record()（deque.append，原子操作，无锁）。
    UI 线程调 flush() 消费队列，再读 speed()/eta()。
    """

    WINDOW_SECONDS = 5.0

    def __init__(self):
        # worker 线程写入，UI 线程 flush 消费
        self._pending: deque[tuple[float, int]] = deque()
        # flush 后的滑动窗口（仅 UI 线程访问）
        self._samples: deque[tuple[float, int]] = deque()
        self._last_speed = 0.0

    def record(self, cumulative_bytes: int) -> None:
        """worker 线程调用，零开销。"""
        self._pending.append((time.monotonic(), cumulative_bytes))

    def flush(self) -> None:
        """UI 线程调用，将 pending 数据消费到滑动窗口并计算速度。"""
        # 一次性消费所有 pending
        while self._pending:
            try:
                sample = self._pending.popleft()
            except IndexError:
                break
            self._samples.append(sample)

        now = time.monotonic()
        cutoff = now - self.WINDOW_SECONDS * 2
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

        # 计算速度
        if len(self._samples) < 2:
            self._last_speed = 0.0
            return
        window_cutoff = now - self.WINDOW_SECONDS
        oldest_in_window = None
        for ts, val in self._samples:
            if ts >= window_cutoff:
                oldest_in_window = (ts, val)
                break
        if oldest_in_window is None:
            self._last_speed = 0.0
            return
        newest = self._samples[-1]
        dt = newest[0] - oldest_in_window[0]
        if dt <= 0:
            self._last_speed = 0.0
            return
        self._last_speed = (newest[1] - oldest_in_window[1]) / dt

    def speed(self) -> float:
        return self._last_speed

    def eta(self, remaining_bytes: int) -> float:
        if self._last_speed <= 0 or remaining_bytes <= 0:
            return -1.0
        return remaining_bytes / self._last_speed

    def reset(self) -> None:
        self._pending.clear()
        self._samples.clear()
        self._last_speed = 0.0
