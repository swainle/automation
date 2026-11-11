"""产品页抓取：截图、主图、SKU、详情图、评论。"""
from automation.pdd.spider.logging_setup import timed_operation
import itertools
import re
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from datetime import datetime, timezone, timedelta

from PIL import Image

from automation.pdd.spider.sogou import crop_width_center, find_sogou_hwnd, freeze_page, get_scrollbar_width, grab_window
from automation.pdd.spider.comments import scrape_comments
from automation.pdd.spider.config import MAIN_IMG_XPATH, PRODUCT_CROP_WIDTH, SKU_PANEL, SKU_SELECTED_CLASS
from automation.pdd.spider.utils import download, sanitize, text, write_csv
from automation.pdd.spider.status import reconcile_product, stage_done, update_stage, set_listing_date, load_status

SOLD_OUT_TEXT = "商品已售罄，推荐以下相似商品"
SOLD_OUT_XPATH = '//*[@id="main"]/div/div[1]/span'


def is_sold_out(page) -> bool:
    notice = page.locator(SOLD_OUT_XPATH).filter(has_text=SOLD_OUT_TEXT)
    return notice.count() > 0 and notice.first.is_visible()


def _sold_out_url_info(url: str, current_url: str = "") -> dict:
    # 分享原链接优先，跳转后 URL 仅补充缺失字段，避免丢失分享参数。
    original = parse_qs(urlsplit(url).query, keep_blank_values=True)
    current = parse_qs(urlsplit(current_url).query, keep_blank_values=True)
    def get(*names):
        for query in (original, current):
            for name in names:
                if query.get(name) and query[name][0]:
                    return query[name][0]
        return ""
    info = {"goods_id": get("goods_id"),
            "search_keyword": get("_x_query", "search_key", "keyword"),
            "share_channel": get("refer_share_channel", "share_channel"),
            "share_timestamp": get("_oak_share_time", "share_time"), "share_time": ""}
    try:
        timestamp = int(info["share_timestamp"])
        if timestamp >= 10**12:
            timestamp /= 1000
        info["share_time"] = datetime.fromtimestamp(timestamp, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S +08:00")
    except (ValueError, OverflowError, OSError):
        pass
    return info


def mark_sold_out(rank_dir: Path, url: str = "", current_url: str = "") -> None:
    status = load_status(rank_dir)
    previous = status.get("stages", {}).get("product", {})
    info = _sold_out_url_info(url or status.get("rank_data", {}).get("产品链接", ""), current_url)
    attempts = int(previous.get("sold_out_attempts", 0)) + 1
    skipped = attempts > 10
    update_stage(rank_dir, "product", "skipped" if skipped else "retry_pending",
                 SOLD_OUT_TEXT, sold_out_attempts=attempts, outcome="sold_out", retryable=not skipped,
                 sold_out_url_info=info)
    reconcile_product(rank_dir)
    print(f"    售罄第 {attempts} 次，{'超过10次，永久跳过' if skipped else '保留待重试'}")
    print(f"    产品ID: {info['goods_id'] or 'URL未提供'} | 搜索关键词: {info['search_keyword'] or 'URL未提供'}")
    print(f"    分享渠道: {info['share_channel'] or 'URL未提供'} | 分享时间戳: {info['share_timestamp'] or 'URL未提供'}"
          f" | 分享时间: {info['share_time'] or '无法解析或未提供'}")


def _clean_sku_text(value: str) -> str:
    value = re.sub(r"[\u200b-\u200f\ufeff]", "", value)
    notices = r"(?:快要抢光|即将售罄|库存紧张|仅剩\s*\d+\s*件)"
    lines = []
    for line in value.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if not line or re.fullmatch(notices, line):
            continue
        line = re.sub(r"\s+" + notices + r"$", "", line).strip()
        if line:
            lines.append(line)
    return " ".join(lines)


def _sku_price(value: str) -> str:
    match = re.search(r"[¥￥]\s*(\d+(?:\.\d+)?)", value)
    if not match:
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*", value)
    return match.group(1) if match else ""


@timed_operation('[商品] 打开商品页')
def open_product_page(context, url: str, *, on_page=None):
    """新建页面直接打开产品链接，返回产品页 Page 对象。"""
    pg = context.new_page()
    try:
        if on_page is not None:
            on_page(pg)
        response = pg.goto(url, wait_until="domcontentloaded", timeout=30000)
        if response and response.status >= 400:
            raise RuntimeError(f"商品页 HTTP {response.status}")
    except Exception:
        pg.close()
        raise
    pg.bring_to_front()
    pg.wait_for_timeout(3000)
    return pg


def scrape_product(page, rank_dir: Path, row: dict) -> None:
    """抓取单个产品：截图、主图、SKU、详情图、评论。"""
    reconcile_product(rank_dir, row)
    if is_sold_out(page):
        mark_sold_out(rank_dir, row.get("产品链接", ""), page.url)
        return
    freeze_page(page)  # 产品页同样禁用动画/过渡

    page.wait_for_timeout(3000)

    # 1. 产品页截图 + 裁剪（宽 386 居中，高度不变）
    stage = "screenshot"
    try:
        if not stage_done(rank_dir, stage):
            if not grab_window(page, rank_dir / "产品页截图.jpg"):
                raise RuntimeError("产品页截图失败")
            hwnd = find_sogou_hwnd()
            scrollbar = get_scrollbar_width(page, hwnd) if hwnd else 0
            crop_width_center(rank_dir / "产品页截图.jpg", PRODUCT_CROP_WIDTH, scrollbar=scrollbar)
            update_stage(rank_dir, stage, "completed")
        stage = "main_images"
        if not stage_done(rank_dir, stage):
            row["上架日期"] = _scrape_main_images(page, rank_dir)
            update_stage(rank_dir, stage, "completed", count=len(list((rank_dir / "主图").glob("主图_*.jpg"))))
        from automation.pdd.spider.utils import refresh_listing_dates
        refresh_listing_dates(rank_dir.parent / "榜单数据.xlsx")
        stage = "sku"
        if not stage_done(rank_dir, stage):
            found = _scrape_sku(page, rank_dir, row)
            update_stage(rank_dir, stage, "completed" if found else "skipped",
                         "" if found else "未找到可枚举的 SKU")
        stage = "details"
        if not stage_done(rank_dir, stage):
            found = _scrape_detail_images(page, rank_dir)
            update_stage(rank_dir, stage, "completed" if found else "skipped",
                         "" if found else "页面未提供详情图")
        stage = "comments"
        if not stage_done(rank_dir, "comments") or not stage_done(rank_dir, "negative_comments"):
            scrape_comments(page, rank_dir)
    except Exception as ex:
        if stage != "comments":
            update_stage(rank_dir, stage, "failed", str(ex))
        raise
    finally:
        reconcile_product(rank_dir, row)


def listing_date_from_image_url(url: str) -> str:
    """Keep the existing first-image URL date rule; never infer a timestamp."""
    from datetime import date
    match = re.search(r"/(\d{4}-\d{2}-\d{2})/", urlsplit(url).path)
    if match:
        try:
            return date.fromisoformat(match.group(1)).isoformat()
        except ValueError:
            pass
    return ""


@timed_operation('[商品] 采集主图和上架日期')
def _scrape_main_images(page, rank_dir: Path, *, collect_date: bool = True) -> str:
    main_dir = rank_dir / "主图"
    main_dir.mkdir(parents=True, exist_ok=True)
    n = 1
    failed = False
    listing_date = ""
    for i in range(1, 30):
        loc = page.locator(MAIN_IMG_XPATH.format(i))
        if loc.count() == 0:
            break
        src = loc.first.get_attribute("src") or ""
        if not src:
            break
        if i == 1 and collect_date:
            # 只取首图路径中的日期；后续轮播图不能替代首图。
            listing_date = listing_date_from_image_url(src)
            # 与 iOS 榜单增量保存共享日期，避免后续写表将日期清空。
            set_listing_date(rank_dir, listing_date)
        if download(src, main_dir / f"主图_{i:02d}.jpg"):
            n += 1
        else:
            failed = True
    print(f"    主图 {n - 1} 张")
    if n == 1 or failed:
        raise RuntimeError("产品主图下载不完整，需重试")
    return listing_date


def _ensure_sku_panel(page):
    """隐藏面板也保留完整 DOM，必须确认已打开且选项可见。"""
    panel = page.locator(SKU_PANEL + ":visible").first
    if panel.count():
        panel.locator(".F7sZG3xe").first.wait_for(state="visible", timeout=5000)
        return panel
    labels = re.compile("免拼购买|立即购买|单独购买|发起拼单|立即拼单")
    candidates = page.get_by_role("button").filter(has_text=labels)
    for button in candidates.all():
        if not button.is_visible():
            continue
        button.click(timeout=5000)
        panel.wait_for(state="visible", timeout=5000)
        panel.locator(".F7sZG3xe").first.wait_for(state="visible", timeout=5000)
        return panel
    raise RuntimeError("未找到可见购买入口，SKU 面板未打开；保留待采状态")


def _select_sku_combo(page, dims, combo):
    for attempt in range(2):
        try:
            panel = _ensure_sku_panel(page)
            for di, option in enumerate(combo):
                element = panel.locator(".bIhLWVqm").nth(dims[di]["index"]).locator(".F7sZG3xe").nth(option["index"])
                element.wait_for(state="visible", timeout=3000)
                if SKU_SELECTED_CLASS not in (element.get_attribute("class") or "").split():
                    element.scroll_into_view_if_needed(timeout=3000)
                    element.click(timeout=3000)
                    page.wait_for_timeout(300)
                if SKU_SELECTED_CLASS not in (element.get_attribute("class") or "").split():
                    raise RuntimeError(f"SKU 选项未成功选中：{option['text']}")
            return panel
        except Exception:
            if attempt:
                raise
            print("    SKU 面板或选项状态变化，重新打开并选择当前组合")
    raise RuntimeError("SKU 组合选择失败")


@timed_operation('[商品] 采集 SKU')
def _scrape_sku(page, rank_dir: Path, row: dict) -> bool:
    """打开 SKU 面板，枚举变体组合，下载 SKU 图 + 记录 SKU.csv。"""
    panel = _ensure_sku_panel(page)

    # 枚举 SPU 维度（class 定位，避免 div 索引错位）
    dims = []
    dim_els = panel.locator(".bIhLWVqm")
    for d in range(dim_els.count()):
        dim = dim_els.nth(d)
        key = _clean_sku_text(text(dim.locator(".sku-specs-key")))
        opts = dim.locator(".F7sZG3xe")
        opt_texts = []
        for o in range(opts.count()):
            opt = opts.nth(o)
            t = _clean_sku_text(text(opt))
            if not t:
                continue
            opt_texts.append({"index": o, "text": t})
        if key and opt_texts:
            dims.append({"name": key, "index": d, "options": opt_texts})

    print(f"    SPU 维度: {[d['name'] for d in dims]}")

    combos = list(itertools.product(*[d["options"] for d in dims])) if dims else []
    print(f"    变体组合数: {len(combos)}")

    sku_dir = rank_dir / "SKU"
    sku_dir.mkdir(parents=True, exist_ok=True)

    sku_rows = []
    for seq, combo in enumerate(combos, 1):
        panel = _select_sku_combo(page, dims, combo)
        page.wait_for_timeout(1000)

        img_src = panel.locator("img").first.get_attribute("src") or ""
        price = _sku_price(text(panel.locator(".ujEqGzEB")))
        labels = [option["text"] for option in combo]

        fname = "SKU_" + "_".join([f"{seq:02d}"] + [sanitize(c) for c in labels]) + ".jpg"
        if not download(img_src, sku_dir / fname):
            raise RuntimeError(f"SKU 图片下载失败：{seq}")

        sku_rows.append([seq, "_".join(labels), price, *labels])
        print(f"      SKU{seq:02d} | {price} | {' / '.join(labels)}")

    header = ["序号", "商品名称", "卷后价"] + [f"SPU{i}" for i in range(1, len(dims) + 1)]
    write_csv(rank_dir / "SKU.csv", header, sku_rows)

    # 关闭面板
    try:
        panel.locator(".GvCXfXbo").first.click(timeout=3000)
    except Exception:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
    page.wait_for_timeout(800)
    return bool(combos)


@timed_operation('[商品] 采集详情图')
def _scrape_detail_images(page, rank_dir: Path) -> bool:
    """滚动加载详情图并下载，最后合并长图。"""
    detail_dir = rank_dir / "详情图片"
    detail_dir.mkdir(parents=True, exist_ok=True)

    for _ in range(8):
        page.evaluate("window.scrollBy(0, 4000)")
        page.wait_for_timeout(600)

    srcs = page.evaluate("""() => {
        const header = document.querySelector('#main .pApAfrwF');
        if (!header) return [];
        let box = header.nextElementSibling;
        if (!box) return [];
        return Array.from(box.querySelectorAll('img')).map(im => im.getAttribute('src') || '').filter(Boolean);
    }""")
    print(f"    详情图 {len(srcs)} 张")
    paths = []
    for i, src in enumerate(srcs, 1):
        p = detail_dir / f"{i:02d}.jpg"
        if download(src, p):
            paths.append(p)
    _merge_long_image(paths, detail_dir / "长图.jpg")
    if srcs and len(paths) != len(srcs):
        raise RuntimeError("部分详情图下载失败")
    if srcs and not (detail_dir / "长图.jpg").exists():
        raise RuntimeError("详情长图合成失败")
    return bool(srcs)


@timed_operation('[商品] 合并长图')
def _merge_long_image(paths: list[Path], out: Path) -> None:
    """将详情图按顺序竖排合并成长图：等宽，小图等比放大到统一宽度。"""
    if not paths:
        return
    imgs = []
    for p in paths:
        try:
            imgs.append(Image.open(p))
        except Exception:
            continue
    if not imgs:
        return
    width = max(im.width for im in imgs)  # 统一宽度（取最大）
    scaled = []
    total_h = 0
    for im in imgs:
        if im.width != width:
            new_h = int(im.height * width / im.width)  # 等比缩放
            im = im.resize((width, new_h), Image.LANCZOS)
        scaled.append(im)
        total_h += im.height
    canvas = Image.new("RGB", (width, total_h), "white")
    y = 0
    for im in scaled:
        canvas.paste(im, (0, y))
        y += im.height
    canvas.save(out, quality=90)
    print(f"    长图已合并 -> {out.name}")
