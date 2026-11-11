"""按来源表行顺序安排产品采集，并维护主榜来源名称。"""
import re
from pathlib import Path

from openpyxl import load_workbook

from automation.pdd.spider.status import _save, load_status
from automation.pdd.spider.utils import _xlsx_lock, sanitize


def source_rows(path: Path) -> list[dict]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        result = []
        for row_number, row in enumerate(sheet.iter_rows(min_row=2, values_only=True), 2):
            if len(row) > 1 and row[1] is not None and str(row[1]).strip():
                result.append({"file": str(path), "sheet": sheet.title, "row": row_number,
                               "name": str(row[1]).strip(),
                               "category": str(row[0] or "").strip(),
                               **({'main_name': str(row[2] or '').strip(),
                                   'tab': '降价榜' if row[3] == '降价版' else str(row[3] or '').strip()}
                                  if len(row) >= 5 else {})})
        return result
    finally:
        workbook.close()


def _directory_name(name: str) -> str:
    return sanitize(re.sub(r"【[^】]*】$", "", name).strip())


def sync_board_source(board: Path, rows: list[dict]) -> dict:
    with _xlsx_lock(board / "status.json"):
        status = load_status(board)
        previous = status.get("source", {})
        # 已人工指定的表格名称优先；目录匹配只去掉尾部说明，不做模糊包含匹配。
        matches = [r for r in rows if r["name"] == previous.get("name")]
        if not matches:
            matches = [r for r in rows if sanitize(r["name"]) == board.name]
        if not matches:
            matches = [r for r in rows if _directory_name(r["name"]) == board.name]
        if matches and len({r["name"] for r in matches}) == 1:
            selected = next((r for r in matches if r["row"] == previous.get("row")), matches[0])
            source = {**selected, "match_state": "matched"}
        else:
            source = {**previous, "match_state": "unmatched", "row": None,
                      "reason": "来源表未匹配唯一对应名称，请维护 source.name"}
            if matches:
                source["candidates"] = [r["name"] for r in matches]
        status["source"] = source
        status.setdefault("level", "board")
        status.setdefault("name", board.name)
        _save(board, status)
        return source


def ordered_product_tables(root: Path, source_path: Path) -> list[Path]:
    files = list(root.glob("**/榜单数据.xlsx"))
    if not files:
        return []
    rows = source_rows(source_path)
    if rows and 'main_name' in rows[0]:
        order = {(sanitize(row['main_name']), sanitize(row['tab'])): row['row'] for row in rows if row['tab']}
        return sorted(files, key=lambda path: (
            order.get(tuple(path.relative_to(root).parts[:2]), float('inf')), str(path.relative_to(root))))
    boards = {root / p.relative_to(root).parts[0] for p in files}
    sources = {}
    for board in sorted(boards):
        source = sync_board_source(board, rows)
        sources[board.name] = source
        if source["match_state"] != "matched":
            print(f"[product] {board.name} 未匹配来源表，排在已匹配榜单之后")
    return sorted(files, key=lambda p: (
        sources[p.relative_to(root).parts[0]].get("row") or float("inf"),
        str(p.relative_to(root))))
