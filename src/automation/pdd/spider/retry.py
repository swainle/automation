"""连续售罄保护；冷却时间和续采位置持久化到根 status.json。"""
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

from automation.pdd.spider.status import load_status, product_done, set_product_worker

FAILURE_LIMIT = 5
RETRY_SECONDS = 300


def _directory(task):
    path, row = task
    return path.parent / f"榜{int(row['排行']):02d}"


def _pointer(task):
    path, row = task
    return {"table": str(path), "rank": int(row["排行"]), "title": row.get("标题", "")}


def _retryable(task):
    state = load_status(_directory(task))
    if state.get("product_state") == "skipped":
        return False
    product = state.get("stages", {}).get("product", {})
    return (product.get("state") == "retry_pending" and
            product.get("outcome") == "sold_out" and product.get("retryable", False))


def _wait_until(epoch):
    while True:
        remaining = epoch - time.time()
        if remaining <= 0:
            return
        time.sleep(min(30, remaining))


def run_product_queue(tasks: list[tuple[Path, dict]], root: Path, collect):
    if not tasks:
        return
    previous = load_status(root).get("product_worker", {})
    index = 0
    failures = 0
    first = None
    if previous.get("state") == "waiting_retry" and previous.get("trigger") == "consecutive_sold_out":
        pointer = previous.get("resume_from")
        index = next((i for i, t in enumerate(tasks) if _pointer(t) == pointer), 0)
        _wait_until(previous.get("retry_at_epoch", 0))

    def cooldown(start, count):
        epoch = time.time() + RETRY_SECONDS
        pointer = _pointer(tasks[start])
        set_product_worker(root, "waiting_retry", resume_from=pointer,
                           trigger="consecutive_sold_out", consecutive_sold_out=count, retry_at_epoch=epoch,
                           retry_at=datetime.fromtimestamp(epoch, timezone(timedelta(hours=8))).isoformat())
        print(f"[product] 暂停采集，5分钟后从 {pointer['table']} 排行{pointer['rank']} 重试", flush=True)
        _wait_until(epoch)

    try:
        while True:
            while index < len(tasks):
                task = tasks[index]
                if product_done(_directory(task)):
                    index += 1
                    continue
                set_product_worker(root, "running", current=_pointer(task), consecutive_sold_out=failures)
                collect(*task)
                if _retryable(task):
                    if not failures:
                        first = index
                    failures += 1
                    print(f"[product] 连续售罄 {failures}/{FAILURE_LIMIT}", flush=True)
                    if failures >= FAILURE_LIMIT:
                        cooldown(first, failures)
                        index, failures, first = first, 0, None
                        continue
                else:
                    failures, first = 0, None
                index += 1
            # 不足5个连续售罄只保留待采状态，不触发冷却或立即重跑。
            break
    except BaseException:
        # 等待中的状态原样保留，重启后沿用 retry_at 和首个失败位置。
        if load_status(root).get("product_worker", {}).get("state") != "waiting_retry":
            set_product_worker(root, "interrupted", resume_from=_pointer(tasks[min(index, len(tasks)-1)]))
        raise
    state = "completed" if all(product_done(_directory(t)) for t in tasks) else "pending"
    set_product_worker(root, state)
