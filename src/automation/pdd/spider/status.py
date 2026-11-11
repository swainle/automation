"""采集状态与现有产物核对。rank 和 product 分开，失败不算完成。"""
import csv
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from automation.pdd.spider.utils import _xlsx_lock, read_xlsx, write_csv

PRODUCT_STAGES = ("screenshot", "main_images", "sku", "details", "comments", "negative_comments")
FINISHED = {"completed", "skipped"}


def _now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def load_status(directory: Path) -> dict:
    path = directory / "status.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save(directory: Path, value: dict):
    path = directory / "status.json"
    temporary = directory / "status.json.tmp"
    value["updated_at"] = _now()
    value.setdefault("created_at", _now())
    value.setdefault("schema_version", 1)
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def update_stage(directory: Path, stage: str, state: str, reason: str = "", **metadata):
    with _xlsx_lock(directory / "status.json"):
        value = load_status(directory)
        data = metadata.pop("data", None)
        if data is not None:
            value["rank_data"] = data
        stages = value.setdefault("stages", {})
        stages[stage] = {**stages.get(stage, {}),
                         "state": state, "reason": reason, "updated_at": _now(), **metadata}
        if state in FINISHED:
            value["stages"][stage]["completed_at"] = _now()
        else:
            value["stages"][stage].pop("completed_at", None)
        value.setdefault("level", "product")
        _save(directory, value)


def stage_done(directory: Path, stage: str) -> bool:
    return load_status(directory).get("stages", {}).get(stage, {}).get("state") in FINISHED


def set_listing_date(directory: Path, date: str):
    with _xlsx_lock(directory / "status.json"):
        value = load_status(directory)
        value["listing_date"] = date
        value.setdefault("rank_data", {})["上架日期"] = date
        _save(directory, value)


def set_worker(root: Path, state: str, board: str = "", reason: str = ""):
    with _xlsx_lock(root / "status.json"):
        value = load_status(root)
        value["level"] = "workspace"
        value["rank_worker"] = {"state": state, "board": board, "reason": reason, "updated_at": _now()}
        _save(root, value)


def set_product_worker(root: Path, state: str, **metadata):
    with _xlsx_lock(root / "status.json"):
        value = load_status(root)
        value["level"] = "workspace"
        value["product_worker"] = {"state": state, "updated_at": _now(), **metadata}
        _save(root, value)


def reconcile_product(directory: Path, row: dict | None = None, *, require_screenshot=True) -> dict:
    """从旧产物补齐状态；没有文件也没有明确空结果时，保持待采集。"""
    with _xlsx_lock(directory / "status.json"):
        value = load_status(directory)
        value.setdefault("created_at", _now())
        value.update(level="product", name=directory.name, date=directory.parent.name)
        if row:
            value["rank"] = row.get("排行")
            value["title"] = row.get("标题", "")
            value["url"] = row.get("产品链接", "")
            existing = value.setdefault("rank_data", {})
            for key, item in row.items():
                if item not in (None, "") or key not in existing:
                    existing[key] = item
        if "listing_date" in value:
            value.setdefault("rank_data", {})["上架日期"] = value["listing_date"]
        stages = value.setdefault("stages", {})
        previous_product = stages.get("product", {})
        if (previous_product.get("state") == "skipped" and
                previous_product.get("reason") == "商品已售罄，推荐以下相似商品" and
                "sold_out_attempts" not in previous_product):
            previous_product.update(state="retry_pending", sold_out_attempts=1,
                                    outcome="sold_out", retryable=True)
        stages.setdefault("rank", {"state": "pending", "reason": "榜单链接采集未完成", "updated_at": _now()})
        rank_done = stages["rank"]["state"] == "completed"
        files = {"screenshot": directory / "产品页截图.jpg", "sku": directory / "SKU.csv",
                 "details": directory / "详情图片" / "长图.jpg", "comments": directory / "评论.csv"}
        if not require_screenshot:
            files.pop('screenshot')
        for stage, path in files.items():
            if path.exists() and path.stat().st_size:
                stages.setdefault(stage, {"state": "completed", "reason": "根据已有产物恢复"})
            elif stages.get(stage, {}).get("state") not in {"skipped", "failed", "running"}:
                stages[stage] = {"state": "pending", "reason": f"缺少 {path.name}"}
        images = list((directory / "主图").glob("主图_*.jpg"))
        if images and all(p.stat().st_size for p in images):
            stages.setdefault("main_images", {"state": "completed", "reason": "根据已有主图恢复", "count": len(images)})
        else:
            stages["main_images"] = {"state": "pending", "reason": "缺少产品主图"}
        negative_file = directory / "差评.csv"
        if negative_file.exists():
            with negative_file.open(encoding="utf-8-sig", newline="") as stream:
                table = list(csv.reader(stream))
            if len(table) <= 1:
                negative_file.unlink()
                stages["negative_comments"] = {"state": "skipped", "reason": "历史差评表无记录，已移除空 CSV", "count": 0}
            else:
                if "原因" not in table[0]:
                    write_csv(negative_file, table[0] + ["原因"],
                              [r + ["历史差评，原始标签未记录"] for r in table[1:]])
                if stages.get("negative_comments", {}).get("state") not in {"failed", "running"}:
                    entry = stages.setdefault("negative_comments", {})
                    entry.update(state="completed", count=len(table) - 1)
                    entry.setdefault("reason", "根据已有差评恢复")
        elif stages.get("negative_comments", {}).get("state") != "skipped":
            stages["negative_comments"] = {"state": "pending", "reason": "尚未确认灰色标签评价结果"}
        skipped = stages.get("product", {}).get("state") == "skipped"
        retry_pending = stages.get("product", {}).get("state") == "retry_pending"
        required = [s for s in PRODUCT_STAGES if require_screenshot or s != 'screenshot']
        product_done = skipped or (not retry_pending and all(stages.get(s, {}).get("state") in FINISHED for s in required))
        value["rank_state"] = "completed" if rank_done else "pending"
        value["product_state"] = "skipped" if skipped else "completed" if product_done else "pending"
        value["state"] = "skipped" if skipped else "completed" if rank_done and product_done else "pending"
        value["reason"] = stages["product"].get("reason", "") if skipped or stages.get("product", {}).get("state") == "retry_pending" else "; ".join(stages[s].get("reason", s) for s in ("rank", *required)
                                      if stages.get(s, {}).get("state") not in FINISHED)
        for stage in stages.values():
            stage.setdefault("updated_at", _now())
        _save(directory, value)
        return value


def product_done(directory: Path, *, require_screenshot=True) -> bool:
    return reconcile_product(directory, require_screenshot=require_screenshot)["product_state"] in FINISHED


def read_rank_rows(base: Path) -> list[dict]:
    path = base / "榜单数据.xlsx"
    if not path.exists():
        return []
    header, values = read_xlsx(path)
    return [dict(zip(header, r)) for r in values if r and r[0] is not None]


def reconcile_subrank(base: Path) -> dict:
    rows = read_rank_rows(base)
    products = []
    for row in rows:
        directory = base / f"榜{int(row['排行']):02d}"
        products.append(reconcile_product(directory, row))
    total = len(rows)
    rank_complete = bool(total) and all(p["rank_state"] == "completed" for p in products)
    detail_complete = bool(total) and all(p["product_state"] in FINISHED for p in products)
    result = {"level": "subrank", "name": base.parent.name, "date": base.name,
              "state": "completed" if rank_complete and detail_complete else "pending",
              "rank_state": "completed" if rank_complete else "pending",
              "product_state": "completed" if detail_complete else "pending", "total": total,
              "pending_rank": [p["name"] for p in products if p["rank_state"] != "completed"],
              "pending_products": [p["name"] for p in products if p["product_state"] not in FINISHED],
              "skipped_products": [p["name"] for p in products if p["product_state"] == "skipped"],
              "reason": "" if rank_complete and detail_complete else "仍有未完成商品或缺少榜单表"}
    with _xlsx_lock(base / "status.json"):
        _save(base, result)
    with _xlsx_lock(base.parent / "status.json"):
        previous = load_status(base.parent)
        if previous.get("date", "") <= base.name:
            _save(base.parent, result.copy())
    return result


def reconcile_board(board: Path) -> dict:
    children = []
    for sub in sorted(board.iterdir()) if board.exists() else []:
        if not sub.is_dir():
            continue
        dates = sorted(p for p in sub.iterdir() if p.is_dir() and re_date(p.name))
        for base in dates:
            reconcile_subrank(base)
        if dates:
            children.append(load_status(sub))
    result = {"level": "board", "name": board.name, "subranks": children,
              "rank_state": "completed" if children and all(c["rank_state"] == "completed" for c in children) else "pending",
              "state": "completed" if children and all(c["state"] == "completed" for c in children) else "pending",
              "pending_rank": [c["name"] for c in children if c["rank_state"] != "completed"],
              "pending_subranks": [c["name"] for c in children if c["state"] != "completed"],
              "reason": "" if children and all(c["state"] == "completed" for c in children) else "存在未完成子榜"}
    with _xlsx_lock(board / "status.json"):
        previous = load_status(board)
        if "source" in previous:
            result["source"] = previous["source"]
        _save(board, result)
    return result


def re_date(name: str) -> bool:
    import re
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", name))


def scan_output(root: Path) -> list[dict]:
    result = []
    if root.exists():
        for board in sorted(p for p in root.iterdir() if p.is_dir()):
            if any(re_date(p.name) for sub in board.iterdir() if sub.is_dir()
                   for p in sub.iterdir() if p.is_dir()):
                state = reconcile_board(board)
                print(f"[status] {board.name}：待采链接子榜 {state['pending_rank']}；未完成子榜 {state['pending_subranks']}")
                result.append(state)
    return result
