"""Open a shared goods URL in Android Chrome and retain web response evidence."""
import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from automation.pdd.spider import config
from automation.pdd.spider.android import AndroidConnection
from automation.pdd.spider.artifacts import write_json
from automation.pdd.spider.native_product import PACKAGE, bounds, product_info


def validate_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not any(
        parsed.hostname == host or (parsed.hostname or '').endswith('.' + host)
        for host in ('yangkeduo.com', 'pinduoduo.com')
    ):
        raise ValueError('--jump 需要拼多多 HTTP/HTTPS 商品链接')
    return url


def jump_target(root, width, *, confirm=False):
    """Click the business Open button, or its explicit Android confirmation."""
    labels = {'打开拼多多APP', '打开拼多多App'}
    if confirm:
        dialog = ''.join(node.get('text', '') for node in root.iter())
        if '拼多多' not in dialog:
            return None
        labels = {'打开', '允许'}
    for node in root.iter():
        label = (node.get('text') or node.get('content-desc') or '').strip()
        if label not in labels or node.get('enabled') == 'false':
            continue
        if confirm and node.get('resource-id') != 'android:id/button1':
            continue
        x1, y1, x2, y2 = bounds(node)
        if 0 <= x1 < x2 <= width and y2 > y1 >= 0:
            return label, ((x1 + x2) / 2, (y1 + y2) / 2)
    return None


def jump(url, directory=None, *, close_page=False):
    validate_url(url)
    directory = Path(directory) if directory else config.OUTPUT_ROOT / '网页调试' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    directory.mkdir(parents=True, exist_ok=True)
    connection = AndroidConnection()
    connection.prepare()
    device = connection.device
    if not connection.command('shell', 'pm', 'path', 'com.android.chrome').startswith('package:'):
        raise RuntimeError('手机未安装 Google Chrome（com.android.chrome）')
    # Warm up Chrome before discovering its devtools socket; do not replace any existing forward.
    device.shell(['am', 'start', '-a', 'android.intent.action.VIEW', '-d', 'about:blank', '-p', 'com.android.chrome'])
    time.sleep(1)
    port = connection.command('forward', 'tcp:0', 'localabstract:chrome_devtools_remote')
    events, responses, actions = [], [], []
    summary = {'requested_url': url, 'app_opened': False, 'directory': str(directory)}
    page = None
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f'http://127.0.0.1:{int(port)}', timeout=20000)
            context = browser.contexts[0]
            page = context.new_page()

            try:
                def save_response(response, body=None):
                    number = len(responses) + 1
                    entry = {'url': response.url, 'status': response.status,
                             'resource_type': response.request.resource_type,
                             'content_type': response.headers.get('content-type', '')}
                    responses.append(entry)
                    try:
                        body = response.body() if body is None else body
                        if len(body) <= 10_000_000:
                            relative = f'responses/{number:03d}.txt'
                            path = directory / relative
                            path.parent.mkdir(exist_ok=True)
                            path.write_bytes(body)
                            entry['body_path'] = relative
                        else:
                            entry['body_skipped'] = '超过 10 MB'
                    except Exception as error:
                        entry['body_error'] = str(error)
                    events.append(entry)

                def request_finished(request):
                    response = request.response()
                    if response and request.resource_type in {'document', 'xhr', 'fetch'}:
                        save_response(response)

                page.on('requestfinished', request_finished)
                page.on('requestfailed', lambda request: events.append({'url': request.url, 'failure': request.failure}))
                try:
                    page.goto(url, wait_until='domcontentloaded', timeout=30000)
                except Exception as error:
                    events.append({'navigation_error': str(error)})
                # Collect initial requests before invoking the app. Playwright waits pump response events.
                page.wait_for_timeout(3000)
                summary['final_url'] = page.url
                (directory / '网页.html').write_text(page.content(), encoding='utf-8')
                write_json(directory / '页面数据.json', page.evaluate('''() => ({title: document.title,
                    text: document.body.innerText, rawData: window.rawData || null,
                    images: [...document.images].map(i => i.currentSrc || i.src)})'''))
                business_clicked = False
                for _ in range(24):
                    current = device.app_current().get('package')
                    xml = device.dump_hierarchy(compressed=False)
                    root = ET.fromstring(xml)
                    if current == PACKAGE:
                        if not business_clicked:
                            raise RuntimeError('未点击业务“打开拼多多APP”按钮，未将前台 App 视为本次跳转成功')
                        summary['app_opened'] = True
                        (directory / 'App页面.xml').write_text(xml, encoding='utf-8')
                        try:
                            summary['app_product'] = product_info(root)
                        except RuntimeError as error:
                            summary['app_product_error'] = str(error)
                        break
                    target = jump_target(root, device.window_size()[0], confirm=business_clicked)
                    if target:
                        label, point = target
                        actions.append({'label': label, 'point': point,
                                        'kind': 'system_confirmation' if business_clicked else 'business_open'})
                        device.click(*point)
                        if not business_clicked:
                            business_clicked = True
                            print('[Android] 已点击业务按钮：打开拼多多APP', flush=True)
                    elif not business_clicked:
                        # Accessibility may omit the label; click the same visible business button in the DOM.
                        candidate = page.get_by_text(re.compile(r'^打开拼多多APP$', re.I)).first
                        if candidate.is_visible():
                            candidate.click(timeout=1500, no_wait_after=True)
                            actions.append({'label': '打开拼多多APP', 'kind': 'business_open', 'method': 'web_dom'})
                            business_clicked = True
                            print('[Android] 已点击业务按钮：打开拼多多APP', flush=True)
                    page.wait_for_timeout(750)
                documents = [entry for entry in responses if entry['resource_type'] == 'document']
                summary['goods_ids'] = sorted(set(re.findall(r'(?:[?&]|%26)goods_id(?:=|%3D)(\d+)',
                    '\n'.join(entry['url'] for entry in documents))))
                summary['web_raw_data'] = []
                for entry in documents:
                    if 'body_path' not in entry:
                        continue
                    source = (directory / entry['body_path']).read_text(encoding='utf-8', errors='replace')
                    match = re.search(r'window\.rawData\s*=\s*(\{.*?\});\s*</script>', source, re.S)
                    if match:
                        try:
                            raw = json.loads(match[1])
                            summary['web_raw_data'].append({'url': entry['url'], 'data': raw})
                        except ValueError:
                            pass
                device.screenshot().save(directory / '最终页面.png')
                # Only the tab created by this jump is owned here; leave other tabs intact.
            finally:
                try:
                    if close_page:
                        page.close()
                except Exception as error:
                    events.append({'tab_cleanup_error': str(error)})

    finally:
        write_json(directory / '网络记录.json', events)
        write_json(directory / '跳转操作.json', actions)
        write_json(directory / '结果.json', summary)
        connection.command('forward', '--remove', f'tcp:{port}')
    print(f'[Android] 浏览器跳转完成：App={summary["app_opened"]}，已记录 {len(responses)} 个网页响应；{directory}', flush=True)
    if not summary['app_opened']:
        raise RuntimeError(f'未确认打开拼多多，网页记录已保存：{directory}')
    return directory
