"""上传/下载共用的并发控制常量与慢启动调度工具。"""
import threading

# ---- 并发控制常量 ----
RATE_LIMIT_CODES = frozenset({429, 503})
MAX_RATE_LIMITS = 50
RATE_LIMIT_BACKOFF = 2
PROGRESS_INTERVAL = 0.1


def slow_start_scheduler(
    worker_fn, max_workers, part_queue, progress_lock,
    active_workers, allowed_workers, failed,
    worker_feedback, is_stopped_fn, notify_conn_fn, thread_prefix="worker",
):
    """持续调度器：根据 allowed_workers 维持 worker 数量，成功增长/失败收缩。"""
    threads = []

    # 启动第 1 个 worker
    if part_queue.empty() or failed[0] or is_stopped_fn():
        return
    allowed_workers[0] = 1
    t = threading.Thread(target=worker_fn, name=f"{thread_prefix}_0", daemon=True)
    threads.append(t)
    t.start()
    notify_conn_fn(1, 1)

    # 监控循环：根据 allowed_workers 补充 worker
    while True:
        worker_feedback.wait(timeout=10)
        worker_feedback.clear()

        if failed[0] or is_stopped_fn():
            break

        with progress_lock:
            active = active_workers[0]
            allowed = allowed_workers[0]

        # 补充 worker 到 allowed 水位
        while active < allowed and not part_queue.empty() and not failed[0]:
            t = threading.Thread(
                target=worker_fn,
                name=f"{thread_prefix}_{len(threads)}",
                daemon=True,
            )
            threads.append(t)
            t.start()
            active += 1

        # 所有 worker 退出且队列空 → 完成
        with progress_lock:
            if active_workers[0] == 0 and part_queue.empty():
                break
            # 安全网：所有 worker 退出但队列非空且 allowed >= 1 → 重启
            if active_workers[0] == 0 and not part_queue.empty() and allowed_workers[0] >= 1:
                t = threading.Thread(
                    target=worker_fn,
                    name=f"{thread_prefix}_{len(threads)}",
                    daemon=True,
                )
                threads.append(t)
                t.start()

    for t in threads:
        t.join()
