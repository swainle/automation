"""Device adapters reuse the existing element parsing and image algorithms."""
from pathlib import Path

from automation.pdd.spider import config
from automation.pdd.spider.collectors import IosElementRanking, RankingItem
from automation.pdd.spider.utils import sanitize


def directory(board, tab, day):
    return config.OUTPUT_ROOT / sanitize(board.get('main_name') or board['name']) / sanitize(tab) / day


def target(board, entry, day):
    return RankingItem(board['name'], url=board['url'], tab=entry['tab'], index=entry['tab_index'],
                       has_sub=bool(entry['has_sub']), directory=Path(entry['directory']) if entry['directory'] else directory(board, entry['tab'], day))


class Sogou:
    """Ranking browser adapter: all page access goes through the extension."""
    name = 'sogou'

    def __init__(self, runtime):
        self.runtime = runtime

    def navigate(self, url):
        self.runtime.start_ranking_browser()
        self.runtime.ranking_request('ranking_navigate', {'tab_id': self.runtime.ranking_tab, 'url': url})

    def snapshot(self):
        return self.runtime.ranking_request('ranking_read', {'tab_id': self.runtime.ranking_tab, 'load': True})

    def discover(self, board):
        from automation.pdd.spider.rank import TAB_TO_CLASS
        self.navigate(board['url'])
        data = self.snapshot()
        tabs = data.get('tabs') or []
        if not tabs:
            tabs = [name for name, cls in TAB_TO_CLASS.items()
                    if data.get('heading', '').endswith(name) or
                    any(cls.marker in card.get('text', '') for card in data.get('cards', []))]
        items = [RankingItem(board['name'], url=board['url'], tab=name, index=index, has_sub=len(tabs) > 1)
                 for index, name in enumerate(tabs) if name in TAB_TO_CLASS]
        if not items:
            raise RuntimeError('插件未读到有效子榜或商品元素')
        return items

    def read(self, board, entry, day):
        from automation.pdd.spider.rank import TAB_TO_CLASS, set_active_tab
        self.navigate(set_active_tab(board['url'], entry['tab_index']))
        data = self.snapshot()
        leaderboard = TAB_TO_CLASS[entry['tab']](board['url'])
        rows = leaderboard.parse_cards(data.get('cards', []))
        if not rows:
            raise RuntimeError('插件未读取到当前子榜的商品元素')
        main = data.get('heading', '').removesuffix(entry['tab']).removesuffix('排行榜').strip()
        if board.get('main_name') and main != board['main_name']:
            raise RuntimeError('页面主榜或子榜与 Excel C/D 不对应，未保存商品')
        for row in rows:
            row.update({'主榜单名称': main, '子榜单名称': entry['tab'], '榜单统计说明': data.get('description', '')})
        return leaderboard.header, rows, directory(board, entry['tab'], day)

    def prepare(self, board, entry, day):
        from automation.pdd.spider.rank import set_active_tab
        self.current = target(board, entry, day)
        self.navigate(set_active_tab(board['url'], entry['tab_index']))
        self.snapshot()

    def screenshot(self, row):
        from automation.com.extension.client import save_jpeg
        result = self.runtime.ranking_request('ranking_screenshot',
            {'tab_id': self.runtime.ranking_tab, 'goods_id': str(row['产品ID'])})
        path = self.current.directory / f"榜{int(row['排行']):02d}/排行截图.jpg"
        save_jpeg(result, path)

    def crop(self, row):
        from PIL import Image
        from automation.pdd.spider.rank import TAB_TO_CLASS
        folder = self.current.directory / f"榜{int(row['排行']):02d}"
        base_width, base_height = TAB_TO_CLASS[self.current.tab](self.current.url).crop
        with Image.open(folder / '排行截图.jpg') as image:
            # The capture is independent of the 50% device preview. Also accept
            # browsers returning scaled images, preserving the original crop ratio.
            height = round(base_height * image.width / base_width)
            if image.height < height:
                raise RuntimeError('移动视口高度不足，无法按榜单规则裁剪')
            cropped = image.convert('RGB').crop((0, 0, image.width, height))
        output = folder / '产品截图.jpg'
        temporary = output.with_suffix('.tmp')
        try:
            cropped.save(temporary, format='JPEG', quality=95)
            temporary.replace(output)
        finally:
            cropped.close()
            temporary.unlink(missing_ok=True)

    def link(self, row):
        if not row.get('产品链接'):
            raise RuntimeError('网页榜单未提供商品链接')
        return row

    def finish_item(self):
        pass

    def close(self):
        self.runtime.close()


class Ios:
    name = 'ios'

    def __init__(self, runtime):
        self.runtime = runtime
        self.collector = IosElementRanking(runtime)
        self.collector._selected = {'get_crop_image'}

    def open(self, board):
        from automation.pdd.spider.navigation import open_app_board
        open_app_board(self.runtime.ensure_wda(), board)

    def discover(self, board):
        self.open(board)
        items = self.collector.list_tabs()
        for index, item in enumerate(items):
            item.index = index
        return items

    def read(self, board, entry, day):
        self.open(board)
        item = target(board, entry, day)
        self.collector.switch_tab(item)
        self.collector.scroll_top()
        self.collector.list_rank_data(item, persist=False)
        if board.get('main_name') and (item.name != board['main_name'] or item.tab != entry['tab']):
            raise RuntimeError('页面主榜或子榜与 Excel C/D 不对应，未保存商品')
        return item.header, item.rows, directory(board, entry['tab'], day)

    def prepare(self, board, entry, day):
        self.open(board)
        self.current = target(board, entry, day)
        self.collector.switch_tab(self.current)
        self.collector.scroll_top()
        self.collector.get_screenshot(self.current)

    def screenshot(self, row):
        self.current.row = row
        self.collector.scroll_center(self.current)
        self.collector.get_screenshot(self.current)

    def crop(self, row):
        self.current.row = row
        self.collector.image = self.current.image_directory / '排行截图.jpg'
        self.collector.get_crop_image(self.current)

    def link(self, row):
        from automation.pdd.spider import ios
        self.current.row = row
        self.collector.scroll_center(self.current)
        try:
            self.collector.open_product(self.current)
            link = ios._copy_product_link(self.collector.client)
            if not link:
                raise RuntimeError('未获取到商品分享链接')
            return {**row, '产品链接': link, '产品ID': ios._extract_goods_id(link)}
        finally:
            if self.collector._in_product:
                self.collector.back_rank(self.current)

    def finish_item(self):
        self.collector.finish_item(self.current)

    def close(self):
        self.collector.close()
