"""Atomic, per-product artifacts shared by iOS and browser collectors."""
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit

from automation.pdd.spider.utils import read_xlsx


def write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_share_link(directory: Path, row: dict):
    link = row.get("产品链接", "")
    if not isinstance(link, str) or urlsplit(link).scheme not in ("http", "https") or not urlsplit(link).netloc:
        raise ValueError("商品缺少有效的 HTTP/HTTPS 分享链接")
    write_json(directory / "分享链接.json", {
        "share_link": link, "goods_id": str(row.get("产品ID") or ""),
        "title": str(row.get("标题") or ""),
    })


def read_share_link(directory: Path, title=""):
    path = directory / "分享链接.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ValueError(f"无法读取分享链接文件: {path}") from error
    if not isinstance(data, dict):
        raise ValueError(f"分享链接文件必须是 JSON 对象: {path}")
    link = data.get("share_link")
    if not isinstance(link, str) or urlsplit(link).scheme not in ("http", "https") or not urlsplit(link).netloc:
        raise ValueError(f"分享链接文件缺少有效 share_link: {path}")
    if title and data.get("title") and data["title"] != title:
        raise ValueError(f"分享链接文件与当前商品标题不匹配: {path}")
    return data


def read_products(table: Path):
    """Use Excel for inventory; product URLs come exclusively from share JSON."""
    header, values = read_xlsx(table)
    if "排行" not in header:
        raise ValueError(f"榜单表缺少排行列: {table}")
    result = []
    for values_row in values:
        row = dict(zip(header, values_row))
        rank = row.get("排行")
        if rank in (None, "") and not any(row.values()):
            continue
        try:
            number = int(rank)
            if number < 1 or str(rank).strip() not in (str(number), f"{number}.0"):
                raise ValueError
        except (ValueError, TypeError) as error:
            raise ValueError(f"榜单表包含无效排行: {table}") from error
        share = read_share_link(table.parent / f"榜{number:02d}", str(row.get("标题") or ""))
        row["产品链接"] = share["share_link"] if share else ""
        if share and share.get("goods_id"):
            row["产品ID"] = share["goods_id"]
        row.setdefault("产品ID", "")
        row["标题"] = str(row.get("标题") or "")
        result.append(row)
    return result


def write_rank_share_links(directory: Path, rows):
    for row in rows:
        if row.get("产品链接"):
            write_share_link(directory / f"榜{int(row['排行']):02d}", row)
