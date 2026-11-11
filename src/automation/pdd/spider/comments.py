"""评价采集：保留普通评价，并单独采集灰色负面标签评价。"""
from automation.pdd.spider.logging_setup import timed_operation
import re
from pathlib import Path
from urllib.parse import urlsplit

from automation.pdd.spider.utils import format_specs, ts_to_date, write_csv
from automation.pdd.spider.status import stage_done, update_stage

COMMENT_HEADER = ["序号", "SKU", "用户昵称", "复购", "评论日期", "评论内容", "获赞", "回复数"]
COMMENT_ENTRY = '//*[@id="main"]/div/div[2]/div[17]/div[1]/div/span'
TAG_SELECTOR = "#main ul li"


def _comment_rows(comments: list[dict]) -> list[list]:
    return [[i, format_specs(c.get("specs", "")), c.get("name", ""),
             c.get("order_num_text", ""), c.get("评论日期") or ts_to_date(c.get("time")),
             c.get("comment", ""), c.get("favor_count", ""), c.get("reply_count", "")]
            for i, c in enumerate(comments, 1)]


def _review_path(url: str) -> str | None:
    path = urlsplit(url).path
    return path if re.fullmatch(r"/proxy/api/reviews/[^/]+/list", path) else None


def _collect_review_batch(page, trigger, limit: int) -> list[dict]:
    """仅接收本轮点击之后发出的请求，排除上个筛选尚未返回的响应。"""
    comments = []
    seen = set()
    requests = set()
    review_path = None
    received = False

    def on_request(request):
        if _review_path(request.url):
            requests.add(request)

    def on_response(response):
        nonlocal review_path, received
        if response.request not in requests:
            return
        requests.discard(response.request)
        path = _review_path(response.url)
        if not path or (review_path and path != review_path):
            return
        try:
            data = response.json().get("data")
            if not isinstance(data, list):
                return
            received = True
            review_path = path
            for item in data:
                rid = item.get("review_id")
                if rid is not None and str(rid) not in seen:
                    seen.add(str(rid))
                    comments.append(item)
        except Exception as ex:
            print(f"    评价接口解析失败: {type(ex).__name__}")

    page.on("request", on_request)
    page.on("response", on_response)
    try:
        trigger()
        page.locator("#goods-comments-list").wait_for(state="attached", timeout=10000)
        page.wait_for_timeout(1500)
        stalled = 0
        for _ in range(40):
            if len(comments) >= limit:
                break
            before = len(comments)
            page.keyboard.press("End")
            page.wait_for_timeout(1500)
            stalled = stalled + 1 if len(comments) == before else 0
            if stalled >= 2:
                break
        if not received:
            raise RuntimeError("未收到本轮评价接口，不能把采集失败判定为无评价")
        return comments[:limit]
    finally:
        page.remove_listener("request", on_request)
        page.remove_listener("response", on_response)


def _negative_tag_names(page) -> list[str]:
    # 实测负面标签带 pEu3DjzW，背景为 rgb(240,240,240)。
    return page.locator(TAG_SELECTOR).evaluate_all("""els => els.filter(e => {
        const rgb = getComputedStyle(e).backgroundColor.match(/\\d+/g) || [];
        const [r,g,b] = rgb.map(Number);
        return e.classList.contains('pEu3DjzW') ||
            (r >= 220 && r <= 249 && Math.abs(r-g) <= 3 && Math.abs(g-b) <= 3);
    }).map(e => e.innerText.trim()).filter(Boolean)""")


def _click_negative_tag(page, name: str) -> None:
    tag = page.locator(TAG_SELECTOR).filter(has_text=re.compile("^" + re.escape(name) + "$", re.S)).first
    page.evaluate("window.scrollTo(0,0)")
    try:
        tag.click(timeout=2500)
    except Exception:
        # 展开 XPath 匹配两个 div，真实展开按钮为 mQMJpeQt。
        expander = page.locator("#main .mQMJpeQt").first
        if expander.count():
            expander.click(timeout=2500)
        tag.click(timeout=5000)


def _enter_comments(page) -> None:
    selectors = ["//div[starts-with(normalize-space(text()),'商品评价')]/following-sibling::span",
                 COMMENT_ENTRY, "//span[normalize-space(text())='查看全部']"]
    for selector in selectors:
        for locator in page.locator(selector).all()[:3]:
            try:
                locator.click(timeout=3000)
                page.locator("#goods-comments-list").wait_for(state="attached", timeout=6000)
                return
            except Exception:
                continue
    raise RuntimeError("未找到可进入评价列表的商品评价入口")


@timed_operation('[商品] 采集评价')
def scrape_comments(page, rank_dir: Path, *, normal: bool = True,
                    negative: bool = True, force: bool = False) -> None:
    """普通评价前200条 + 灰色标签评价合并去重前100条，使用相同列格式。"""
    already_in_list = page.locator("#goods-comments-list").count() > 0

    def enter():
        if already_in_list:
            page.reload(wait_until="domcontentloaded")
        else:
            _enter_comments(page)

    active_stage = "comments" if normal else "negative_comments"
    try:
        if not normal:
            if not already_in_list:
                _enter_comments(page)
        elif not force and stage_done(rank_dir, "comments"):
            if not already_in_list:
                _enter_comments(page)
            print("    普通评价已完成，直接继续差评")
        else:
            comments = _collect_review_batch(page, enter, 200)
            write_csv(rank_dir / "评论.csv", COMMENT_HEADER, _comment_rows(comments))
            update_stage(rank_dir, "comments", "completed", count=len(comments))
            print(f"    评论 {len(comments)} 条")
        if not negative:
            return
        active_stage = "negative_comments"
        if not force and stage_done(rank_dir, "negative_comments"):
            print("    差评已完成或已确认无差评，跳过")
            return
        names = list(dict.fromkeys(_negative_tag_names(page)))
        negative = []
        seen = set()
        for name in names:
            if len(negative) >= 100:
                break
            print(f"    采集灰色评价标签：{name}")
            batch = _collect_review_batch(page, lambda: _click_negative_tag(page, name), 100)
            for item in batch:
                rid = str(item["review_id"])
                if rid not in seen:
                    seen.add(rid)
                    negative.append({**item, "negative_reason": name})
            negative = negative[:100]
            if negative:
                write_csv(rank_dir / "差评.csv", COMMENT_HEADER + ["原因"],
                          [r + [c["negative_reason"]] for r, c in zip(_comment_rows(negative), negative)])
        if negative:
            update_stage(rank_dir, "negative_comments", "completed", count=len(negative), tags=names)
        else:
            (rank_dir / "差评.csv").unlink(missing_ok=True)
            reason = "无灰色负面标签" if not names else "灰色标签接口返回空评价"
            update_stage(rank_dir, "negative_comments", "skipped", reason, count=0, tags=names)
            print(f"    {reason}，不生成差评 CSV")
        print(f"    差评 {len(negative)} 条")
    except Exception as ex:
        update_stage(rank_dir, active_stage, "failed", str(ex))
        raise
    finally:
        if not already_in_list and "goods_comments" in page.url:
            page.go_back(wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
