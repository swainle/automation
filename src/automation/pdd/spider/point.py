#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""电商图片卖点抽取 —— 一条命令跑完 扫图 → 调视觉模型 → 校验 → 写库。

用法:
    uv run python -m automation.pdd.spider.point --ranking-id 319
    uv run python -m automation.pdd.spider.point --ranking-product-id 558

配置读取业务目录 .env，默认 DeepSeek；JSON 保存在业务目录 data/points/。
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures as futures
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from psycopg import Error as DatabaseError
from psycopg.rows import tuple_row
from PIL import Image

from automation.pdd.spider import config as spider_config
from automation.pdd.spider.mobile_database import connect

DIRS = {"main": "主图", "sku": "SKU", "detail": "详情"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
SKIP_NAME = re.compile(r"长图", re.I)

RULES = """你是电商图片卖点抽取Agent。只依据给定图片上的文字作证据。

规则：
1. quote 必须逐字出现在所标注的文件里，不改写、不概括、不跨位置拼接、不改数字。
2. 只有图上文字才算证据；信息≠卖点，只留能影响购买判断的。
3. 主卖点最多3条，不足不凑；副卖点最多5条（数值或检测／成分技术／结构工艺／适用安全／产地认证）。
4. 禁止用常识或标题补图里没有的信息；无证据的卖点删除。
5. 每条卖点 ≤10 字符（各字符计1），产品名不限；卖点内禁止 "/"。
6. 一条只表达一个卖点；主卖点之间不同义；副卖点不重复主卖点；同义合并，多图支持时全写入。
7. 排除：活动价/券/赠品、售后物流、颜色/包装数量/净含量/保质期、口味与SKU枚举、
   同链接其他商品、纯使用方法、空泛口号。例外保留安全适用信息。
8. 带"智能总结/智能合成"水印的图不要引用。"""

CLAIM_SPEC = """从这批图片里挑出可以作为卖点的候选，逐条列出。

只输出 JSON，不要代码围栏：
{"claims":[{"kind":"main|secondary","point":"卖点(≤10字,不含/)","image_type":"main|sku|detail","file":"文件名","quote":"图上原句"}]}

要求：
- quote 逐字照抄图上文字，允许保留标点；不要拼接两处文字。
- image_type 与 file 必须来自本批给你的清单。
- 宁可少给，也不要编造；没有可用卖点时输出 {"claims":[]}。"""

MERGE_SPEC = """下面是从同一个商品的多批图片中得到候选卖点（已带证据）。
请合并去重，选出最终 3 条主卖点 + 最多 5 条副卖点。

只输出 JSON，不要代码围栏：
{"product_name":"","product_name_evidence":[{"image_type":"main","file":"","quote":""}],
 "main_selling_points":[{"point":"","image_evidence":[{"image_type":"main","file":"","quote":""}]}],
 "secondary_selling_points":[{"point":"","image_evidence":[{"image_type":"detail","file":"","quote":""}]}]}

要求：
- 每条卖点 ≤10 字符，不含 "/"；主卖点之间不同义；副卖点不重复主卖点。
- 同义候选合并，把支持它的多条证据都写进 image_evidence。
- 只能使用候选里出现过的 point 与证据，不得新增图上没有的信息。
- 产品名取图上品名，找不到填"未识别"。"""

REPAIR_SPEC = """你输出的 JSON 有以下问题，请修正后重新输出完整 JSON（不要代码围栏、不要解释）。
只改有问题的地方，不要新增没有证据的内容。

问题列表：
__ERRORS__

原 JSON：
__JSON__"""


# ---------------------------------------------------------------- .env / 配置

class Config:
    def __init__(self, args: argparse.Namespace | None = None) -> None:
        args = args or argparse.Namespace(base_url=None, model=None, batch=6, max_images=36,
                                          max_edge=1280, quality=80, timeout=180, retries=2)
        self.api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        self.base_url = (args.base_url or os.environ.get("DEEPSEEK_BASE_URL")
                         or "https://api.deepseek.com").rstrip("/")
        self.vision_model = args.model or os.environ.get("VISION_MODEL") or "deepseek-flash"
        self.text_model = os.environ.get("TEXT_MODEL") or self.vision_model
        self.batch = args.batch
        self.max_images = args.max_images
        self.max_edge = args.max_edge
        self.quality = args.quality
        self.timeout = args.timeout
        self.retries = args.retries

    def check(self) -> None:
        missing = [n for n, v in (
            ("DEEPSEEK_API_KEY", self.api_key),
            ("VISION_MODEL", self.vision_model),
        ) if not v]
        if missing:
            raise ValueError("缺少配置: " + ", ".join(missing) +
                     "\n请写进 .env 或设为环境变量后重试。")


# ---------------------------------------------------------------- 模型调用

def chat(cfg: Config, model: str, content: list[dict], retries: int | None = None) -> str:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0,
    }
    data = json.dumps(payload).encode("utf-8")
    url = cfg.base_url + "/chat/completions"
    last = None
    for attempt in range((retries if retries is not None else cfg.retries) + 1):
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + cfg.api_key)
        try:
            with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return body["choices"][0]["message"]["content"] or ""
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            last = f"HTTP {e.code}: {detail}"
            if e.code in (400, 401, 403, 404):
                break
        except Exception as e:  # 超时、连接、JSON 解析
            last = f"{type(e).__name__}: {e}"
        time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"调用模型失败: {last}")


def parse_json(text: str) -> dict:
    """从模型回复里抠出第一个 JSON 对象。"""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    start = t.find("{")
    if start < 0:
        raise ValueError("回复里没有 JSON: " + text[:200])
    obj, _ = json.JSONDecoder().raw_decode(t[start:])
    if not isinstance(obj, dict):
        raise ValueError("模型回复必须是 JSON 对象")
    return obj


# ---------------------------------------------------------------- 图片

def list_images(directory: str, max_images: int) -> list[tuple[str, str, str]]:
    """返回 [(image_type, 相对文件名, 绝对路径)]，按 主图→SKU→详情 排序并截断。"""
    out: list[tuple[str, str, str]] = []
    for kind in ("main", "sku", "detail"):
        sub = os.path.join(directory, DIRS[kind])
        if not os.path.isdir(sub):
            continue
        names = sorted(
            n for n in os.listdir(sub)
            if os.path.splitext(n)[1].lower() in IMAGE_EXT and not SKIP_NAME.search(n)
        )
        for n in names:
            out.append((kind, n, os.path.join(sub, n)))
    if len(out) > max_images:
        # 优先保留主图，其余按原顺序截断
        main = [x for x in out if x[0] == "main"]
        rest = [x for x in out if x[0] != "main"]
        out = main + rest[: max(0, max_images - len(main))]
    return out


def to_data_url(path: str, max_edge: int = 1280, quality: int = 80) -> str:
    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size
        scale = min(1.0, max_edge / max(w, h))
        if scale < 1.0:
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return "data:image/jpeg;base64," + b64


def image_parts(items: list[tuple[str, str, str]], max_edge: int = 1280,
                quality: int = 80) -> list[dict]:
    parts: list[dict] = []
    for kind, name, path in items:
        parts.append({"type": "text",
                      "text": f"图片：image_type={kind}, file={name}"})
        parts.append({"type": "image_url",
                      "image_url": {"url": to_data_url(path, max_edge, quality)}})
    return parts


# ---------------------------------------------------------------- 抽取流程

def extract_candidates(cfg: Config, directory: str, images: list) -> list[dict]:
    claims: list[dict] = []
    for i in range(0, len(images), cfg.batch):
        chunk = images[i:i + cfg.batch]
        allowed = {(kind, name) for kind, name, _ in chunk}
        content = [{"type": "text", "text": RULES + "\n\n" + CLAIM_SPEC}] + \
                  image_parts(chunk, cfg.max_edge, cfg.quality)
        try:
            raw = chat(cfg, cfg.vision_model, content)
            obj = parse_json(raw)
            for c in obj.get("claims") or []:
                if (isinstance(c, dict) and c.get("point") and c.get("quote")
                        and (c.get("image_type"), c.get("file")) in allowed):
                    claims.append(c)
        except Exception as e:
            print(f"      ! 批次 {i // cfg.batch + 1} 失败: {e}", file=sys.stderr)
    return claims


def merge_claims(cfg: Config, claims: list[dict]) -> dict:
    payload = json.dumps(claims, ensure_ascii=False)
    content = [{"type": "text", "text": MERGE_SPEC + "\n\n候选卖点：\n" + payload}]
    return parse_json(chat(cfg, cfg.text_model, content))


def normalize(data: dict) -> dict:
    """容忍模型的小偏差：中文 image_type、file 带子目录、多余字段。"""
    alias = {"主图": "main", "详情": "detail", "细节": "detail", "sku": "sku", "SKU": "sku"}
    for label in ("main_selling_points", "secondary_selling_points"):
        for item in data.get(label) or []:
            ev = []
            for e in item.get("image_evidence") or []:
                t = str(e.get("image_type") or "").strip()
                t = alias.get(t, t)
                f = str(e.get("file") or "").strip().replace("\\", "/")
                if "/" in f:
                    head, tail = f.rsplit("/", 1)
                    t = alias.get(head, t) if head in DIRS else t
                    f = tail
                ev.append({"image_type": t, "file": f,
                           "quote": str(e.get("quote") or "")})
            item["image_evidence"] = ev
            item["point"] = str(item.get("point") or "").strip()
    for e in data.get("product_name_evidence") or []:
        t = alias.get(str(e.get("image_type") or "").strip(), e.get("image_type"))
        e["image_type"] = t
        f = str(e.get("file") or "").replace("\\", "/")
        e["file"] = f.rsplit("/", 1)[-1]
    return data


def validate(data: dict, directory: str) -> list[str]:
    """校验卖点数量、长度及本地图片证据路径。"""
    errors: list[str] = []
    mains = data.get("main_selling_points") or []
    secs = data.get("secondary_selling_points") or []
    if not mains and not secs:
        errors.append("没有有效卖点")
    if len(mains) > 3:
        errors.append(f"main selling points = {len(mains)} (>3)")
    if len(secs) > 5:
        errors.append(f"secondary selling points = {len(secs)} (>5)")
    for label in ("main_selling_points", "secondary_selling_points"):
        for i, item in enumerate(data.get(label) or []):
            pt = item.get("point") or ""
            if not pt:
                errors.append(f"{label}[{i}] empty point")
            if len(pt) > 10:
                errors.append(f"{label}[{i}] point too long ({len(pt)}): {pt}")
            if "/" in pt:
                errors.append(f"{label}[{i}] point contains '/': {pt}")
            ev = item.get("image_evidence") or []
            if not ev:
                errors.append(f"{label}[{i}] no evidence: {pt}")
            for e in ev:
                t, f = e.get("image_type"), e.get("file") or ""
                if t not in DIRS:
                    errors.append(f"{label}[{i}] bad image_type: {t}")
                    continue
                if not f or Path(f).name != f or not Path(directory, DIRS[t], f).is_file():
                    errors.append(f"{label}[{i}] file missing: {DIRS[t]}/{f}")
                if not str(e.get("quote") or "").strip():
                    errors.append(f"{label}[{i}] empty quote")
    return errors


def repair(cfg: Config, data: dict, errors: list[str], directory: str) -> dict:
    body = REPAIR_SPEC.replace("__ERRORS__", "\n".join("- " + e for e in errors))
    body = body.replace("__JSON__", json.dumps(data, ensure_ascii=False))
    content = [{"type": "text", "text": RULES + "\n\n" + body}]
    return parse_json(chat(cfg, cfg.text_model, content))


def extract_one(cfg: Config, product: dict, out_dir: str) -> tuple[str, dict | None, str]:
    """返回 (状态, 结果, 说明)。"""
    directory = product["directory"]
    images = list_images(directory, cfg.max_images)
    if not images:
        return "skip", None, "没有可读图片"
    claims = extract_candidates(cfg, directory, images)
    if not claims:
        return "fail", None, "模型未给出任何候选卖点"
    data = merge_claims(cfg, claims)
    data = normalize(data)
    errors = validate(data, directory)
    if errors:
        try:
            data = normalize(repair(cfg, data, errors, directory))
            errors = validate(data, directory)
        except Exception as e:
            return "fail", data, f"修正失败: {e}"
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, product["folder"] + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if errors:
        return "invalid", data, f"{len(errors)} 处校验未过: " + "; ".join(errors[:3])
    return "ok", data, path


# ---------------------------------------------------------------- 数据库

def select_products(cur, args) -> list[dict]:
    sql = """
        select rp.id, rp.ranking_id, rp.platform_product_id, rp.directory,
               rp.title, sp.id is not null as done
          from ods.t_pdd_ranking_products rp
          left join ods.t_pdd_selling_point sp on sp.ranking_product_id = rp.id
         where true
    """
    params: list[Any] = []
    if args.product:
        sql += " and rp.id = %s"
        params.append(args.product)
    if args.ranking:
        sql += " and rp.ranking_id = %s"
        params.append(args.ranking)
    if not args.force:
        sql += " and sp.id is null"
    sql += " order by rp.ranking_id, rp.rank, rp.id"
    cur.execute(sql, params)
    rows = []
    for pid, rid, ppid, directory, title, done in cur.fetchall():
        if not directory:
            continue
        path = Path(directory).expanduser()
        directory = str((path if path.is_absolute() else spider_config.BUSINESS_ROOT / path).resolve())
        folder = os.path.basename(directory.rstrip("\\/"))
        if args.only and args.only not in folder:
            continue
        rows.append({"id": pid, "ranking": rid, "pid": ppid.strip(),
                     "directory": directory, "title": title,
                     "folder": folder, "done": done})
    if args.limit:
        rows = rows[: args.limit]
    return rows


def find_copy(cur, ppid: str) -> tuple[str, str, str] | None:
    if not ppid:
        return None
    cur.execute(
        """select main_point, secondary_point, raw_json
             from ods.t_pdd_selling_point
            where platform_product_id = %s
            order by id limit 1""",
        (ppid,),
    )
    return cur.fetchone()


def process_product(store, item, *, force=False):
    """Run only an unfinished extraction, including products with no known platform ID."""
    cur = store.db.cursor(row_factory=tuple_row)
    existing = cur.execute('SELECT id FROM ods.t_pdd_selling_point WHERE ranking_product_id=%s', (item['id'],)).fetchone()
    if existing and not force:
        cur.execute('UPDATE ods.t_pdd_selling_point SET platform_product_id=%s WHERE ranking_product_id=%s',
                    (item['platform_product_id'], item['id']))
        return
    product = dict(id=item['id'], pid=item['platform_product_id'].strip(), ranking=item['ranking_id'],
                   directory=item['directory'], folder=Path(item['directory']).name)
    source = None if force else find_copy(cur, product['pid'])
    if source:
        insert_copy(cur, product, source)
        return
    cfg = Config()
    cfg.check()
    status, data, info = extract_one(cfg, product, str(spider_config.BUSINESS_ROOT / 'data/points' / str(item['ranking_id'])))
    if status != 'ok':
        raise ValueError(info)
    upsert(cur, product, data)


def save_points(cur, product: dict, main: str, secondary: str, raw: str, *, replace: bool) -> str:
    # The deployed table has no unique product constraint: serialize writers on its parent row.
    with cur.connection.transaction():
        cur.execute("SELECT id FROM ods.t_pdd_ranking_products WHERE id=%s FOR UPDATE", (product["id"],))
        if not cur.fetchone():
            raise ValueError(f"产品已不存在：{product['id']}")
        cur.execute("SELECT id FROM ods.t_pdd_selling_point WHERE ranking_product_id=%s ORDER BY id", (product["id"],))
        existing = cur.fetchone()
        if existing:
            if not replace:
                return "exists"
            cur.execute("""UPDATE ods.t_pdd_selling_point SET platform_product_id=%s,
                main_point=%s,secondary_point=%s,raw_json=%s,updated_at=now()
                WHERE ranking_product_id=%s""", (product["pid"], main, secondary, raw, product["id"]))
            point_id = existing[0]
        else:
            cur.execute("""INSERT INTO ods.t_pdd_selling_point
                (ranking_product_id,platform_product_id,main_point,secondary_point,raw_json)
                VALUES(%s,%s,%s,%s,%s) RETURNING id""", (product["id"], product["pid"], main, secondary, raw))
            point_id = cur.fetchone()[0]
    return f"id={point_id} | {main} | {secondary}"


def upsert(cur, product: dict, data: dict) -> str:
    main = "/".join(x["point"] for x in data.get("main_selling_points") or [])
    secondary = "/".join(x["point"] for x in data.get("secondary_selling_points") or [])
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return save_points(cur, product, main, secondary, raw, replace=True)


def insert_copy(cur, product: dict, src: tuple[str, str, str]) -> str:
    return save_points(cur, product, *src, replace=False)


# ---------------------------------------------------------------- 主流程

def run_product(cfg: Config, product: dict, args) -> tuple[int, str]:
    label = f"{product['ranking']} {product['folder']} ({product['id']})"
    out_dir = os.path.join(args.out_root, str(product["ranking"]))
    if args.dry_run:
        with connect(readonly=True) as conn:
            cur = conn.cursor(row_factory=tuple_row)
            if not args.no_copy and not args.force:
                src = find_copy(cur, product["pid"])
                if src:
                    return 0, f"COPY {label}"
        status, data, info = extract_one(cfg, product, out_dir)
        return (0 if status == "ok" else 1), f"{status.upper():7} {label}: {info}"
    with connect() as conn:
        cur = conn.cursor(row_factory=tuple_row)
        if not args.no_copy and not args.force:
            src = find_copy(cur, product["pid"])
            if src:
                return 0, f"COPY    {label}: {insert_copy(cur, product, src)}"
        status, data, info = extract_one(cfg, product, out_dir)
        if status != "ok":
            return 1, f"{status.upper():7} {label}: {info}"
        return 0, f"OK      {label}: {upsert(cur, product, data)}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="电商图片卖点抽取（视觉模型版）")
    ap.add_argument("--ranking-id", dest="ranking", type=int, help="榜单 id，例如 319")
    ap.add_argument("--ranking-product-id", dest="product", type=int, help="只跑单个 ranking_product_id")
    ap.add_argument("--only", help="只跑文件夹名包含该字符串的产品")
    ap.add_argument("--limit", type=int, help="最多处理 N 个")
    ap.add_argument("--dry-run", action="store_true", help="只生成 JSON，不写库")
    ap.add_argument("--force", action="store_true", help="已完成的也重跑")
    ap.add_argument("--no-copy", action="store_true", help="不复用同款已有卖点")
    ap.add_argument("--list", action="store_true", help="只列出待处理产品")
    ap.add_argument("--workers", type=int, default=3, help="并发产品数，默认 3")
    ap.add_argument("--batch", type=int, default=6, help="每次请求送几张图，默认 6")
    ap.add_argument("--max-images", type=int, default=36, help="单产品最多读几张图")
    ap.add_argument("--max-edge", type=int, default=1280, help="图片长边上限（像素）")
    ap.add_argument("--quality", type=int, default=80, help="JPEG 压缩质量 1-95")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--model", help="覆盖 VISION_MODEL")
    ap.add_argument("--base-url", help="覆盖 DEEPSEEK_BASE_URL")
    ap.add_argument("--out-root", help="JSON 输出目录，默认业务目录 data/points/；相对路径基于业务目录")
    args = ap.parse_args(argv)
    for name in ("ranking", "product", "limit", "workers", "batch", "max_images", "max_edge", "timeout"):
        value = getattr(args, name)
        if value is not None and value <= 0:
            ap.error(f"{name.replace('_', '-')} 必须为正整数")
    if args.retries < 0 or not 1 <= args.quality <= 95:
        ap.error("retries 必须非负，quality 必须为 1-95")
    out_root = Path(args.out_root or "data/points").expanduser()
    args.out_root = str((out_root if out_root.is_absolute() else spider_config.BUSINESS_ROOT / out_root).resolve())

    cfg = Config(args)

    with connect(readonly=True) as conn:
        cur = conn.cursor(row_factory=tuple_row)
        products = select_products(cur, args)

    if not products:
        print("没有需要处理的产品。")
        return 0
    if args.list:
        for p in products:
            print(f"{p['ranking']}\t{p['folder']}\t{p['id']}\t{p['title'][:24]}")
        print(f"共 {len(products)} 个")
        return 0

    cfg.check()
    print(f"待处理 {len(products)} 个产品；模型 {cfg.vision_model} @ {cfg.base_url}")
    if args.dry_run:
        print("（dry-run：不写数据库）")

    done = failed = 0
    with futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        jobs = {pool.submit(run_product, cfg, p, args): p for p in products}
        for job in futures.as_completed(jobs):
            try:
                rc, line = job.result()
            except Exception as e:
                rc, line = 1, f"ERROR   {jobs[job]['id']}: {type(e).__name__}: {e}"
            print(line, flush=True)
            if rc == 0:
                done += 1
            else:
                failed += 1

    print(f"\n完成 {done} 个，失败 {failed} 个，共 {len(products)} 个")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, RuntimeError, OSError, DatabaseError) as error:
        print(f"卖点提取失败：{error}", file=sys.stderr)
        sys.exit(1)
