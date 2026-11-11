"""Read product UI, capture enlarged images, enumerate SKU and collect reviews."""
from automation.pdd.spider.logging_setup import timed_operation
import hashlib
import csv
from collections import Counter
import itertools
import json
import math
from pathlib import Path
import random
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from automation.pdd.spider import config
from automation.pdd.spider.artifacts import write_json
from automation.pdd.spider.comments import COMMENT_HEADER, _comment_rows
from automation.pdd.spider.database import today
from automation.pdd.spider.status import load_status, update_stage
from automation.pdd.spider.utils import sanitize, write_csv
from automation.pdd.spider.workflow import FatalWorkflowError

PACKAGE = 'com.xunmeng.pinduoduo'
PAGER_CLASSES = {'android.support.v4.view.ViewPager', 'androidx.viewpager.widget.ViewPager'}


class UnreadableWebView(RuntimeError):
    pass


class SkuNotSelectable(RuntimeError):
    pass


def bounds(node):
    values = [int(value) for value in re.findall(r'\d+', node.get('bounds', ''))]
    return tuple(values) if len(values) == 4 else (0, 0, 0, 0)


def saved_image(path):
    from PIL import Image
    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except (OSError, ValueError):
        return False


def texts(root):
    return list(dict.fromkeys(node.get('text', '').strip() for node in root.iter()
                             if node.get('package') == PACKAGE and node.get('text', '').strip()))


SOLD_OUT_REASON = '商品已售罄，推荐以下相似商品'


def sold_out_notice(root, image):
    if SOLD_OUT_REASON in ''.join(texts(root)):
        return True
    import cv2
    import numpy as np
    # shortcut: 模板来自当前安卓浅色页面；提示样式变化时补充模板或改用中文 OCR。
    image = image.convert('L').resize((1080, round(image.height * 1080 / image.width)))
    header = np.array(image.crop((0, 0, 1080, min(image.height, 780))))
    from PIL import Image
    with Image.open(Path(__file__).with_name('assets') / 'sold_out_notice.png') as template:
        template = np.array(template.convert('L'))
    if header.shape[0] < template.shape[0]:
        return False
    return float(cv2.matchTemplate(header, template, cv2.TM_CCOEFF_NORMED).max()) >= .92


def image_crop_bounds(image, rectangle):
    """Remove black viewer bars when Android exposes a full-screen ImageView."""
    x1, y1, x2, y2 = rectangle
    if y2 - y1 < image.height * .8:
        return rectangle
    area = image.crop(rectangle).convert('L').resize((64, y2 - y1))
    pixels = area.tobytes()
    active = [sum(pixel > 24 for pixel in pixels[row * 64:(row + 1) * 64]) >= 3
              for row in range(area.height)]
    runs, start = [], None
    for index, value in enumerate([*active, False]):
        if value and start is None:
            start = index
        elif not value and start is not None:
            runs.append((start, index))
            start = None
    top, bottom = max(runs, key=lambda pair: pair[1] - pair[0], default=(0, 0))
    if bottom - top < 24:
        raise RuntimeError('无法从放大图片识别有效内容区域，未保存整屏截图')
    # A portrait image may also have black side bars. Derive its width from
    # the content band rather than forcing the screenshot's full width.
    band = image.crop((x1, y1 + top, x2, y1 + bottom)).convert('L')
    mask = band.point(lambda pixel: 255 if pixel > 24 else 0)
    horizontal = mask.getbbox()
    if horizontal is None:
        raise RuntimeError('放大图片内容为空')
    return x1 + horizontal[0], y1 + top, x1 + horizontal[2], y1 + bottom


def image_counter(root):
    for value in texts(root):
        match = re.fullmatch(r'(?:商品\s*)?(\d+)\s*/\s*(\d+)', value)
        if match:
            return list(map(int, match.groups()))
        match = re.fullmatch(r'共\s*(\d+)\s*张商品图\s*当前为第\s*(\d+)\s*张', value)
        if match:
            return [int(match[2]), int(match[1])]
    return None


def product_info(root):
    nodes = [node for node in root.iter() if node.get('package') == PACKAGE]
    labels = texts(root)
    web_title = webview_product_title(nodes)
    has_purchase = any('购买' in value or '拼单' in value for value in labels)
    has_share = any(node.get('content-desc') == '分享' for node in nodes)
    if not has_purchase or (not has_share and not web_title):
        webviews = [n for n in nodes if n.get('class', '').endswith('WebView')]
        if webviews and not any(len(value) >= 8 for value in labels) and not any(
                child.get('text', '').strip() or child.get('content-desc', '').strip()
                for view in webviews for child in view.iter()):
            raise UnreadableWebView('商品页面只有空 WebView，无法读取元素，已标记待统一处理')
        raise RuntimeError('当前不是可识别的拼多多商品页，请手动打开商品详情')
    candidates = [node.get('content-desc', '').strip() for node in nodes
                  if 12 <= len(node.get('content-desc', '')) <= 220
                  and '\n' not in node.get('content-desc', '')
                  and node.get('content-desc') != 'tronplayer_view'
                  and node.get('class') in {'android.support.v4.view.ViewPager', 'android.widget.ImageView',
                                           'android.widget.FrameLayout', 'android.view.ViewGroup'}]
    if not candidates and not web_title and not any(
            node.get('class') in PAGER_CLASSES and 12 <= len(node.get('content-desc', '')) <= 220
            for node in nodes):
        raise RuntimeError('商品标题未暴露到控件树，未将其他页面作为商品采集')
    pager_title = next((node.get('content-desc', '').strip() for node in nodes
                        if node.get('class') in PAGER_CLASSES
                        and 12 <= len(node.get('content-desc', '')) <= 220), '')
    title = web_title or pager_title or max(dict.fromkeys(candidates), key=lambda value: candidates.count(value))
    price = ui_price(labels)
    return {'source': 'android_native_ui', 'goods_name': title, 'display_price': price,
            'texts': labels, 'captured_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'image_method': 'enlarged_screenshot_crop',
            'page_structure': 'webview' if web_title else 'native'}


def webview_product_title(nodes):
    """Recognize the product header, not reviews or arbitrary long WebView text."""
    if not any(n.get('class') == 'android.webkit.WebView' for n in nodes):
        return ''
    images = [n for n in nodes if n.get('class') == 'android.widget.Image'
              and n.get('text') == '商品大图' and n.get('clickable') == 'true']
    purchase = [n for n in nodes if n.get('class') == 'android.widget.Button'
                and n.get('clickable') == 'true'
                and re.search(r'购买|拼单', n.get('text', ''))]
    if not images or not purchase:
        return ''
    image = max(images, key=lambda n: bounds(n)[2] - bounds(n)[0])
    x1, _, x2, bottom = bounds(image)
    width = x2 - x1
    if width <= 0:
        return ''
    # The title must precede the review section and the bottom purchase toolbar.
    end = min([bounds(n)[1] for n in nodes if n.get('text', '').startswith('商品评价')]
              + [bounds(n)[1] for n in purchase])
    candidates = [n for n in nodes if n.get('class') == 'android.view.View'
                  and n.get('clickable') != 'true' and 12 <= len(n.get('text', '')) <= 220
                  and bottom <= bounds(n)[1] < end
                  and bounds(n)[2] - bounds(n)[0] >= width * .65
                  and not re.search(r'先用后付|确认收货|人下单|人推荐|退货包运费|已抢|最后\d+天', n.get('text', ''))]
    if not candidates:
        return ''
    return max(candidates, key=lambda n: len(n.get('text', ''))).get('text', '').strip()


def ui_price(labels):
    complete = next((value for value in labels if re.fullmatch(r'[¥￥]\s*\d+(?:\.\d+)?', value)), '')
    if complete:
        return complete
    embedded = next((match.group(0).replace(' ', '') for value in labels
                     if (match := re.search(r'[¥￥]\s*\d+(?:\.\d+)?', value))), '')
    if embedded:
        return embedded
    for index, value in enumerate(labels[:-1]):
        if value in {'¥', '￥'} and re.fullmatch(r'\d+(?:\.\d+)?', labels[index + 1]):
            return value + labels[index + 1]
    return ''


def sku_option_value(value):
    # The option's selected summary omits the changing repurchase badge.
    value = re.sub(r'\s*\d+(?:\.\d+)?[万千]?\+?人回购\s*$', '', value)
    return re.sub(r'\s|#', '', re.sub(r'最多人选择|(?:\d{1,2}\.\d{1,2})?爆卖', '', value))


def sku_dimensions(root):
    result = []
    for group in root.iter():
        children = list(group)
        if len(children) < 2:
            continue
        heading_nodes = [node for node in children[0].iter() if node.get('text')]
        if any(node.get('clickable') == 'true' for node in heading_nodes):
            continue
        headings = [node.get('text', '').strip() for node in heading_nodes]
        if len(headings) != 1 or not 1 <= len(headings[0]) <= 16:
            continue
        if not re.search(r'(颜色|组合|规格|套餐|口味|尺码|版本|型号|款式|容量|分类|尺寸|种类|香型|系列|材质|套装|净含量|机型|度数|数量|包装)$', headings[0]):
            continue
        options = list(dict.fromkeys(node.get('text') for node in children[1].iter()
                                    if node.get('class') == 'android.widget.TextView'
                                    and node.get('clickable') == 'true' and node.get('text')
                                    and node.get('enabled') != 'false'))
        if options and headings[0] not in {'确认款式', '确定', '购买数量'}:
            result.append({'name': headings[0], 'options': options})
    # Nested layouts can repeat a dimension heading around several groups.
    # Keep the smallest matching group rather than multiplying outer containers.
    unique = {}
    for dimension in result:
        name = dimension['name']
        if name not in unique or len(dimension['options']) < len(unique[name]['options']):
            unique[name] = dimension
    return list(unique.values())


def review_rows(root, *, controls=False):
    parents = {child: parent for parent in root.iter() for child in parent}
    rows = []
    for avatar in root.iter():
        if avatar.get('content-desc') != '头像' or avatar.get('package') != PACKAGE:
            continue
        header = parents.get(avatar)
        card = parents.get(header)
        if card is None:
            continue
        header_text = [node.get('text', '').strip() for node in header.iter() if node.get('text')]
        candidates = [node for node in card.iter() if node.get('text') and node not in list(header.iter())]
        content = next((node.get('text') for node in candidates
                        if node.get('resource-id', '').endswith('/tv_content')), '')
        if not content:
            content = max((node.get('text') for node in candidates
                           if len(node.get('text', '')) >= 8 and not re.match(
                               r'^(颜色|规格|组合|口味|款式|尺码)[:：]', node.get('text', ''))), key=len, default='')
        if not content:
            continue
        specs = next((node.get('text') for node in candidates if re.match(
            r'^(颜色|规格|组合|口味|款式|尺码)[:：]', node.get('text', ''))), '')
        if not specs:
            for group in card.iter():
                parts = [n.get('text', '').strip() for n in group.iter() if n.get('text', '').strip()]
                if (group.get('clickable') == 'true'
                        and any(re.match(r'^[¥￥]\s*\d', value) for value in parts)
                        and any(n.get('class') == 'android.widget.ImageView' for n in group.iter())):
                    values = [value for value in parts if not re.match(r'^[¥￥]\s*\d', value)]
                    if values:
                        specs = ''.join(values)
                        break
        row = {'source': 'android_native_ui', 'name': header_text[0] if header_text else '',
               'comment': content, 'specs': specs,
               'order_num_text': next((value for value in header_text if '拼过' in value), '')}
        row['ui_fingerprint'] = hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if controls:
            menu = next((n for n in header.iter() if n.get('content-desc') == '问题反馈'
                         and n.get('clickable') == 'true'), None)
            if menu is not None:
                row['_menu_bounds'] = bounds(menu)
        rows.append(row)
    return rows


def tag_background(image, rectangle):
    x1, y1, x2, y2 = rectangle
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(image.width, x2), min(image.height, y2)
    if x2 - x1 < 12 or y2 - y1 < 12:
        return None
    # Most pixels belong to the flat tag background, even with text and rounded corners.
    pixels = image.convert('RGB').crop((x1 + 4, y1 + 4, x2 - 4, y2 - 4)).resize((40, 20)).tobytes()
    colors = Counter(tuple(pixels[index:index + 3]) for index in range(0, len(pixels), 3))
    return colors.most_common(1)[0][0] if colors else None


def review_filters(root, image):
    result, seen = [], set()
    for item in root.iter():
        if item.get('package') != PACKAGE or item.get('clickable') != 'true':
            continue
        label = (item.get('content-desc') or item.get('text') or '').strip()
        match = re.fullmatch(r'(.+?)[（(]([\d,]+)[)）]', label)
        name = match[1].strip() if match else label
        if not match and name not in {'好评', '差评', '中差评', '负面评价'}:
            continue
        if name in {'全部', '最新', '追加', '图/视频', '有图', '视频', '回头客', '商品评价'} or label in seen:
            continue
        color = tag_background(image, bounds(item))
        kind, basis = None, None
        if name in {'差评', '中差评', '负面评价'}:
            kind, basis = 'negative', 'explicit_app_filter'
        elif name == '好评':
            kind, basis = 'positive', 'explicit_app_filter'
        elif color:
            red, green, blue = color
            if min(color) >= 220 and max(color) <= 250 and max(color) - min(color) <= 4:
                kind, basis = 'negative', 'app_gray_tag'
            elif red >= 245 and 220 <= green <= 247 and 220 <= blue <= 249 and red - green >= 6:
                kind, basis = 'positive', 'app_pink_tag'
        if kind:
            seen.add(label)
            result.append({'label': label, 'name': name, 'kind': kind, 'basis': basis,
                           'count': int(match[2].replace(',', '')) if match else None,
                           'bounds': list(bounds(item)), 'background': list(color) if color else None})
    return result


class NativeProduct:
    def __init__(self, device, *, comment_limit=100, image_limit=0, sku_limit=0):
        self.d = device
        self.comment_limit, self.image_limit, self.sku_limit = comment_limit, image_limit, sku_limit

    def bind_store(self, store, task):
        self.store, self.task = store, task
        return self

    def update_stage(self, directory, stage, state, reason='', **metadata):
        if hasattr(self, 'store'):
            with self.store.db:
                self.store._set_stage(self.task['ranking_list_id'], stage, state,
                                      item_id=self.task['id'], last_error=reason, **metadata)
        update_stage(directory, stage, state, reason, **metadata)

    @timed_operation('[Android] 读取页面元素')
    def snapshot(self):
        if self.d.app_current().get('package') != PACKAGE:
            raise FatalWorkflowError('拼多多已不在前台，停止原生采集')
        xml = self.d.dump_hierarchy(compressed=False)
        return xml, ET.fromstring(xml)

    def retry_click(self, selector):
        for attempt in range(3):
            if selector.click_exists(timeout=1):
                time.sleep(.4)
                return
            if attempt < 2:
                time.sleep(1)
        raise RuntimeError('原生 App 控件未出现，点击重试三次失败')

    def check_title(self, title):
        # Scrolling changes which title nodes are exposed. Locate the purchase
        # control instead of comparing text from unrelated visible containers.
        _, root = self.snapshot()
        purchase = next((node for node in root.iter()
                         if node.get('package') == PACKAGE
                         and node.get('clickable') == 'true'
                         and node.get('enabled') != 'false'
                         and any(re.search(r'购买|拼单', child.get('text', '') + child.get('content-desc', ''))
                                 for child in node.iter())), None)
        if purchase is None:
            raise FatalWorkflowError('未定位到商品购买按钮，停止后续操作')
        return root

    def top(self):
        self.d(text='顶部').click_exists(timeout=.3)
        time.sleep(.4)

    def enlarged_image(self, directory, relative):
        _, root = self.snapshot()
        width, height = self.d.window_size()
        images = [node for node in root.iter() if node.get('class') in {'android.widget.ImageView', 'android.widget.Image'}
                  and node.get('package') == PACKAGE and bounds(node)[2] - bounds(node)[0] >= 24
                  and bounds(node)[3] - bounds(node)[1] >= 24]
        if not images:
            raise RuntimeError('放大图片中没有可确认的图片区域，未使用整屏冒充图片')
        node = max(images, key=lambda n: (bounds(n)[2] - bounds(n)[0]) * (bounds(n)[3] - bounds(n)[1]))
        x1, y1, x2, y2 = bounds(node)
        image = self.d.screenshot()
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        crop = image_crop_bounds(image, (max(0, x1), max(0, y1), min(width, x2), min(height, y2)))
        image.crop(crop).convert('RGB').save(path, quality=95)
        counter = image_counter(root)
        return {'image_type': 'sku' if relative.startswith('SKU/') else ('detail' if relative.startswith('详情图片/') else 'main'), 'position': 1,
                'source_url': None, 'local_path': relative, 'state': 'completed',
                'capture_method': 'android_enlarged_screenshot_crop', 'crop': list(crop),
                'page_counter': counter}

    @timed_operation('[Android] 打开详情图')
    def open_detail(self, title):
        width, height = self.d.window_size()
        in_details = False
        for _ in range(35):
            root = self.check_title(title)
            headings = [bounds(n)[3] for n in root.iter() if n.get('text') in {'商品详情', '图文详情'}]
            if headings:
                in_details = True
            if in_details:
                boundary = max(headings) if headings else 0
                candidates = [n for n in root.iter() if n.get('content-desc') == title
                              and any(child.get('class') == 'android.widget.ImageView' for child in n.iter())
                              and bounds(n)[2] - bounds(n)[0] > width * .7
                              and bounds(n)[1] >= boundary and bounds(n)[1] < height * .75
                              and bounds(n)[3] - bounds(n)[1] > 150]
                for candidate in candidates:
                    for attempt in range(3):
                        x1, y1, x2, y2 = bounds(candidate)
                        self.d.click((x1+x2)/2, (y1+min(y2, height*.78))/2)
                        time.sleep(.6)
                        _, viewer = self.snapshot()
                        enlarged = any(node.get('package') == PACKAGE
                                       and node.get('class') in {'android.widget.ImageView', 'android.widget.Image'}
                                       and bounds(node)[2] - bounds(node)[0] >= 24
                                       and bounds(node)[3] - bounds(node)[1] >= 24
                                       for node in viewer.iter())
                        if image_counter(viewer) and enlarged and not any(
                                node.get('class') in PAGER_CLASSES and node.get('content-desc') == title
                                for node in viewer.iter()):
                            return viewer
                        print('[Android] 详情图未放大，重试点击', flush=True)
                        self.check_title(title)
                # Move just enough to expose the image without losing the heading.
                self.d.swipe(width*.4, height*.7, width*.4, height*.5, duration=.2)
            else:
                if any('看了又看' in value or '猜你喜欢' in value or value == '查看精选推荐' for value in texts(root)):
                    raise RuntimeError('已到推荐区域，未找到可确认的商品详情图片')
                self.d.swipe(width*.4, height*.72, width*.4, height*.42, duration=.2)
            time.sleep(.4)
        raise RuntimeError('未找到商品详情图片入口')

    def image_viewer(self, root, title):
        counter = image_counter(root)
        if not counter or not 1 <= counter[0] <= counter[1] <= 100:
            return False
        width, height = self.d.window_size()
        return any(n.get('class') in PAGER_CLASSES
                   and not n.get('content-desc')
                   and bounds(n)[2] - bounds(n)[0] >= width * .9
                   and bounds(n)[3] - bounds(n)[1] >= height * .8
                   and any(child.get('class') == 'android.widget.ImageView' for child in n.iter())
                   for n in root.iter()) or (not any(
                       n.get('class') in PAGER_CLASSES and n.get('content-desc') == title for n in root.iter())
                       and not any(re.search(r'购买|拼单', value) for value in texts(root)))

    @timed_operation('[Android] 采集商品图片')
    def images(self, directory, title, *, detail=False, _entry_attempt=0):
        if not _entry_attempt:
            self.top()
        root = self.check_title(title)
        if detail:
            root = self.open_detail(title)
        kind = 'detail' if detail else 'main'
        folder = '详情图片/详情' if detail else '主图/主图'
        counter = image_counter(root)
        if not counter:
            raise RuntimeError('图片没有页码，无法确认数量')
        current, total = counter
        if not 1 <= current <= total <= 100:
            raise RuntimeError('图片页码异常')
        manifest = directory / '图片清单.json'
        existing = json.loads(manifest.read_text(encoding='utf-8')) if manifest.is_file() else []
        completed = [record for record in existing if record.get('image_type') == kind
                     and record.get('capture_method') == 'android_enlarged_screenshot_crop'
                     and record.get('page_counter') == [record.get('position'), total]
                     and saved_image(directory / record['local_path'])]
        if not detail and not self.image_limit and {record['position'] for record in completed} == set(range(1, total + 1)):
            print(f'[Android] 复用已保存主图 {total} 张', flush=True)
            return existing, total
        image = next((node for node in root.iter() if node.get('class') == 'android.widget.ImageView'
                      and node.get('content-desc') == title and node.get('clickable') == 'true'), None)
        if image is None and not detail:
            image = next((node for node in root.iter() if node.get('class') == 'android.widget.Image'
                          and node.get('text') == '商品大图' and node.get('clickable') == 'true'), None)
        if image is None and not detail:
            # Native carousel dispatches taps even when its children report clickable=false.
            pager = next((node for node in root.iter()
                          if node.get('package') == PACKAGE and node.get('class') in PAGER_CLASSES
                          and node.get('content-desc') == title), None)
            if pager is not None:
                image = next((node for node in pager.iter()
                              if node.get('class') == 'android.widget.ImageView'
                              and node.get('content-desc') == title
                              and bounds(node) == bounds(pager)), None)
        if not detail and not self.image_viewer(root, title):
            video = any(n.get('content-desc') == 'tronplayer_view' for n in root.iter())
            opened = False
            if image is not None and not video:
                x1, y1, x2, y2 = bounds(image)
                self.d.click((x1 + x2) / 2, (y1 + y2) / 2)
                time.sleep(.5)
                _, viewer = self.snapshot()
                counter_after = image_counter(viewer)
                opened = self.image_viewer(viewer, title)
            if not opened:
                self.check_title(title)
                if _entry_attempt >= min(total * 3, 30):
                    raise RuntimeError('主图多次点击或切换后仍未进入放大图')
                width, height = self.d.window_size()
                pager = next((n for n in root.iter() if n.get('class') in PAGER_CLASSES), None)
                y = (bounds(pager)[1] + bounds(pager)[3]) / 2 if pager is not None else width * .5
                self.d.swipe(width * .85, y, width * .15, y, duration=.2)
                time.sleep(.6)
                return self.images(directory, title, _entry_attempt=_entry_attempt + 1)
        records = [record for record in existing if record.get('image_type') not in ({'detail', 'detail_merged', 'detail_video'} if detail else {'main', 'main_video'})]
        def swipe():
            width, height = self.d.window_size()
            y = height * random.uniform(.45, .55)
            self.d.swipe(width * random.uniform(.8, .88), y,
                         width * random.uniform(.12, .2), y, duration=random.uniform(.12, .2))
            time.sleep(random.uniform(.5, 1))
        try:
            _, viewer = self.snapshot()
            opened_counter = image_counter(viewer)
            if not opened_counter or not 1 <= opened_counter[0] <= opened_counter[1] <= 100:
                raise RuntimeError('放大图片页码异常')
            # The product carousel includes videos that the gallery may omit.
            current, total = opened_counter
            # The gallery stops at the last image instead of wrapping around.
            for _ in range((current - 1) * 3):
                if current == 1:
                    break
                width, height = self.d.window_size()
                self.d.swipe(width * .15, height * .5, width * .85, height * .5, duration=.2)
                time.sleep(.6)
                _, first_page = self.snapshot()
                previous_counter = image_counter(first_page)
                if not previous_counter or previous_counter[1] != total or previous_counter[0] > current:
                    raise RuntimeError('放大图片回到首图时页码异常')
                current = previous_counter[0]
            if current != 1:
                raise RuntimeError('放大图片未能回到首图，停止滑动')
            seen_pages = set()
            count = min(total, self.image_limit) if self.image_limit else total
            for index in range(count):
                number = (current - 1 + index) % total + 1
                for page_attempt in range(3):
                    _, page = self.snapshot()
                    page_counter = image_counter(page)
                    if page_counter == [number, total]:
                        break
                    previous = (number - 2) % total + 1
                    if page_attempt == 2 or page_counter != [previous, total]:
                        raise RuntimeError(f'图片滑动后页码不一致：预期 {number}/{total}，实际 {page_counter}，已停止滑动')
                    print(f'[Android] 图片/视频页码未推进，重试滑动 {page_attempt + 2}/3', flush=True)
                    swipe()
                seen_pages.add(tuple(page_counter))
                if any(node.get('package') == PACKAGE and node.get('content-desc') == 'tronplayer_view'
                       for node in page.iter()):
                    if image_counter(page) != [number, total]:
                        raise RuntimeError('视频页码与预期不一致')
                    records.append({'image_type': kind + '_video', 'position': number, 'page_counter': [number, total], 'state': 'skipped'})
                    write_json(manifest, records)
                    print(f'[Android] 跳过轮播视频 {number}/{total}，不作为商品图片保存', flush=True)
                    if index + 1 < count:
                        swipe()
                    continue
                for attempt in range(3):
                    record = self.enlarged_image(directory, f'{folder}_{number:02d}.jpg')
                    if record['page_counter'] == [number, total]:
                        break
                    (directory / record['local_path']).unlink(missing_ok=True)
                    previous = (number - 2) % total + 1
                    if attempt == 2 or record['page_counter'] != [previous, total]:
                        raise RuntimeError('图片拖动后页码未正确变化，未保存重复或错位图片')
                    print(f'[Android] 图片页码未推进，重试滑动 {attempt+2}/3', flush=True)
                    swipe()
                record['position'] = number
                records.append(record)
                write_json(directory / '图片清单.json', records)
                if index + 1 < count:
                    swipe()
        finally:
            self.d.press('back')
            time.sleep(.4)
            self.check_title(title)
        if detail:
            from automation.pdd.spider.product_data import _merge_long_image
            paths = [directory / row['local_path'] for row in sorted(records, key=lambda row: row.get('position', 0)) if row.get('image_type') == 'detail']
            if len(paths) == total:
                _merge_long_image(paths, directory / '详情图片/长图.jpg')
                records.append({'image_type': 'detail_merged', 'position': 1, 'source_url': None, 'local_path': '详情图片/长图.jpg', 'state': 'completed'})
                write_json(manifest, records)
        image_count = sum(record.get('image_type') == kind for record in records)
        video_count = sum(record.get('image_type') == kind + '_video' for record in records)
        print(f'[Android] {"详情图" if detail else "主图"}：保存图片 {image_count} 张，跳过视频 {video_count} 项，轮播已处理 {image_count + video_count}/{total}', flush=True)
        return records, total

    @timed_operation('[Android] 采集 SKU')
    def sku(self, directory, title, records):
        self.retry_click(self.d(textMatches='.*免拼购买$|^(单独购买|发起拼单|立即购买)$'))
        rows = []
        try:
            self.sku_to_top()
            _, root = self.snapshot()
            if not any(node.get('content-desc') == '增加数量' for node in root.iter()):
                raise RuntimeError('未确认进入规格面板，停止规格操作')
            dims = self.all_sku_dimensions(root)
            (directory / 'SKU面板.xml').write_text(ET.tostring(root, encoding='unicode'), encoding='utf-8')
            write_json(directory / 'SKU规格.json', dims)
            if not dims:
                raise RuntimeError('未读取到可枚举的 SKU 规格')
            total = math.prod(len(dim['options']) for dim in dims)
            if total > 1000:
                raise RuntimeError('SKU 组合超过 1000，需核对规格结构')
            combinations = list(itertools.product(*(dim['options'] for dim in dims)))
            selected = combinations[:self.sku_limit] if self.sku_limit else combinations
            unavailable = []
            header = ['序号', '商品名称', '卷后价', *[f'SPU{i+1}' for i in range(len(dims))], '图片路径']
            saved = {}
            if (directory / 'SKU.csv').is_file():
                with (directory / 'SKU.csv').open(encoding='utf-8-sig', newline='') as stream:
                    saved = {row['序号']: row for row in csv.DictReader(stream)}
            for index, combination in enumerate(selected, 1):
                previous = saved.get(str(index))
                if previous and previous.get('卷后价') and all(previous.get(f'SPU{i+1}') == value.strip()
                        for i, value in enumerate(combination)) and saved_image(directory / previous.get('图片路径', '')):
                    rows.append([previous.get(key, '') for key in header])
                    print(f'[Android] 复用已保存 SKU {index}/{len(selected)}', flush=True)
                    continue
                try:
                    for option in combination:
                        selector = self.d(text=option, className='android.widget.TextView')
                        option_info = self.visible_sku_option(selector)
                        if not option_info.get('selected'):
                            for attempt in range(3):
                                self.retry_click(selector)
                                if selector.info.get('selected'):
                                    break
                                if attempt == 2:
                                    raise SkuNotSelectable(f'选项重试后仍不可选：{option.strip()}')
                                time.sleep(1)
                    _, root = self.snapshot()
                    summary = next((value for value in texts(root) if value.startswith('已选择')), '')
                    if summary and not all(sku_option_value(option) in sku_option_value(summary) for option in combination):
                        raise SkuNotSelectable('App 已选择的规格与此组合不一致')
                except SkuNotSelectable as error:
                    unavailable.append({'options': [value.strip() for value in combination], 'reason': str(error)})
                    write_json(directory / 'SKU不可选组合.json', unavailable)
                    print(f'[Android] SKU {index}/{len(selected)} 不可选，已记录并继续：{error}', flush=True)
                    continue
                price = ui_price(texts(root))
                if not price:
                    raise RuntimeError('当前 SKU 价格未读到，保留待重试')
                self.retry_click(self.d(description='查看大图'))
                try:
                    record = self.enlarged_image(directory, f'SKU/SKU_{index:03d}.jpg')
                    record['position'] = index
                    records[:] = [old for old in records if not (old.get('image_type') == 'sku' and old.get('position') == index)]
                    records.append(record)
                    write_json(directory / '图片清单.json', records)
                finally:
                    self.d.press('back')
                    time.sleep(.4)
                rows.append([index, '_'.join(value.strip() for value in combination), price,
                             *[value.strip() for value in combination], record['local_path']])
                write_csv(directory / 'SKU.csv', header, rows)
                print(f'[Android] SKU {index}/{len(selected)} 已保存', flush=True)
            if not rows:
                raise RuntimeError('没有成功读取任何可选 SKU，未标记完成')
            write_json(directory / 'SKU采集摘要.json', {'candidate_count': total, 'collected_count': len(rows),
                       'unselectable_count': len(unavailable), 'limited': bool(self.sku_limit)})
            return rows, total if self.sku_limit else len(rows)
        finally:
            if self.d(description='关闭').exists:
                self.d(description='关闭').click()
            else:
                self.d.press('back')
            time.sleep(.4)
            self.check_title(title)

    def all_sku_dimensions(self, root):
        dimensions = {}
        previous = None
        for _ in range(20):
            for dim in sku_dimensions(root):
                options = dimensions.setdefault(dim['name'], [])
                options.extend(option for option in dim['options'] if option not in options)
            scrolls = [n for n in root.iter() if n.get('package') == PACKAGE
                       and n.get('scrollable') == 'true']
            if not scrolls:
                break
            signature = tuple(ET.tostring(scroll, encoding='unicode') for scroll in scrolls)
            if signature == previous:
                break
            previous = signature
            region, can_scroll = self.sku_region()
            if not can_scroll:
                break
            width, height = self.d.window_size()
            confirm = self.d(text='确定')
            bottom = min(region['bottom'], confirm.info['bounds']['top'] if confirm.exists else height) - 50
            top = region['top'] + 60
            if bottom - top < 150:
                break
            self.d.swipe(width * .7, bottom, width * .7, top, duration=.25)
            time.sleep(.3)
            _, root = self.snapshot()
        else:
            raise RuntimeError('SKU 选项扫描超过上限，未使用不完整规格组合')
        self.sku_to_top()
        return [{'name': name, 'options': options} for name, options in dimensions.items()]

    def visible_sku_option(self, selector):
        try:
            info = selector.info
            if info.get('selected'):
                return info
        except Exception:
            info = None
        width, height = self.d.window_size()
        scroll, can_scroll = self.sku_region()
        top = scroll['top'] + 30
        confirm = self.d(text='确定')
        bottom = (confirm.info['bounds']['top'] if confirm.exists else min(scroll['bottom'], height)) - 30
        if bottom - top < 150:
            raise RuntimeError('规格列表可滚动区域不足，未点击确认按钮')
        box = info.get('bounds', {}) if info else {}
        if box.get('top', -1) >= top and box.get('bottom', height) <= bottom and box.get('bottom', 0) - box.get('top', 0) >= 25:
            return info
        # Only reset the list when this option is outside the visible safe area.
        if can_scroll:
            self.sku_to_top()
        for _ in range(12):
            try:
                info = selector.info
            except Exception:
                info = None
            box = info.get('bounds', {}) if info else {}
            if box.get('top', -1) >= top and box.get('bottom', height) <= bottom and box.get('bottom', 0) - box.get('top', 0) >= 25:
                return info
            if not can_scroll:
                raise SkuNotSelectable('静态规格面板中未找到安全可见的选项')
            self.d.swipe(width * .7, bottom - 40, width * .7, top + 40, duration=.25)
            time.sleep(.3)
        raise RuntimeError('无法将 SKU 选项滚入安全可见范围，未点击遮挡区域')

    def sku_region(self):
        try:
            scroll = self.d(className='android.widget.ScrollView', scrollable=True)
            if not scroll.exists:
                raise LookupError('规格面板没有 ScrollView')
            return scroll.info['bounds'], True
        except Exception:
            _, root = self.snapshot()
            if not any(n.get('content-desc') == '增加数量' for n in root.iter()):
                raise RuntimeError('未确认进入规格面板，无法定位选项区域')
            width, height = self.d.window_size()
            headings = [bounds(n)[1] for n in root.iter()
                        if n.get('text') in {dim['name'] for dim in sku_dimensions(root)}]
            confirms = [bounds(n)[1] for n in root.iter() if n.get('text') == '确定']
            if not headings or not confirms:
                raise RuntimeError('无法确认静态规格面板的选项边界')
            return {'left': 0, 'top': min(headings) - 30,
                    'right': width, 'bottom': min(confirms)}, False

    def sku_to_top(self):
        width, height = self.d.window_size()
        region, can_scroll = self.sku_region()
        if not can_scroll:
            return
        confirm = self.d(text='确定')
        bottom = (confirm.info['bounds']['top'] if confirm.exists else min(region['bottom'], height)) - 50
        for _ in range(3):
            self.d.swipe(width * .7, region['top'] + 60, width * .7, bottom, duration=.25)
        time.sleep(.3)

    def _write_reviews(self, directory, base, rows):
        write_json(directory / f'{base}原始.json', rows)
        if base == '评论':
            write_csv(directory / f'{base}.csv', COMMENT_HEADER, _comment_rows(rows))
        else:
            tag_key = 'positive_tags' if base == '好评' else 'negative_tags'
            write_csv(directory / f'{base}.csv', COMMENT_HEADER + ['标签', '分类依据'],
                      [values + [' / '.join(row.get(tag_key, [])), row.get('classification_basis', '')]
                       for values, row in zip(_comment_rows(rows), rows)])

    def review_metadata(self, row):
        rectangle = row.pop('_menu_bounds', None)
        if not rectangle:
            return row
        opened = False
        for attempt in range(3):
            x1, y1, x2, y2 = rectangle
            self.d.click((x1 + x2) / 2, (y1 + y2) / 2)
            for _ in range(3):
                time.sleep(.5)
                _, menu = self.snapshot()
                labels = texts(menu)
                cancel = next((node for node in menu.iter() if node.get('package') == PACKAGE
                               and (node.get('text') == '取消' or node.get('content-desc') == '取消')), None)
                if cancel is not None:
                    opened = True
                    break
            if opened:
                break
            # Never retry a stale screen coordinate on an unrelated page.
            visible = review_rows(menu, controls=True)
            same = next((item for item in visible if
                         (row.get('ui_fingerprint') and item['ui_fingerprint'] == row['ui_fingerprint'])
                         or (item.get('name') == row.get('name') and item.get('comment') == row.get('comment')
                             and item.get('specs') == row.get('specs'))), None)
            if same is None:
                raise RuntimeError('评论菜单未打开，且未确认仍在原评论列表，停止后续点击')
            rectangle = same.get('_menu_bounds')
            if rectangle is None or attempt == 2:
                row['date_status'] = 'unavailable'
                row['date_error'] = '三点菜单未打开，保留评论，日期待补充'
                print('[Android] 评论日期未读取，保留评论并继续', flush=True)
                return row
            print(f'[Android] 评论菜单未打开，重新定位后重试 {attempt + 2}/3', flush=True)
        try:
            value = next((value for value in labels if '发表时间' in value), '')
            match = re.search(r'(\d{4})[.年/-](\d{1,2})[.月/-](\d{1,2})', value)
            if match:
                from datetime import date
                row['评论日期'] = date(*map(int, match.groups())).isoformat()
                row['date_source'] = 'android_review_menu'
        finally:
            if cancel.get('text') == '取消':
                self.d(text='取消').click()
            else:
                self.d(description='取消').click()
            time.sleep(.4)
        return row

    @timed_operation('[Android] 采集当前评价列表')
    def _review_batch(self, directory, base, limit, *, tag=None):
        seen, rows, stalled = set(), [], 0
        if limit == 0:
            self._write_reviews(directory, base, [])
            return [], 'end'
        for _ in range(100):
            xml, root = self.snapshot()
            before = len(rows)
            for row in review_rows(root, controls=True):
                if not row.get('name'):
                    continue
                if row['ui_fingerprint'] not in seen:
                    if len(rows) >= limit:
                        break
                    row = self.review_metadata(row)
                    seen.add(row['ui_fingerprint'])
                    if tag:
                        key = 'positive_tags' if tag['kind'] == 'positive' else 'negative_tags'
                        row.update({key: [tag['name']], 'classification_basis': tag['basis'],
                                    'review_type': tag['kind']})
                    rows.append(row)
            rows = rows[:limit]
            self._write_reviews(directory, base, rows)
            (directory / f'{base}页面.xml').write_text(xml, encoding='utf-8')
            if len(rows) >= limit:
                return rows, 'limit'
            if any(any(mark in value for mark in ('没有更多', '已经到底', '暂无评价', '暂无评论', '暂无相关评价')) for value in texts(root)):
                return rows, 'end'
            stalled = stalled + 1 if len(rows) == before else 0
            if stalled >= 3:
                raise RuntimeError(f'{base}连续三页无新增，仅保存 {len(rows)} 条，保留待重试')
            self.d.swipe_ext('up', scale=.65)
            time.sleep(random.uniform(.5, 1))
        raise RuntimeError(f'{base}超过分页上限，已保存进度')

    def _review_top(self):
        self.top()
        self.d(description='展开', className='android.widget.ImageView').click_exists(timeout=.3)
        time.sleep(.3)

    def _select_review_filter(self, label):
        self._review_top()
        self.retry_click(self.d(text=label))
        _, root = self.snapshot()
        nodes = [node for node in root.iter() if node.get('package') == PACKAGE
                 and (node.get('content-desc') or node.get('text') or '').strip() == label]
        image = self.d.screenshot()
        for node in nodes:
            color = tag_background(image, bounds(node))
            if node.get('selected') == 'true' or (color and color[0] >= 160 and
                    color[0] - color[1] >= 80 and color[0] - color[2] >= 80):
                time.sleep(.5)
                return
        raise RuntimeError(f'未确认评价筛选已选中：{label}，未将旧列表标记为该类别')

    def _classified_reviews(self, directory, filters, kind):
        base = '好评' if kind == 'positive' else '差评'
        stage = 'positive_comments' if kind == 'positive' else 'negative_comments'
        tags = [tag for tag in filters if tag['kind'] == kind]
        # An explicit whole-category filter takes precedence over overlapping tags.
        explicit = [tag for tag in tags if tag['basis'] == 'explicit_app_filter']
        if explicit:
            tags = explicit[:1]
        self._write_reviews(directory, base, [])
        if not tags:
            self.update_stage(directory, stage, 'skipped', f'当前 App 未提供{base}筛选入口，不能据此认定没有{base}',
                         classification='app_filter', count=0, limit=self.comment_limit)
            return []
        merged = {}
        try:
            for tag in tags:
                remaining = self.comment_limit - len(merged)
                if remaining <= 0:
                    break
                self._select_review_filter(tag['label'])
                target = min(self.comment_limit, tag['count']) if tag['count'] is not None else self.comment_limit
                # Keep the aggregate file intact while individual filters are collected.
                batch_dir = directory / '评价筛选' / (sanitize(tag['label'])[:60] + '-' + hashlib.sha256(tag['label'].encode()).hexdigest()[:6])
                batch_dir.mkdir(parents=True, exist_ok=True)
                batch, _ = self._review_batch(batch_dir, base, target, tag=tag)
                key = 'positive_tags' if kind == 'positive' else 'negative_tags'
                for row in batch:
                    if row['ui_fingerprint'] not in merged and len(merged) >= self.comment_limit:
                        continue
                    previous = merged.setdefault(row['ui_fingerprint'], row)
                    previous[key] = list(dict.fromkeys([*previous.get(key, []), tag['name']]))
                self._write_reviews(directory, base, list(merged.values()))
            rows = list(merged.values())
            self.update_stage(directory, stage, 'completed', count=len(rows), limit=self.comment_limit,
                         tags=[tag['name'] for tag in tags], classification='app_filter_or_tag',
                         coverage='limit' if len(rows) >= self.comment_limit else 'visible_tags')
            print(f'[Android] {base}已保存 {len(rows)} 条', flush=True)
            return rows
        except Exception as error:
            self.update_stage(directory, stage, 'failed', str(error), count=len(merged))
            raise

    @timed_operation('[Android] 采集评价')
    def comments(self, directory, title):
        opened = False
        for _ in range(20):
            _, root = self.snapshot()
            if any(re.fullmatch(r'全部[（(][\d,]+[)）]', value) for value in texts(root)):
                opened = True
                break
            parents = {child: parent for parent in root.iter() for child in parent}
            entry = next((node for node in root.iter() if node.get('text') == '查看全部' and any(
                '商品评价' in (value.get('text', '') + value.get('content-desc', ''))
                for value in parents.get(node, root).iter())), None)
            if entry is not None:
                x1, y1, x2, y2 = bounds(entry)
                self.d.click((x1 + x2) / 2, (y1 + y2) / 2)
                time.sleep(.5)
                opened = True
                break
            self.d.swipe_ext('up', scale=.55)
            time.sleep(.5)
        if not opened:
            raise RuntimeError('未找到商品评价入口，未将缺失评论标记为空评论')
        try:
            self._review_top()
            _, root = self.snapshot()
            filters = review_filters(root, self.d.screenshot())
            write_json(directory / '评价标签.json', [tag for tag in filters if tag['kind'] == 'negative'])
            self.update_stage(directory, 'comments', 'skipped', '按配置仅采集差评')
            self.update_stage(directory, 'positive_comments', 'skipped', '按配置仅采集差评')
            rows = self._classified_reviews(directory, filters, 'negative')
            coverage = 'limit' if len(rows) >= self.comment_limit else 'visible_tags'
            return rows, coverage
        finally:
            self.d.press('back')
            time.sleep(.4)
            self.check_title(title)

    def mark_sold_out(self, directory, xml, root):
        image = self.d.screenshot().convert('RGB')
        if not sold_out_notice(root, image):
            return False
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        image.save(directory / '售罄页面.jpg', quality=95)
        (directory / '售罄页面.xml').write_text(xml, encoding='utf-8')
        self.update_stage(directory, 'product', 'skipped', SOLD_OUT_REASON,
                          source='android_native_ui', outcome='sold_out', retryable=False, sold_out_attempts=1)
        print(f'[Android] 商品已售罄，跳过：{directory}', flush=True)
        return True

    def mark_webview(self, directory, xml, reason):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'WebView页面.xml').write_text(xml, encoding='utf-8')
        self.d.screenshot().convert('RGB').save(directory / 'WebView页面.jpg', quality=95)
        self.update_stage(directory, 'product', 'blocked_webview', str(reason), source='android_native_ui')
        print(f'[Android] 空 WebView 已标记待统一处理：{directory}', flush=True)
        return directory

    def stage_complete(self, directory, stage, *, entry=None):
        if entry is None:
            entry = (self.store.stage(self.task['ranking_list_id'], stage, self.task['id'])
                     if hasattr(self, 'store') else load_status(directory).get('stages', {}).get(stage, {}))
        if entry.get('state') != 'completed':
            return False
        try:
            if stage == 'product_info':
                return bool(json.loads((directory / '产品信息.json').read_text(encoding='utf-8')).get('goods_name'))
            if stage in {'main_images', 'detail_images'}:
                kind = 'main' if stage == 'main_images' else 'detail'
                records = json.loads((directory / '图片清单.json').read_text(encoding='utf-8'))
                images = [r for r in records if r.get('image_type') == kind]
                videos = [r for r in records if r.get('image_type') == kind + '_video']
                expected = entry.get('expected')
                return (bool(images) and isinstance(expected, int)
                        and {r['position'] for r in images + videos} == set(range(1, expected + 1))
                        and all(saved_image(directory / r['local_path']) for r in images))
            if stage == 'sku':
                with (directory / 'SKU.csv').open(encoding='utf-8-sig', newline='') as stream:
                    rows = list(csv.DictReader(stream))
                return bool(rows) and len(rows) == entry.get('count') and all(
                    r.get('卷后价') and saved_image(directory / r['图片路径']) for r in rows)
            if stage == 'negative_comments':
                rows = json.loads((directory / '差评原始.json').read_text(encoding='utf-8'))
                return isinstance(rows, list) and len(rows) == entry.get('count') and (directory / '差评.csv').is_file()
        except (OSError, ValueError, KeyError, TypeError):
            return False
        return False

    def collect_current(self, directory=None, *, steps=None):
        selected = set(steps) if steps is not None else {'image_main', 'image_detail', 'sku', 'comment'}
        if not selected or selected - {'image_main', 'image_detail', 'sku', 'comment'}:
            raise ValueError('无效的原生商品采集项目')
        full = selected == {'image_main', 'image_detail', 'sku', 'comment'}
        xml, root = self.snapshot()
        if '确认款式' in texts(root) and any(node.get('content-desc') == '增加数量' for node in root.iter()):
            self.retry_click(self.d(description='关闭', className='android.widget.ImageView'))
            xml, root = self.snapshot()
        # Current may be the review overlay belonging to the same product.
        if selected != {'comment'} and any(re.fullmatch(r'全部[（(][\d,]+[)）]', value) for value in texts(root)):
            title = product_info(root)['goods_name']
            self.d.press('back')
            time.sleep(.4)
            self.check_title(title)
        self.top()
        xml, root = self.snapshot()
        try:
            info = product_info(root)
        except RuntimeError as error:
            directory = directory or config.OUTPUT_ROOT / '当前商品' / today() / ('未识别商品-' + time.strftime('%H%M%S'))
            if self.mark_sold_out(directory, xml, root):
                return Path(directory)
            if isinstance(error, UnreadableWebView):
                return self.mark_webview(directory, xml, error)
            raise
        title = info['goods_name']
        directory = Path(directory) if directory else config.OUTPUT_ROOT / '当前商品' / today() / (sanitize(title)[:70] + '-' + hashlib.sha256(title.encode()).hexdigest()[:8])
        directory.mkdir(parents=True, exist_ok=True)
        info_complete = self.stage_complete(directory, 'product_info')
        (directory / '商品页面.xml').write_text(xml, encoding='utf-8')
        if full and not info_complete:
            self.d.screenshot().convert('RGB').save(directory / '产品页截图.jpg', quality=95)
        # The verified current page is already available; refresh basic info in
        # place while retaining all independent image/SKU/review checkpoints.
        write_json(directory / '产品信息.json', info)
        if hasattr(self, 'store'):
            self.store.refresh_product_info(self.task['id'], info)
        self.update_stage(directory, 'product_info', 'completed', source='android_native_ui')
        stage, partial, metadata = 'product_info', False, {}
        try:
            manifest = directory / '图片清单.json'
            records = json.loads(manifest.read_text(encoding='utf-8')) if manifest.is_file() else []
            if 'image_main' in selected and not self.stage_complete(directory, 'main_images'):
                stage = 'main_images'
                records, main_total = self.images(directory, title)
                count = sum(record.get('image_type') in {'main', 'main_video'} for record in records)
                partial |= count < main_total
                self.update_stage(directory, stage, 'completed' if count == main_total else 'failed', count=count, expected=main_total)
                metadata['image_total'] = main_total
            if 'sku' in selected and not self.stage_complete(directory, 'sku'):
                stage = 'sku'
                sku, sku_total = self.sku(directory, title, records)
                partial |= len(sku) < sku_total
                self.update_stage(directory, stage, 'completed' if len(sku) == sku_total else 'failed', count=len(sku), expected=sku_total)
                metadata['sku_total'] = sku_total
            if 'comment' in selected and not self.stage_complete(directory, 'negative_comments'):
                stage = 'comments'
                comments, coverage = self.comments(directory, title)
                metadata.update(comments_coverage=coverage, negative_comment_count=len(comments), comment_limit=self.comment_limit)
            if 'image_detail' in selected and not self.stage_complete(directory, 'detail_images'):
                stage = 'detail_images'
                records, total = self.images(directory, title, detail=True)
                count = sum(row.get('image_type') in {'detail', 'detail_video'} for row in records)
                partial |= count < total
                self.update_stage(directory, stage, 'completed' if count == total else 'failed', count=count, expected=total)
            self.update_stage(directory, 'product' if full else 'native_selection', 'failed' if partial else 'completed',
                         '按调试数量上限部分采集' if partial else '', source='android_native_ui',
                         image_method='enlarged_screenshot_crop', selected_steps=sorted(selected), **metadata)
            print(f'[Android] 当前商品已保存：{directory}；项目：{" / ".join(sorted(selected))}', flush=True)
            return directory
        except Exception as error:
            self.update_stage(directory, stage, 'failed', str(error), source='android_native_ui')
            if full:
                self.update_stage(directory, 'product', 'failed', str(error), source='android_native_ui')
            raise

    def collect(self, task):
        from automation.pdd.spider.product import artifacts
        row = json.loads(task['row_json'])
        url = urlsplit(row['产品链接'])
        if url.scheme not in {'http', 'https'} or not any(
                url.hostname == domain or (url.hostname or '').endswith('.' + domain)
                for domain in ('pinduoduo.com', 'yangkeduo.com')):
            raise ValueError('原生商品链接不是受支持的拼多多地址')
        from automation.pdd.spider.android_jump import jump
        directory = Path(task['directory']) / f"榜{task['rank']:02d}"
        jump(row['产品链接'], directory / '网页跳转', close_page=True)
        error = None
        for attempt in range(10):
            root = None
            try:
                xml, root = self.snapshot()
                product_info(root)
                break
            except RuntimeError as failure:
                if root is not None and self.mark_sold_out(directory, xml, root):
                    return 'skipped', artifacts(directory)
                error = failure
            if attempt == 9:
                if isinstance(error, UnreadableWebView):
                    self.mark_webview(directory, xml, error)
                    return 'blocked_webview', artifacts(directory)
                raise error
            time.sleep(.7)
        self.collect_current(directory)
        details = artifacts(directory)
        product_state = details.get('status', {}).get('stages', {}).get('product', {}).get('state')
        state = product_state if product_state in {'skipped', 'blocked_webview'} else 'pending' if product_state == 'failed' else 'completed'
        return state, details
