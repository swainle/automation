"""Current iOS leaderboard -> long image -> product screenshots and copied share URLs."""
from automation.pdd.spider.logging_setup import operation, timed_operation
import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import uuid
import xml.etree.ElementTree as ET

from PIL import Image
from wda.usbmux.exceptions import HTTPError

from automation.pdd.spider import config, ios, longshot
from automation.pdd.spider.artifacts import write_share_link, read_share_link
from automation.pdd.spider.crop import save_product
from automation.pdd.spider.rank import TAB_TO_CLASS
from automation.pdd.spider.workflow import FatalWorkflowError


def named_element(client, names, *, class_name=None, visible=None):
    for name in names:
        for kind in ([class_name] if class_name else ['Button', 'StaticText', None]):
            selector = client(name=name, **({'className': kind} if kind else {}),
                              **({'visible': visible} if visible is not None else {}))
            elements = selector.find_elements()
            if len(elements) == 1:
                return elements[0]
            if len(elements) > 1:
                raise RuntimeError(f'元素“{name}”不唯一，未执行点击')
    raise RuntimeError(f'未找到元素：{" / ".join(names)}')


def tap_with_retry(client, names, *, class_name=None, attempts=3):
    """Retry the existing lookup/tap only on failure; no extra page snapshots."""
    for attempt in range(attempts):
        try:
            named_element(client, names, class_name=class_name).tap()
            return
        except Exception as error:
            if isinstance(error, FatalWorkflowError) or '不唯一' in str(error) or attempt + 1 == attempts:
                raise
            print(f'[商品] {" / ".join(names)} 失败，1 秒后重试（{attempt + 2}/{attempts}）：{error}')
            time.sleep(1)


def product_element(client, title, *, occurrence=1):
    # .tap() uses /element/{id}/click; the library's .click() converts to coordinates.
    # Ranking entries precede recommendations in the XML document. An explicit
    # occurrence avoids selecting a duplicate title from the recommendation area.
    from wda import Element
    from wda.exceptions import WDAElementNotFoundError
    predicate = 'type == "XCUIElementTypeStaticText" AND name == ' + json.dumps(title, ensure_ascii=False)
    matches = client._session_http.post('/elements', {
        'using': 'predicate string', 'value': predicate,
    }, timeout=8).value
    if occurrence < 1 or len(matches) < occurrence:
        raise WDAElementNotFoundError(f'未找到排行商品标题（第 {occurrence} 次）：{title}')
    match = matches[occurrence - 1]
    return Element(client, match.get('ELEMENT') or match['element-6066-11e4-a52e-4f735466cecf'])


def verify_board(client, main, tab):
    ios.require_pdd_foreground(client)
    xml = ios._current_page_source(client)
    elements = ios._xcui_texts(xml) if xml else []
    details = ios._rank_details(elements, TAB_TO_CLASS[tab])
    if details['主榜单名称'] != main or ios._guess_tab(elements) != tab:
        raise FatalWorkflowError('未确认已返回原榜单和 Tab，停止后续点击以免采错商品')
    client.__dict__['_rank_last_elements'] = elements
    client.__dict__['_rank_page_source'] = xml
    return xml, elements


@timed_operation('[榜单] 准备顶部截图位置')
def prepare_top(client, main, tab, rows, elements):
    """Use the app's top button when present; otherwise scroll the first element visible."""
    first = next((element for element in elements if element['type'] == 'StaticText'
                  and element['text'].replace('\u200b', '').strip() == rows[0]['标题']), None)
    width, height = ios._get_window_size(client)
    if first and height * .15 <= first['y'] < height * .65:
        return client.__dict__.get('_last_page_source'), elements
    try:
        named_element(client, ['顶部', '回到顶部']).tap()
    except RuntimeError:
        ios._scroll_to_top(client, rows[0]['标题'], force=True)
    time.sleep(.5)
    xml, refreshed = verify_board(client, main, tab)
    # Strict geometry validation after this step rejects a partially hidden first card.
    return xml, refreshed


def clear_clipboard(client):
    marker = 'spider-share-' + uuid.uuid4().hex
    payload = {'contentType': 'plaintext', 'content': base64.b64encode(marker.encode()).decode()}
    try:
        client._session_http.post('/wda/setPasteboard', payload, timeout=5)
        if ios._read_clipboard(client) != marker:
            raise RuntimeError('未确认旧剪贴板已清除')
    except Exception:
        bundle = ios.wda_bundle_id()
        try:
            ios._activate_clipboard_app(client, bundle)
            if not ios._wait_foreground(client, bundle):
                raise RuntimeError('无法激活 WDA 清理旧剪贴板')
            client._session_http.post('/wda/setPasteboard', payload, timeout=5)
            result = client._session_http.post('/wda/getPasteboard', {'contentType': 'plaintext'}, timeout=5).value
            if base64.b64decode(result).decode('utf-8', errors='replace') != marker:
                raise RuntimeError('无法验证剪贴板清理结果，停止复制链接')
        finally:
            ios._restore_foreground(client, ios.PDD_BUNDLE_ID)
    return marker


@timed_operation('[商品] 复制分享链接')
def copy_share(client):
    ios.require_pdd_foreground(client)
    marker = clear_clipboard(client)
    tap_with_retry(client, ['分享'], class_name='Button')
    client.__dict__['_spider_share_open'] = True
    time.sleep(.5)
    tap_with_retry(client, ['复制链接', '复制', '拷贝'])
    client.__dict__['_spider_share_open'] = False
    time.sleep(.5)
    content = ios._read_clipboard(client)
    if not content or marker in content:
        raise RuntimeError('未取得本次复制的新链接，未使用旧剪贴板数据')
    match = re.search(r'https?://[^\s<>"\u3000]+', content)
    if not match:
        raise RuntimeError('复制内容不包含有效分享链接')
    link = match.group(0).rstrip('，。；）】')
    host = urlsplit(link).hostname or ''
    if not any(host == domain or host.endswith('.' + domain) for domain in ('pinduoduo.com', 'yangkeduo.com')):
        raise RuntimeError('本次剪贴板链接不是拼多多分享地址，未入库')
    return link


def dismiss_sku_panel(client):
    """Dismiss the quantity/specification sheet before inspecting the product page."""
    ios.require_pdd_foreground(client)
    quantity = client(predicate='visible == true AND ('
                      '(type == "XCUIElementTypeButton" AND '
                      '(name == "增加数量" OR name == "减少数量")) OR '
                      '(type == "XCUIElementTypeStaticText" AND '
                      '(name == "选择规格" OR name == "请选择规格" OR '
                      'name == "购买数量" OR name == "选择商品规格")))')
    if not quantity.find_elements():
        return False
    buttons = client(name='关闭', className='Button', visible=True).find_elements()
    if len(buttons) != 1:
        raise RuntimeError('检测到 SKU 面板，但未找到唯一可见的关闭按钮，未截图')
    buttons[0].tap()
    for _ in range(8):
        time.sleep(.25)
        if not quantity.find_elements():
            print('[商品] 已关闭 SKU 面板，继续校验商品页')
            return True
    raise RuntimeError('点击关闭后 SKU 面板仍未消失，未截图')


def product_page_checks(xml, title):
    """Verify product-page structure; the fresh share URL establishes identity."""
    elements = ios._xcui_texts(xml) if xml else []
    normalized = re.sub(r'\s|\u200b', '', title)
    matches = bool(normalized) and any(
        re.sub(r'\s|\u200b', '', element['text']) == normalized for element in elements)
    nodes = list(ET.fromstring(xml).iter()) if xml else []
    share = any(node.tag == 'XCUIElementTypeButton' and node.get('visible') != 'false'
                and (node.get('label') or node.get('name')) == '分享' for node in nodes)
    visible_buttons = [(node.get('label') or node.get('name') or '') for node in nodes
                       if node.tag == 'XCUIElementTypeButton' and node.get('visible') != 'false']
    back = any(name in ('返回', 'Back') for name in visible_buttons)
    controls = any(name in ('店铺', '收藏', '客服') or '购买' in name for name in visible_buttons)
    # Native pages may expose the real title as Image/Other rather than StaticText.
    candidates = [element['text'] for element in elements
                  if element['type'] in ('Image', 'Other', 'StaticText') and 8 <= len(element['text']) <= 300]
    observed = next((text for text in candidates if normalized and normalized in re.sub(r'\s|\u200b', '', text)), '')
    if not observed:
        observed = next((node.get('label') or node.get('name') or '' for node in nodes
                         if node.tag == 'XCUIElementTypeImage' and node.get('visible') != 'false'
                         and len(node.get('label') or node.get('name') or '') >= 8
                         and float(node.get('width') or 0) >= 150
                         and 0 <= float(node.get('y') or 0) <= 500), '')
    boards = [element['text'] for element in elements if element['type'] == 'NavigationBar'
              and element['text'].endswith(tuple(TAB_TO_CLASS))]
    reasons = []
    if not xml:
        reasons.append('页面元素树为空')
    if not share:
        reasons.append('未找到可见的分享按钮')
    if not back or not controls:
        reasons.append('商品页返回按钮或购买/店铺控件尚未就绪')
    if boards:
        reasons.append('仍存在榜单导航栏：' + ' / '.join(boards))
    return reasons, {
        'expected_title': title, 'title_matched': matches, 'share_visible': share,
        'board_titles': boards, 'observed_title': observed.strip(),
        'back_visible': back, 'product_controls_visible': controls,
        'title_candidates': candidates,
        'texts': [element['text'] for element in elements],
    }


def save_product_diagnostics(client, xml, details):
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S-%f')
    source = config.BUSINESS_ROOT / 'data/diagnostics' / f'product-{stamp}.xml'
    try:
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(xml or '', encoding='utf-8')
        try:
            client.screenshot().save(source.with_suffix('.png'))
        except Exception as error:
            details['screenshot_error'] = str(error)
        source.with_suffix('.json').write_text(
            json.dumps(details, ensure_ascii=False, indent=2), encoding='utf-8')
        return source
    except Exception as error:
        print(f'[商品] 保存商品页诊断失败：{error}')
        return None


@timed_operation('[商品] 校验商品页')
def verify_product(client, title, *, timeout=12, attempts=6):
    deadline = time.monotonic() + timeout
    xml, details = None, {'expected_title': title}
    reasons = ['页面尚未就绪']
    for attempt in range(max(1, attempts)):
        ios.require_pdd_foreground(client)
        xml = None
        try:
            dismiss_sku_panel(client)
            xml = ios._current_page_source(client)
            reasons, details = product_page_checks(xml, title)
        except Exception as error:
            reasons = [str(error)]
            details = {'expected_title': title, 'read_error': str(error)}
        if not reasons:
            if not details['title_matched']:
                print(f'[商品] 标题与榜单不同，采用当前商品页；榜单：{title}；'
                      f'商品页：{details["observed_title"] or "未暴露标题，身份待分享链接确认"}')
            return details
        print(f'[商品] 商品页校验第 {attempt + 1} 次未通过：{"；".join(reasons)}')
        if time.monotonic() >= deadline or attempt + 1 >= attempts:
            break
        time.sleep(min(.5, max(0, deadline - time.monotonic())))
    details['reasons'] = reasons
    print(f'[商品] 预期标题：{title}；页面标题候选：{details.get("title_candidates", [])[:8]}')
    if xml is None:
        try:
            xml = ios._current_page_source(client)
        except Exception as error:
            details['source_error'] = str(error)
    diagnostic = save_product_diagnostics(client, xml, details)
    raise RuntimeError('未确认进入对应商品页，未截图或读取分享链接：' + '；'.join(reasons)
                       + (f'；诊断文件：{diagnostic}' if diagnostic else ''))


@timed_operation('[商品] 返回原榜单')
def return_to_board(client, main, tab):
    ios.require_pdd_foreground(client)
    # A failed share operation may leave the share sheet open.
    if client.__dict__.get('_spider_share_open', False):
        tap_with_retry(client, ['取消', '关闭'], class_name='Button')
        client.__dict__['_spider_share_open'] = False
    try:
        named_element(client, [main + tab], class_name='NavigationBar', visible=True)
    except RuntimeError as error:
        if '不唯一' in str(error):
            raise FatalWorkflowError('原榜单导航栏不唯一，未执行返回') from error
    else:
        print('[商品] 仍在原榜单，跳过返回按钮')
        return client.__dict__.get('_rank_page_source'), client.__dict__.get('_rank_last_elements', [])
    tap_with_retry(client, ['返回', 'Back'], class_name='Button')
    time.sleep(.5)
    check_board_context(client, main, tab)
    return client.__dict__.get('_rank_page_source'), client.__dict__.get('_rank_last_elements', [])


def check_board_context(client, main, tab):
    """Verify the native navigation title without serializing the whole WebView."""
    try:
        ios.require_pdd_foreground(client)
        named_element(client, [main + tab], class_name='NavigationBar', visible=True)
    except Exception as error:
        raise FatalWorkflowError('未确认返回原榜单标题，停止后续商品点击') from error


def valid_image(path):
    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except (OSError, ValueError):
        return False


class IosFullFlow:
    def __init__(self, store, client, *, refresh=False, latest_snapshot=False, product_collector=None):
        self.store, self.client = store, client
        self.refresh = refresh
        self.latest_snapshot = latest_snapshot
        self.product_collector = product_collector

    def run_list(self, list_id, rows, elements):
        entry = self.store.list(list_id)
        directory = Path(entry['directory'])
        main, tab = entry['main_name'], entry['tab']
        image_stage = 'ranking_long_screenshot'
        if self.refresh or not longshot.reusable(directory, rows):
            self.store.stage_state(list_id, image_stage, 'running')
            for item in self.store.items(list_id):
                self.store.stage_state(list_id, 'ranking_screenshot', 'running', item_id=item['id'])
            try:
                xml, elements = prepare_top(self.client, main, tab, rows, elements)
                if not xml:
                    raise RuntimeError('缺少榜单原始 XML，无法生成长图')
                artifacts = longshot.capture(self.client, rows, xml, directory)
                # Long screenshots finish at the bottom; discard the stale top-position snapshot.
                _, elements = prepare_top(self.client, main, tab, rows, [])
                self.store.stage_state(list_id, image_stage, 'completed', artifacts=artifacts)
            except Exception as error:
                self.store.stage_state(list_id, image_stage, 'failed', reason=str(error))
                for item in self.store.items(list_id):
                    self.store.stage_state(list_id, 'ranking_screenshot', 'failed', item_id=item['id'], reason=str(error))
                raise
        else:
            artifacts = [directory / '榜单长图.jpg', directory / '榜单长图.meta.json',
                         *(directory / f"榜{int(row['排行']):02d}/产品截图.jpg" for row in rows)]
            self.store.stage_state(list_id, image_stage, 'completed', artifacts=artifacts)
        for item in self.store.items(list_id):
            self.store.stage_state(list_id, 'ranking_screenshot', 'completed', item_id=item['id'], artifacts=[directory / '榜单长图.jpg', directory / '榜单长图.meta.json'])
            path = directory / f"榜{item['rank']:02d}/产品截图.jpg"
            if not valid_image(path):
                raise RuntimeError(f"排行 {item['rank']} 缺少有效的卡片裁剪图")
            self.store.stage_state(list_id, 'crop', 'completed', item_id=item['id'], artifacts=[path])
        errors = []
        occurrences = {}
        for item in self.store.items(list_id):
            row = json.loads(item['row_json'])
            native_details_done = item['product_state'] == 'completed' and json.loads(item['product_json'] or '{}').get('info', {}).get('image_method') == 'ios_enlarged_screenshot_crop'
            board_title = row.get('榜单标题') or row['标题']
            occurrences[board_title] = occurrences.get(board_title, 0) + 1
            if item['state'] == 'completed' and not self.refresh and not self.latest_snapshot and (not self.product_collector or native_details_done):
                print(f"[商品] 跳过当天完整采集已完成：排行 {item['rank']}")
                continue
            folder = directory / f"榜{item['rank']:02d}"
            screenshot = folder / '产品页截图.jpg'
            image_done = not self.refresh and self.store.stage(list_id, 'product_screenshot', item['id'])['state'] == 'completed' and valid_image(screenshot)
            try:
                saved = read_share_link(folder, '' if self.latest_snapshot else row['标题'])
            except ValueError:
                saved = None
            link_done = not self.refresh and item['link_state'] == 'completed' and saved and saved['share_link'] == row.get('产品链接')
            if self.latest_snapshot:
                if link_done and row.get('产品ID'):
                    saved_id = str(saved.get('goods_id') or ios._extract_goods_id(saved['share_link']))
                    link_done = saved_id == str(row['产品ID'])
            if image_done and ((not self.product_collector and link_done) or (self.product_collector and native_details_done)):
                self.store.item_state(item['id'], 'completed')
                print(f"[商品] 跳过已完成：排行 {item['rank']}")
                continue
            open_attempted = False
            succeeded = False
            self.store.item_state(item['id'], 'running')
            stage = 'open_product'
            self.store.stage_state(list_id, stage, 'running', item_id=item['id'])
            try:
                ios.require_pdd_foreground(self.client)
                with operation(f"[商品] 排行 {item['rank']}：定位标题元素"):
                    element = product_element(self.client, board_title, occurrence=occurrences[board_title])
                if not element.displayed:
                    with operation(f"[商品] 排行 {item['rank']}：滚动目标到可见位置"):
                        width, height = ios._get_window_size(self.client)
                        for attempt in range(12):
                            check_board_context(self.client, main, tab)
                            longshot.drag_for_screenshot(self.client, width / 2, height * .75, height * .4)
                            time.sleep(.25)
                            element = product_element(self.client, board_title, occurrence=occurrences[board_title])
                            if element.displayed:
                                break
                        else:
                            raise RuntimeError('滚动后商品标题仍不可见，未执行点击')
                open_attempted = True
                with operation(f"[商品] 排行 {item['rank']}：点击打开商品"):
                    element.tap()
                time.sleep(.5)
                if self.product_collector:
                    xml = ios._current_page_source(self.client)
                    reasons, page = product_page_checks(xml, board_title)
                    if reasons or not page['title_matched']:
                        raise RuntimeError('未确认当前商品身份，停止详情采集：' + '；'.join(reasons or ['商品标题不匹配']))
                else:
                    page = verify_product(self.client, board_title)
                if isinstance(page, dict) and page.get('observed_title'):
                    row['榜单标题'] = board_title
                    row['商品页标题'] = page['observed_title']
                self.store.stage_state(list_id, stage, 'completed', item_id=item['id'])
                if not image_done:
                    stage = 'product_screenshot'
                    self.store.stage_state(list_id, stage, 'running', item_id=item['id'])
                    with operation(f"[商品] 排行 {item['rank']}：截取商品页"):
                        save_product(self.client.screenshot().convert('RGB'), screenshot)
                    self.store.stage_state(list_id, stage, 'completed', item_id=item['id'], artifacts=[screenshot])
                if self.product_collector:
                    stage = 'product'
                    self.store.product_state(item['id'], 'running')
                    self.product_collector.collect(item, folder, row, initial_xml=self.client.__dict__.get('_last_page_source'))
                elif not link_done:
                    stage = 'link'
                    self.store.stage_state(list_id, stage, 'running', item_id=item['id'])
                    link = copy_share(self.client)
                    goods_id = ios._extract_goods_id(link)
                    if goods_id and row.get('产品ID') and goods_id != row['产品ID']:
                        if not self.latest_snapshot:
                            raise RuntimeError('分享链接的商品 ID 与原记录不一致，未覆盖')
                    if self.latest_snapshot and (not goods_id or goods_id != row.get('产品ID')):
                        self.store.reset_item_capture(item['id'])
                        if image_done:
                            save_product(self.client.screenshot().convert('RGB'), screenshot)
                        self.store.stage_state(list_id, 'product_screenshot', 'completed', item_id=item['id'], artifacts=[screenshot])
                        self.store.stage_state(list_id, 'crop', 'completed', item_id=item['id'], artifacts=[folder / '产品截图.jpg'])
                    row.update(产品链接=link, 产品ID=goods_id if self.latest_snapshot else goods_id or row.get('产品ID', ''))
                    write_share_link(folder, row)
                    self.store.item_stage(item['id'], 'link', row=row)
                    self.store.stage_state(list_id, 'link', 'completed', item_id=item['id'], artifacts=[folder / '分享链接.json'])
                elif self.latest_snapshot:
                    write_share_link(folder, row)
                    self.store.item_stage(item['id'], 'link', row=row)
                print(f"[商品] 排行 {item['rank']}：{'商品详情完成' if self.product_collector else '截图和分享链接完成'}")
                succeeded = True
            except Exception as error:
                if self.product_collector:
                    self.store.product_state(item['id'], 'failed', reason=str(error))
                self.store.stage_state(list_id, stage, 'failed', item_id=item['id'], reason=str(error))
                self.store.item_state(item['id'], 'failed')
                errors.append(f"排行{item['rank']}：{error}")
                print(f"[商品] {errors[-1]}")
                if isinstance(error, FatalWorkflowError):
                    raise
                if isinstance(error, HTTPError):
                    raise FatalWorkflowError('WDA 通信异常，停止本轮，避免继续误判后续商品或榜单') from error
            finally:
                if open_attempted:
                    try:
                        _, elements = return_to_board(self.client, main, tab)
                    except Exception as error:
                        self.store.item_state(item['id'], 'failed')
                        self.store.stage_state(list_id, 'return_to_board', 'failed', item_id=item['id'], reason=str(error))
                        raise FatalWorkflowError(f'无法返回原榜单，已停止后续操作：{error}') from error
                else:
                    check_board_context(self.client, main, tab)
            if succeeded:
                self.store.item_state(item['id'], 'completed')
        if errors:
            raise RuntimeError('本子榜有未完成商品，下次运行续采：' + '；'.join(errors))
        self.store.export(list_id)
