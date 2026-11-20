"""Open leaderboard links in Safari and verify the destination App page."""
from automation.pdd.spider.logging_setup import timed_operation
import re
import time
import xml.etree.ElementTree as ET


class BoardUnavailableError(RuntimeError):
    """The link did not reach the requested leaderboard before the deadline."""


def board_name(value):
    return re.sub(r'【[^】]*】$', '', value.strip()).strip()


def is_target_board(elements, expected):
    expected = re.sub(r'(畅销榜|好评榜|降价榜)$', '', board_name(expected)).strip()
    return any(element['type'] in {'NavigationBar', 'StaticText'}
               and re.sub(r'(畅销榜|好评榜|降价榜)$', '', element['text']).strip() == expected
               and element['text'].endswith(('畅销榜', '好评榜', '降价榜')) for element in elements)


def _accept_app_prompt(client):
    from wda.exceptions import WDARequestError
    try:
        message = client.http.get('/alert/text', timeout=10).value
    except WDARequestError as error:
        if 'no such alert' in str(error).lower():
            return False
        raise
    title = (message or '').splitlines()[0] if message else ''
    if '拼多多' not in title or not any(word in title.lower() for word in ('open', '打开')):
        return False
    # Use the default affirmative action for both Chinese and English iOS.
    client.http.post('/alert/accept', {}, timeout=5)
    print('[榜单] 已确认在拼多多中打开', flush=True)
    return True


@timed_operation('[榜单] 打开链接并核对目标榜单')
def open_app_board(client, source, *, timeout=90, inspect_xml=None):
    from automation.pdd.spider import ios
    from wda.usbmux.exceptions import HTTPError
    url = source['url'].strip()
    if not re.match(r'^https?://', url):
        raise ValueError('榜单链接必须为 HTTP/HTTPS 地址')
    print('[榜单] 结束拼多多，准备打开下一条链接', flush=True)
    client._session_http.post('/wda/apps/terminate', {'bundleId': ios.PDD_BUNDLE_ID}, timeout=10)
    state = client._session_http.post('/wda/apps/state', {'bundleId': ios.PDD_BUNDLE_ID}, timeout=3).value
    if state != 1:  # XCUIApplicationStateNotRunning
        raise RuntimeError(f'拼多多未确认结束（状态 {state}），未打开下一条榜单链接')
    browser = client.session(ios.SAFARI_BUNDLE_ID)
    browser.open_url(url)
    time.sleep(2)  # Safari needs time to load before probing the Open prompt.
    client.session_id = browser.session_id
    # A new Safari session replaces WDA's previous collection session.
    for key in ('_source_configured', '_rank_last_elements', '_rank_page_source', '_last_page_source'):
        client.__dict__.pop(key, None)
    print(f"[榜单] Safari 已打开链接：{source['name']}", flush=True)
    # Alert detection also waits for Safari to become idle. Configure before
    # the first alert request, rather than waiting until the source read below.
    ios._configure_source(client)
    deadline = time.monotonic() + timeout
    last_timeout = None
    while time.monotonic() < deadline:
        try:
            try:
                accepted = _accept_app_prompt(client)
            except (TimeoutError, HTTPError) as error:
                if not isinstance(error, TimeoutError) and not (error.args and isinstance(error.args[0], TimeoutError)):
                    raise
                # Safari's large WebView can make alert enumeration slow even
                # with no alert. Still inspect the page and its Open-in-App link.
                last_timeout = error
                accepted = False
                print('[榜单] 弹窗检测超时，继续识别当前页面', flush=True)
            if accepted:
                time.sleep(3)  # Allow the App transition and initial page load.
            info = client.http.get('/wda/activeAppInfo', timeout=8).value
            foreground = info.get('bundleId', '')
            xml = ios._current_page_source(client)
            if xml:
                last_timeout = None
                if foreground == ios.PDD_BUNDLE_ID and inspect_xml is not None:
                    inspected = inspect_xml(xml)
                    if isinstance(inspected, str):
                        xml = inspected
                elements = ios._xcui_texts(xml)
                if foreground == ios.PDD_BUNDLE_ID and is_target_board(elements, source.get('main_name') or source['name']):
                    time.sleep(2)  # The heading may appear before the product cards finish loading.
                    print(f"[榜单] 已核对目标榜单：{source['name']}", flush=True)
                    return xml
                if foreground == ios.SAFARI_BUNDLE_ID:
                    # Safari renders some Open-in-App prompts outside WDA's alert API.
                    dialogs = ET.fromstring(xml).findall(".//*[@name='SFDialogView']")
                    for dialog in dialogs:
                        prompt = ios._xcui_texts(ET.tostring(dialog, encoding='unicode'))
                        if not any('拼多多' in e['text'] and any(word in e['text'].lower()
                                   for word in ('打开', 'open')) for e in prompt):
                            continue
                        button = next((e for e in prompt if e['type'] == 'Button'
                                       and e['text'].casefold() in {'打开', 'open'}
                                       and e['w'] > 0 and e['h'] > 0), None)
                        if button is not None:
                            if not ios._tap_xy(client, button['x'] + button['w'] / 2,
                                               button['y'] + button['h'] / 2, button['text']):
                                raise RuntimeError('Safari 确认打开拼多多失败')
                            print('[榜单] 已确认 Safari 在拼多多中打开', flush=True)
                            time.sleep(3)
                            break
                    else:
                        dialogs = []
                    if dialogs:
                        continue
                    candidates = [element for element in elements
                                  if ''.join(element['text'].split()).casefold() in {
                                      '打开拼多多app', '在app打开', '在app中打开', '在拼多多中打开'}
                                  and element['w'] > 0 and element['h'] > 0
                                  and element['x'] >= 0 and element['y'] >= 0]
                    if candidates:
                        button = min(candidates, key=lambda element: (element['y'], -element['x']))
                        if not ios._tap_xy(client, button['x'] + button['w'] / 2,
                                           button['y'] + button['h'] / 2, button['text']):
                            raise RuntimeError(f"Safari 点击 {button['text']} 失败")
                        time.sleep(1)  # Allow the system Open prompt to appear.
                        print(f"[榜单] 已点击：{button['text']}", flush=True)
        except (TimeoutError, HTTPError) as error:
            if not isinstance(error, TimeoutError) and not (
                    error.args and isinstance(error.args[0], TimeoutError)):
                raise
            last_timeout = error
            print('[榜单] 页面加载期间 WDA 请求超时，继续等待目标榜单', flush=True)
        time.sleep(.5)
    if last_timeout is not None:
        raise last_timeout
    raise BoardUnavailableError(f"Safari 未打开目标榜单：{source['name']}；请检查打开 App 提示及链接有效性")
