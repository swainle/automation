import base64
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from automation.pdd.spider import config
from automation.pdd.spider.devices import Sogou
from automation.pdd.spider.runtime import Runtime
import pytest


@pytest.mark.parametrize('heading', ['散装猫零食排行榜', '散装猫零食畅销榜'])
def test_sogou_accepts_board_heading_without_tab_suffix(heading):
    runtime = Mock(ranking_tab=10)
    runtime.ranking_request.return_value = {'heading': heading, 'cards': [
        {'goods_id': '123', 'title': '商品', 'text': '热卖指数100', 'listing': '2026-11-01'}]}
    entry = {'tab': '畅销榜', 'tab_index': 0}
    _, rows, _ = Sogou(runtime).read({'name': '来源', 'main_name': '散装猫零食',
        'url': 'https://mobile.yangkeduo.com/rank'}, entry, '2026-11-10')
    assert rows[0]['产品ID'] == '123'


def test_ranking_launch_has_no_debugging_port_and_only_closes_its_tab(tmp_path):
    executable = tmp_path / 'SogouExplorer.exe'
    executable.touch()
    runtime = Runtime()
    runtime.ranking_request = Mock(side_effect=[{'connected': False, 'protocol': 3}, {'connected': True, 'protocol': 3, 'extension_peer_port': 4444}, {'tab_id': 10, 'protocol': 2}])
    with patch.object(config, 'SOGOU_EXECUTABLE', str(executable)), patch.object(config, 'BUSINESS_ROOT', tmp_path), \
            patch('automation.pdd.spider.runtime.port_ready', return_value=False), \
            patch.object(runtime.processes, 'start') as start, \
            patch('automation.pdd.spider.runtime.subprocess.Popen') as launch, \
            patch.object(runtime, '_verify_ranking_browser') as verify, \
            patch('playwright.sync_api.sync_playwright') as playwright:
        runtime.start_ranking_browser()
        argv = launch.call_args.args[0]
        assert not any('remote-debugging' in argument for argument in argv)
        assert argv[0] == str(executable)
        assert launch.call_args_list[0].args[0][3] == 'automation.com.extension_bridge'
        start.assert_not_called()
        verify.assert_called_once_with(4444)
        playwright.assert_not_called()
        runtime.ranking_request.side_effect = None
        runtime.ranking_request.return_value = {'closed': True}
        runtime.close()
        runtime.ranking_request.assert_called_with('ranking_close', {'tab_id': 10}, timeout=35)
        launch.return_value.terminate.assert_not_called()


def test_browser_cards_and_images_use_extension_without_a_playwright_page(tmp_path):
    runtime = Mock(ranking_tab=10)
    image = BytesIO()
    Image.new('RGB', (390, 844), 'red').save(image, format='JPEG')
    snapshot = {'tabs': ['畅销榜'], 'cards': [{'goods_id': '123', 'title': '商品', 'text': '热卖指数100 疯抢2万件', 'price': '12.30', 'listing': '2024-04-27'}]}
    def request(action, params):
        if action == 'ranking_read':
            return snapshot
        if action == 'ranking_screenshot':
            return {'format': 'jpeg', 'data': base64.b64encode(image.getvalue()).decode()}
        return {}
    runtime.ranking_request.side_effect = request
    device = Sogou(runtime)
    board = {'name': '主榜', 'url': 'https://mobile.yangkeduo.com/rank'}
    entry = {'tab': '畅销榜', 'tab_index': 0, 'has_sub': False, 'directory': ''}
    with patch.object(config, 'OUTPUT_ROOT', tmp_path):
        assert device.discover(board)[0].tab == '畅销榜'
        header, rows, directory = device.read(board, entry, '2026-10-31')
        assert rows[0]['销量'] == 20000
        assert rows[0]['产品ID'] == '123'
        assert '热卖指数' in header
        entry['directory'] = str(directory)
        device.prepare(board, entry, '2026-10-31')
        device.screenshot(rows[0])
        device.crop(rows[0])
        with Image.open(directory / '榜01/产品截图.jpg') as cropped:
            assert cropped.size == (390, 442)
    runtime.open_browser.assert_not_called()


def test_prepared_browser_and_workspace_are_reused_and_remain_open(tmp_path):
    executable = tmp_path / 'SogouExplorer.exe'
    executable.touch()
    runtime = Runtime()
    runtime.ranking_request = Mock(side_effect=[{'connected': True, 'protocol': 3, 'extension_peer_port': 4444},
                                              {'tab_id': 8, 'persistent': True, 'reused': True, 'protocol': 2}])
    with patch.object(config, 'SOGOU_EXECUTABLE', str(executable)), \
            patch('automation.pdd.spider.runtime.port_ready', return_value=True), \
            patch.object(runtime, '_verify_ranking_browser'), \
            patch.object(runtime.processes, 'start') as start, \
            patch('automation.pdd.spider.runtime.subprocess.Popen') as launch:
        runtime.start_ranking_browser(persistent=True)
        assert runtime.ranking_tab == 8
        assert runtime._ranking_keep_open
        assert runtime.ranking_request.call_args.args[0] == 'ranking_prepare'
        runtime.close()
        assert runtime.ranking_request.call_count == 2
        launch.assert_not_called()
        start.assert_not_called()
