"""Object-oriented collectors exposing the existing scraping operations as steps."""
from dataclasses import dataclass, field
from datetime import date
import hashlib
import json
import logging
from pathlib import Path
import re
import shutil
import sys
from tempfile import TemporaryDirectory
import time

from automation.pdd.spider import config
from automation.pdd.spider.workflow import StepConditions, Workflow, parse_flow, step_names


@dataclass(frozen=True)
class Step:
    description: str
    conditions: StepConditions
    phases: frozenset[str]
    root_only: bool = False
    implemented: bool = True


def step(description, *, requires=(), provides=(), invalidates=(), loop_requires=(),
         item_conditions=(), phases=("before", "loop", "after"), root_only=False, implemented=True):
    def decorate(method):
        method.step = Step(description, StepConditions(frozenset(requires),
            frozenset(provides), frozenset(invalidates), frozenset(loop_requires),
            frozenset(item_conditions)), frozenset(phases), root_only, implemented)
        return method
    return decorate


@dataclass
class ProductItem:
    table: Path
    row: dict

    @property
    def directory(self):
        return self.table.parent / f"榜{int(self.row['排行']):02d}"


@dataclass
class RankingItem:
    name: str
    url: str = ""
    tab: str = ""
    index: int = 0
    has_sub: bool = True
    directory: Path | None = None
    header: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    row: dict | None = None
    raw: dict | None = None
    groups: list = field(default_factory=list)
    main_name: str = ''

    @property
    def image_directory(self):
        if self.row is None:
            return self.directory
        return self.directory / f"榜{int(self.row['排行']):02d}"


def _write_rank_item_links(item):
    from automation.pdd.spider.artifacts import write_rank_share_links
    if item.rows:
        write_rank_share_links(item.directory, [item.row] if item.row is not None else item.rows)
    elif item.groups:
        for group in item.groups:
            _write_rank_item_links(group)
    else:
        raise RuntimeError("当前榜单尚未获取商品链接")


class Collector:
    device = ""
    mode = ""
    collect = ""
    unsupported_steps = frozenset()

    def __init__(self, runtime):
        self.runtime = runtime
        self.steps = {name: getattr(type(self), name).step for name in dir(type(self))
                      if hasattr(getattr(type(self), name), "step") and name not in self.unsupported_steps}
        self.workflow = Workflow(self, self.steps,
            conditions={name: spec.conditions for name, spec in self.steps.items()},
            phases={name: spec.phases for name, spec in self.steps.items()},
            root_only={name for name, spec in self.steps.items() if spec.root_only},
            available={"device"})
        self.image = None
        self.images = []
        self._temporary = None
        self._session = str(time.time_ns())
        self._shot = 0
        self._selected = set()

    def set_device(self, device):
        if device != type(self).device:
            raise ValueError(f"本采集器仅支持 {type(self).device}")
        return self

    @property
    def property(self):
        return {"device": self.device, "mode": self.mode, "collect": self.collect,
                "output_root": config.OUTPUT_ROOT, "source": config.EXCEL_PATH}

    def validate(self, flow):
        nodes = parse_flow(flow) if isinstance(flow, str) else tuple(flow)
        for name in step_names(nodes):
            if name in self.steps and not self.steps[name].implemented:
                raise ValueError(f"步骤 {name} 尚未实现，等待详细设计")
        nodes = self.workflow.validate(nodes)
        selected = set(step_names(nodes))
        if selected & {"get_data", "list_rank_data", "get_boards", "list_boards", "collect_all"}:
            try:
                self.check_inputs()
            except ValueError:
                raise
            except Exception as error:
                raise ValueError(f"检查采集输入失败: {error}") from error
        return nodes

    def check_inputs(self):
        pass

    def run(self, flow):
        nodes = self.validate(flow)
        self._selected = set(step_names(nodes))
        try:
            self.workflow.run(nodes)
        finally:
            original_error = sys.exception()
            try:
                self.close()
            except Exception:
                if original_error is None:
                    raise
                logging.getLogger(__name__).exception("清理采集器失败，保留原始执行异常")

    def start_runtime(self):
        if self._selected and (self.runtime.proxy or self.runtime.key):
            self.runtime.start_mitm()

    def close(self):
        try:
            self.runtime.close()
        finally:
            if self._temporary is not None:
                self._temporary.cleanup()
                self._temporary = None

    def temporary_image(self, name="capture.jpg"):
        if self._temporary is None:
            self._temporary = TemporaryDirectory(prefix="spider-")
        return Path(self._temporary.name) / name

    def screenshot_browser(self):
        from automation.pdd.spider.sogou import grab_window
        path = self.temporary_image()
        if not grab_window(self.runtime.page, path):
            raise RuntimeError("浏览器内容截图失败")
        self.image = path

    def fail_item(self, item, name, error):
        logging.getLogger(__name__).error("步骤 %s 失败: %s", name, error)

    @step("启动或复用 mitm", phases=("before",), provides=("mitm",), root_only=True)
    def start_mitm(self):
        self.runtime.start_mitm()

    @step("停止本次启动的 mitm", phases=("after",), invalidates=("mitm",), root_only=True)
    def stop_mitm(self):
        self.runtime.stop_mitm()


class BrowserCollector(Collector):
    device = "sogou"

    @step("使用现有用户数据打开或复用浏览器", phases=("before",), provides=("browser",), root_only=True)
    def open_browser(self):
        self.runtime.open_browser()

    @step("连接已启动的浏览器", phases=("before",), provides=("browser",), root_only=True)
    def connect_browser(self):
        from automation.pdd.spider.sogou import cdp_ready
        if not cdp_ready(config.DEBUG_PORT, timeout=0.5):
            raise RuntimeError("浏览器调试端口未就绪，请使用 open_browser")
        self.runtime.open_browser()

    @step("断开浏览器并关闭本次启动的浏览器", phases=("after",), invalidates=("browser",), root_only=True)
    def close_browser(self):
        self.runtime.close_browser()


class IosCollector(Collector):
    device = "ios"

    @property
    def client(self):
        return self.runtime.ensure_wda()

    @step("检查并复用或启动隧道", phases=("before",))
    def ensure_tunnel(self):
        self.runtime.ensure_tunnel()

    @step("检查并复用已挂载镜像，缺失时自动挂载匹配的开发者镜像", phases=("before",))
    def ensure_image(self):
        self.runtime.ensure_image()

    @step("检查并复用或启动设备上的 WDA 驱动", phases=("before",))
    def ensure_wda(self):
        self.runtime.ensure_driver()

    @step("检查并复用或启动 WDA 端口转发", phases=("before",))
    def ensure_forward(self):
        self.runtime.ensure_forward()


class IosElementRanking(IosCollector):
    mode = "element"
    collect = "ranking"

    def __init__(self, runtime):
        super().__init__(runtime)
        self._in_product = False
        self.center = None
        self.tabs = []
        self._rank_source = None
        self._banners = {}
        self._banner_sources = {}

    @step("获取当前子榜数据，写入榜单数据.xlsx 并初始化商品列表", provides=("items", "ranking_data"),
          item_conditions=("rank_item", "rank_context"), phases=("before", "loop"))
    def list_rank_data(self, item=None, *, persist=True):
        from automation.pdd.spider import ios
        from automation.pdd.spider.rank import TAB_TO_CLASS
        from automation.pdd.spider.utils import sanitize, read_xlsx, write_xlsx
        xml = self._rank_source or ios._current_page_source(self.client)
        self._rank_source = None
        elements = ios._xcui_texts(xml) if xml else []
        tab = ios._guess_tab(elements)
        headings = [e for e in elements if e["type"] in ("NavigationBar", "StaticText")
                    and e["text"].endswith(("畅销榜", "好评榜", "降价榜"))
                    and e["text"] not in TAB_TO_CLASS]
        if tab not in TAB_TO_CLASS or not headings:
            raise RuntimeError("请先手动打开拼多多 App 榜单页面")
        heading = min(headings, key=lambda e: (e["type"] != "NavigationBar", len(e["text"])))
        main = re.sub(r"(?:畅销榜|好评榜|降价榜)$", "", heading["text"]).strip()
        if not main:
            raise RuntimeError("未识别到主榜单名称")
        rows = ios._parse_rows(elements, TAB_TO_CLASS[tab])
        if not rows:
            raise RuntimeError("当前子榜未解析出商品")
        directory = config.OUTPUT_ROOT / sanitize(main) / sanitize(tab) / date.today().isoformat()
        table = directory / "榜单数据.xlsx"
        if persist and table.exists():
            header, values = read_xlsx(table)
            saved = {str(r.get("排行")): r for r in (dict(zip(header, value)) for value in values)}
            for row in rows:
                previous = saved.get(str(row["排行"]), {})
                if previous.get("标题") == row.get("标题"):
                    for key in ("产品ID", "产品链接", "上架日期"):
                        if previous.get(key):
                            row[key] = previous[key]
        self.tabs = [name for name in TAB_TO_CLASS if any(e["text"] == name for e in elements)]
        if item is not None:
            item.name, item.tab = main, tab
            item.directory, item.header, item.rows = directory, ios._ios_rank_header(TAB_TO_CLASS[tab]), rows
        header = ios._ios_rank_header(TAB_TO_CLASS[tab])
        if persist:
            write_xlsx(table, header, [[row.get(key, "") for key in header] for row in rows])
        has_sub = any(e["text"] in TAB_TO_CLASS for e in elements)
        items = [RankingItem(main, tab=tab, has_sub=has_sub, directory=directory,
            header=header, rows=rows, row=row) for row in rows]
        self.workflow.set_items(items)
        return items

    @step("滚动到当前榜单顶部")
    def scroll_top(self, item=None):
        from automation.pdd.spider import ios
        first = self.workflow.items[0].row["标题"] if self.workflow.items and self.workflow.items[0].row else None
        self._rank_source = ios._scroll_to_top(self.client, first, initial_xml=self._rank_source)

    @step("获取当前主榜的子榜列表", provides=("items", "tabs"),
          item_conditions=("tab_item", "rank_context"), phases=("before", "loop"))
    def list_tabs(self, item=None):
        from automation.pdd.spider import ios
        from automation.pdd.spider.rank import TAB_TO_CLASS
        xml = ios._current_page_source(self.client)
        self._rank_source = xml
        elements = ios._xcui_texts(xml) if xml else []
        self.tabs = [name for name in TAB_TO_CLASS if any(e["text"] == name for e in elements)]
        if not self.tabs:
            current = ios._guess_tab(elements)
            if current in TAB_TO_CLASS:
                self.tabs = [current]
        if not self.tabs:
            raise RuntimeError("未识别到子榜，请先手动进入 App 榜单页面")
        has_sub = any(e["text"] in TAB_TO_CLASS for e in elements)
        items = [RankingItem("", tab=name, has_sub=has_sub) for name in self.tabs]
        self.workflow.set_items(items)
        return items

    @step("切换当前子榜或当前商品所属子榜", requires=("item", "rank_context"), phases=("loop",))
    def switch_tab(self, item=None):
        from automation.pdd.spider import ios
        if not item.has_sub:
            xml = self._rank_source or ios._current_page_source(self.client)
            elements = ios._xcui_texts(xml) if xml else []
            if ios._guess_tab(elements) != item.tab:
                raise RuntimeError("当前榜单与待采集榜单不一致，请重新读取榜单列表")
            return
        self._rank_source = None
        ios._switch_rank_tab(self.client, item.tab)

    @step("定位当前商品并滚动居中", requires=("item", "rank_item"), provides=("centered",), phases=("loop",))
    def scroll_center(self, item=None):
        from automation.pdd.spider import ios
        self._rank_source = None
        options = {}
        if "get_crop_image" in self._selected:
            from PIL import Image
            from automation.pdd.spider.crop import Banner, logical_scale
            scope = item.directory.resolve()
            reference_path = self._banner_sources.get(scope)
            if reference_path is None:
                reference = self.client.screenshot()
                banner = Banner.from_image(reference)
                _, height = ios._get_window_size(self.client)
                scale = reference.height / height
            else:
                metadata = json.loads(reference_path.with_suffix(".meta.json").read_text(encoding="utf-8"))
                with Image.open(reference_path) as reference:
                    if scope not in self._banners:
                        self._banners[scope] = Banner.from_image(reference)
                    banner = self._banners[scope]
                    _, scale, _ = logical_scale(metadata["elements"], reference.size)
            options["safe_top"] = banner.height / scale + 8
        self.center = ios._locate_card_centered(self.client, item.row["标题"], **options)
        if not self.center:
            raise RuntimeError(f"未定位到商品: {item.row['标题']}")

    @step("截取当前屏幕并保存排行截图.jpg", provides=("image",))
    def get_screenshot(self, item=None):
        from automation.pdd.spider.artifacts import write_json
        from automation.pdd.spider import ios
        from automation.pdd.spider.crop import elements_from_xml, logical_scale
        crop_selected = "get_crop_image" in self._selected
        elements = None
        if crop_selected:
            source = ios._current_page_source(self.client)
            if not source:
                raise RuntimeError("读取截图配套元素快照失败")
            elements = elements_from_xml(source)
        if item is None or item.directory is None:
            self._shot += 1
            directory = config.OUTPUT_ROOT / "screenshots" / self._session
            filename = f"{self._shot:06d}.jpg"
        else:
            directory = item.image_directory
            filename = "榜单截图.jpg" if item.row is None else "排行截图.jpg"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / filename
        captured = self.client.screenshot().convert("RGB")
        if elements is not None:
            logical_scale(elements, captured.size)
        captured.save(path, quality=95)
        if elements is not None:
            write_json(path.with_suffix(".meta.json"), {
                "image_size": list(captured.size), "elements": elements,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "title": item.row.get("标题", "") if item is not None and item.row else "",
            })
            if item is not None and item.row is None:
                scope = item.directory.resolve()
                self._banner_sources[scope] = path
                self._banners.pop(scope, None)
        self.image = path

    @step("复用屏幕截图，合并横幅与半宽高度加 10px 的卡片，保存产品截图.jpg",
          requires=("item", "rank_item", "image"), phases=("loop",))
    def get_crop_image(self, item=None):
        from PIL import Image
        from automation.pdd.spider.artifacts import write_json
        from automation.pdd.spider.crop import Banner, compose_product
        from automation.pdd.spider.status import update_stage
        if self.image is None:
            raise ValueError("缺少已保存的屏幕截图")
        metadata_path = self.image.with_suffix(".meta.json")
        if not metadata_path.is_file():
            raise ValueError("截图缺少配套元素快照，请先执行 get_screenshot")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("sha256") != hashlib.sha256(self.image.read_bytes()).hexdigest():
            raise ValueError("截图与配套元素快照不匹配，请重新执行 get_screenshot")
        title = item.row["标题"]
        if metadata.get("title") and metadata["title"] != title:
            raise ValueError("截图不属于当前商品，请重新执行 get_screenshot")
        scope = item.directory.resolve()
        with Image.open(self.image) as raw:
            image = raw.convert("RGB")
        if list(image.size) != metadata.get("image_size"):
            raise ValueError("截图尺寸与元素快照不一致")
        if scope not in self._banners:
            reference_path = self._banner_sources.get(scope, self.image)
            with Image.open(reference_path) as reference:
                self._banners[scope] = Banner.from_image(reference)
        result, report = compose_product(image, metadata["elements"], title, self._banners[scope])
        from automation.pdd.spider.crop import save_product
        output = item.image_directory / "产品截图.jpg"
        save_product(result, output)
        write_json(item.image_directory / "产品截图.meta.json", report)
        update_stage(item.image_directory, "rank_crop", "completed", file=output.name)

    @step("打开居中的商品", requires=("item", "rank_item", "centered"), provides=("product_page",), phases=("loop",))
    def open_product(self, item=None):
        from automation.pdd.spider import ios
        if not ios._tap_xy(self.client, *self.center, "商品卡片"):
            raise RuntimeError("点击商品卡片失败")
        self._in_product = True
        time.sleep(3)

    @step("获取分享链接并保存分享链接.json", requires=("item", "product_page"), provides=("product_link",), phases=("loop",))
    def get_share_link(self, item=None):
        from automation.pdd.spider import ios
        from automation.pdd.spider.artifacts import write_share_link
        from automation.pdd.spider.status import update_stage, reconcile_product, reconcile_subrank, reconcile_board
        link = ios._copy_product_link(self.client)
        if not link:
            raise RuntimeError("未获取到分享链接，请检查手机粘贴权限")
        item.row.update(产品链接=link, 产品ID=ios._extract_goods_id(link))
        write_share_link(item.image_directory, item.row)
        update_stage(item.image_directory, "rank", "completed", data=dict(item.row))
        reconcile_product(item.image_directory, item.row)
        reconcile_subrank(item.directory)
        reconcile_board(item.directory.parent.parent)

    @step("返回榜单页面", requires=("product_page",), invalidates=("product_page",), phases=("loop",))
    def back_rank(self, item=None):
        from automation.pdd.spider import ios
        self._rank_source = None
        if not (ios._tap_label(self.client, "返回") or ios._tap_label(self.client, "back")):
            if not ios._tap_xy(self.client, 26, 68, "返回榜单"):
                raise RuntimeError("返回榜单失败")
        self._in_product = False
        time.sleep(1.5)


    def finish_item(self, item):
        self._rank_source = None
        if self._in_product:
            self.back_rank(item)
        self.center = None


class BrowserProduct(BrowserCollector):
    mode = "element"
    collect = "product"

    def __init__(self, runtime):
        super().__init__(runtime)
        self.product_page = None
        self.link = None
        self.current_item = None

    def check_inputs(self):
        from automation.pdd.spider.artifacts import read_products
        tables = list(config.OUTPUT_ROOT.rglob("榜单数据.xlsx"))
        if not tables:
            raise ValueError("未找到榜单数据.xlsx，请先采集榜单")
        has_links = False
        for table in tables:
            has_links |= any(row["产品链接"] for row in read_products(table))
        if not has_links:
            raise ValueError("未找到可用的分享链接.json，请先执行 get_share_link")
        from automation.pdd.spider.schedule import source_rows
        source_rows(config.EXCEL_PATH)

    @step("从已有榜单 Excel 初始化商品列表", provides=("items",),
          item_conditions=("product_item",), phases=("before", "loop"))
    def get_data(self, item=None):
        from automation.pdd.spider.schedule import ordered_product_tables
        from automation.pdd.spider.artifacts import read_products
        items = []
        for table in ordered_product_tables(config.OUTPUT_ROOT, config.EXCEL_PATH):
            rows = read_products(table)
            targets = rows if config.LIMIT is None else rows[:config.LIMIT]
            items.extend(ProductItem(table, row) for row in targets if row.get("产品链接"))
        self.workflow.set_items(items)

    @step("从分享链接.json 读取当前商品链接", requires=("item", "product_item"), provides=("product_link",), phases=("loop",))
    def get_jump_link(self, item=None):
        from automation.pdd.spider.artifacts import read_share_link
        share = read_share_link(item.directory, str(item.row.get("标题") or ""))
        if not share:
            raise RuntimeError("当前商品缺少分享链接.json")
        self.link = share["share_link"]
        item.row["产品链接"] = self.link
        if share.get("goods_id"):
            item.row["产品ID"] = share["goods_id"]
        self.current_item = item

    @step("打开产品页", requires=("item", "browser", "product_link"), provides=("product_page",), phases=("loop",))
    def open_product(self, item=None):
        from automation.pdd.spider.product_data import open_product_page, is_sold_out, mark_sold_out
        from automation.pdd.spider.sogou import freeze_page
        from automation.pdd.spider.status import update_stage
        item.directory.mkdir(parents=True, exist_ok=True)
        self.product_page = open_product_page(self.runtime.page.context, self.link)
        self.current_item = item
        if is_sold_out(self.product_page):
            mark_sold_out(item.directory, self.link, self.product_page.url)
            raise RuntimeError("商品已售罄，已记录状态并继续下一个")
        if "goods" not in self.product_page.url:
            raise RuntimeError("未跳转到产品页，疑似风控")
        freeze_page(self.product_page)
        self.product_page.wait_for_timeout(3000)
        update_stage(item.directory, "access", "completed")
        update_stage(item.directory, "product", "running", sold_out_attempts=0,
                     retryable=False, outcome="available")

    @step("截取产品页", requires=("item", "product_page"), provides=("image",), phases=("loop",))
    def screenshot(self, item=None):
        from automation.pdd.spider.sogou import grab_window
        path = self.temporary_image()
        if not grab_window(self.product_page, path):
            raise RuntimeError("产品页截图失败")
        self.image = path

    @step("按现有宽度裁剪产品截图", requires=("image", "product_page"), phases=("loop",))
    def crop(self, item=None):
        from automation.pdd.spider.sogou import crop_width_center, find_sogou_hwnd, get_scrollbar_width
        hwnd = find_sogou_hwnd()
        scrollbar = get_scrollbar_width(self.product_page, hwnd) if hwnd else 0
        crop_width_center(self.image, config.PRODUCT_CROP_WIDTH, scrollbar=scrollbar)

    @step("保存产品截图", requires=("item", "image"), phases=("loop",))
    def save_image(self, item=None):
        from automation.pdd.spider.status import update_stage
        item.directory.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.image, item.directory / "产品页截图.jpg")
        update_stage(item.directory, "screenshot", "completed")

    @step("获取主图并保存主图目录", requires=("item", "product_page"), phases=("loop",))
    def get_main_images(self, item=None):
        from automation.pdd.spider.product_data import _scrape_main_images
        from automation.pdd.spider.status import update_stage
        _scrape_main_images(self.product_page, item.directory, collect_date=False)
        update_stage(item.directory, "main_images", "completed",
                     count=len(list((item.directory / "主图").glob("主图_*.jpg"))))

    @step("获取首图 URL 中的上架日期并保存上架时间.json", requires=("item", "product_page"), phases=("loop",))
    def get_shelves_time(self, item=None):
        from automation.pdd.spider.artifacts import write_json
        from automation.pdd.spider.product_data import listing_date_from_image_url
        from automation.pdd.spider.status import update_stage
        image = self.product_page.locator(config.MAIN_IMG_XPATH.format(1))
        source = image.first.get_attribute("src") if image.count() else ""
        value = listing_date_from_image_url(source or "")
        if not value:
            raise RuntimeError("首张主图 URL 未包含有效日期，无法获取上架时间")
        write_json(item.directory / "上架时间.json", {"shelves_time": value, "precision": "date"})
        update_stage(item.directory, "shelves_time", "completed", value=value, precision="date")

    @step("获取 SKU", requires=("item", "product_page"), phases=("loop",))
    def get_sku(self, item=None):
        from automation.pdd.spider.product_data import _scrape_sku
        from automation.pdd.spider.status import update_stage
        found = _scrape_sku(self.product_page, item.directory, item.row)
        update_stage(item.directory, "sku", "completed" if found else "skipped",
                     "" if found else "未找到可枚举的 SKU")

    @step("获取详情图", requires=("item", "product_page"), phases=("loop",))
    def get_details(self, item=None):
        from automation.pdd.spider.product_data import _scrape_detail_images
        from automation.pdd.spider.status import update_stage
        found = _scrape_detail_images(self.product_page, item.directory)
        update_stage(item.directory, "details", "completed" if found else "skipped",
                     "" if found else "页面未提供详情图")

    @step("获取普通评论", requires=("item", "product_page"), phases=("loop",))
    def get_comments(self, item=None):
        from automation.pdd.spider.comments import scrape_comments
        scrape_comments(self.product_page, item.directory, normal=True, negative=False, force=True)

    @step("获取灰色标签差评", requires=("item", "product_page"), phases=("loop",))
    def get_negative_comments(self, item=None):
        from automation.pdd.spider.comments import scrape_comments
        scrape_comments(self.product_page, item.directory, normal=False, negative=True, force=True)

    @step("保存上架日期并汇总状态", requires=("items",))
    def save(self, item=None):
        from automation.pdd.spider.status import reconcile_product, reconcile_board
        from automation.pdd.spider.utils import refresh_listing_dates
        targets = [item] if item is not None else (self.workflow.items or [])
        tables = set()
        for target in targets:
            reconcile_product(target.directory, target.row)
            tables.add(target.table)
        for table in tables:
            refresh_listing_dates(table)
            reconcile_board(table.parent.parent.parent)

    @step("关闭当前产品页", requires=("product_page",), invalidates=("product_page",), phases=("loop",))
    def close_product(self, item=None):
        if self.product_page is not None:
            try:
                self.product_page.close()
            finally:
                self.product_page = None


    def fail_item(self, item, name, error):
        super().fail_item(item, name, error)
        if item is not None:
            from automation.pdd.spider.status import update_stage
            stages = {"get_main_images": "main_images", "get_sku": "sku", "get_details": "details",
                      "get_shelves_time": "shelves_time",
                      "get_comments": "comments", "get_negative_comments": "negative_comments",
                      "screenshot": "screenshot", "save_image": "screenshot"}
            if name in stages:
                update_stage(item.directory, stages[name], "failed", str(error))

    def finish_item(self, item):
        try:
            self.close_product(item)
        finally:
            self.image = self.link = self.current_item = None
            if item is not None:
                from automation.pdd.spider.status import reconcile_product, reconcile_board
                reconcile_product(item.directory, item.row)
                reconcile_board(item.table.parent.parent.parent)

    def close(self):
        try:
            self.close_product()
        finally:
            super().close()


class BrowserRanking(BrowserCollector):
    mode = "element"
    collect = "ranking"

    def check_inputs(self):
        from automation.pdd.spider.rank import read_leaderboards
        if not config.EXCEL_PATH.is_file():
            raise ValueError(f"榜单来源文件不存在: {config.EXCEL_PATH}")
        if not read_leaderboards(config.EXCEL_PATH):
            raise ValueError(f"来源表没有有效榜单名称和链接: {config.EXCEL_PATH}")

    @step("读取来源 Excel，初始化主榜列表", provides=("items",),
          item_conditions=("browser_rank_item",), phases=("before", "loop"))
    def list_boards(self, item=None):
        from automation.pdd.spider.rank import read_leaderboards
        self.workflow.set_items([RankingItem(source['name'], url=source['url'], main_name=source['main_name'],
                                            tab=source['tab'], index=source['tab_index'])
                                 for source in read_leaderboards(config.EXCEL_PATH) if source['tab']])

    @step("读取来源并发现各子榜，初始化子榜列表", requires=("browser",),
          provides=("items", "tabs"), item_conditions=("browser_rank_item",), phases=("before", "loop"))
    def get_data(self, item=None):
        from automation.pdd.spider.rank import read_leaderboards
        items = []
        for source in read_leaderboards(config.EXCEL_PATH):
            if not source['tab']:
                continue
            self.runtime.page.goto(source["url"], wait_until="domcontentloaded")
            tabs = self._read_tabs()
            if source['tab'] not in tabs:
                raise RuntimeError(f"{source['name']} 未找到榜单 tab，疑似风控")
            items.append(RankingItem(source['name'], url=source['url'], tab=source['tab'],
                                     main_name=source['main_name'], index=source['tab_index'], has_sub=len(tabs) > 1))
        self.workflow.set_items(items)

    def _read_tabs(self):
        from automation.pdd.spider.rank import get_tabs
        for _ in range(10):
            tabs = get_tabs(self.runtime.page)
            if tabs:
                return tabs
            self.runtime.page.wait_for_timeout(1000)
        return []

    @step("打开当前榜单", requires=("browser", "item", "browser_rank_item"), provides=("ranking_page",), phases=("loop",))
    def open_ranking(self, item=None):
        self.runtime.page.goto(item.url, wait_until="domcontentloaded")

    @step("获取页面子榜并初始化子榜列表", requires=("browser",), provides=("tabs", "items"),
          item_conditions=("browser_rank_item",), phases=("before", "loop"))
    def list_tabs(self, item=None):
        from automation.pdd.spider.rank import get_leaderboard_name
        self.tabs = self._read_tabs()
        if not self.tabs:
            raise RuntimeError("未找到子榜 tab，疑似风控")
        if item is not None and item.main_name:
            if item.tab not in self.tabs:
                raise RuntimeError('页面子榜与 Excel D 列不对应')
            item.has_sub = len(self.tabs) > 1
            self.workflow.set_items([item])
            return [item]
        name = item.name if item is not None else get_leaderboard_name(self.runtime.page) or "当前榜单"
        url = item.url if item is not None else self.runtime.page.url
        items = [RankingItem(name, url=url, tab=tab, index=index,
                                           has_sub=len(self.tabs) > 1)
                                 for index, tab in enumerate(self.tabs)]
        self.workflow.set_items(items)
        return items

    @step("切换当前子榜", requires=("item", "browser", "ranking_page"), phases=("loop",))
    def switch_tab(self, item=None):
        from automation.pdd.spider.rank import set_active_tab
        self.runtime.page.goto(set_active_tab(item.url, item.index), wait_until="domcontentloaded")

    def _leaderboard(self, item):
        from automation.pdd.spider.rank import TAB_TO_CLASS, set_active_tab
        mapping = TAB_TO_CLASS
        if item.tab not in mapping:
            raise RuntimeError(f"未知榜单类型: {item.tab}")
        return mapping[item.tab](set_active_tab(item.url, item.index))

    def _collect_rows(self, item):
        from automation.pdd.spider.rank import get_leaderboard_name
        from automation.pdd.spider.utils import sanitize
        leaderboard = self._leaderboard(item)
        item.rows = leaderboard.scrape(self.runtime.page)
        if not item.rows:
            raise RuntimeError(f"{item.tab} 未获取到商品数据")
        main = (get_leaderboard_name(self.runtime.page) or '').removesuffix(item.tab).strip()
        if item.main_name and main != item.main_name:
            raise RuntimeError('页面主榜与 Excel C 列不对应，未保存商品')
        item.directory = config.OUTPUT_ROOT / sanitize(main or item.name) / sanitize(item.tab) / date.today().isoformat()
        item.header = leaderboard.header
        self.workflow.set_items([RankingItem(item.name, url=item.url, tab=item.tab,
            index=item.index, has_sub=item.has_sub, directory=item.directory,
            header=item.header, rows=item.rows, row=row) for row in item.rows])

    @step("解析当前子榜全部商品，写入 Excel 并初始化商品列表", requires=("item", "browser", "ranking_page"),
          provides=("ranking_rows", "items"), item_conditions=("browser_rank_item", "ranking_rows"), phases=("loop",))
    def list_products(self, item=None):
        self._collect_rows(item)
        self._save_data(item)

    @step("将榜单商品链接保存到各商品的分享链接.json", requires=("item", "ranking_rows"), phases=("loop",))
    def get_share_link(self, item=None):
        _write_rank_item_links(item)

    @step("逐商品对齐卡片并保存排行截图.jpg", requires=("item", "ranking_rows", "browser"),
          provides=("images",), phases=("loop",))
    def screenshot(self, item=None):
        from automation.pdd.spider.sogou import scroll_card_to_top, grab_window
        leaderboard = self._leaderboard(item)
        self.images = []
        rows = [item.row] if item.row is not None else item.rows
        for row in rows:
            if not scroll_card_to_top(self.runtime.page, row["产品ID"], leaderboard.ref_selector):
                raise RuntimeError(f"未找到排行 {row['排行']} 卡片")
            self.runtime.page.wait_for_timeout(800)
            directory = item.directory / f"榜{int(row['排行']):02d}"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "排行截图.jpg"
            if not grab_window(self.runtime.page, path):
                raise RuntimeError(f"排行 {row['排行']} 截图失败")
            self.images.append((row, path))

    @step("按榜单规则裁剪已保存截图，保存产品截图.jpg", requires=("item", "images", "browser"), phases=("loop",))
    def get_crop_image(self, item=None):
        from automation.pdd.spider.sogou import crop_center_top, find_sogou_hwnd, get_scrollbar_width
        hwnd = find_sogou_hwnd()
        scrollbar = get_scrollbar_width(self.runtime.page, hwnd) if hwnd else 0
        for row, path in self.images:
            directory = item.directory / f"榜{int(row['排行']):02d}"
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / "产品截图.jpg"
            shutil.copy2(path, target)
            crop_center_top(target, *self._leaderboard(item).crop, scrollbar=scrollbar)

    def _save_data(self, item=None):
        from automation.pdd.spider.utils import write_xlsx
        targets = [item] if item is not None else (self.workflow.items or [])
        for target in targets:
            if target.rows and target.directory is not None:
                write_xlsx(target.directory / "榜单数据.xlsx", target.header,
                           [[row.get(key, "") for key in target.header] for row in target.rows])
            elif item is not None:
                raise RuntimeError("当前子榜尚未取得商品数据")


    def finish_item(self, item):
        self.images = []


COLLECTORS = {
    ("ios", "element", "ranking"): IosElementRanking,
    ("sogou", "element", "ranking"): BrowserRanking,
    ("sogou", "element", "product"): BrowserProduct,
}


def create_collector(device, mode, collect, runtime):
    cls = COLLECTORS.get((device, mode, collect))
    if cls is None:
        raise ValueError(f"暂不支持 device={device}, mode={mode}, collect={collect}")
    return cls(runtime).set_device(device)
