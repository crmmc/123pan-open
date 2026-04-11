import time
from collections import deque


class SpeedTracker:
    """滑动窗口速度计算器。

    Aggregator 线程调 record()（deque.append，原子操作，无锁）。
    UI 线程调 flush() 裁剪窗口并计算速度，再读 speed()/eta()。
    """

    WINDOW_SECONDS = 60.0

    def __init__(self):
        self._samples: deque[tuple[float, int]] = deque()
        self._last_speed = 0.0

    def record(self, cumulative_bytes: int) -> None:
        """Aggregator 线程调用，直接写入滑动窗口。"""
        self._samples.append((time.monotonic(), cumulative_bytes))

    def flush(self) -> None:
        """UI 线程调用，裁剪窗口并计算速度。"""
        now = time.monotonic()
        cutoff = now - self.WINDOW_SECONDS
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

        if len(self._samples) < 2:
            self._last_speed = 0.0
            return

        oldest = self._samples[0]
        newest = self._samples[-1]
        dt = newest[0] - oldest[0]
        if dt < 1.0:
            return  # 数据跨度不足 1s，保持上次速度
        self._last_speed = (newest[1] - oldest[1]) / dt

    def speed(self) -> float:
        return self._last_speed

    def eta(self, remaining_bytes: int) -> float:
        if self._last_speed <= 0 or remaining_bytes <= 0:
            return -1.0
        return remaining_bytes / self._last_speed

    def reset(self) -> None:
        self._samples.clear()
        self._last_speed = 0.0
