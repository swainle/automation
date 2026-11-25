"""Collect weekly mobile products and resume unfinished steps."""
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from statistics import median

from automation.pdd.spider import config, ios, longshot
from automation.pdd.spider.crop import save_product
from automation.pdd.spider.mobile_files import cleanup_ranking, merge_details
from automation.pdd.spider.mobile_page import WebviewElementSkip, require_elements, wait_for_elements
from automation.pdd.spider.database import today
from automation.pdd.spider.full_flow import (prepare_top, dismiss_sku_panel,
    product_page_checks, return_to_board, valid_image, ProductUnavailable, product_unavailable_reason)
from automation.pdd.spider.ios_product import IosProduct, label
from automation.pdd.spider.mobile_database import WEBVIEW_SKIP, week_start, product_complete, product_status, PRODUCT_COLUMNS
from automation.pdd.spider.rank import TAB_TO_CLASS, read_leaderboards
from automation.pdd.spider.rank.current import read_tab
from automation.pdd.spider.workflow import FatalWorkflowError
from automation.pdd.spider.utils import write_csv
from wda.usbmux.exceptions import HTTPError


def product_element(client, title, *, occurrence=1):
    # WDA displayed queries on offscreen WebView entries can block for minutes.
    xml = wait_for_elements(lambda: ios._current_page_source(client))
    nodes = [n for n in ET.fromstring(xml).iter('XCUIElementTypeStaticText')
             if n.get('name') == title or label(n) == title]
    if occurrence < 1 or len(nodes) < occurrence:
        raise RuntimeError(f'未找到排行商品标题（第 {occurrence} 次）：{title}')
    node = nodes[occurrence - 1]
    x, y, width, height = (float(node.get(k, 0)) for k in ('x', 'y', 'width', 'height'))
    screen_width, screen_height = ios._get_window_size(client)
    displayed = (node.get('visible') == 'true' and width > 0 and height > 0
                 and 0 <= x + width / 2 < screen_width and 0 <= y + height / 2 < screen_height - 50)
    def tap():
        elements = client(name=node.get('name') or label(node), className='StaticText').find_elements()
        if len(elements) < occurrence:
            raise RuntimeError('榜单标题元素发生变化，停止点击')
        elements[occurrence - 1].tap()
    return SimpleNamespace(displayed=displayed, tap=tap)


def locate_product(client, entry, rows, rank):
    """Use full-board coordinates to approach, then correct against live title positions."""
    title = next(row['标题'] for row in rows if int(row['排行']) == rank)
    occurrence = sum(row['标题'] == title for row in rows if int(row['排行']) <= rank)
    baseline = sorted(client.__dict__.get('_rank_top_elements', []), key=lambda e: e.get('order', 0))
    titles = {row['标题'] for row in rows}
    original = {}
    for element in baseline:
        if element['type'] == 'StaticText' and element['text'] in titles:
            original.setdefault(element['text'], []).append(element)
    if len(original.get(title, [])) < occurrence:
        raise RuntimeError(f'完整榜单中无法确认排名 {rank} 的标题位置')
    target = original[title][occurrence - 1]
    width, height = ios._get_window_size(client)
    last_offset, stalled = None, 0
    for _ in range(40):
        ios.require_pdd_foreground(client)
        xml = wait_for_elements(lambda: ios._current_page_source(client))
        elements = ios._xcui_texts(xml)
        visible = {order for order, node in enumerate(ET.fromstring(xml).iter()) if node.get('visible') == 'true'}
        main = ios._rank_details(elements, TAB_TO_CLASS[entry['tab']])['主榜单名称']
        if main != entry['main_name'] or ios._guess_tab(elements) != entry['tab']:
            raise FatalWorkflowError('定位期间榜单或子榜变化，停止点击')
        current = {}
        for element in sorted(elements, key=lambda e: e.get('order', 0)):
            if element['type'] == 'StaticText' and element['text'] in titles:
                current.setdefault(element['text'], []).append(element)
        offsets = []
        for text, nodes in current.items():
            old = original.get(text, [])
            if len(nodes) == len(old):
                offsets.extend(a['y'] - b['y'] for a, b in zip(old, nodes)
                               if b['order'] in visible and b['w'] > 0 and b['h'] > 0 and 0 <= b['x'] < width
                               and height * .15 < b['y'] < height * .85)
        if not offsets:
            raise RuntimeError('可见范围没有可对应的榜单标题，停止盲目滑动')
        offset = median(offsets)
        print(f'[定位] 排名 {rank}，当前滚动位置 {offset:.0f}pt', flush=True)
        matches = current.get(title, [])
        node = matches[occurrence - 1] if len(matches) == len(original[title]) else None
        if node and node['order'] in visible and node['w'] > 0 and node['h'] > 0 and 0 <= node['x'] < width and (
                height * .2 <= node['y'] and node['y'] + node['h'] <= height * .8):
            def tap():
                if not ios._tap_xy(client, node['x'] + node['w'] / 2,
                                   node['y'] + node['h'] / 2, f'排行 {rank}：{title}'):
                    raise RuntimeError('点击排行商品失败')
            return SimpleNamespace(displayed=True, tap=tap)
        stalled = stalled + 1 if last_offset is not None and abs(offset - last_offset) < 3 else 0
        if stalled >= 2:
            raise RuntimeError('连续滑动无位移，未定位到目标商品')
        delta = target['y'] - offset - height * .45
        limit = height * (.65 if abs(delta) > height * .7 else .18)
        travel = max(-limit, min(limit, delta))
        if abs(travel) < 3:
            raise RuntimeError('目标位置与当前标题不一致，停止点击')
        start = height * (.82 if travel > 0 else .17)
        longshot.drag_for_screenshot(client, width / 2, start, start - travel)
        time.sleep(.25)
        last_offset = offset
    raise RuntimeError('超过定位滑动次数，未点击商品')


class WeeklyProduct(IosProduct):
    save_diagnostics = False

    def comments(self):
        super().comments()
        if not self.negative:
            write_csv(self.directory / '差评.csv', ['用户', '评论内容', 'SKU', '原因', '评论日期'], [])

    def details(self):
        super().details()
        paths = [self.directory / r['local_path'] for r in self.images if r['image_type'] == 'detail']
        merge_details(paths, self.directory / '详情/长图.jpg')

    def find(self, pattern, *, kind=None):
        if getattr(self, '_source_error', None) is not None:
            raise self._source_error
        return super().find(pattern, kind=kind)

    def tap(self, node):
        if getattr(self, '_source_error', None) is not None:
            raise self._source_error
        return super().tap(node)

    def snapshot(self, *, initial_xml=None):
        self._source_error = None
        def read():
            self.source_reads += 1
            self.xml = ios._current_page_source(self.client)
            if self.xml:
                self.root = ET.fromstring(self.xml)
            return self.xml
        try:
            self.xml = wait_for_elements(read, initial_xml=initial_xml)
            reason = product_unavailable_reason(self.xml)
            if reason:
                raise ProductUnavailable(reason)
            self.root = ET.fromstring(self.xml)
            self.dismiss_parameters()
        except Exception as error:
            self._source_error = error
            raise
        return self.root

    def collect(self, item, directory, row, *, initial_xml=None):
        self.item, self.directory = item, directory
        directory.mkdir(parents=True, exist_ok=True)
        saved = json.loads(self.store.item(item['id'])['product_json'])
        self.images = saved['images']
        self.sku_rows, self.negative = saved['sku'], saved['negative_comments']
        self.detail_hashes, self.source_reads = set(), 0
        self.xml = initial_xml
        self.root = self.snapshot(initial_xml=initial_xml)
        app = next(self.root.iter('XCUIElementTypeApplication'))
        self.width, self.height = float(app.get('width')), float(app.get('height'))
        info = {'goods_name': row.get('商品页标题') or row['标题'],
                'page_text': '\n'.join(label(n) for n in self.root.iter() if n.tag.endswith('StaticText'))}
        self.store.refresh_product_info(item['id'], info)
        actions = [('main_images', self.main_images), ('sku', self.skus),
                   ('detail_images', self.details), ('negative_comments', self.comments)]
        for stage, action in actions:
            if self.store.stage(item['list_id'], stage, item['id'])['state'] == 'completed':
                print(f'[iOS 商品] 复用已完成阶段：{stage}', flush=True)
                continue
            # Restart only the unfinished stage; preserve other completed stages.
            kind = {'main_images': 'main', 'sku': 'sku', 'detail_images': 'detail'}.get(stage)
            if kind:
                self.images = [r for r in self.images if r['image_type'] != kind]
                if kind != 'sku':
                    self.store.restart_images(item['id'], kind)
            self.store.stage_state(item['list_id'], stage, 'running', item_id=item['id'])
            print(f'[iOS 商品] 排行 {item["rank"]}：{stage}', flush=True)
            try:
                action()
                self.store.save_progress(item['id'], {'sku': self.sku_rows, 'negative_comments': self.negative})
            except BaseException as error:
                self.store.save_progress(item['id'], {'sku': self.sku_rows, 'negative_comments': self.negative})
                self.store.stage_state(item['list_id'], stage, 'failed', item_id=item['id'], reason=str(error))
                # Keep the confirmed read failure when viewer cleanup also fails.
                if self._source_error is not None:
                    raise self._source_error
                raise
            self.store.stage_state(item['list_id'], stage, 'completed', item_id=item['id'])
        self.store.product_state(item['id'], 'completed')


def capture_ranking(store, client, entry, rows, elements):
    list_id, directory = entry['id'], Path(entry['directory'])
    if not valid_image(directory / '榜单长图.jpg') or any(i['ranking_product_screenshot_state'] != 'completed' for i in store.items(list_id)):
        store.stage_state(list_id, 'ranking_long_screenshot', 'running')
        xml, elements = prepare_top(client, entry['main_name'], entry['tab'], rows, elements)
        xml = wait_for_elements(lambda: ios._current_page_source(client), initial_xml=xml)
        try:
            longshot.capture(client, rows, xml, directory, compact=True)
            client.__dict__.pop('_rank_last_elements', None)
        except Exception:
            store.stage_state(list_id, 'ranking_long_screenshot', 'failed')
            raise
        store.stage_state(list_id, 'ranking_long_screenshot', 'completed')
    for row in rows:
        if not valid_image(directory / f"榜{int(row['排行']):02d}/产品榜单截图.jpg"):
            raise RuntimeError(f"排行 {row['排行']} 缺少有效的榜单裁剪图")
    for item in store.items(list_id):
        store.stage_state(list_id, 'ranking_product_screenshot', 'completed', item_id=item['id'])


def run_products(store, client, entry, live_rows, *, only_webview=False, single_round=False, product_id=None, analysis=None):
    from automation.pdd.spider.full_flow import prepare_top as scroll_top
    list_id = entry['id']
    items = store.items(list_id)
    targets = [i for i in items if not product_complete(i)]
    if single_round:
        saved = {i['rank']: i for i in items}
        targets = [saved.get(int(row['排行']), dict(id=None, rank=int(row['排行']),
                   directory=str(Path(entry['directory']) / f"榜{int(row['排行']):02d}")))
                   for row in live_rows if (int(row['排行']) not in saved or not product_complete(saved[int(row['排行'])]))
                   and (product_id is None or saved.get(int(row['排行']), {}).get('id') == product_id)]
    if not targets:
        return
    live_by_rank = {int(r['排行']): r for r in live_rows}
    # Long-image capture can leave the page at the bottom. Navigation is necessary
    # on resume, but never repeats a completed data/image collection step.
    # Longshot capture invalidates its stale coordinates; a newly opened board
    # can reuse its fresh snapshot instead of another full XML read.
    if not single_round:
        scroll_top(client, entry['main_name'], entry['tab'], live_rows, client.__dict__.get('_rank_last_elements', []))
    collector = WeeklyProduct(store, client)
    pending = list(targets)
    last_errors = []
    for attempt in range(1 if single_round else config.RETRY_ATTEMPTS):
        errors = []
        for item in pending:
            row = live_by_rank.get(item['rank'])
            if row is None:
                raise RuntimeError(f"当前榜单找不到原排名 {item['rank']}，保留断点")
            title = row['标题']
            occurrence = sum(r['标题'] == title for r in live_rows if int(r['排行']) <= item['rank'])
            folder = Path(item['directory'])
            opened = False
            try:
                print(f"[商品] 排行 {item['rank']}：定位当前榜单标题", flush=True)
                element = (locate_product(client, entry, live_rows, item['rank']) if single_round
                           else product_element(client, title, occurrence=occurrence))
                if not element.displayed:
                    width, height = ios._get_window_size(client)
                    for _ in range(12):
                        elements = ios._xcui_texts(client.__dict__['_last_page_source'])
                        main = ios._rank_details(elements, TAB_TO_CLASS[entry['tab']])['主榜单名称']
                        if main != entry['main_name'] or ios._guess_tab(elements) != entry['tab']:
                            raise FatalWorkflowError('未确认原榜单和 Tab，停止商品定位')
                        longshot.drag_for_screenshot(client, width / 2, height * .75, height * .4)
                        time.sleep(.25)
                        element = product_element(client, title, occurrence=occurrence)
                        if element.displayed:
                            break
                    else:
                        raise RuntimeError('滚动后商品标题仍不可见')
                opened = True
                print(f"[商品] 排行 {item['rank']}：打开商品页", flush=True)
                element.tap()
                # 部分商品页打开后会自动弹出 SKU 面板，遮挡分享/返回等控件，
                # 需先关闭 SKU 面板再校验页面就绪；重商品页（如冻干主粮）加载
                # 元素树可能超过 120 秒，因此给足 180 秒等待。
                def page_ready(source):
                    try:
                        dismiss_sku_panel(client)
                    except RuntimeError:
                        pass
                    return not product_page_checks(source, title)[0]
                def read_product_source():
                    source = ios._current_page_source(client)
                    reason = product_unavailable_reason(source)
                    if reason:
                        raise ProductUnavailable(reason)
                    return source
                xml = wait_for_elements(read_product_source,
                                        timeout=180,
                                        ready=page_ready)
                reason = product_unavailable_reason(xml)
                if reason:
                    raise ProductUnavailable(reason)
                reasons, page = product_page_checks(xml, title)
                if reasons:
                    raise RuntimeError('未确认当前商品页：' + '；'.join(reasons))
                normalize = lambda value: re.sub(r'[\s\u200b【】]', '', value)
                if not any(normalize(title) in normalize(text) for text in page['title_candidates']):
                    raise RuntimeError(f'商品身份不符，停止保存：目标 {title}；实际 {page["observed_title"]}')
                if single_round:
                    if item['id'] is not None and normalize(title) not in normalize(item['title']):
                        from automation.pdd.spider.unified import reset_mobile
                        reset_mobile(store, item, row)
                        with store.db.transaction():
                            store.db.execute("""UPDATE t_pdd_ranking_products SET platform_product_id='',
                                platform_product_link='',listing_date=NULL WHERE id=%s""", (item['id'],))
                    item = store.save_item(list_id, entry['directory'], row)
                    crop_state = 'completed' if valid_image(folder / '产品榜单截图.jpg') else 'failed'
                    store.stage_state(list_id, 'ranking_product_screenshot', crop_state, item_id=item['id'])
                row = dict(row, 商品页标题=page['observed_title'] or title)
                screenshot = folder / '产品截图.jpg'
                if item['product_screenshot_state'] != 'completed':
                    store.stage_state(list_id, 'product_screenshot', 'running', item_id=item['id'])
                    save_product(client.screenshot().convert('RGB'), screenshot)
                    store.stage_state(list_id, 'product_screenshot', 'completed', item_id=item['id'])
                collector.collect(item, folder, row, initial_xml=xml)
                if analysis is not None:
                    analysis.process(store.item(item['id']))

            except ProductUnavailable as error:
                if single_round:
                    if item['id'] is not None and re.sub(r'[\s\u200b【】]', '', title) not in re.sub(r'[\s\u200b【】]', '', item['title']):
                        from automation.pdd.spider.unified import reset_mobile
                        reset_mobile(store, item, row)
                        store.db.execute("UPDATE t_pdd_ranking_products SET platform_product_id='',platform_product_link='',listing_date=NULL WHERE id=%s", (item['id'],))
                    item = store.save_item(list_id, entry['directory'], row)
                store.skip_product(item['id'], str(error))
                print(f"[商品] 排行 {item['rank']}：{error}，已跳过", flush=True)
            except WebviewElementSkip as error:
                if item['id'] is not None:
                    store.product_state(item['id'], WEBVIEW_SKIP, reason=str(error))
                if single_round:
                    errors.append(f"排行{item['rank']}：{error}")
                print(f"[商品] 排行 {item['rank']}：{WEBVIEW_SKIP}", flush=True)
            except Exception as error:
                if item['id'] is not None:
                    store.product_state(item['id'], 'failed', reason=str(error))
                errors.append(f"排行{item['rank']}：{error}")
                print(f"[商品] {errors[-1]}", flush=True)
                # 单个商品失败不中止整榜：继续采集下一个商品，仅在致命错误时停止。
                if isinstance(error, FatalWorkflowError):
                    raise
                if isinstance(error, HTTPError):
                    raise FatalWorkflowError('WDA 通信异常，停止本轮，避免继续误判后续商品或榜单') from error
            finally:
                if opened:
                    interrupted = isinstance(sys.exc_info()[1], KeyboardInterrupt)
                    try:
                        return_to_board(client, entry['main_name'], entry['tab'])
                    except Exception as error:
                        if interrupted:
                            print(f'[商品] 中断后未返回原榜单，断点已保留：{error}', flush=True)
                        else:
                            # 商品页卡死/慢加载导致返回失败：重新打开榜单恢复状态并继续，
                            # 避免一个慢 WebView 商品中止整轮。重新打开会刷新榜单元素缓存。
                            try:
                                from automation.pdd.spider.navigation import open_app_board
                                xml = open_app_board(client, {'name': entry['source_name'],
                                                               'main_name': entry['main_name'], 'url': entry['source_url']})
                                client.__dict__['_rank_last_elements'] = ios._xcui_texts(xml)
                                client.__dict__['_rank_page_source'] = xml
                                print(f'[商品] 返回榜单失败，已重新打开榜单恢复并继续：{error}', flush=True)
                            except Exception as reopen_error:
                                raise FatalWorkflowError(
                                    f'无法返回原榜单且重新打开失败，停止：{error}；重开失败：{reopen_error}') from error
        if not errors:
            return
        last_errors = errors
        if single_round or attempt + 1 >= config.RETRY_ATTEMPTS:
            break
        # 整个榜单采集一遍后，补采本轮失败的商品，最多重试 config.RETRY_ATTEMPTS 次。
        pending = [i for i in store.items(list_id) if not product_complete(i)]
        if not pending:
            return
        print(f'[榜单] 本轮 {len(errors)} 个商品失败，补采剩余 {len(pending)} 个失败商品（{attempt + 2}/{config.RETRY_ATTEMPTS}）', flush=True)
        scroll_top(client, entry['main_name'], entry['tab'], live_rows, client.__dict__.get('_rank_last_elements', []))
    raise RuntimeError('本子榜有未完成商品，下次运行续采：' + '；'.join(last_errors))


def collect_sources(store, runtime, *, only_webview=False):
    from automation.pdd.spider.navigation import open_app_board
    if only_webview:
        entries = [store.list(r['id']) for r in store.db.execute(
            "SELECT id FROM t_pdd_rankings WHERE week_start=%s AND state<>'completed' ORDER BY source_order,tab_index",
            (week_start(today()),))]
    else:
        sources = read_leaderboards(config.EXCEL_PATH)
        if not sources:
            raise ValueError('来源 Excel 没有有效榜单')
        entries = store.seed_rankings(today(), sources, config.OUTPUT_ROOT)
    client, failed = None, False
    for entry in entries:
        if not only_webview and store.requeue_undated_comments(entry['id']):
            entry = store.list(entry['id'])
            print(f"[榜单] {entry['main_name']}/{entry['tab']}：补采旧评论发表日期，复用图片和 SKU", flush=True)
        for item in store.items(entry['id']):
            output = Path(item['directory']) / '详情/长图.jpg'
            if item['detail_images_state'] == 'completed' and not valid_image(output):
                images = json.loads(item['product_json'])['images']
                merge_details([Path(item['directory']) / r['local_path'] for r in images if r['image_type'] == 'detail'], output)
        cleanup_ranking(entry['directory'])
        if not only_webview and entry['state'] == 'completed':
            continue
        try:
            if not entry['tab']:
                raise ValueError('链接失效：来源表子榜为空')
            if client is None:
                client = runtime.ensure_wda()
            xml = open_app_board(client, {'name': entry['source_name'], 'main_name': entry['main_name'], 'url': entry['source_url']},
                inspect_xml=lambda source: wait_for_elements(lambda: ios._current_page_source(client), initial_xml=source))
            # Detect inaccessible WebViews before the legacy parser reports a generic error.
            require_elements(xml)
            elements = ios._xcui_texts(xml)
            main = ios._rank_details(elements, TAB_TO_CLASS[entry['tab']])['主榜单名称']
            if main != entry['main_name'] or ios._guess_tab(elements) != entry['tab']:
                raise RuntimeError('当前页面主榜或子榜与来源表不对应')
            client.__dict__['_rank_last_elements'] = elements
            client.__dict__['_rank_page_source'] = xml
            if not store.items(entry['id']):
                header, rows = read_tab(client, main, entry['tab'], initial_elements=elements)
                store.save_rows(entry['id'], entry['directory'], header, rows)
            else:
                # Read current titles for navigation only; never replace the saved ranking.
                rows = ios._parse_rows(elements, TAB_TO_CLASS[entry['tab']])
            if not rows:
                raise RuntimeError('当前榜单没有可定位的商品元素')
            saved_rows = [json.loads(i['row_json']) for i in store.items(entry['id'])]
            capture_ranking(store, client, store.list(entry['id']), saved_rows, elements)
            store.export(entry['id'])
            store.list_state(entry['id'], 'running')
            run_products(store, client, entry, rows, only_webview=only_webview)
            store.export(entry['id'])
            terminal = all(product_complete(i) for i in store.items(entry['id']))
            store.list_state(entry['id'], 'completed' if terminal else 'pending')
        except WebviewElementSkip:
            store.list_state(entry['id'], WEBVIEW_SKIP)
            print(f"[榜单] {entry['main_name']}/{entry['tab']}：{WEBVIEW_SKIP}", flush=True)
        except (HTTPError, OSError, FatalWorkflowError):
            store.list_state(entry['id'], 'failed')
            raise
        except Exception as error:
            failed = True
            store.list_state(entry['id'], 'failed')
            print(f"[榜单] {entry['main_name']}/{entry['tab']} 失败，进度已保留：{error}", flush=True)
        finally:
            cleanup_ranking(entry['directory'])
    return int(failed)
