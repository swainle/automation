"""Product elements and response bodies are read by the Sogou extension."""
from automation.pdd.spider.logging_setup import operation as log_operation, timed_operation
import itertools
import json
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from automation.pdd.spider.artifacts import write_json
from automation.pdd.spider.comments import COMMENT_HEADER, _comment_rows
from automation.pdd.spider.product_data import _clean_sku_text, _sku_price, listing_date_from_image_url, _merge_long_image, mark_sold_out
from automation.pdd.spider.status import stage_done, update_stage, reconcile_product, product_done, load_status, set_listing_date
from automation.pdd.spider.utils import download, sanitize, write_csv, refresh_listing_dates


class ExtensionProduct:
    def __init__(self, runtime, *, wait=time.sleep, on_identity=None, on_image=None):
        self.runtime, self.wait = runtime, wait
        self.tab = None
        self.on_identity = on_identity
        self.on_image = on_image

    @timed_operation('[商品] 核验商品身份')
    def resolve_identity(self, row):
        from automation.pdd.spider.product_info import valid_goods_id, goods_id_from_url, discover_goods_id
        known = valid_goods_id(row.get('产品ID'))
        linked = goods_id_from_url(row['产品链接'])
        if known and linked and known != linked:
            raise RuntimeError('商品链接中的 ID 与榜单不一致')
        known = known or linked
        for attempt in range(6):
            page = self.dom('info')
            current = goods_id_from_url(page['url'])
            if known and current and known != current:
                raise RuntimeError('商品跳转后的 ID 与榜单不一致')
            goods_id = known or current
            if not goods_id:
                goods_id = discover_goods_id(self.rpc('product_network', kind='info')['responses'])
            if goods_id:
                row['产品ID'] = goods_id
                if self.on_identity:
                    self.on_identity(goods_id)
                return page, goods_id
            if page['sold_out']:
                return page, ''
            if attempt < 5:
                self.wait(.5)
        raise RuntimeError('暂未从链接、跳转页面或商品接口取得真实商品 ID，保留待重试')

    def rpc(self, action, **params):
        return self.runtime.ranking_request(action, {'tab_id': self.tab, **params})

    def dom(self, operation, **params):
        return self.rpc('product_dom', operation=operation, **params)

    @timed_operation('[商品] 获取商品信息')
    def information(self, goods_id):
        from automation.pdd.spider.product_info import ProductInfo
        capture = ProductInfo(goods_id)
        for item in self.rpc('product_network', kind='info')['responses']:
            response = SimpleNamespace(url=item['url'], status=item['status'],
                request=SimpleNamespace(resource_type=item['type'].lower(), post_data_json=item.get('body')),
                json=lambda data=item['data']: data)
            capture.receive(response)
        return capture.finish() if capture.responses else None

    def record_image(self, directory, record):
        manifest = directory / '图片清单.json'
        rows = json.loads(manifest.read_text(encoding='utf-8')) if manifest.exists() else []
        rows = [row for row in rows if (row['image_type'], row['position']) != (record['image_type'], record['position'])]
        rows.append(record)
        write_json(manifest, rows)
        if self.on_image:
            self.on_image(record)

    def begin_images(self, directory, image_type):
        manifest = directory / '图片清单.json'
        rows = json.loads(manifest.read_text(encoding='utf-8')) if manifest.exists() else []
        write_json(manifest, [row for row in rows if row['image_type'] != image_type])

    def images_recorded(self, directory, image_type):
        manifest = directory / '图片清单.json'
        if not manifest.exists():
            return False
        records = [row for row in json.loads(manifest.read_text(encoding='utf-8')) if row['image_type'] == image_type]
        return bool(records) and all(row.get('source_url') and row['state'] == 'completed'
                                     and (directory / row['local_path']).is_file() for row in records)

    def download_image(self, directory, image_type, position, url, relative):
        from PIL import Image
        record = {'image_type': image_type, 'position': position, 'source_url': url,
                  'local_path': relative, 'state': 'pending', 'last_error': ''}
        self.record_image(directory, record)
        try:
            if not download(url, directory / relative):
                raise RuntimeError('图片下载失败')
            with Image.open(directory / relative) as image:
                image.verify()
        except Exception as error:
            record.update(state='failed', last_error=str(error))
            self.record_image(directory, record)
            raise RuntimeError(f'{image_type} 图片 {position} 下载或校验失败：{error}') from error
        record['state'] = 'completed'
        self.record_image(directory, record)

    @timed_operation('[商品] 采集主图')
    def images(self, directory, row):
        result = self.dom('images')
        urls = list(dict.fromkeys(result['urls']))
        if not urls:
            raise RuntimeError('插件未读取到主图，保留待采')
        if result.get('verification') != 'counter-drag-v1':
            raise RuntimeError('请重新加载搜狗插件：主图采集需要页码和真实拖拽校验')
        expected = result.get('expected')
        if not isinstance(expected, int) or expected < 1 or set(result.get('visited_pages') or []) != set(range(1, expected + 1)):
            raise RuntimeError('主图页码未全部遍历，保留待重试')
        if result.get('complete') is False:
            raise RuntimeError(f'主图尚未加载完整：读取 {len(urls)} 张，预期 {result.get("expected")} 张')
        self.begin_images(directory, 'main')
        print(f'[商品] 已核验主图 {expected} 页，取得 {len(urls)} 个图片 URL')
        value = listing_date_from_image_url(urls[0])
        set_listing_date(directory, value)
        row['上架日期'] = value
        for number, url in enumerate(urls, 1):
            self.download_image(directory, 'main', number, url, f'主图/主图_{number:02d}.jpg')
        update_stage(directory, 'main_images', 'completed', count=len(urls),
                     verification=result['verification'], expected=result.get('expected'), visited_pages=result.get('visited_pages'))
        refresh_listing_dates(directory.parent / '榜单数据.xlsx')

    @timed_operation('[商品] 采集 SKU')
    def sku(self, directory):
        self.begin_images(directory, 'sku')
        raw = self.dom('sku_dimensions')['dimensions']
        dimensions = []
        for dim in raw:
            name = _clean_sku_text(dim['name'])
            options = [{**option, 'text': _clean_sku_text(option['text'])} for option in dim['options']]
            options = [option for option in options if option['text']]
            if name and options:
                dimensions.append({**dim, 'name': name, 'options': options})
        combinations = itertools.product(*(dim['options'] for dim in dimensions)) if dimensions else []
        rows = []
        try:
            for number, combination in enumerate(combinations, 1):
                selections = [{'dimension': dim['index'], 'option': option['index']} for dim, option in zip(dimensions, combination)]
                for attempt in range(2):
                    try:
                        result = self.dom('sku_select', selections=selections)
                        break
                    except Exception:
                        if attempt:
                            raise
                labels = [option['text'] for option in combination]
                filename = 'SKU_' + '_'.join([f'{number:02d}', *(sanitize(label) for label in labels)]) + '.jpg'
                self.download_image(directory, 'sku', number, result['image'], 'SKU/' + filename)
                rows.append([number, '_'.join(labels), _sku_price(result['price']), *labels])
            write_csv(directory / 'SKU.csv', ['序号', '商品名称', '卷后价', *(f'SPU{index}' for index in range(1, len(dimensions) + 1)), '图片路径'],
                      [row + ['SKU/' + 'SKU_' + '_'.join([f'{row[0]:02d}', *(sanitize(label) for label in row[3:])]) + '.jpg'] for row in rows])
        finally:
            self.dom('sku_close')
        update_stage(directory, 'sku', 'completed' if rows else 'skipped', '' if rows else '未找到可枚举的 SKU')

    @timed_operation('[商品] 采集详情图')
    def details(self, directory):
        self.begin_images(directory, 'detail')
        self.begin_images(directory, 'detail_merged')
        urls = self.dom('details')['urls']
        paths = []
        for number, url in enumerate(urls, 1):
            path = directory / f'详情图片/{number:02d}.jpg'
            self.download_image(directory, 'detail', number, url, f'详情图片/{number:02d}.jpg')
            paths.append(path)
        if paths:
            _merge_long_image(paths, directory / '详情图片/长图.jpg')
            if not (directory / '详情图片/长图.jpg').is_file():
                raise RuntimeError('详情长图合成失败')
            self.record_image(directory, {'image_type': 'detail_merged', 'position': 1, 'source_url': None,
                                         'local_path': '详情图片/长图.jpg', 'state': 'completed', 'last_error': ''})
        update_stage(directory, 'details', 'completed' if paths else 'skipped', '' if paths else '页面未提供详情图')

    def review_batch(self, phase, limit, expected=None):
        self.wait(1.5)
        stalled, before = 0, -1
        result = {'received': False, 'comments': []}
        for _ in range(100):
            result = self.rpc('product_network', kind='reviews', phase=phase)
            count = len(result['comments'])
            if count >= limit or (result['received'] and (result.get('exhausted') or expected is not None and count >= min(expected, limit))):
                return result['comments'][:limit]
            stalled = stalled + 1 if count == before else 0
            if stalled >= 5:
                raise RuntimeError('评论分页未确认结束且数量不足，保留待重试')
            before = count
            self.dom('reviews_scroll')
            self.wait(1.5)
        if not result['received']:
            raise RuntimeError('未收到本轮评价接口，不能判定为无评价')
        raise RuntimeError('评论分页超出本轮等待次数，保留待重试')

    @timed_operation('[商品] 采集评价')
    def comments(self, directory):
        stages = load_status(directory).get('stages', {})
        update_stage(directory, 'comments', 'skipped', '按配置仅采集差评')
        negative = not stage_done(directory, 'negative_comments') or stages.get('negative_comments', {}).get('limit') != 100
        if not negative:
            return
        phase = self.rpc('product_reviews_begin')['phase']
        self.review_stage = 'negative_comments'
        try:
            self.dom('reviews_enter')
        except Exception:
            pass  # Full navigation can destroy the click script; poll without clicking again.
        for attempt in range(50):
            try:
                if self.dom('reviews_ready')['ready']:
                    break
            except Exception:
                pass
            self.wait(.2)
        else:
            raise RuntimeError('未进入评论列表，不能判定为无评价')
        self.review_stage = 'negative_comments'
        tags = self.dom('reviews_tags')
        names = list(dict.fromkeys(tags['names']))
        counts = {tag['name']: tag.get('count') for tag in tags.get('tags', [])}
        collected, seen, tag_counts = [], {}, {}
        write_json(directory / '差评原始.json', [])
        for name in names:
            if len(collected) >= 100:
                break
            phase = self.rpc('product_reviews_begin')['phase']
            self.dom('reviews_tag', name=name)
            batch = self.review_batch(phase, 100, expected=counts.get(name))
            tag_counts[name] = len(batch)
            for row in batch:
                identity = str(row['review_id'])
                if identity not in seen and len(collected) >= 100:
                    continue
                if identity not in seen:
                    seen[identity] = {**row, 'negative_tags': []}
                    collected.append(seen[identity])
                if name not in seen[identity]['negative_tags']:
                    seen[identity]['negative_tags'].append(name)
                seen[identity]['negative_reason'] = '；'.join(seen[identity]['negative_tags'])
            if collected:
                write_json(directory / '差评原始.json', collected)
                write_csv(directory / '差评.csv', COMMENT_HEADER + ['原因'],
                    [row + [item['negative_reason']] for row, item in zip(_comment_rows(collected), collected)])
        if not collected:
            (directory / '差评.csv').unlink(missing_ok=True)
        update_stage(directory, 'negative_comments', 'completed' if collected else 'skipped',
            '' if collected else '无灰色负面标签' if not names else '灰色标签接口返回空评价',
            count=len(collected), tags=names, tag_counts=tag_counts, per_tag_limit=100, limit=100)

    def collect(self, task):
        from automation.pdd.spider.product import artifacts
        row = json.loads(task['row_json'])
        directory = Path(task['directory']) / f"榜{task['rank']:02d}"
        directory.mkdir(parents=True, exist_ok=True)
        url = row.get('产品链接', '')
        if not url:
            raise RuntimeError('榜单缺少商品链接')
        stage = 'access'
        try:
            with log_operation(f"[商品] 排行 {task['rank']}：打开商品页"):
                opened = self.runtime.ranking_request('product_open', {'url': url})
                self.tab = opened['tab_id']
                self.wait(3)
            page, goods_id = self.resolve_identity(row)
            if page['sold_out']:
                mark_sold_out(directory, url, page['url'])
                if load_status(directory).get('stages', {}).get('product', {}).get('state') == 'skipped':
                    return 'skipped', artifacts(directory)
                raise RuntimeError('商品售罄，保留重试')
            if 'goods' not in page['url']:
                raise RuntimeError('商品页面访问失败或被风控')
            current_id = parse_qs(urlsplit(page['url']).query).get('goods_id', [''])[0]
            if current_id and current_id != str(goods_id):
                raise RuntimeError('商品跳转后的 ID 与榜单不一致')
            reconcile_product(directory, row, require_screenshot=False)
            update_stage(directory, 'access', 'completed')
            update_stage(directory, 'product', 'running', sold_out_attempts=0, retryable=False, outcome='available')
            info_path = directory / '产品信息.json'
            info = self.information(str(goods_id))
            if not info and current_id != str(goods_id):
                raise RuntimeError('无法确认当前商品身份，等待接口信息后重试')
            if info:
                write_json(info_path, info)
            for stage, operation in [('main_images', lambda: self.images(directory, row)),
                                     ('sku', lambda: self.sku(directory)), ('details', lambda: self.details(directory))]:
                image_type = {'main_images': 'main', 'sku': 'sku', 'details': 'detail'}[stage]
                skipped = load_status(directory).get('stages', {}).get(stage, {}).get('state') == 'skipped'
                verified = stage != 'main_images' or load_status(directory).get('stages', {}).get(stage, {}).get('verification') == 'counter-drag-v1'
                if not verified or not stage_done(directory, stage) or not skipped and not self.images_recorded(directory, image_type):
                    update_stage(directory, stage, 'running')
                    operation()
            stage = 'comments'
            self.comments(directory)
            stage = 'product_info'
            info = self.information(str(goods_id)) or (json.loads(info_path.read_text(encoding='utf-8')) if info_path.is_file() else None)
            if not info or str(info.get('goods_id')) != str(goods_id):
                raise RuntimeError('未监听到当前商品信息，保留待重试')
            write_json(info_path, info)
            reconcile_product(directory, row, require_screenshot=False)
            if not product_done(directory, require_screenshot=False):
                raise RuntimeError('商品产物未齐全，保留待重试')
            update_stage(directory, 'product', 'completed')
            return 'completed', artifacts(directory)
        except Exception as error:
            if stage == 'comments':
                stage = getattr(self, 'review_stage', stage)
            if stage != 'access' or not load_status(directory).get('stages', {}).get('product', {}).get('retryable'):
                update_stage(directory, stage, 'failed', str(error))
            raise
        finally:
            if self.tab is not None:
                try:
                    self.rpc('product_close')
                finally:
                    self.tab = None
