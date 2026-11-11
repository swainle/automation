"""DOM 版榜单：解析页面卡片元素提取数据。

包含：
- Leaderboard —— DOM 版榜单基类（抽象）
- BestSelling / BestReviewed / PriceDrop —— 畅销/好评/降价榜（DOM 版）
- 榜单来源读取（Excel）+ tab 读取 + tab→类映射（TAB_TO_CLASS）

与 api.py（接口版）并列，两者输出列一致，可写同一份榜单数据.xlsx。
"""
from __future__ import annotations

import re
import urllib.parse
from abc import ABC, abstractmethod
from pathlib import Path

import openpyxl

from automation.pdd.spider.sogou import (
    crop_center_top,
    find_sogou_hwnd,
    freeze_page,
    get_scrollbar_width,
    grab_window,
    scroll_card_to_top,
)
from automation.pdd.spider.config import COMMON_HEADER, GOODS_URL
from automation.pdd.spider.exceptions import RiskControlError
from automation.pdd.spider.utils import sales_to_int

# 公共卡片提取 JS：查询页面所有榜单卡片，提取公共字段 + 卡片文本。
# 每个榜单类型按自己的「特有字段标记」筛选后再提取特有字段。
_COMMON_EXTRACT_JS = r"""
() => {
    const cards = document.querySelectorAll('#main [data-ranking-goods-id]');
    const seen = new Set();
    const out = [];
    cards.forEach((card) => {
        const gid = card.getAttribute('data-ranking-goods-id');
        if (!gid || seen.has(gid)) return;
        seen.add(gid);
        const txt = card.innerText || '';

        // 标题
        let title = '';
        const tEl = card.querySelector('[title]');
        if (tEl) title = (tEl.getAttribute('title') || tEl.innerText || '').trim();
        if (!title) {
            const qg = card.querySelector('.QGXh9I1y');
            if (qg) title = (qg.innerText || '').trim();
        }

        // 排行角标
        let rank = '';
        const badge = card.querySelector('.xxxVM4ts');
        if (badge) rank = (badge.innerText || '').trim();

        // 价格
        let price = '';
        const pm = txt.match(/[¥￥]\s*([\d.]+)/);
        if (pm) price = pm[1];

        // 上架日期（图片 URL 里的日期）
        let listing = '';
        for (const im of card.querySelectorAll('img')) {
            const m = (im.getAttribute('src') || '').match(/(\d{4}-\d{2}-\d{2})/);
            if (m) { listing = m[1]; break; }
        }

        out.push({ rank: rank || String(out.length + 1), goods_id: gid, title, price, listing, text: txt });
    });
    return out;
}
"""


class Leaderboard(ABC):
    """榜单接口（DOM 版基类）。

    每个榜单类必须提供：name、ref_selector、crop、extra_fields、marker、
    以及 extract_cards(page) 方法（返回 [{字段: 值}, ...]，每行须含 '产品ID'）。
    URL 由实例化时传入（来自 Excel）。
    """

    name: str = ""                      # 榜单类型名（畅销榜/好评榜/降价榜）
    ref_selector: str = "#main > div:first-of-type"  # 截图对齐参考元素（吸顶元素 #main > div[1]）
    crop: tuple[int, int] = (390, 442)  # 截图裁剪框 (宽, 高)
    extra_fields: list[str] = []        # 榜单特有字段，追加在固定列后面
    marker: str = ""                    # 卡片特有字段标记（用于从所有卡片里筛出本榜单）

    def __init__(self, url: str):
        self.url = url

    @property
    def header(self) -> list[str]:
        return COMMON_HEADER + self.extra_fields

    def _extract_raw(self, page) -> list[dict]:
        """提取所有卡片公共字段（含卡片文本）。"""
        return page.evaluate(_COMMON_EXTRACT_JS)

    def _common_fields(self, r: dict, i: int) -> dict:
        """公共字段行（i 为榜单内序号，即排行）。"""
        from automation.pdd.spider.metrics import from_text
        return {
            "metrics": from_text(r.get("text", "")),
            "排行": i,
            "产品ID": r["goods_id"],
            "产品链接": GOODS_URL.format(r["goods_id"]),
            "标题": r["title"],
            "价格": r.get("price", ""),
            "上架日期": r.get("listing", ""),
        }

    @abstractmethod
    def extract_cards(self, page) -> list[dict]:
        """提取卡片，返回 [{字段: 值}, ...]，每行须含 '产品ID'。"""
        raise NotImplementedError

    def scrape(self, page) -> list[dict]:
        """通用抓取流程：goto → 等卡片 → 滚动加载 → 冻结动画 → extract_cards。

        卡片超时未加载视为风控，抛 RiskControlError。
        """
        page.goto(self.url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        # 等待卡片加载（超时则疑似风控）
        for _ in range(15):
            if page.locator("[data-ranking-goods-id]").count() > 0:
                break
            page.wait_for_timeout(1000)
        else:
            raise RiskControlError(f"{self.name} 页面未加载出榜单卡片，疑似风控")

        # 滚动到底部，触发懒加载（反复滚到底，等卡片分批加载完）
        for _ in range(8):
            page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            page.wait_for_timeout(2000)

        freeze_page(page)  # 滚动加载完再冻结动画，避免干扰懒加载

        return self.extract_cards(page)

    def shoot_card(self, page, goods_id: str, path: Path) -> None:
        """截图：滚动对齐 → 截内容区 → 按本榜单裁剪框裁剪。"""
        scroll_card_to_top(page, goods_id, self.ref_selector)
        page.wait_for_timeout(800)  # 等画面稳定，避免截到闪动帧
        if grab_window(page, path):
            hwnd = find_sogou_hwnd()
            scrollbar = get_scrollbar_width(page, hwnd) if hwnd else 0
            crop_center_top(path, *self.crop, scrollbar=scrollbar)


class BestSelling(Leaderboard):
    """畅销榜。"""

    name = "畅销榜"
    extra_fields = ["霸榜天数", "销量", "热卖指数"]
    marker = "热卖指数"

    def extract_cards(self, page) -> list[dict]:
        return self.parse_cards(self._extract_raw(page))

    def parse_cards(self, raw):
        rows = []
        seq = 0
        for r in raw:
            txt = r.get("text", "")
            if self.marker not in txt:
                continue
            seq += 1
            row = self._common_fields(r, seq)

            mb = re.search(r"蝉联榜首\d+天|霸榜前三\d+天|霸榜\d+天|蝉联\d+天", txt)
            ms = re.search(r"疯抢[\d.]+万件|已抢[\d.]+万件|[\d.]+万件|[\d.]+人已拼|[\d.]+件", txt)
            mh = re.search(r"热卖指数\s*(\d+)", txt)

            row["霸榜天数"] = mb.group(0) if mb else ""
            row["销量"] = sales_to_int(ms.group(0)) if ms else 0
            row["热卖指数"] = mh.group(1) if mh else ""

            rows.append(row)
            print(f"  排行{row['排行']} | {row['产品ID']} | {row['标题'][:20]} | {row['价格']} | 销量{row['销量']}")
        print(f"榜单卡片数量: {len(rows)}")
        return rows


class BestReviewed(Leaderboard):
    """好评榜。"""

    name = "好评榜"
    extra_fields = ["好评率"]
    marker = "好评率"

    def extract_cards(self, page) -> list[dict]:
        return self.parse_cards(self._extract_raw(page))

    def parse_cards(self, raw):
        rows = []
        seq = 0
        for r in raw:
            txt = r.get("text", "")
            if self.marker not in txt:
                continue
            seq += 1
            row = self._common_fields(r, seq)

            m = re.search(r"好评率\s*([\d.]+%)", txt)
            row["好评率"] = m.group(1) if m else ""

            rows.append(row)
            print(f"  排行{row['排行']} | {row['产品ID']} | {row['标题'][:20]} | {row['价格']} | 好评率{row['好评率']}")
        print(f"榜单卡片数量: {len(rows)}")
        return rows


class PriceDrop(Leaderboard):
    """降价榜。"""

    name = "降价榜"
    extra_fields = ["降价", "近30天成交价"]
    marker = "成交价"

    def extract_cards(self, page) -> list[dict]:
        return self.parse_cards(self._extract_raw(page))

    def parse_cards(self, raw):
        rows = []
        seq = 0
        for r in raw:
            txt = r.get("text", "")
            if self.marker not in txt:
                continue
            seq += 1
            row = self._common_fields(r, seq)

            m1 = re.search(r"直降[\s\S]*?([¥￥][\d.]+)", txt)
            m2 = re.search(r"近30日成交价\s*([¥￥][\d.]+)", txt)
            row["降价"] = m1.group(1) if m1 else ""
            row["近30天成交价"] = m2.group(1) if m2 else ""

            rows.append(row)
            print(f"  排行{row['排行']} | {row['产品ID']} | {row['标题'][:20]} | {row['价格']} | 降价{row['降价']} 成交价{row['近30天成交价']}")
        print(f"榜单卡片数量: {len(rows)}")
        return rows


# 榜单 tab 名称 -> DOM 版类
TAB_TO_CLASS = {
    "畅销榜": BestSelling,
    "好评榜": BestReviewed,
    "降价榜": PriceDrop,
}


def read_leaderboards(path: Path) -> list[dict]:
    """读取 B 来源、C 主榜、D 子榜、E 链接；一行只对应一个子榜。"""
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    items = []
    seen = set()
    identities = set()
    try:
        for number, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
            if len(row) < 2 or not row[1]:
                continue
            values = list(row) + [None] * 5
            name, main, tab, url = [str(value or '').strip() for value in values[1:5]]
            tab = '降价榜' if tab == '降价版' else tab
            if not main or (tab and tab not in TAB_TO_CLASS):
                raise ValueError(f'来源表第 {number} 行的 C/D 列无效')
            parts = urllib.parse.urlsplit(url)
            if tab and (parts.scheme not in {'http', 'https'} or not parts.hostname):
                raise ValueError(f'来源表第 {number} 行 E 列不是有效 HTTP/HTTPS 链接')
            index = list(TAB_TO_CLASS).index(tab) if tab else 0
            active = urllib.parse.parse_qs(parts.query).get('activeTab', [str(index)])
            if tab and active != [str(index)]:
                raise ValueError(f'来源表第 {number} 行链接 activeTab 与 D 列不对应')
            key = (name, tab)
            if key in seen:
                raise ValueError(f'来源表第 {number} 行重复来源/子榜：{name}/{tab}')
            if (main, tab) in identities:
                raise ValueError(f'来源表第 {number} 行重复主榜/子榜：{main}/{tab}')
            seen.add(key)
            identities.add((main, tab))
            items.append({'name': name, 'main_name': main, 'tab': tab, 'url': url, 'tab_index': index})
    finally:
        wb.close()
    return items


def normalize_url(url: str) -> str:
    """去掉 activeTab 参数后的 URL（用于链接去重）。"""
    parts = urllib.parse.urlsplit(url)
    qs = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k != "activeTab"]
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(qs), parts.fragment))


def set_active_tab(url: str, tab: int) -> str:
    """设置/替换 URL 里的 activeTab 参数。"""
    parts = urllib.parse.urlsplit(url)
    qs = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k != "activeTab"]
    qs.append(("activeTab", str(tab)))
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(qs), parts.fragment))


def get_tabs(page) -> list[str]:
    """读取榜单页的 tab 名称（class Bt00keaq，文本如「畅销榜/好评榜/降价榜」）。"""
    return page.evaluate("""() => {
        return Array.from(document.querySelectorAll('.Bt00keaq'))
            .map(el => (el.innerText || '').trim())
            .filter(Boolean);
    }""")


def _first_text(page, xpaths: list[str]) -> str:
    """按多个 XPath 依次尝试，返回第一个命中的文本。"""
    for xp in xpaths:
        loc = page.locator(xp)
        if loc.count():
            return (loc.first.text_content() or "").strip()
    return ""


def get_leaderboard_name(page) -> str:
    """榜单名（子榜单用这个名字）。"""
    return _first_text(page, ['//*[@id="main"]/div[1]/div/div[2]/div[3]/div[2]/div/div[1]'])


def get_period_info(page) -> str:
    """榜单周期信息（如「根据近30日…每日更新」）。"""
    return _first_text(page, [
        '//*[@id="main"]/div[1]/div/div[2]/div[3]/div[2]/div/div[2]',
        '//*[@id="main"]/div[1]/div/div[1]/div[1]/div[1]',
    ])
