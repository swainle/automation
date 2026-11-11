from unittest.mock import Mock
import xml.etree.ElementTree as ET

import pytest
from wda.usbmux.exceptions import HTTPError

from automation.pdd.spider import mobile_page as page, navigation, ios
from automation.pdd.spider.weekly_mobile import WeeklyProduct

WEBVIEW = '<App><XCUIElementTypeWebView/></App>'
LOADING = '<App><XCUIElementTypeStaticText name="加载中"/></App>'
READY = '<App><XCUIElementTypeNavigationBar name="猫粮畅销榜"/><XCUIElementTypeStaticText name="商品"/></App>'


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(page, 'monotonic', lambda: now[0])
    monkeypatch.setattr(page, 'sleep', lambda seconds: now.__setitem__(0, now[0] + seconds))
    return now


def test_transient_webview_and_empty_snapshot_recover(clock):
    read = Mock(side_effect=[None, WEBVIEW, READY])
    assert page.wait_for_elements(read, timeout=6) == READY
    assert read.call_count == 3
    assert clock[0] == 4


def test_persistent_webview_is_only_skipped_after_wait_and_multiple_confirmations(clock):
    read = Mock(return_value=WEBVIEW)
    with pytest.raises(page.WebviewElementSkip, match='等待 6 秒'):
        page.wait_for_elements(read, initial_xml=WEBVIEW, timeout=6)
    assert clock[0] == 6
    assert read.call_count == 3


def test_one_slow_webview_read_does_not_prove_inaccessibility(clock):
    def slow_read():
        clock[0] += 8
        return WEBVIEW
    with pytest.raises(RuntimeError, match='尚未确认') as caught:
        page.wait_for_elements(slow_read, timeout=6)
    assert not isinstance(caught.value, page.WebviewElementSkip)


@pytest.mark.parametrize('wrapped', [False, True])
def test_wda_timeouts_do_not_become_webview_skip(clock, wrapped):
    error = TimeoutError('WDA慢')
    if wrapped:
        error = HTTPError(error)
    read = Mock(side_effect=[WEBVIEW, WEBVIEW, WEBVIEW, error])
    with pytest.raises(type(error)) as caught:
        page.wait_for_elements(read, timeout=6)
    assert caught.value is error
    assert not isinstance(caught.value, page.WebviewElementSkip)


def test_empty_wda_reads_remain_retryable_failures(clock):
    with pytest.raises(RuntimeError, match='未返回页面元素树') as caught:
        page.wait_for_elements(lambda: None, timeout=6)
    assert not isinstance(caught.value, page.WebviewElementSkip)


def test_page_controls_must_be_ready_even_when_text_is_already_present(clock):
    ready = Mock(side_effect=[False, False, True])
    read = Mock(side_effect=[LOADING, LOADING, READY])
    assert page.wait_for_elements(read, ready=ready, timeout=6) == READY
    assert ready.call_count == 3


def test_connection_refused_is_not_treated_as_loading(clock):
    error = HTTPError(ConnectionRefusedError('disconnected'))
    with pytest.raises(HTTPError) as caught:
        page.wait_for_elements(Mock(side_effect=error), timeout=6)
    assert caught.value is error
    assert clock[0] == 0


def test_snapshot_retries_and_counts_actual_wda_reads(clock, monkeypatch):
    collector = WeeklyProduct(Mock(), Mock())
    read = Mock(side_effect=[WEBVIEW, READY])
    monkeypatch.setattr(ios, '_current_page_source', read)
    assert collector.snapshot().find('XCUIElementTypeStaticText').get('name') == '商品'
    assert collector.source_reads == 2
    assert collector._source_error is None


def test_cleanup_preserves_read_timeout_and_does_not_tap_stale_coordinates(clock, monkeypatch):
    collector = WeeklyProduct(Mock(), Mock())
    collector.root, collector.xml = ET.fromstring(WEBVIEW), WEBVIEW
    error = HTTPError(TimeoutError('WDA读取超时'))
    monkeypatch.setattr(ios, '_current_page_source', Mock(side_effect=error))
    with pytest.raises(HTTPError):
        collector.snapshot()
    with pytest.raises(HTTPError) as caught:
        collector.find('关闭')
    assert caught.value is error
    with pytest.raises(HTTPError):
        collector.tap(ET.Element('Button'))
    collector.client.click.assert_not_called()


def test_navigation_uses_refreshed_snapshot_after_webview_loading(clock, monkeypatch):
    client = Mock()
    client._session_http.post.return_value.value = 1
    client.http.get.return_value.value = {'bundleId': ios.PDD_BUNDLE_ID}
    monkeypatch.setattr(navigation, '_accept_app_prompt', lambda _: False)
    monkeypatch.setattr(navigation.time, 'sleep', lambda _: None)
    read = Mock(side_effect=[WEBVIEW, READY])
    monkeypatch.setattr(ios, '_current_page_source', read)
    xml = navigation.open_app_board(client, {'name': '猫粮', 'url': 'https://example.com/rank'},
        inspect_xml=lambda source: page.wait_for_elements(lambda: ios._current_page_source(client), initial_xml=source))
    assert xml == READY
    assert read.call_count == 2
    client.session.return_value.open_url.assert_called_once()
