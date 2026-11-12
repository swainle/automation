"""Collect native iOS product screens, reusing one XML snapshot per page change."""
import hashlib
import itertools
import json
import math
import re
import sys
import time
from datetime import date
from pathlib import Path
import xml.etree.ElementTree as ET

from automation.pdd.spider import ios
from automation.pdd.spider.artifacts import write_json
from automation.pdd.spider.native_product import review_filters
from automation.pdd.spider.utils import write_csv


def label(node):
    return (node.get('label') or node.get('name') or node.get('value') or '').strip()


def rect(node):
    x, y, w, h = (float(node.get(k, 0)) for k in ('x', 'y', 'width', 'height'))
    return x, y, x + w, y + h


def visible(node):
    return node.get('visible') == 'true'


# 商品页「详情」与「评价」入口在不同页面/版本文案不一致。集中维护，
# 新增页面样式时只需在此追加文案，无需改动采集流程。
DETAIL_ENTRIES = (r'^详情$', r'^图文详情$', r'^商品详情$', r'^详情页$', r'^产品详情$')
REVIEW_ENTRIES = (r'^评价$', r'^商品评价$', r'^用户评价$', r'^全部评价$')
REVIEW_HEADER_PATTERN = r'商品评价|用户评价|暂无评价'
REVIEW_MORE_ENTRIES = (r'^查看全部$', r'^查看全部评价$', r'^全部评价$', r'^更多评价$', r'^查看全部评论$')


def sku_groups(root, known=None):
    """Only SKU collection headers/cells; purchase/payment buttons are excluded."""
    result = {}
    lookup = {name: group for group, options in (known or {}).items() for name in options}
    for collection in root.iter('XCUIElementTypeCollectionView'):
        group = next((lookup[label(n)] for n in collection.iter('XCUIElementTypeButton') if label(n) in lookup), None)
        for child in collection:
            if 'Header' in child.get('traits', ''):
                group = next((label(n) for n in child.iter('XCUIElementTypeStaticText') if label(n)), None)
            elif group and child.tag.endswith('Cell'):
                for button in child.iter('XCUIElementTypeButton'):
                    if label(button):
                        result.setdefault(group, {})[label(button)] = button
    return result


def review_rows(root, *, controls=False):
    """iOS review header and content occupy adjacent collection cells."""
    rows = []
    for collection in root.iter('XCUIElementTypeCollectionView'):
        cells = list(collection)
        for i, cell in enumerate(cells):
            menu = next((n for n in cell.iter('XCUIElementTypeButton') if label(n) == '菜单'), None)
            if menu is None or (controls and not visible(menu)):
                continue
            header = [label(n) for n in cell.iter() if label(n) and not list(n) and n.tag.endswith(('StaticText', 'Other'))]
            body = []
            for following in cells[i + 1:]:
                if any(label(n) == '菜单' for n in following.iter('XCUIElementTypeButton')):
                    break
                body.extend(label(n) for n in following.iter('XCUIElementTypeOther')
                            if label(n) and not list(n) and not label(n).startswith(('垂直滚动条', '水平滚动条')))
            # Body content precedes interaction, recommendation and merchant replies.
            content = next((s for s in body if len(s) >= 4 and not re.search(r'：|猜你想看', s[:14])), '')
            if not content:
                continue
            specs = next((s for s in header + body if re.search(r'口味：|型号：|规格：|颜色：|尺码：', s)), '')
            row = {'name': header[0] if header else '', 'comment': content, 'specs': specs}
            row['ui_fingerprint'] = hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if controls:
                row['_menu'] = menu
            rows.append(row)
    return rows


def sku_summary(root, known):
    views = [rect(n) for n in root.iter('XCUIElementTypeCollectionView') if sku_groups(n, known)]
    if not views:
        raise RuntimeError('未确认 SKU 规格列表')
    top = min(v[1] for v in views)
    return ''.join(label(n) for n in root.iter('XCUIElementTypeOther') if visible(n) and rect(n)[3] <= top)


def gallery_sku(root, catalog, width):
    normalize = lambda value: re.sub(r'\s|\u200b', '', value).replace('（', '(').replace('）', ')').replace('｜', '|')
    candidates = []
    for node in root.iter('XCUIElementTypeOther'):
        x1, _, x2, _ = rect(node)
        if not visible(node) or not width * .25 <= (x1 + x2) / 2 <= width * .75:
            continue
        text = normalize(label(node))
        matches = [[name for name in options if normalize(name) in text] for options in catalog.values()]
        if all(len(names) == 1 for names in matches):
            candidates.append(tuple(names[0] for names in matches))
    prices = {match[1] for node in root.iter('XCUIElementTypeStaticText') if visible(node)
              and (match := re.fullmatch(r'(?:券后\s*)?[¥￥]\s*(\d+(?:\.\d+)?)', label(node)))}
    if len(candidates) != 1 or len(prices) != 1:
        raise RuntimeError('SKU 图库未暴露唯一完整规格与确定价格，未保存猜测数据')
    return candidates[0], prices.pop()


def negative_filters(root, image):
    adapted, originals = ET.Element('hierarchy'), {}
    for node in root.iter():
        text = label(node)
        match = re.fullmatch(r'(.+?)[（(]([\d,万+.]+)[)）]', text)
        if not visible(node) or not (match or text in {'差评', '中差评', '负面评价'}):
            continue
        unknown_count = bool(match and not re.fullmatch(r'[\d,]+', match[2]))
        alias = match[1] + '(0)' if unknown_count else text
        originals[alias] = text, unknown_count
        x1, y1, x2, y2 = map(round, rect(node))
        ET.SubElement(adapted, 'node', {'package': 'com.xunmeng.pinduoduo', 'clickable': 'true',
                      'text': alias, 'bounds': f'[{x1},{y1}][{x2},{y2}]'})
    if not list(adapted):
        raise RuntimeError('评价筛选标签未加载，无法确认是否存在差评')
    result = []
    for tag in review_filters(adapted, image):
        if tag['kind'] == 'negative':
            tag['label'], unknown_count = originals[tag['label']]
            if unknown_count:
                tag['count'] = None
            result.append(tag)
    return result


class IosProduct:
    save_diagnostics = True

    def debug_json(self, name, value):
        if self.save_diagnostics:
            write_json(self.directory / name, value)

    def debug_xml(self, name):
        if self.save_diagnostics:
            (self.directory / name).write_text(self.xml or '', encoding='utf-8')

    def __init__(self, store, client=None):
        self.store, self.client = store, client
        self.source_reads = 0

    def snapshot(self):
        xml = ios._current_page_source(self.client)
        self.source_reads += 1
        if not xml:
            raise RuntimeError('iOS 商品页面元素树为空')
        self.xml, self.root = xml, ET.fromstring(xml)
        return self.root

    def tap(self, node):
        if not visible(node):
            element = self.client(name=node.get('name') or label(node), className=node.tag.replace('XCUIElementType', '')).get(timeout=5)
            element.scroll('visible')
            element.tap()
        else:
            x1, y1, x2, y2 = rect(node)
            self.client.click(round((x1 + x2) / 2), round((y1 + y2) / 2))
        time.sleep(.3)

    def find(self, pattern, *, kind=None):
        return next((n for n in self.root.iter() if visible(n) and (kind is None or n.tag.endswith(kind)) and re.search(pattern, label(n))), None)

    def find_entry(self, patterns, *, kind=None):
        """当前快照中匹配任一入口文案的可见元素（多页面文案兼容）。"""
        return next((n for n in self.root.iter()
                     if visible(n) and (kind is None or n.tag.endswith(kind))
                     and any(re.search(pattern, label(n)) for pattern in patterns)), None)

    def seek_entry(self, patterns, *, kind=None, pages=50):
        """先看当前快照，再向下滚动寻找任一入口文案；找不到时抛出统一错误。"""
        node = self.find_entry(patterns, kind=kind)
        if node is not None:
            return node
        previous = None
        for _ in range(pages):
            signature = [(label(n), n.get('y')) for n in self.root.iter() if visible(n) and label(n)]
            if signature == previous:
                break
            previous = signature
            self.swipe()
            node = self.find_entry(patterns, kind=kind)
            if node is not None:
                return node
        raise RuntimeError('未找到页面入口：' + ' / '.join(patterns))

    def close_panel(self):
        node = self.find(r'^关闭$', kind='Button')
        if node is None:
            raise RuntimeError('未找到面板关闭按钮，停止后续采集')
        self.tap(node)
        self.snapshot()

    def image(self, node, path):
        screenshot = self.client.screenshot().convert('RGB')
        app = next(self.root.iter('XCUIElementTypeApplication'))
        sx, sy = screenshot.width / float(app.get('width')), screenshot.height / float(app.get('height'))
        x1, y1, x2, y2 = rect(node)
        rectangle = (max(0, round(x1 * sx)), max(0, round(y1 * sy)), min(screenshot.width, round(x2 * sx)), min(screenshot.height, round(y2 * sy)))
        if rectangle[2] <= rectangle[0] or rectangle[3] <= rectangle[1]:
            raise RuntimeError('图片区域为空')
        path.parent.mkdir(parents=True, exist_ok=True)
        screenshot.crop(rectangle).save(path, quality=95)

    def record_image(self, kind, path, *, position=None):
        record = {'image_type': kind, 'position': position if position is not None else sum(r['image_type'] == kind for r in self.images) + 1,
                  'local_path': str(path.relative_to(self.directory)), 'state': 'completed',
                  'capture_method': 'ios_enlarged_screenshot_crop'}
        self.images.append(record)
        self.store.save_product_image(self.item['id'], record)

    def swipe(self, *, up=True, x=None, top=.24, bottom=.78):
        x = self.width / 2 if x is None else x
        a, b = (bottom, top) if up else (top, bottom)
        if not ios._swipe(self.client, x, self.height * a, x, self.height * b, duration=0, velocity=400):
            raise RuntimeError('iOS 页面滑动失败')
        time.sleep(.3)
        return self.snapshot()

    def viewer_counter(self):
        for node in self.root.iter('XCUIElementTypeStaticText'):
            match = re.fullmatch(r'(\d+)\s*/\s*(\d+)', label(node))
            if visible(node) and match:
                return int(match[1]), int(match[2])
        texts = [label(n) for n in self.root.iter('XCUIElementTypeStaticText') if visible(n) and label(n)]
        raise RuntimeError(f'未确认放大图片浏览器页码；可见文本：{texts[:60]}')

    def wait_viewer_page(self, previous, expected, total):
        for attempt in range(3):
            self.snapshot()
            position, current_total = self.viewer_counter()
            if (position, current_total) == (expected, total):
                return position
            if current_total != total or position != previous or attempt == 2:
                raise RuntimeError(f'大图页码未确认：预期 {expected}/{total}，实际 {position}/{current_total}')
            time.sleep(.3)

    def viewer_image(self):
        deadline = time.monotonic() + 60
        expected_page = None
        for attempt in range(12):
            images = [n for n in self.root.iter('XCUIElementTypeImage') if visible(n)
                      and rect(n)[0] < self.width and rect(n)[2] > 0
                      and rect(n)[1] < self.height and rect(n)[3] > 0
                      and (rect(n)[2] - rect(n)[0]) * (rect(n)[3] - rect(n)[1]) > 4096]
            if images:
                return max(images, key=lambda n: (min(self.width, rect(n)[2]) - max(0, rect(n)[0])) * (min(self.height, rect(n)[3]) - max(0, rect(n)[1])))
            if attempt == 0:
                try:
                    expected_page = self.viewer_counter()
                except RuntimeError:
                    pass
            if attempt == 11 or time.monotonic() >= deadline:
                raise RuntimeError('放大浏览器中没有可见的图片区域')
            print(f'[iOS 商品] 大图图片区域未就绪，重读 {attempt + 2}/12（最多等待60秒）', flush=True)
            time.sleep(.5)
            self.snapshot()
            if expected_page is not None and self.viewer_counter() != expected_page:
                raise RuntimeError('大图加载期间页码发生变化，未保存图片')

    def close_viewer(self, *, sku=False):
        failed = sys.exc_info()[0] is not None
        try:
            self._close_viewer(sku=sku)
        except Exception as error:
            if not failed:
                raise
            print(f'[iOS 商品] 大图清理失败，保留原始错误：{error}', flush=True)

    def _close_viewer(self, *, sku=False):
        if sku:
            self.tap(self.viewer_image())
        else:
            buttons = [n for n in self.root.iter('XCUIElementTypeButton') if visible(n)
                       and rect(n)[0] < self.width * .2 and rect(n)[1] < 100]
            if len(buttons) != 1:
                raise RuntimeError('未找到唯一大图返回按钮')
            self.tap(buttons[0])
        self.snapshot()
        if sku and self.find(r'^增加数量$', kind='Button') is None:
            image_top = rect(self.viewer_image())[1]
            if image_top > 130:
                self.client.click(round(self.width / 2), round((100 + image_top) / 2))
            else:
                self.swipe(up=False, top=.35, bottom=.75)
            time.sleep(.3)
            self.snapshot()
            if self.find(r'^增加数量$', kind='Button') is None:
                raise RuntimeError('SKU 大图关闭后未返回规格面板')

    def sku_image(self, thumbnail, path):
        self.tap(thumbnail)
        self.snapshot()
        try:
            # The panel verified specs and price; its selected image may have no counter or text.
            if self.find(r'^增加数量$', kind='Button') is not None:
                raise RuntimeError('未进入 SKU 大图浏览器，未保存图片')
            self.image(self.viewer_image(), path)
        finally:
            self.close_viewer(sku=True)

    def main_images(self):
        images = [n for n in self.root.iter() if visible(n)
                  and (n.tag == 'XCUIElementTypeImage' or (n.tag == 'XCUIElementTypeOther' and label(n) == '浏览动态图片'))
                  and rect(n)[2] - rect(n)[0] > self.width * .7 and rect(n)[3] - rect(n)[1] > 150]
        if not images:
            controls = [(n.tag.removeprefix('XCUIElementType'), label(n), rect(n))
                        for n in self.root.iter() if visible(n) and label(n) and rect(n)[1] < self.height * .6]
            raise RuntimeError('未识别到商品主图；首屏控件：' + str(controls)[:1200])
        self.tap(images[0])
        self.snapshot()
        count = next((re.fullmatch(r'(\d+)\s*/\s*(\d+)', label(n)) for n in self.root.iter('XCUIElementTypeStaticText') if re.fullmatch(r'(\d+)\s*/\s*(\d+)', label(n)) and visible(n)), None)
        if count is None or int(count[1]) != 1:
            raise RuntimeError('未确认主图浏览器从第 1 张开始')
        total = int(count[2])
        try:
            for position in range(1, total + 1):
                if position > 1:
                    self.client(name=f'{position}/{total}', className='StaticText').get(timeout=5)
                candidates = [n for n in self.root.iter() if visible(n)
                              and (n.tag == 'XCUIElementTypeImage' or (n.tag == 'XCUIElementTypeOther' and label(n) == '浏览动态图片'))
                              and rect(n)[2] - rect(n)[0] > self.width * .7]
                if not candidates:
                    raise RuntimeError('主图浏览器未暴露图片区域')
                image = max(candidates, key=lambda n: (rect(n)[2] - rect(n)[0]) * (rect(n)[3] - rect(n)[1]))
                path = self.directory / '主图' / f'{position:03d}.jpg'
                self.image(image, path)
                self.record_image('main', path)
                if position < total:
                    if not ios._swipe(self.client, self.width * .85, self.height * .45, self.width * .15, self.height * .45, duration=0, velocity=400):
                        raise RuntimeError('iOS 主图翻页失败')
                    time.sleep(.3)
        finally:
            # The viewer's close button is icon-only, anchored to the navigation bar.
            buttons = [n for n in self.root.iter('XCUIElementTypeButton') if visible(n) and rect(n)[0] < self.width * .2 and rect(n)[1] < 100]
            if len(buttons) != 1:
                raise RuntimeError('未找到唯一主图浏览器返回按钮')
            self.tap(buttons[0])
            self.snapshot()

    def skus(self):
        button = self.find(r'购买|拼单|^立即参与万人团', kind='Button')
        if button is None:
            raise RuntimeError('未找到商品规格入口')
        self.tap(button)
        self.snapshot()
        if self.find(r'^增加数量$', kind='Button') is None:
            self.client(name='增加数量', className='Button', visible=True).get(timeout=15)
            self.snapshot()
            if self.find(r'^增加数量$', kind='Button') is None:
                raise RuntimeError('未确认商品规格面板')
        catalog = self.sku_catalog = {}
        previous = None
        try:
            for _ in range(40):
                groups = sku_groups(self.root, catalog)
                for group, options in groups.items():
                    catalog.setdefault(group, {}).update({name: None for name in options})
                signature = [(g, tuple((name, rect(node)) for name, node in options.items())) for g, options in groups.items()]
                if signature == previous:
                    break
                previous = signature
                self.scroll_skus(up=True)
            else:
                raise RuntimeError('SKU 规格列表超过 40 页，未确认全部加载')
            if not catalog:
                raise RuntimeError('未解析出 SKU 分组选项')
            self.debug_json('SKU选项.json', {k: list(v) for k, v in catalog.items()})
            self.debug_xml('SKU列表.xml')
            if not self.sku_rows and self.sku_gallery(catalog):
                write_csv(self.directory / 'SKU.csv', [*(f'SPU{i}' for i in range(1, len(catalog) + 1)), 'price', '图片路径'],
                          [[*(r.get(f'SPU{i}', '') for i in range(1, len(catalog) + 1)), r['price'], r['图片路径']] for r in self.sku_rows])
                return
            options = [list(v) for v in catalog.values()]
            prefixes = itertools.product(*options[:-1])
            combinations = (prefix + (value,) for i, prefix in enumerate(prefixes) for value in (options[-1] if i % 2 == 0 else reversed(options[-1])))
            saved_combinations = {tuple(row.get(f'SPU{i}', '') for i in range(1, len(catalog) + 1)) for row in self.sku_rows if row.get('available') is False or (row.get('price') and row.get('capture_method') == 'ios_enlarged_screenshot_crop')}
            for combination in combinations:
                if combination in saved_combinations:
                    continue
                row = {f'SPU{i}': value for i, value in enumerate(combination, 1)}
                unavailable = False
                for group, name in zip(catalog, combination):
                    if not self.select_sku(name):
                        unavailable = True
                        break
                if unavailable:
                    self.sku_rows.append(dict(row, available=False, price='', 图片路径=''))
                    continue
                selection = sku_summary(self.root, catalog)
                normalize = lambda s: re.sub(r'\s|\u200b', '', s)
                if not all(normalize(name) in normalize(selection) for name in combination):
                    time.sleep(.5)
                    self.snapshot()
                    selection = sku_summary(self.root, catalog)
                for name in combination:
                    if normalize(name) not in normalize(selection):
                        self.select_sku(name)
                        selection = sku_summary(self.root, catalog)
                if not all(normalize(name) in normalize(selection) for name in combination):
                    self.sku_rows.append(dict(row, available=False, price='', 图片路径=''))
                    print(f'[SKU] 组合不可选，App 自动清除或改选：{combination}；页面 {selection}；不保存价格和图片', flush=True)
                    continue
                selected = self.find(r'[¥￥]\s*\d')
                if selected is None:
                    raise RuntimeError('选择 SKU 后未读取到价格')
                if re.search(r'[¥￥]\s*\d+(?:\.\d+)?\s*[-~～]', label(selected)):
                    raise RuntimeError('SKU 价格仍是区间，未确认完整规格组合')
                price = re.search(r'[¥￥]\s*(\d+(?:\.\d+)?)', label(selected))[1]
                row.update(price=price, source='ios_native_ui')
                candidates = [n for n in self.root.iter('XCUIElementTypeImage') if visible(n) and 70 < rect(n)[2] - rect(n)[0] < self.width * .6 and rect(n)[3] - rect(n)[1] > 70]
                if not candidates:
                    raise RuntimeError('SKU 面板未暴露商品图片')
                # Unavailable combinations can leave gaps when progress is reloaded.
                positions = [int(Path(r['图片路径']).stem) for r in self.sku_rows
                             if r.get('图片路径') and Path(r['图片路径']).stem.isdigit()]
                path = self.directory / 'SKU' / f'{max(positions, default=0) + 1:04d}.jpg'
                self.sku_image(max(candidates, key=lambda n: rect(n)[3] - rect(n)[1]), path)
                self.record_image('sku', path)
                row['capture_method'] = 'ios_enlarged_screenshot_crop'
                row['图片路径'] = str(path.relative_to(self.directory))
                self.sku_rows.append(row)
                self.debug_json('SKU原始.json', self.sku_rows)
            available_rows = [r for r in self.sku_rows if r.get('available') is not False]
            if not available_rows:
                raise RuntimeError('没有成功读取任何可选 SKU，未标记完成')
            write_csv(self.directory / 'SKU.csv', [*(f'SPU{i}' for i in range(1, len(catalog) + 1)), 'price', '图片路径'],
                      [[*(r.get(f'SPU{i}', '') for i in range(1, len(catalog) + 1)), r['price'], r['图片路径']] for r in available_rows])
        finally:
            failed = sys.exc_info()[0] is not None
            try:
                self.debug_xml('SKU页面.xml')
                self.close_panel()
            except Exception as error:
                if not failed:
                    raise
                print(f'[SKU] 面板清理失败，保留原始错误：{error}', flush=True)

    def sku_gallery(self, catalog):
        thumbnails = [n for n in self.root.iter('XCUIElementTypeImage') if visible(n)
                      and 70 < rect(n)[2] - rect(n)[0] < self.width * .6 and rect(n)[3] - rect(n)[1] > 70]
        if not thumbnails:
            return False
        self.tap(max(thumbnails, key=lambda n: rect(n)[3] - rect(n)[1]))
        self.snapshot()
        try:
            expected_total = math.prod(len(options) for options in catalog.values())
            try:
                position, total = self.viewer_counter()
            except RuntimeError:
                if expected_total != 1:
                    return False
                try:
                    gallery_sku(self.root, catalog, self.width)
                except RuntimeError:
                    return False
                position, total = 1, 1
            if total != expected_total:
                print('[SKU] 图库数量与规格组合不一致，返回规格面板逐项采集', flush=True)
                return False
            expected = set(itertools.product(*(list(options) for options in catalog.values())))
            self.sku_rows = []
            self.images = [r for r in self.images if r['image_type'] != 'sku']
            seen, combinations = set(), set()
            direction = -1 if position <= (total + 1) / 2 else 1
            for _ in range(total * 2):
                if position not in seen:
                    try:
                        combination, price = gallery_sku(self.root, catalog, self.width)
                    except RuntimeError:
                        print('[SKU] 图库未完整展示规格或价格，保留已确认记录并返回面板逐项采集', flush=True)
                        return False
                    if combination not in expected or combination in combinations:
                        raise RuntimeError('SKU 图库规格重复或不在已确认规格中，停止保存')
                    path = self.directory / 'SKU' / f'{position:04d}.jpg'
                    self.image(self.viewer_image(), path)
                    self.record_image('sku', path, position=position)
                    row = {f'SPU{i}': name for i, name in enumerate(combination, 1)}
                    row.update(price=price, 图片路径=str(path.relative_to(self.directory)), source='ios_native_ui',
                               capture_method='ios_enlarged_screenshot_crop', gallery_position=position)
                    self.sku_rows.append(row)
                    self.sku_rows.sort(key=lambda r: r['gallery_position'])
                    self.debug_json('SKU原始.json', self.sku_rows)
                    seen.add(position)
                    combinations.add(combination)
                    print(f'[SKU] 图库采集 {len(seen)}/{total}：{combination}，价格 {price}', flush=True)
                if len(seen) == total:
                    break
                if position in (1, total):
                    direction = 1 if position == 1 else -1
                target = position + direction
                x1, x2 = (self.width * .85, self.width * .15) if direction == 1 else (self.width * .15, self.width * .85)
                if not ios._swipe(self.client, x1, self.height * .45, x2, self.height * .45, duration=0, velocity=400):
                    raise RuntimeError('SKU 图库翻页失败')
                time.sleep(.5)
                position = self.wait_viewer_page(position, target, total)
            if combinations != expected:
                raise RuntimeError('SKU 图库尚未覆盖全部规格组合')
            return True
        finally:
            self.close_viewer(sku=True)

    def select_sku(self, name):
        normalize = lambda s: re.sub(r'\s|\u200b', '', s)
        if normalize(name) in normalize(sku_summary(self.root, self.sku_catalog)):
            return True
        for direction in (False, True):
            previous = None
            for _ in range(16):
                groups = sku_groups(self.root, self.sku_catalog)
                node = next((options[name] for options in groups.values() if name in options), None)
                if node is not None:
                    x1, y1, x2, y2 = rect(node)
                    views = [rect(n) for n in self.root.iter('XCUIElementTypeCollectionView')
                             if any(child is node for child in n.iter())]
                    if (views and visible(node) and max(0, max(v[1] for v in views)) <= y1
                            and y2 <= min(self.height, min(v[3] for v in views))):
                        if node.get('enabled') == 'false':
                            return False
                        for attempt in range(3):
                            self.client(name=node.get('name') or name, className='Button', visible=True).get(timeout=3).tap()
                            time.sleep(.5)
                            self.snapshot()
                            selection = sku_summary(self.root, self.sku_catalog)
                            # Selecting a dependent option can clear another group.
                            # The complete combination is checked before reading price.
                            if normalize(name) in normalize(selection) or '请选择' in selection:
                                return True
                        print(f'[SKU] 选项重试后仍不可选：{name}；页面 {selection}', flush=True)
                        return False
                signature = [(g, tuple((key, value.get('y')) for key, value in opts.items())) for g, opts in groups.items()]
                if signature == previous:
                    break
                previous = signature
                self.scroll_skus(up=direction)
        raise RuntimeError(f'未找到可点击 SKU 选项：{name}')

    def scroll_skus(self, *, up):
        groups = sku_groups(self.root, self.sku_catalog)
        if not groups:
            raise RuntimeError('SKU 面板已离开，停止规格操作')
        heading = next(iter(self.sku_catalog))
        # Native scroll targets the SKU collection instead of dragging the order sheet.
        names = list(dict.fromkeys([heading, *(name for options in groups.values() for name in options)]))
        predicate = ' or '.join('@name=' + json.dumps(name, ensure_ascii=False) for name in names)
        self.client(xpath=f'//XCUIElementTypeCollectionView[.//*[{predicate}]]').get(timeout=5).scroll('down' if up else 'up', distance=.65)
        time.sleep(.3)
        self.snapshot()

    def seek(self, pattern, *, pages=50):
        # 兼容旧调用：单个正则（可含 | 交替）滚动查找入口，复用 seek_entry。
        return self.seek_entry((pattern,) if isinstance(pattern, str) else tuple(pattern), pages=pages)

    def comment_time(self, row):
        self.client._session_http.post('/appium/settings', {'settings': {
            'shouldUseCompactResponses': False, 'elementResponseAttributes': 'type,label',
        }})
        for attempt in range(3):
            current = next((r for r in review_rows(self.root, controls=True)
                            if r['ui_fingerprint'] == row['ui_fingerprint']), None)
            if current is None:
                raise RuntimeError('当前页面未找到原评论菜单，保留进度')
            menus = [n for n in self.root.iter('XCUIElementTypeButton') if label(n) == '菜单']
            elements = self.client(name='菜单', className='Button').find_elements()
            if len(elements) != len(menus):
                self.snapshot()
                if attempt == 2:
                    raise RuntimeError('评论菜单数量发生变化，保留进度')
                continue
            # Review images can load after XML capture and move screen coordinates.
            elements[menus.index(current['_menu'])].tap()
            time.sleep(.3)
            matches = self.client._session_http.post('/elements', {
                'using': 'predicate string',
                'value': '(type == "XCUIElementTypeStaticText" AND name BEGINSWITH "发表时间") '
                         'OR (type == "XCUIElementTypeButton" AND name == "取消")',
            }, timeout=8).value
            cancels = [m for m in matches if m.get('type') == 'XCUIElementTypeButton' and m.get('label') == '取消']
            if len(cancels) == 1:
                cancel = cancels[0].get('ELEMENT') or cancels[0]['element-6066-11e4-a52e-4f735466cecf']
                break
            self.snapshot()
            if attempt == 2:
                raise RuntimeError('重试后仍未确认评论时间菜单，停止后续评论点击')
            print(f'[iOS 商品] 评论菜单未打开，重新定位后重试 {attempt + 2}/3', flush=True)
        try:
            values = {m.get('label', '') for m in matches if m.get('type') == 'XCUIElementTypeStaticText'}
            dates = set()
            for value in values:
                match = re.search(r'(\d{4})[.年/-](\d{1,2})[.月/-](\d{1,2})', value)
                if match:
                    dates.add(date(*map(int, match.groups())).isoformat())
            if len(dates) != 1:
                raise RuntimeError('评论菜单未暴露唯一发表日期，保留进度')
            row['time'] = dates.pop()
        finally:
            self.client._session_http.post(f'/element/{cancel}/click')
            time.sleep(.3)
            self.snapshot()
        if not any(r['ui_fingerprint'] == row['ui_fingerprint'] for r in review_rows(self.root)):
            raise RuntimeError('评论菜单关闭后未确认原评论，停止后续点击')

    def comments(self):
        tab = self.find_entry(REVIEW_ENTRIES, kind='StaticText')
        if tab is not None:
            self.tap(tab)
            self.snapshot()
        self.seek(REVIEW_HEADER_PATTERN)
        if self.find(r'暂无评价') is not None:
            return
        self.tap(self.seek_entry(REVIEW_MORE_ENTRIES))
        self.snapshot()
        try:
            expand = self.find(r'^标签展开按钮$')
            if expand is not None:
                self.tap(expand)
                self.snapshot()
            screenshot = self.client.screenshot().resize((round(self.width), round(self.height)))
            tags = negative_filters(self.root, screenshot)
            self.debug_json('评价标签.json', tags)
            dated = {r['ui_fingerprint']: r['time'] for r in self.negative
                     if r.get('ui_fingerprint') and r.get('time')}
            seen = {}
            for tag in tags:
                node = self.find('^' + re.escape(tag['label']) + '$')
                if node is None:
                    element = self.client(name=tag['label']).get(timeout=5)
                    element.scroll('visible')
                    element.tap()
                else:
                    self.tap(node)
                time.sleep(.8)
                self.snapshot()
                previous, stagnant, count = None, 0, set()
                for _ in range(100):
                    batch = review_rows(self.root, controls=True)
                    for row in batch:
                        row.pop('_menu')
                        key = row['ui_fingerprint']
                        count.add(key)
                        if key in seen:
                            seen[key]['negative_tags'] = list(dict.fromkeys([*seen[key]['negative_tags'], tag['name']]))
                        elif len(seen) < 100:
                            if key in dated:
                                row['time'] = dated[key]
                            else:
                                self.comment_time(row)
                            seen[key] = dict(row, negative_tags=[tag['name']], negative_reason=tag['name'])
                            print(f'[iOS 商品] 差评 {len(seen)}：发表日期 {row["time"]}', flush=True)
                        self.negative = list(seen.values())
                    self.debug_json('差评原始.json', self.negative)
                    if len(seen) >= 100 or self.find(r'没有更多|暂无相关评价|暂无评价|已显示全部|到底了') is not None or (tag['count'] is not None and len(count) >= tag['count']):
                        break
                    signature = tuple(r['ui_fingerprint'] for r in batch)
                    stagnant = stagnant + 1 if signature == previous else 0
                    if stagnant >= 2:
                        raise RuntimeError('差评翻页无新增且没有结束标记，保留进度待续采')
                    previous = signature
                    self.swipe(top=.35, bottom=.85)
                else:
                    raise RuntimeError('差评翻页超过 100 页，未确认结束')
                if len(seen) >= 100:
                    break
            self.debug_json('差评原始.json', self.negative)
            write_csv(self.directory / '差评.csv', ['用户', '评论内容', 'SKU', '原因', '评论日期'],
                      [[r['name'], r['comment'], r['specs'], '、'.join(r['negative_tags']), r.get('time', '')] for r in self.negative])
        finally:
            failed = sys.exc_info()[0] is not None
            try:
                if failed:
                    self.snapshot()
                cancel = self.find(r'^取消$')
                if cancel is not None:
                    self.tap(cancel)
                    self.snapshot()
                self.close_panel()
            except Exception as error:
                if not failed:
                    raise
                print(f'[iOS 商品] 评价面板清理失败，保留原始错误：{error}', flush=True)

    def _detail_thumbnail(self):
        """详情区可点击的大图（排除播放/领券等运营卡片）。"""
        excluded_images = {image for cell in self.root.iter('XCUIElementTypeCell')
                           if any(re.search(r'player (?:play|pause)|播放|暂停', label(button))
                                  for button in cell.iter('XCUIElementTypeButton'))
                           or any(re.search(r'领券|抢券|送你.*券|大促直降', label(text))
                                  for text in cell.iter('XCUIElementTypeStaticText'))
                           for image in cell.iter('XCUIElementTypeImage')}
        return next((n for n in self.root.iter('XCUIElementTypeImage') if visible(n)
                     and n not in excluded_images
                     and rect(n)[2] - rect(n)[0] >= self.width * .8
                     and rect(n)[3] - rect(n)[1] > 150
                     and min(rect(n)[3], self.height - 85) - max(rect(n)[1], 125) >= 60), None)

    def _open_details(self):
        """进入详情区：点「详情」入口，或滚动到已内联展开的详情图。

        兼容没有独立「详情」入口的页面（详情已内联展开），以及入口文案
        不在已知列表中的页面。用签名稳定早停避免页面被弹窗遮挡或滚动到底
        时做满 50 次无效滑动。
        """
        entry = self.find_entry(DETAIL_ENTRIES)
        if entry is not None:
            self.tap(entry)
            self.snapshot()
            return
        previous = None
        for _ in range(50):
            signature = [(label(n), n.get('y')) for n in self.root.iter() if visible(n) and label(n)]
            if signature == previous:
                break
            previous = signature
            self.swipe(top=.3, bottom=.7)
            entry = self.find_entry(DETAIL_ENTRIES)
            if entry is not None:
                self.tap(entry)
                self.snapshot()
                return
            if self._detail_thumbnail() is not None:
                return
        raise RuntimeError('未找到详情入口或可点击的详情图片：' + ' / '.join(DETAIL_ENTRIES))

    def details(self):
        self._open_details()
        for attempt in range(12):
            thumbnail = self._detail_thumbnail()
            if thumbnail is not None:
                break
            if attempt == 11:
                raise RuntimeError('滚动后未找到可点击的详情图片，未使用页面分段截图代替')
            self.swipe(top=.3, bottom=.7)
        x1, y1, x2, y2 = rect(thumbnail)
        self.client.click(round((x1 + x2) / 2), round(min(max(y1, 125) + 60, y2 - 1, self.height - 100)))
        time.sleep(.5)
        self.snapshot()
        try:
            position, total = self.viewer_counter()
        except RuntimeError:
            # 部分商品详情为文字/规格说明（品牌故事+规格参数），没有可放大的详情图片；
            # 点击缩略图后仍在商品详情页（无 N/M 页码）。重读两次确认后按无详情图完成。
            position = None
            for _ in range(2):
                time.sleep(.6)
                self.snapshot()
                try:
                    position, total = self.viewer_counter()
                    break
                except RuntimeError:
                    continue
            if position is None:
                print('[iOS 商品] 详情无放大图片浏览器（文字/规格详情），跳过详情图片采集')
                return
        seen = set()
        try:
            for _ in range(total):
                if position in seen:
                    raise RuntimeError('详情大图页码重复，未确认采集完整')
                path = self.directory / '详情' / f'{position:04d}.jpg'
                self.image(self.viewer_image(), path)
                self.record_image('detail', path, position=position)
                seen.add(position)
                if len(seen) == total:
                    break
                expected = position - 1 if position > 1 else total
                if not ios._swipe(self.client, self.width * .15, self.height * .45,
                                  self.width * .85, self.height * .45, duration=0, velocity=400):
                    raise RuntimeError('详情大图右滑失败')
                time.sleep(.5)
                position = self.wait_viewer_page(position, expected, total)
            if len(seen) != total:
                raise RuntimeError('详情大图尚未完整采集')
        finally:
            self.close_viewer()

    def collect(self, item, directory, row, *, initial_xml=None):
        self.item, self.directory = item, directory
        self.directory.mkdir(parents=True, exist_ok=True)
        saved = json.loads(self.store.item(item['id'])['product_json'] or '{}')
        upgrade = saved.get('info', {}).get('image_method') != 'ios_enlarged_screenshot_crop'
        self.images = saved.get('images', [])
        self.sku_rows, self.negative, self.detail_hashes = saved.get('sku', []), saved.get('negative_comments', []), set()
        if upgrade:
            for stage in ('main_images', 'sku', 'detail_images'):
                self.store.stage_state(item['list_id'], stage, 'pending', item_id=item['id'])
            self.sku_rows = [r for r in self.sku_rows if r.get('capture_method') == 'ios_enlarged_screenshot_crop' or r.get('available') is False]
            kept = {r.get('图片路径') for r in self.sku_rows}
            self.images = [r for r in self.images if r['image_type'] != 'sku' or r['local_path'] in kept]
        known_paths = {r['local_path'] for r in self.images if r['image_type'] == 'sku'}
        for sku in self.sku_rows:
            path = sku.get('图片路径')
            if path and path not in known_paths and (directory / path).is_file():
                self.images.append({'image_type': 'sku', 'position': len(known_paths) + 1, 'local_path': path, 'state': 'completed'})
                known_paths.add(path)
        self.source_reads = 0
        self.xml = initial_xml
        if initial_xml:
            self.root = ET.fromstring(initial_xml)
        else:
            self.snapshot()
        app = next(self.root.iter('XCUIElementTypeApplication'))
        self.width, self.height = float(app.get('width')), float(app.get('height'))
        info = {'source': 'ios_native_ui', 'goods_name': row.get('商品页标题') or row['标题'], 'image_method': 'ios_enlarged_screenshot_crop'}
        info.update({key: item[key] for key in ('shop_id', 'shop_name') if item.get(key)})
        write_json(directory / '产品信息.json', info)
        self.store.refresh_product_info(item['id'], info)
        self.store.stage_state(item['list_id'], 'product_info', 'completed', item_id=item['id'])
        error_message = ''
        try:
            for stage, action in [('main_images', self.main_images), ('sku', self.skus), ('detail_images', self.details), ('negative_comments', self.comments)]:
                kind = {'main_images': 'main', 'sku': 'sku', 'detail_images': 'detail'}.get(stage)
                existing = [r for r in self.images if r['image_type'] == kind] if kind else []
                if not (upgrade and stage in {'sku', 'detail_images'}) and self.store.stage(item['list_id'], stage, item['id'])['state'] == 'completed' and (not kind or (existing and all((directory / r['local_path']).is_file() for r in existing))):
                    print(f'[iOS 商品] 复用已完成阶段：{stage}', flush=True)
                    continue
                if kind and stage != 'sku':
                    self.images = [r for r in self.images if r['image_type'] != kind]
                if stage == 'negative_comments':
                    self.negative = []
                self.store.stage_state(item['list_id'], stage, 'running', item_id=item['id'])
                print(f'[iOS 商品] 排行 {item["rank"]}：{stage}', flush=True)
                try:
                    action()
                except Exception as error:
                    error_message = str(error)
                    self.store.stage_state(item['list_id'], stage, 'failed', item_id=item['id'], reason=str(error))
                    raise
                self.store.stage_state(item['list_id'], stage, 'completed', item_id=item['id'])
        finally:
            write_json(directory / '图片清单.json', self.images)
            stages = {stage: self.store.stage(item['list_id'], stage, item['id']) for stage in ('main_images', 'sku', 'negative_comments', 'detail_images')}
            details = {'info': info, 'images': self.images, 'sku': self.sku_rows, 'negative_comments': self.negative, 'status': {'stages': stages}}
            self.store.product_state(item['id'], 'failed' if error_message else 'running', details=details, reason=error_message)
            (directory / '商品页面.xml').write_text(self.xml or '', encoding='utf-8')
            print(f'[iOS 商品] 本商品读取元素树 {self.source_reads} 次', flush=True)
        self.store.product_state(item['id'], 'completed')
