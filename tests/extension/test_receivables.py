"""隔离浏览器合成页面实测：uv run python tests/extension/test_receivables.py。"""
import asyncio
import base64
from contextlib import AsyncExitStack
from io import BytesIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from automation.com.extension_bridge import Bridge, request
from websockets.asyncio.server import serve

ROOT = Path(__file__).resolve().parents[2]


def fixture(index, hidden):
    value = "查看金额" if hidden else "待到账0.00"
    return f'''<!doctype html><meta charset="utf-8"><style>
body{{margin:0;background:white}}#mms-main{{padding-top:120px}}
#panel{{width:70vw;height:220px;background:#e0edf5}}p{{margin:0;height:30px}}
</style><div id="mms-main">{'<div></div>' * (index - 1)}
<div id="panel"><div><div><div><div></div><div></div><div></div><div>
<p>待收货</p><p>10</p><p><span id="amount" onclick="window.clicks=(window.clicks||0)+1;
setTimeout(()=>this.innerText='待到账569.05',150)">{value}</span></p>
</div></div></div></div></div></div>'''


@unittest.skipUnless(__name__ == "__main__", "仅手动运行时启动隔离测试浏览器")
class ReceivableLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_relative_panel_jpeg_and_hidden_or_visible_amount(self):
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / "data/browsers"))
        from playwright.async_api import async_playwright
        from PIL import Image

        bridge = Bridge()
        async with serve(bridge.handler, "127.0.0.1", 0) as server:
            endpoint = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
            async with async_playwright() as playwright, AsyncExitStack() as stack:
                profile = stack.enter_context(TemporaryDirectory())
                extension = ROOT / "extension"
                context = await playwright.chromium.launch_persistent_context(
                    profile, channel="chromium", headless=True, no_viewport=True,
                    args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}"])
                try:
                    worker = context.service_workers[0] if context.service_workers else await context.wait_for_event("serviceworker")
                    options = await context.new_page()
                    await options.goto(f"chrome-extension://{worker.url.split('/')[2]}/options.html")
                    await options.locator("#endpoint").fill(endpoint)
                    await options.locator("button").click()
                    for _ in range(100):
                        if bridge.extension is not None:
                            break
                        await asyncio.sleep(.1)
                    self.assertIsNotNone(bridge.extension)
                    for index, hidden in ((8, True), (9, False)):
                        with self.subTest(index=index, hidden=hidden):
                            await context.route("https://mms.pinduoduo.com/**", lambda route: route.fulfill(
                                status=200, content_type="text/html", body=fixture(index, hidden)))
                            page = await context.new_page()
                            await page.goto(f"https://mms.pinduoduo.com/home/case{index}")
                            tabs = (await request(endpoint, "tabs"))["tabs"]
                            tab_id = next(tab["tab_id"] for tab in tabs if tab["url"] == page.url)
                            state = await request(endpoint, "login_status", {"tab_id": tab_id})
                            self.assertEqual(state, {"status": "logged_in", "evidence": "receivable_panel"})
                            identity = await request(endpoint, "shop_identity", {"tab_id": tab_id,
                                "selector": "#mms-header-next .user-info-top .user-name-name > span"})
                            self.assertIsNone(identity["shop_name"])
                            panel = await request(endpoint, "receivable_panel", {"tab_id": tab_id})
                            self.assertEqual(panel["selector"], f'//*[@id="mms-main"]/*[{index}]')
                            result = await request(endpoint, "screenshot", {"tab_id": tab_id,
                                "selector": panel["selector"], "selector_type": "xpath",
                                "viewport_to_bottom": True, "format": "jpeg"})
                            dimensions = await page.evaluate("({width:innerWidth,bottom:document.querySelector('#panel').getBoundingClientRect().bottom})")
                            with Image.open(BytesIO(base64.b64decode(result["data"]))) as image:
                                self.assertEqual(image.format, "JPEG")
                                self.assertEqual(image.size, (dimensions["width"], dimensions["bottom"]))
                            amount = await request(endpoint, "receivable_amount", {"tab_id": tab_id})
                            self.assertEqual(amount["amount"], "569.05" if hidden else "0.00")
                            await request(endpoint, "receivable_amount", {"tab_id": tab_id})
                            self.assertEqual(await page.evaluate("window.clicks||0"), 1 if hidden else 0)
                            await page.close()
                            await context.unroute("https://mms.pinduoduo.com/**")
                finally:
                    await context.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
