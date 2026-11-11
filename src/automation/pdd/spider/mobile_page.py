"""Wait for slow WDA snapshots before classifying an inaccessible WebView."""
from time import monotonic, sleep
import xml.etree.ElementTree as ET

from wda.usbmux.exceptions import HTTPError


class WebviewElementSkip(RuntimeError):
    pass


def require_elements(xml):
    if not xml:
        raise RuntimeError('WDA 未返回页面元素树')
    root = ET.fromstring(xml)
    if any(n.tag.endswith('WebView') for n in root.iter()) and not any(
            n.tag.endswith('StaticText') and (n.get('label') or n.get('name') or n.get('value') or '').strip()
            for n in root.iter()):
        raise WebviewElementSkip('WebView 未暴露可采集的文本元素')
    return root


def wait_for_elements(read_source, *, initial_xml=None, ready=None, timeout=60, interval=2):
    deadline = monotonic() + timeout
    xml = initial_xml
    consecutive_webviews = 0
    last_error = None
    reason = '页面尚未就绪'
    while True:
        try:
            if xml is None:
                xml = read_source()
            if not xml:
                consecutive_webviews = 0
                reason = 'WDA 未返回页面元素树'
            else:
                try:
                    require_elements(xml)
                except WebviewElementSkip:
                    consecutive_webviews += 1
                    reason = f'连续 {consecutive_webviews} 次仅获取到 WebView 容器'
                else:
                    consecutive_webviews = 0
                    if ready is None or ready(xml):
                        return xml
                    reason = '页面控件尚未加载完整'
            last_error = None
        except (TimeoutError, HTTPError) as error:
            if not isinstance(error, TimeoutError) and not (error.args and isinstance(error.args[0], TimeoutError)):
                raise
            consecutive_webviews = 0
            last_error = error
            reason = 'WDA 读取超时'
        remaining = deadline - monotonic()
        if remaining <= 0:
            if consecutive_webviews >= 3:
                raise WebviewElementSkip(f'等待 {timeout:g} 秒后仍无可采集文本：{reason}')
            if last_error is not None:
                raise last_error
            raise RuntimeError(f'等待页面元素超时（{timeout:g} 秒）：{reason}，尚未确认 WebView 不可采集')
        print(f'[页面] {reason}，继续等待并重读元素', flush=True)
        sleep(min(interval, remaining))
        xml = None
