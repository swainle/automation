import xml.etree.ElementTree as ET
from unittest.mock import Mock, patch

import pytest

from automation.pdd.spider.android_jump import jump_target, validate_url


@pytest.mark.parametrize('url', ['file:///sdcard/test', 'https://yangkeduo.com.evil.test/x', 'https://example.com'])
def test_jump_rejects_unrelated_urls(url):
    with pytest.raises(ValueError):
        validate_url(url)


@pytest.mark.parametrize('label', ['打开拼多多APP', '打开拼多多App'])
def test_business_open_button(label):
    root = ET.fromstring(f'<hierarchy><node text="{label}" bounds="[10,20][110,60]"/></hierarchy>')
    assert jump_target(root, 1080) == (label, (60, 40))


def test_jump_cli_does_not_start_collection():
    from automation.pdd.spider import product
    url = 'https://mobile.yangkeduo.com/goods1.html?ps=abc'
    with patch('automation.pdd.spider.commands.execute', side_effect=lambda description, callback, **kw: callback(Mock(), Mock())), \
            patch('automation.pdd.spider.android_jump.jump') as jump, patch.object(product.Product, 'run') as queue:
        assert product.main(['--android', '--jump', url]) == 0
    jump.assert_called_once_with(url)
    queue.assert_not_called()


@pytest.mark.parametrize('args', [['--jump', 'https://mobile.yangkeduo.com/x'],
                                  ['--android', '--current', '--jump', 'https://mobile.yangkeduo.com/x']])
def test_jump_cli_rejects_conflicting_modes(args):
    from automation.pdd.spider import product
    with pytest.raises(SystemExit):
        product.main(args)


@pytest.mark.parametrize('label', ['继续', '允许', '打开', '在App打开', '在APP打开'])
def test_unrelated_buttons_are_not_business_jump(label):
    root = ET.fromstring(f'<hierarchy><node text="{label}" bounds="[10,20][110,60]"/></hierarchy>')
    assert jump_target(root, 1080) is None


def test_system_confirmation_requires_business_click_and_explicit_pdd_dialog():
    root = ET.fromstring('<hierarchy><node text="在拼多多中打开？"/>'
                         '<node text="打开" resource-id="android:id/button1" bounds="[10,20][110,60]"/></hierarchy>')
    assert jump_target(root, 1080) is None
    assert jump_target(root, 1080, confirm=True) == ('打开', (60, 40))
    root[0].set('text', '打开其他应用？')
    assert jump_target(root, 1080, confirm=True) is None


def test_jump_clicks_business_button_before_accepting_foreground_app(tmp_path):
    import json
    from automation.pdd.spider.android_jump import jump
    connection = Mock()
    connection.command.side_effect = ['package:chrome', '12345', '']
    device = connection.device
    device.app_current.side_effect = [{'package': 'com.android.chrome'}, {'package': 'com.xunmeng.pinduoduo'}]
    device.window_size.return_value = (1080, 2340)
    device.dump_hierarchy.side_effect = [
        '<hierarchy><node text="继续" bounds="[0,0][40,40]"/>'
        '<node text="打开拼多多APP" bounds="[10,100][210,160]"/></hierarchy>', '<hierarchy/>']
    playwright = Mock()
    context = Mock()
    playwright.chromium.connect_over_cdp.return_value.contexts = [context]
    page = context.new_page.return_value
    page.url = 'https://mobile.yangkeduo.com/goods.html?goods_id=123'
    page.content.return_value = '<html/>'
    page.evaluate.return_value = {}
    def navigate(*args, **kwargs):
        finished = next(call.args[1] for call in page.on.call_args_list if call.args[0] == 'requestfinished')
        request = Mock(resource_type='document')
        response = request.response.return_value
        response.url, response.status = page.url, 200
        response.request = request
        response.headers = {'content-type': 'text/html'}
        response.body.return_value = b'<html/>'
        finished(request)
    page.goto.side_effect = navigate
    with patch('automation.pdd.spider.android_jump.AndroidConnection', return_value=connection), \
            patch('playwright.sync_api.sync_playwright') as start, \
            patch('automation.pdd.spider.android_jump.time.sleep'):
        start.return_value.__enter__.return_value = playwright
        jump(page.url, tmp_path, close_page=True)
    device.click.assert_called_once_with(110, 130)
    page.route.assert_not_called()
    assert (tmp_path / 'responses/001.txt').read_bytes() == b'<html/>'
    page.close.assert_called_once()
    actions = json.loads((tmp_path / '跳转操作.json').read_text(encoding='utf-8'))
    assert actions == [{'label': '打开拼多多APP', 'point': [110, 130], 'kind': 'business_open'}]
    assert json.loads((tmp_path / '结果.json').read_text(encoding='utf-8'))['app_opened'] is True
