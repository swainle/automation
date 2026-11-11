"""Isolated Chromium extension fixture; no Sogou profile, accounts, or real PDD pages."""
import asyncio
import base64
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

from PIL import Image
import pytest
from websockets.asyncio.server import serve

from automation.com.extension_bridge import Bridge, request as bridge_request
from tests.extension.test_integration import fixture_server

async def request(endpoint, action, params=None):
    return await bridge_request(endpoint, action, params, browser="chrome")



HTML = '''<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{margin:0}header{height:60px;background:#369} .card{height:500px;background:#eef}
.title{display:block;padding-top:50px}</style></head><body><div id="main">
<header><span class="Bt00keaq">畅销榜</span></header><section>
<div class="card" data-ranking-goods-id="123"><span class="title" title="测试商品">测试商品</span>
<b class="xxxVM4ts">1</b><p>￥12.30 热卖指数100 疯抢2万件</p></div>
</section></div></body></html>'''.encode('utf-8')


@pytest.mark.integration
def test_ranking_extension_reads_mobile_dom_and_captures_without_debugging_port(tmp_path):
    async def scenario():
        from playwright.async_api import async_playwright
        bridge = Bridge()
        extension = Path(__file__).resolve().parents[2] / 'extension'
        async with serve(bridge.handler, '127.0.0.1', 0, max_size=24 * 1024 * 1024) as server:
            endpoint = f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/'
            with patch('tests.extension.test_integration.FIXTURE', HTML):
                async with fixture_server() as base, async_playwright() as playwright:
                    origin = urlsplit(base)
                    url = f'{origin.scheme}://{origin.netloc}/extension-test/ranking/index.html'
                    context = await playwright.chromium.launch_persistent_context(
                        str(tmp_path / 'profile'), channel='chromium', headless=True,
                        args=[f'--disable-extensions-except={extension}', f'--load-extension={extension}'])
                    try:
                        worker = context.service_workers[0] if context.service_workers else await context.wait_for_event('serviceworker')
                        await worker.evaluate('(endpoint) => chrome.storage.local.set({endpoint})', endpoint)
                        for _ in range(100):
                            if bridge.extension is not None:
                                break
                            await asyncio.sleep(.1)
                        assert bridge.extension is not None
                        # Verify the peer-port lookup against the real extension's browser process.
                        from automation.pdd.spider import config
                        from automation.pdd.spider.runtime import Runtime
                        status = await request(endpoint, 'status')
                        with patch.object(config, 'RANKING_WS_PORT', server.sockets[0].getsockname()[1]), \
                                patch.object(config, 'SOGOU_EXECUTABLE', playwright.chromium.executable_path):
                            Runtime()._verify_ranking_browser(status['extension_peer_port'])
                        created = await request(endpoint, 'ranking_open', {'width': 390, 'height': 844, 'scale': .5})
                        assert created['scale'] == .5
                        params = {'tab_id': created['tab_id']}
                        await request(endpoint, 'ranking_navigate', {**params, 'url': url})
                        result = await request(endpoint, 'ranking_read', {**params, 'load': True})
                        assert result['tabs'] == ['畅销榜']
                        assert result['cards'][0]['goods_id'] == '123'
                        assert result['cards'][0]['title'] == '测试商品'
                        assert result['viewport'] == {'width': 390, 'height': 844}
                        screenshot = await request(endpoint, 'ranking_screenshot', {**params, 'goods_id': '123'})
                        with Image.open(BytesIO(base64.b64decode(screenshot['data']))) as image:
                            assert image.size == (390, 844)
                        await request(endpoint, 'ranking_close', params)
                        workspace = await request(endpoint, 'ranking_prepare', {'width': 390, 'height': 844, 'scale': .5})
                        reused = await request(endpoint, 'ranking_open', {'reuse_workspace': True})
                        assert reused['tab_id'] == workspace['tab_id']
                        assert reused['persistent']
                        await request(endpoint, 'ranking_close', {'tab_id': workspace['tab_id']})
                    finally:
                        await context.close()
    asyncio.run(scenario())
