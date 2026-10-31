"""仅手动运行：临时浏览器、本地合成滑块，不访问真实验证码服务。"""
import asyncio
import base64
from contextlib import asynccontextmanager
from io import BytesIO
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from PIL import Image, ImageDraw
from websockets.asyncio.server import serve
from automation.com.extension_bridge import Bridge, BridgeError, request
from automation.pdd.captcha.browser import detect_yidun, recognize_yidun, drag_yidun_once

ROOT = Path(__file__).resolve().parents[3]


def fixture_images():
    image = Image.new("RGB", (640, 320), "#dddddd")
    ImageDraw.Draw(image).rectangle((360, 60, 439, 159), fill="#222222")
    output = BytesIO(); image.save(output, format="PNG")
    bg = output.getvalue()
    output = BytesIO(); Image.new("RGBA", (40, 50), "#777777").save(output, format="PNG")
    return bg, output.getvalue()


@asynccontextmanager
async def fixture_server():
    bg, piece = fixture_images()
    async def handle(reader, writer):
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            path = raw.split(b" ")[1].decode()
            if path.startswith("/bg.png"):
                body, content_type = bg, "image/png"
            else:
                source = f"http://localhost:{port}/bg.png" if "cross=1" in path else "data:image/png;base64," + base64.b64encode(bg).decode()
                piece_src = "data:image/png;base64," + base64.b64encode(piece).decode()
                body = ("""<!doctype html><style>
                body{margin:0}.yidun{position:absolute;left:40px;top:40px;width:320px;height:270px}
                .yidun_bg-img{position:absolute;left:0;top:0;width:320px;height:160px}
                .yidun_jigsaw{position:absolute;left:0;top:30px;width:40px;height:50px}
                .yidun_control{position:absolute;left:0;top:180px;width:320px;height:40px;background:#ccc}
                .yidun_slider{position:absolute;left:0;top:0;width:40px;height:40px;background:#168;cursor:move}
                .yidun_tips__text{position:absolute;top:230px}
                </style><div class="yidun"><img class="yidun_bg-img" src="BG"><img class="yidun_jigsaw" src="PIECE">
                <div class="yidun_control"><div class="yidun_slider"></div></div><div class="yidun_tips__text">拖动滑块</div></div>
                <script>
                window.presses=0;let pressed=false,start=0,dx=0;
                const knob=document.querySelector('.yidun_slider'),piece=document.querySelector('.yidun_jigsaw');
                knob.addEventListener('mousedown',e=>{pressed=true;start=e.clientX;window.presses++;});
                document.addEventListener('mousemove',e=>{if(pressed){dx=e.clientX-start;knob.style.left=dx+'px';piece.style.left=dx+'px';}});
                document.addEventListener('mouseup',()=>{if(pressed){pressed=false;if(window.forcePending)return;
                  const success=Math.abs(dx-180)<2 && !window.forceFailure;
                  document.querySelector('.yidun_tips__text').textContent=success?'验证成功':'验证失败';
                  if(success)document.querySelector('.yidun').classList.add('yidun--success');
                }});
                </script>""").replace('src="BG"', 'src="'+source+'"').replace('src="PIECE"', 'src="'+piece_src+'"').encode()
                content_type = "text/html; charset=utf-8"
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()+body)
            await writer.drain()
        except (ConnectionError, TimeoutError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        yield f"http://127.0.0.1:{port}/extension-test/yidun/index.html"


@unittest.skipUnless(__name__ == "__main__", "手动启动本地易盾模拟集成测试")
class YidunIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_ws_recognition_and_single_drag(self):
        from playwright.async_api import async_playwright
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / "data" / "browsers"))
        bridge = Bridge()
        output = ROOT / "data" / "extension-tests" / "yidun-integration"
        output.mkdir(parents=True, exist_ok=True)
        async with serve(bridge.handler, "127.0.0.1", 0) as server, fixture_server() as url, async_playwright() as p:
            port = server.sockets[0].getsockname()[1]
            endpoint = f"ws://127.0.0.1:{port}/"
            with TemporaryDirectory(dir=output, prefix="profile-") as profile:
                extension = str(ROOT / "extension")
                context = await p.chromium.launch_persistent_context(profile, channel="chromium", headless=True,
                    viewport={"width":900,"height":700}, args=[f"--disable-extensions-except={extension}",f"--load-extension={extension}"])
                try:
                    worker = context.service_workers[0] if context.service_workers else await context.wait_for_event("serviceworker")
                    options = await context.new_page()
                    await options.goto(f"chrome-extension://{worker.url.split('/')[2]}/options.html")
                    await options.locator("#endpoint").fill(endpoint); await options.locator("button").click()
                    for _ in range(100):
                        if bridge.extension is not None: break
                        await asyncio.sleep(.1)
                    self.assertIsNotNone(bridge.extension)
                    page = await context.new_page()
                    for cross in [False, True]:
                        await page.goto(url + ("?cross=1" if cross else ""))
                        kind = "screenshot" if cross else "canvas"
                        detected = await detect_yidun(port=port, output_dir=output / kind / "detect")
                        self.assertEqual(detected["probe"]["image_source"], kind)
                        recognized = await recognize_yidun(port=port, output_dir=output / kind / "recognize")
                        self.assertEqual(recognized["status"], "recognized")
                        self.assertAlmostEqual(recognized["analysis"]["distance"], 180, delta=1)
                        self.assertEqual(await page.evaluate("window.presses"), 0)
                        tab_id = detected["tab_id"]
                        with self.assertRaisesRegex(BridgeError,"yidun_challenge_changed"):
                            await request(endpoint,"yidun_drag",{"tab_id":tab_id,"distance":180,"fingerprint":"stale"})
                        self.assertEqual(await page.evaluate("window.presses"), 0)
                        result = await drag_yidun_once(port=port, output_dir=output / kind / "drag", timeout=2)
                        self.assertEqual(result["status"], "passed")
                        self.assertEqual(await page.evaluate("window.presses"), 1)
                    await page.goto(url)
                    await page.evaluate("window.forceFailure=true")
                    result = await drag_yidun_once(port=port, output_dir=output / "failed", timeout=2)
                    self.assertEqual(result["status"], "failed")
                    self.assertEqual(await page.evaluate("window.presses"), 1)
                    await page.goto(url)
                    await page.evaluate("window.forcePending=true")
                    result = await drag_yidun_once(port=port, output_dir=output / "unconfirmed", timeout=.5)
                    self.assertEqual(result["status"], "unconfirmed")
                    self.assertEqual(await page.evaluate("window.presses"), 1)
                    for mode in ["detect", "recognize", "drag"]:
                        await page.goto(url)
                        process = await asyncio.create_subprocess_exec(
                            sys.executable, str(ROOT / "tests" / "pdd" / "captcha" / f"test_yidun_{mode}.py"),
                            "--port", str(port), "--output-dir", str(output / "cli" / mode),
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                        _, stderr = await asyncio.wait_for(process.communicate(), 30)
                        self.assertEqual(process.returncode, 0, stderr.decode(errors="replace")[-2000:])
                        self.assertEqual(await page.evaluate("window.presses"), 1 if mode == "drag" else 0)
                    print("本地模拟验证通过：Canvas/跨域截图、缩放、指纹拒绝、单次拖动成功/失败/超时及三个脚本入口。")
                finally:
                    await context.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
