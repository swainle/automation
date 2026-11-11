"""保留旧产物续采的兼容业务函数；新命令从 SQLite 调度。"""
import random
import time
from pathlib import Path


from automation.pdd.spider.config import EXCEL_PATH, LIMIT, OUTPUT_ROOT
from automation.pdd.spider.exceptions import RiskControlError
from automation.pdd.spider.product_data import open_product_page, scrape_product, is_sold_out, mark_sold_out
from automation.pdd.spider.schedule import ordered_product_tables
from automation.pdd.spider.retry import run_product_queue
from automation.pdd.spider.status import product_done, reconcile_board, update_stage, load_status


def _read_products(xlsx_path: Path) -> list[dict]:
    """Read the Excel inventory and each product's 分享链接.json."""
    from automation.pdd.spider.artifacts import read_products
    return read_products(xlsx_path)


def collect_products(page) -> None:
    """第 2 步：读榜单数据.xlsx，逐个新建页面打开产品抓取。"""
    xlsx_files = ordered_product_tables(OUTPUT_ROOT, EXCEL_PATH)
    if not xlsx_files:
        print("未找到 榜单数据.xlsx，请先执行榜单采集")
        return

    tasks = []
    for xlsx_path in xlsx_files:
        base_dir = xlsx_path.parent
        print(f"\n===== 采集产品: {base_dir} =====")
        products = _read_products(xlsx_path)
        if not products:
            print("    无产品数据，跳过")
            continue

        pending_products = [r for r in products if not product_done(base_dir / f"榜{int(r['排行']):02d}")]
        print(f"    待续采产品：{len(pending_products)}/{len(products)}")
        targets = pending_products if LIMIT is None else pending_products[:LIMIT]
        for r in targets:
            if r["产品链接"]:
                tasks.append((xlsx_path, r))
    run_product_queue(tasks, OUTPUT_ROOT, lambda path, row: _collect_one(page, path, row))


def _collect_one(page, xlsx_path: Path, r: dict) -> bool:
    """采集单个产品，成功返回 True 并记录排行（异常不中断轮询）。"""
    rank = str(r["排行"])
    if not r["产品链接"]:
        return False
    rank_dir = xlsx_path.parent / f"榜{int(r['排行']):02d}"
    if product_done(rank_dir):
        return False
    rank_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n===== 采集产品 排行{rank} | {r['标题'][:20]} =====")
    update_stage(rank_dir, "access", "running")
    try:
        pg = open_product_page(page.context, r["产品链接"])
        try:
            if is_sold_out(pg):
                update_stage(rank_dir, "access", "completed")
                mark_sold_out(rank_dir, r["产品链接"], pg.url)
            elif "goods" not in pg.url:
                raise RiskControlError(f"打开产品 {r['产品ID']} 失败，疑似风控")
            else:
                update_stage(rank_dir, "access", "completed")
                update_stage(rank_dir, "product", "running", sold_out_attempts=0, retryable=False, outcome="available")
                scrape_product(pg, rank_dir, r)
        finally:
            try:
                pg.close()
            except Exception:
                pass
    except RiskControlError as ex:
        print(f"  ⚠ {ex}（保留为未完成，继续下一个）")
        update_stage(rank_dir, "product", "failed", str(ex))
        if load_status(rank_dir).get("stages", {}).get("access", {}).get("state") == "running":
            update_stage(rank_dir, "access", "failed", str(ex))
        reconcile_board(xlsx_path.parent.parent.parent)
        return False
    except Exception as ex:
        print(f"  ⚠ 采集异常: {ex}")
        update_stage(rank_dir, "product", "failed", str(ex))
        if load_status(rank_dir).get("stages", {}).get("access", {}).get("state") == "running":
            update_stage(rank_dir, "access", "failed", str(ex))
        reconcile_board(xlsx_path.parent.parent.parent)
        return False
    reconcile_board(xlsx_path.parent.parent.parent)
    if not product_done(rank_dir):
        return False
    if load_status(rank_dir).get("product_state") != "skipped":
        update_stage(rank_dir, "product", "completed")
    reconcile_board(xlsx_path.parent.parent.parent)
    time.sleep(random.uniform(3, 8))
    return True
