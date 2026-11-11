"""Real extension network/DOM APIs against an isolated local product fixture."""
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from websockets.asyncio.server import serve
from automation.com.extension_bridge import Bridge, request as bridge_request

async def request(endpoint, action, params=None):
    return await bridge_request(endpoint, action, params, browser="chrome")



HTML = '''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<style>.sku-plus1{display:none;width:300px;height:250px}.F7sZG3xe{display:block;width:120px;height:40px}
#goods-comments-list{display:none} li{background:rgb(240,240,240)}</style></head><body><div id="main">
<button onclick="document.querySelector('.sku-plus1').style.display='block'">立即购买</button>
<div class="sku-plus1"><div class="bIhLWVqm"><span class="sku-specs-key">口味</span>
<span class="F7sZG3xe" onclick="const clone=this.cloneNode(true);clone.classList.add('hr353bdX');this.replaceWith(clone)">鸡肉</span></div>
<img src="/image.jpg"><span class="ujEqGzEB">￥2.50</span>
<button class="GvCXfXbo" onclick="document.querySelector('.sku-plus1').style.display='none'">关闭</button></div>
<div>商品评价</div><span onclick="document.querySelector('#goods-comments-list').style.display='block';fetch('/proxy/api/reviews/123/list')">查看全部</span>
<div id="goods-comments-list"><ul><li class="pEu3DjzW" onclick="fetch('/proxy/api/reviews/123/list?negative=1')">难吃</li></ul></div>
</div><script>fetch('/extension-test/product/api/goods?goods_id=123');</script></body></html>'''.encode('utf-8')


@asynccontextmanager
async def server_fixture():
    async def handle(reader, writer):
        try:
            raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
            path = raw.split(b' ')[1].decode()
            if '/api/goods' in path:
                body, content = json.dumps({'goods_id': 123, 'goods_name': 'fixture'}).encode(), b'application/json'
            elif '/proxy/api/reviews/' in path:
                body, content = json.dumps({'data': [{'review_id': 'negative' if 'negative' in path else 'normal'}]}).encode(), b'application/json'
            else:
                body, content = HTML, b'text/html; charset=utf-8'
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: ' + content + b'\r\nConnection: close\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(handle, '127.0.0.1', 0)
    async with server:
        yield f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/extension-test/product/goods.html'


@pytest.mark.integration
def test_plugin_product_network_sku_and_review_phases(tmp_path):
    async def scenario():
        from playwright.async_api import async_playwright
        extension = Path(__file__).resolve().parents[2] / 'extension'
        bridge = Bridge()
        async with serve(bridge.handler, '127.0.0.1', 0, max_size=24 * 1024 * 1024) as ws:
            endpoint = f'ws://127.0.0.1:{ws.sockets[0].getsockname()[1]}/'
            async with server_fixture() as url, async_playwright() as playwright:
                context = await playwright.chromium.launch_persistent_context(str(tmp_path / 'profile'), channel='chromium', headless=True,
                    args=[f'--disable-extensions-except={extension}', f'--load-extension={extension}'])
                try:
                    worker = context.service_workers[0] if context.service_workers else await context.wait_for_event('serviceworker')
                    await worker.evaluate('(endpoint) => chrome.storage.local.set({endpoint})', endpoint)
                    for _ in range(100):
                        if bridge.extension is not None:
                            break
                        await asyncio.sleep(.1)
                    opened = await request(endpoint, 'product_open', {'url': url})
                    params = {'tab_id': opened['tab_id']}
                    for _ in range(30):
                        info = await request(endpoint, 'product_network', {**params, 'kind': 'info'})
                        if info['responses']:
                            break
                        await asyncio.sleep(.1)
                    assert info['responses'][0]['data']['goods_id'] == 123
                    dims = await request(endpoint, 'product_dom', {**params, 'operation': 'sku_dimensions'})
                    assert dims['dimensions'][0]['name'] == '口味'
                    selected = await request(endpoint, 'product_dom', {**params, 'operation': 'sku_select', 'selections': [{'dimension': 0, 'option': 0}]})
                    assert selected['price'] == '￥2.50'
                    await request(endpoint, 'product_dom', {**params, 'operation': 'sku_close'})
                    phase = (await request(endpoint, 'product_reviews_begin', params))['phase']
                    await request(endpoint, 'product_dom', {**params, 'operation': 'reviews_enter'})
                    for _ in range(30):
                        reviews = await request(endpoint, 'product_network', {**params, 'kind': 'reviews', 'phase': phase})
                        if reviews['received']:
                            break
                        await asyncio.sleep(.1)
                    assert reviews['comments'][0]['review_id'] == 'normal'
                    next_phase = (await request(endpoint, 'product_reviews_begin', params))['phase']
                    await request(endpoint, 'product_dom', {**params, 'operation': 'reviews_tag', 'name': '难吃'})
                    for _ in range(30):
                        reviews = await request(endpoint, 'product_network', {**params, 'kind': 'reviews', 'phase': next_phase})
                        if reviews['received']:
                            break
                        await asyncio.sleep(.1)
                    assert reviews['comments'][0]['review_id'] == 'negative'
                    await request(endpoint, 'product_close', params)
                finally:
                    await context.close()
    asyncio.run(scenario())
