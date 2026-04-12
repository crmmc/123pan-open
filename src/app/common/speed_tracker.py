import math
import threading
import time


class SpeedTracker:
    """EMA（指数移动平均）速度计算器。

    Worker / Aggregator 线程调 record(cumulative_bytes)。
    UI 线程调 flush() → speed() / eta()。
    """

    # EMA 时间常数（秒）：值越小响应越快、越抖动；值越大越平滑
    TAU = 4.0
    # 最小有效采样间隔（秒）：低于此值跳过，过滤噪声
    MIN_DT = 0.05
    # 瞬时速度上限（B/s）：防止单次异常采样污染 EMA（1 GB/s）
    MAX_INSTANT_SPEED = 1_073_741_824.0

    def __init__(self):
        self._lock = threading.Lock()
        self._last_time: float | None = None
        self._last_cumulative: int = 0
        self._ema_speed: float = 0.0
        self._initialized: bool = False

    def record(self, cumulative_bytes: int) -> None:
        """Worker / Aggregator 线程调用，传入累计已传输字节数。"""
        with self._lock:
            now = time.monotonic()

            if not self._initialized or self._last_time is None:
                self._last_time = now
                self._last_cumulative = cumulative_bytes
                self._initialized = True
                return

            dt = now - self._last_time
            if dt < self.MIN_DT:
                return

            delta_bytes = cumulative_bytes - self._last_cumulative
            if delta_bytes < 0:
                # 进度回退（如下载重试），更新基准但不影响速度
                self._last_cumulative = cumulative_bytes
                return
            self._last_time = now
            self._last_cumulative = cumulative_bytes

            instant_speed = min(delta_bytes / dt, self.MAX_INSTANT_SPEED)
            alpha = 1.0 - math.exp(-dt / self.TAU)
            self._ema_speed = alpha * instant_speed + (1.0 - alpha) * self._ema_speed

    def flush(self) -> None:
        """UI 线程调用。检测传输停滞并衰减速度至零。"""
        with self._lock:
            if not self._initialized or self._last_time is None:
                return
            dt = time.monotonic() - self._last_time
            if dt > 3.0 and self._ema_speed > 0:
                self._ema_speed *= math.exp(-dt / self.TAU)
                self._last_time = time.monotonic()

    def speed(self) -> float:
        return self._ema_speed

    def eta(self, remaining_bytes: int) -> float:
        if self._ema_speed <= 0 or remaining_bytes <= 0:
            return -1.0
        return remaining_bytes / self._ema_speed

    def reset(self) -> None:
        with self._lock:
            self._last_time = None
            self._last_cumulative = 0
            self._ema_speed = 0.0
            self._initialized = False

    def resume(self) -> None:
        """暂停恢复后调用，仅重置时间基准，保留 EMA 速度。"""
        with self._lock:
            if self._initialized:
                self._last_time = time.monotonic()
