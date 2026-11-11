import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
import pytest

from automation.pdd.spider.database import Store
from automation.pdd.spider import full_flow, ios


def prepared(tmp_path):
    store = Store(tmp_path / 'sqlite.db')
    board = store.seed('2026-11-02', [{'name': '猫粮', 'url': ''}])[0]
    store.discover(board['id'], [SimpleNamespace(tab='畅销榜', index=0, has_sub=False)])
    rows = [{'排行': rank, '标题': f'商品{rank}'} for rank in (1, 2)]
    store.save_rows(1, tmp_path, ['排行', '标题'], rows, 'ios')
    for row in rows:
        path = tmp_path / f"榜{row['排行']:02d}/产品截图.jpg"
        path.parent.mkdir()
        Image.new('RGB', (100, 100), 'white').save(path)
    store.stage_state(1, 'ranking_long_screenshot', 'completed')
    return store, rows


def test_full_flow_uses_element_tap_and_resumes_only_failed_product(tmp_path):
    store, rows = prepared(tmp_path)
    client = Mock()
    client.http.get.return_value.value = {'bundleId': ios.PDD_BUNDLE_ID}
    client.screenshot.side_effect = lambda: Image.new('RGB', (100, 200), 'white')
    element = Mock()
    with patch.object(full_flow.longshot, 'reusable', return_value=True), \
            patch.object(full_flow, 'product_element', return_value=element), \
            patch.object(full_flow, 'verify_product'), patch.object(full_flow.time, 'sleep'), \
            patch.object(full_flow, 'copy_share', side_effect=['https://mobile.yangkeduo.com/goods.html?goods_id=111', RuntimeError('复制失败')]), \
            patch.object(full_flow, 'return_to_board', return_value=('xml', [])):
        with pytest.raises(RuntimeError, match='未完成'):
            full_flow.IosFullFlow(store, client).run_list(1, rows, [])
    assert element.tap.call_count == 2
    element.click.assert_not_called()
    assert store.items(1)[0]['link_state'] == 'completed'
    assert store.items(1)[1]['link_state'] == 'failed'
    assert [item['state'] for item in store.items(1)] == ['completed', 'failed']
    assert store.stage(1, 'product_screenshot', 2)['state'] == 'completed'
    with patch.object(full_flow.longshot, 'reusable', return_value=True), \
            patch.object(full_flow, 'product_element', return_value=element), \
            patch.object(full_flow, 'verify_product'), patch.object(full_flow.time, 'sleep'), \
            patch.object(full_flow, 'copy_share', return_value='https://mobile.yangkeduo.com/goods.html?goods_id=222') as share, \
            patch.object(full_flow, 'return_to_board', return_value=('xml', [])):
        full_flow.IosFullFlow(store, client).run_list(1, rows, [])
    share.assert_called_once()
    assert [item['goods_id'] for item in store.items(1)] == ['111', '222']
    assert [item['state'] for item in store.items(1)] == ['completed', 'completed']
    store.list_state(1, 'completed')
    assert store.full_list_completed(1)
    assert (tmp_path / '榜单数据.xlsx').exists()
    store.close()


def test_share_rejects_old_clipboard_without_sending_messages():
    client = Mock()
    client.http.get.return_value.value = {'bundleId': ios.PDD_BUNDLE_ID}
    with patch.object(full_flow, 'clear_clipboard', return_value='spider-marker'), \
            patch.object(full_flow, 'named_element') as named, patch.object(full_flow.time, 'sleep'), \
            patch.object(ios, '_read_clipboard', return_value='spider-marker'), \
            pytest.raises(RuntimeError, match='旧剪贴板'):
        full_flow.copy_share(client)
    assert named.call_count == 2
    assert named.call_args_list[0].args[1] == ['分享']
    assert named.call_args_list[1].args[1] == ['复制链接', '复制', '拷贝']


def test_share_token_is_not_a_goods_id():
    assert ios._extract_goods_id('https://mobile.yangkeduo.com/goods2.html?ps=opaque') == ''
    assert ios._extract_goods_id('https://mobile.yangkeduo.com/goods2.html?ps=opaque&goods_id=123') == '123'


def test_share_copy_retries_missing_control_without_reopening_share_or_clearing_clipboard():
    client = Mock()
    client.http.get.return_value.value = {'bundleId': ios.PDD_BUNDLE_ID}
    share, copy = Mock(), Mock()
    with patch.object(full_flow, 'clear_clipboard', return_value='marker') as clear, \
            patch.object(full_flow, 'named_element', side_effect=[share, RuntimeError('未找到元素'), copy]) as named, \
            patch.object(full_flow.time, 'sleep'), \
            patch.object(ios, '_read_clipboard', return_value='https://mobile.yangkeduo.com/goods.html?goods_id=123'), \
            patch.object(ios, '_current_page_source') as source:
        assert full_flow.copy_share(client).endswith('goods_id=123')
    clear.assert_called_once()
    share.tap.assert_called_once()
    copy.tap.assert_called_once()
    assert named.call_count == 3
    source.assert_not_called()
    assert client.__dict__['_spider_share_open'] is False


def test_tap_retry_stops_after_three_failures():
    with patch.object(full_flow, 'named_element', side_effect=RuntimeError('未找到元素')) as named, \
            patch.object(full_flow.time, 'sleep') as sleep, \
            pytest.raises(RuntimeError, match='未找到元素'):
        full_flow.tap_with_retry(Mock(), ['取消', '关闭'], class_name='Button')
    assert named.call_count == 3
    assert sleep.call_count == 2


def test_tap_retry_does_not_retry_ambiguous_elements():
    with patch.object(full_flow, 'named_element', side_effect=RuntimeError('元素不唯一')) as named, \
            patch.object(full_flow.time, 'sleep') as sleep, \
            pytest.raises(RuntimeError, match='不唯一'):
        full_flow.tap_with_retry(Mock(), ['分享'])
    named.assert_called_once()
    sleep.assert_not_called()


def test_return_retries_cancel_then_returns_and_checks_original_board():
    client = Mock()
    client._spider_share_open = True
    client.http.get.return_value.value = {'bundleId': ios.PDD_BUNDLE_ID}
    cancel, back = Mock(), Mock()
    with patch.object(full_flow, 'named_element', side_effect=[RuntimeError('未找到元素'), cancel, RuntimeError('未找到导航栏'), back]), \
            patch.object(full_flow, 'check_board_context') as check, \
            patch.object(full_flow.time, 'sleep'), patch.object(ios, '_current_page_source') as source:
        full_flow.return_to_board(client, '猫粮', '畅销榜')
    cancel.tap.assert_called_once()
    back.tap.assert_called_once()
    check.assert_called_once_with(client, '猫粮', '畅销榜')
    source.assert_not_called()


def test_return_skips_back_only_when_original_navigation_is_visible():
    client = Mock()
    client.http.get.return_value.value = {'bundleId': ios.PDD_BUNDLE_ID}
    client.return_value.find_elements.return_value = [Mock()]
    with patch.object(full_flow, 'tap_with_retry') as back, patch.object(ios, '_current_page_source') as source:
        full_flow.return_to_board(client, '猫/狗保健品', '好评榜')
    client.assert_called_once_with(name='猫/狗保健品好评榜', className='NavigationBar', visible=True)
    back.assert_not_called()
    source.assert_not_called()


def test_return_still_taps_back_when_original_navigation_is_not_visible():
    client = Mock()
    client.http.get.return_value.value = {'bundleId': ios.PDD_BUNDLE_ID}
    client.return_value.find_elements.return_value = []
    with patch.object(full_flow, 'tap_with_retry') as back, \
            patch.object(full_flow, 'check_board_context') as check, patch.object(full_flow.time, 'sleep'):
        full_flow.return_to_board(client, '猫粮', '畅销榜')
    back.assert_called_once_with(client, ['返回', 'Back'], class_name='Button')
    check.assert_called_once_with(client, '猫粮', '畅销榜')


def test_ambiguous_board_does_not_click_back():
    client = Mock()
    client.http.get.return_value.value = {'bundleId': ios.PDD_BUNDLE_ID}
    client.return_value.find_elements.return_value = [Mock(), Mock()]
    with patch.object(full_flow, 'tap_with_retry') as back, \
            pytest.raises(full_flow.FatalWorkflowError, match='不唯一'):
        full_flow.return_to_board(client, '猫粮', '畅销榜')
    back.assert_not_called()


@pytest.mark.parametrize('visible', [True, False])
def test_board_context_requires_visible_original_navigation(visible):
    client = Mock()
    client.http.get.return_value.value = {'bundleId': ios.PDD_BUNDLE_ID}
    client.return_value.find_elements.return_value = [Mock()] if visible else []
    if visible:
        full_flow.check_board_context(client, '猫粮', '畅销榜')
    else:
        with pytest.raises(full_flow.FatalWorkflowError, match='未确认返回原榜单'):
            full_flow.check_board_context(client, '猫粮', '畅销榜')
    client.assert_called_once_with(name='猫粮畅销榜', className='NavigationBar', visible=True)


def test_failed_product_open_on_same_board_does_not_abort_next_product(tmp_path):
    store, rows = prepared(tmp_path)
    client = Mock()
    client.http.get.return_value.value = {'bundleId': ios.PDD_BUNDLE_ID}
    client.screenshot.side_effect = lambda: Image.new('RGB', (100, 200), 'white')
    navigation, back = Mock(), Mock()
    with patch.object(full_flow.longshot, 'reusable', return_value=True), \
            patch.object(full_flow, 'product_element'), \
            patch.object(full_flow, 'verify_product', side_effect=[RuntimeError('仍在榜单'), {'observed_title': '商品2'}]), \
            patch.object(full_flow.time, 'sleep'), \
            patch.object(full_flow, 'copy_share', return_value='https://mobile.yangkeduo.com/goods.html?goods_id=222') as share, \
            patch.object(full_flow, 'named_element', side_effect=[navigation, RuntimeError('未找到导航栏'), back, navigation]), \
            pytest.raises(RuntimeError, match='未完成') as stopped:
        full_flow.IosFullFlow(store, client).run_list(1, rows, [])
    assert not isinstance(stopped.value, full_flow.FatalWorkflowError)
    back.tap.assert_called_once()
    share.assert_called_once()
    assert [item['state'] for item in store.items(1)] == ['failed', 'completed']
    assert store.item(1)['goods_id'] is None
    assert not (tmp_path / '榜01/产品页截图.jpg').exists()
    assert store.item(2)['goods_id'] == '222'
    store.close()


@pytest.mark.parametrize('visible_after_scroll', [True, False])
def test_invisible_product_scrolls_and_rechecks_same_occurrence(tmp_path, visible_after_scroll):
    store, rows = prepared(tmp_path)
    store.item_state(2, 'completed')
    client = Mock()
    client.http.get.return_value.value = {'bundleId': ios.PDD_BUNDLE_ID}
    client.screenshot.return_value = Image.new('RGB', (100, 200), 'white')
    hidden, refreshed = Mock(displayed=False), Mock(displayed=visible_after_scroll)
    with patch.object(full_flow.longshot, 'reusable', return_value=True), \
            patch.object(full_flow, 'product_element', side_effect=[hidden] + [refreshed] * 12) as select, \
            patch.object(full_flow.longshot, 'drag_for_screenshot') as drag, \
            patch.object(ios, '_get_window_size', return_value=(414, 896)), \
            patch.object(full_flow, 'verify_product') as verify, \
            patch.object(full_flow, 'check_board_context'), \
            patch.object(full_flow, 'copy_share', return_value='https://mobile.yangkeduo.com/goods.html?goods_id=111'), \
            patch.object(full_flow, 'return_to_board', return_value=('xml', [])) as restore, patch.object(full_flow.time, 'sleep'):
        if visible_after_scroll:
            full_flow.IosFullFlow(store, client).run_list(1, rows, [])
            refreshed.tap.assert_called_once()
            verify.assert_called_once()
            restore.assert_called_once()
        else:
            with pytest.raises(RuntimeError, match='仍不可见'):
                full_flow.IosFullFlow(store, client).run_list(1, rows, [])
            refreshed.tap.assert_not_called()
            verify.assert_not_called()
            restore.assert_not_called()
    hidden.scroll.assert_not_called()
    assert drag.call_count == (1 if visible_after_scroll else 12)
    hidden.tap.assert_not_called()
    assert all(call.kwargs['occurrence'] == 1 for call in select.call_args_list)
    store.close()


def test_tap_timeout_still_checks_where_navigation_ended(tmp_path):
    store, rows = prepared(tmp_path)
    store.item_state(2, 'completed')
    client = Mock()
    client.http.get.return_value.value = {'bundleId': ios.PDD_BUNDLE_ID}
    element = Mock()
    element.tap.side_effect = RuntimeError('点击请求超时')
    with patch.object(full_flow.longshot, 'reusable', return_value=True), \
            patch.object(full_flow, 'product_element', return_value=element), \
            patch.object(full_flow, 'verify_product') as verify, \
            patch.object(full_flow, 'return_to_board', return_value=('xml', [])) as restore, \
            pytest.raises(RuntimeError, match='点击请求超时'):
        full_flow.IosFullFlow(store, client).run_list(1, rows, [])
    restore.assert_called_once()
    verify.assert_not_called()
    assert store.item(1)['state'] == 'failed'
    store.close()


def test_wda_transport_failure_stops_before_next_product(tmp_path):
    from wda.usbmux.exceptions import HTTPError
    store, rows = prepared(tmp_path)
    client = Mock()
    with patch.object(full_flow.longshot, 'reusable', return_value=True), \
            patch.object(ios, 'require_pdd_foreground'), \
            patch.object(full_flow, 'product_element', side_effect=HTTPError(TimeoutError('timed out'))) as select, \
            patch.object(full_flow, 'check_board_context'), \
            pytest.raises(full_flow.FatalWorkflowError, match='WDA 通信异常'):
        full_flow.IosFullFlow(store, client).run_list(1, rows, [])
    select.assert_called_once()
    assert store.item(2)['state'] == 'pending'
    store.close()


def test_unknown_return_context_still_stops_next_product(tmp_path):
    store, rows = prepared(tmp_path)
    client = Mock()
    client.http.get.return_value.value = {'bundleId': ios.PDD_BUNDLE_ID}
    with patch.object(full_flow.longshot, 'reusable', return_value=True), \
            patch.object(full_flow, 'product_element') as select, \
            patch.object(full_flow, 'verify_product', side_effect=RuntimeError('商品页未就绪')), \
            patch.object(full_flow, 'return_to_board', side_effect=RuntimeError('无法确认当前页面')), \
            patch.object(full_flow.time, 'sleep'), \
            pytest.raises(full_flow.FatalWorkflowError, match='无法返回原榜单'):
        full_flow.IosFullFlow(store, client).run_list(1, rows, [])
    select.assert_called_once()
    assert store.item(2)['state'] == 'pending'
    store.close()


def test_product_selection_uses_native_document_occurrence():
    client = Mock()
    client._session_http.post.return_value.value = [{'ELEMENT': 'first'}, {'ELEMENT': 'second'}]
    element = full_flow.product_element(client, '相同标题', occurrence=2)
    client._session_http.post.assert_called_once_with('/elements', {
        'using': 'predicate string', 'value': 'type == "XCUIElementTypeStaticText" AND name == "相同标题"',
    }, timeout=8)
    assert element.id == 'second'


def test_completed_products_are_not_clicked_again(tmp_path):
    store, rows = prepared(tmp_path)
    store.stage_state(1, 'ranking_long_screenshot', 'failed')
    for item in store.items(1):
        store.item_state(item['id'], 'completed')
    with patch.object(full_flow.longshot, 'reusable', return_value=True), patch.object(full_flow, 'product_element') as click, \
            patch.object(full_flow.longshot, 'capture') as capture:
        full_flow.IosFullFlow(store, Mock()).run_list(1, rows, [])
    click.assert_not_called()
    capture.assert_not_called()
    assert store.stage(1, 'ranking_long_screenshot')['state'] == 'completed'
    store.close()


def test_longshot_returns_to_top_without_reusing_stale_top_coordinates(tmp_path):
    store, rows = prepared(tmp_path)
    for item in store.items(1):
        store.item_state(item['id'], 'completed')
    order = []
    old_top_elements = [{'type': 'StaticText', 'text': '商品1', 'y': 200}]
    def top(client, main, tab, current_rows, elements):
        order.append(('top', elements))
        return 'xml', old_top_elements
    def capture(*args):
        order.append(('capture', None))
        return []
    with patch.object(full_flow.longshot, 'reusable', return_value=False), \
            patch.object(full_flow, 'prepare_top', side_effect=top), \
            patch.object(full_flow.longshot, 'capture', side_effect=capture), \
            patch.object(full_flow, 'product_element') as select:
        full_flow.IosFullFlow(store, Mock()).run_list(1, rows, old_top_elements)
    assert order == [('top', old_top_elements), ('capture', None), ('top', [])]
    select.assert_not_called()
    assert store.stage(1, 'ranking_long_screenshot')['state'] == 'completed'
    store.close()


def sku_client(quantity_results, close_buttons):
    client = Mock()
    client.http.get.return_value.value = {'bundleId': ios.PDD_BUNDLE_ID}
    quantity = Mock()
    quantity.find_elements.side_effect = quantity_results
    close = Mock()
    close.find_elements.return_value = close_buttons
    client.side_effect = lambda **kwargs: quantity if 'predicate' in kwargs else close
    return client


def test_sku_panel_is_closed_before_product_page_is_read():
    button = Mock()
    client = sku_client([[Mock()], [Mock()], []], [button])

    def page_source(_):
        button.tap.assert_called_once()
        assert client.call_args_list[-1].kwargs == {
            'name': '关闭', 'className': 'Button', 'visible': True}
        return product_xml()

    with patch.object(full_flow.time, 'sleep'), \
            patch.object(ios, '_current_page_source', side_effect=page_source):
        full_flow.verify_product(client, '商品1')


def test_product_without_sku_panel_is_not_clicked():
    client = sku_client([[]], [])
    assert full_flow.dismiss_sku_panel(client) is False
    assert client.call_count == 1


@pytest.mark.parametrize('button_count', [0, 2])
def test_sku_panel_requires_unique_visible_close_button(button_count):
    buttons = [Mock() for _ in range(button_count)]
    client = sku_client([[Mock()]], buttons)
    with pytest.raises(RuntimeError, match='唯一可见'):
        full_flow.dismiss_sku_panel(client)
    for button in buttons:
        button.tap.assert_not_called()


def test_stuck_sku_panel_prevents_product_validation(tmp_path):
    button = Mock()
    client = sku_client([[Mock()] for _ in range(9)], [button])
    with patch.object(full_flow.time, 'sleep'), \
            patch.object(full_flow.config, 'BUSINESS_ROOT', tmp_path), \
            patch.object(ios, '_current_page_source', return_value='<App />'), \
            pytest.raises(RuntimeError, match='仍未消失'):
        full_flow.verify_product(client, '商品1', attempts=1)
    button.tap.assert_called_once()


def product_xml(title='商品1', *, share_visible='true', board=''):
    return (f'<App><XCUIElementTypeStaticText label="{title}" />'
            f'<XCUIElementTypeButton label="分享" visible="{share_visible}" />'
            '<XCUIElementTypeButton label="返回" /><XCUIElementTypeButton label="收藏" />'
            f'<XCUIElementTypeNavigationBar label="{board}" /></App>')


def test_product_validation_retries_loading_and_checks_sku_each_time():
    client = Mock()
    client.http.get.return_value.value = {'bundleId': ios.PDD_BUNDLE_ID}
    with patch.object(full_flow, 'dismiss_sku_panel') as dismiss, \
            patch.object(ios, '_current_page_source', side_effect=[None, product_xml()]), \
            patch.object(full_flow.time, 'sleep'), \
            patch.object(full_flow, 'save_product_diagnostics') as diagnostic:
        full_flow.verify_product(client, '商品1')
    assert dismiss.call_count == 2
    diagnostic.assert_not_called()


@pytest.mark.parametrize(('xml', 'reason'), [
    ('<App><XCUIElementTypeButton label="分享" /></App>', '控件尚未就绪'),
    (product_xml(share_visible='false'), '可见的分享按钮'),
    (product_xml(board='猫粮畅销榜'), '榜单导航栏'),
    ('', '元素树为空'),
])
def test_product_validation_reports_failed_condition_and_saves_evidence(tmp_path, xml, reason):
    client = Mock()
    client.http.get.return_value.value = {'bundleId': ios.PDD_BUNDLE_ID}
    client.screenshot.return_value = Image.new('RGB', (20, 30), 'white')
    with patch.object(full_flow, 'dismiss_sku_panel'), \
            patch.object(ios, '_current_page_source', return_value=xml), \
            patch.object(full_flow.config, 'BUSINESS_ROOT', tmp_path), \
            pytest.raises(RuntimeError, match=reason):
        full_flow.verify_product(client, '商品1', timeout=0)
    directory = tmp_path / 'data/diagnostics'
    source, = directory.glob('*.xml')
    assert source.read_text(encoding='utf-8') == xml
    details = json.loads(source.with_suffix('.json').read_text(encoding='utf-8'))
    assert details['expected_title'] == '商品1'
    assert any(reason in value for value in details['reasons'])
    with Image.open(source.with_suffix('.png')) as image:
        assert image.size == (20, 30)


def test_diagnostic_screenshot_failure_keeps_original_validation_error(tmp_path):
    client = Mock()
    client.http.get.return_value.value = {'bundleId': ios.PDD_BUNDLE_ID}
    client.screenshot.side_effect = RuntimeError('截图接口超时')
    with patch.object(full_flow, 'dismiss_sku_panel'), \
            patch.object(ios, '_current_page_source', return_value=product_xml(share_visible='false')), \
            patch.object(full_flow.config, 'BUSINESS_ROOT', tmp_path), \
            pytest.raises(RuntimeError, match='分享按钮'):
        full_flow.verify_product(client, '商品1', attempts=1)
    path, = (tmp_path / 'data/diagnostics').glob('*.json')
    assert json.loads(path.read_text(encoding='utf-8'))['screenshot_error'] == '截图接口超时'


def test_decorated_app_title_is_accepted_and_returned():
    title = 'rtk蟹柳猫罐头整箱48罐金枪鱼猫咪零食'
    xml = product_xml().replace('<App>', f'<App><XCUIElementTypeImage label="【送赠品】{title}" />')
    reasons, page = full_flow.product_page_checks(xml, title)
    assert reasons == []
    assert page['title_matched'] is False
    assert page['observed_title'] == '【送赠品】' + title


def test_missing_or_changed_title_does_not_block_ready_product_page():
    reasons, page = full_flow.product_page_checks(product_xml('其他商品'), '旧榜单标题')
    assert reasons == []
    assert page['title_matched'] is False


def test_latest_product_link_replaces_old_identity_and_recaptures_image(tmp_path):
    store, rows = prepared(tmp_path)
    old = {'排行': 1, '标题': '商品1', '产品ID': '111',
           '产品链接': 'https://mobile.yangkeduo.com/goods.html?goods_id=111'}
    store.item_stage(1, 'link', row=old)
    store.product_state(1, 'completed', details={'info': {'goods_id': '111'}})
    screenshot = tmp_path / '榜01/产品页截图.jpg'
    Image.new('RGB', (20, 20), 'blue').save(screenshot)
    store.stage_state(1, 'product_screenshot', 'completed', item_id=1)
    # Link is pending so the current page must be checked even with an old image.
    store.stage_state(1, 'link', 'pending', item_id=1)
    client = Mock()
    client.http.get.return_value.value = {'bundleId': ios.PDD_BUNDLE_ID}
    client.screenshot.side_effect = lambda: Image.new('RGB', (100, 200), 'red')
    with patch.object(full_flow.longshot, 'reusable', return_value=True), \
            patch.object(full_flow, 'product_element'), \
            patch.object(full_flow, 'verify_product', return_value={'observed_title': '当前商品文案'}), \
            patch.object(full_flow.time, 'sleep'), \
            patch.object(full_flow, 'copy_share', side_effect=[
                'https://mobile.yangkeduo.com/goods.html?goods_id=222',
                'https://mobile.yangkeduo.com/goods.html?goods_id=333']), \
            patch.object(full_flow, 'return_to_board', return_value=('xml', [])):
        full_flow.IosFullFlow(store, client, latest_snapshot=True).run_list(1, rows, [])
    item = store.item(1)
    assert item['goods_id'] == '222'
    assert item['title'] == '商品1'
    assert json.loads(item['row_json'])['商品页标题'] == '当前商品文案'
    assert json.loads(item['row_json'])['榜单标题'] == '商品1'
    assert item['product_json'] == '{}'
    assert item['state'] == item['link_state'] == 'completed'
    with Image.open(screenshot) as image:
        red, green, blue = image.getpixel((50, 50))
        assert red > 240 and green < 10 and blue < 10
    store.close()


def test_same_rank_title_with_opaque_link_can_skip_opening_product(tmp_path):
    store, rows = prepared(tmp_path)
    for item in store.items(1):
        row = json.loads(item['row_json'])
        row.update(产品链接=f'https://mobile.yangkeduo.com/goods2.html?ps=token{item["rank"]}')
        full_flow.write_share_link(tmp_path / f'榜{item["rank"]:02d}', row)
        store.item_stage(item['id'], 'link', row=row)
        image = tmp_path / f'榜{item["rank"]:02d}/产品页截图.jpg'
        Image.new('RGB', (100, 200), 'white').save(image)
        store.stage_state(1, 'product_screenshot', 'completed', item_id=item['id'])
        store.item_state(item['id'], 'completed')
    store.save_rows(1, tmp_path, [], rows, 'ios', latest_snapshot=True)
    with patch.object(full_flow.longshot, 'reusable', return_value=True), \
            patch.object(full_flow, 'product_element') as open_product, \
            patch.object(full_flow, 'copy_share') as share:
        full_flow.IosFullFlow(store, Mock(), latest_snapshot=True).run_list(1, rows, [])
    open_product.assert_not_called()
    share.assert_not_called()
    assert all(item['state'] == 'completed' for item in store.items(1))
    store.close()


def test_refresh_recaptures_images_and_recopies_completed_links(tmp_path):
    store, rows = prepared(tmp_path)
    try:
        for item in store.items(1):
            row = json.loads(item['row_json'])
            row.update(产品ID=str(item['id']), 产品链接=f'https://mobile.yangkeduo.com/goods.html?goods_id={item["id"]}&old=1')
            full_flow.write_share_link(tmp_path / f'榜{item["rank"]:02d}', row)
            store.item_stage(item['id'], 'link', row=row)
            Image.new('RGB', (100, 200), 'white').save(tmp_path / f'榜{item["rank"]:02d}/产品页截图.jpg')
            store.stage_state(1, 'product_screenshot', 'completed', item_id=item['id'])
            store.item_state(item['id'], 'completed')
        client = Mock()
        client.http.get.return_value.value = {'bundleId': ios.PDD_BUNDLE_ID}
        client.screenshot.side_effect = lambda: Image.new('RGB', (100, 200), 'red')
        links = [f'https://mobile.yangkeduo.com/goods.html?goods_id={i}&fresh=1' for i in (1, 2)]
        with patch.object(full_flow.longshot, 'reusable', return_value=True), \
                patch.object(full_flow.longshot, 'capture', return_value=[]) as capture, \
                patch.object(full_flow, 'prepare_top', return_value=('xml', [])), \
                patch.object(full_flow, 'product_element') as element, \
                patch.object(full_flow, 'verify_product'), patch.object(full_flow.time, 'sleep'), \
                patch.object(full_flow, 'copy_share', side_effect=links) as share, \
                patch.object(full_flow, 'return_to_board', return_value=('xml', [])):
            full_flow.IosFullFlow(store, client, refresh=True, latest_snapshot=True).run_list(1, rows, [])
        capture.assert_called_once()
        assert element.call_count == share.call_count == client.screenshot.call_count == 2
        assert [item['goods_url'] for item in store.items(1)] == links
        with Image.open(tmp_path / '榜01/产品页截图.jpg') as image:
            assert image.getpixel((50, 50))[0] > 240
    finally:
        store.close()
