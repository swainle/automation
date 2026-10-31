"""独立浏览器实测：uv run python tests/extension/test_integration.py [--headed]。"""

import argparse
import asyncio
import base64
import json
from contextlib import asynccontextmanager
from io import BytesIO
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from automation.com.extension_bridge import Bridge, request
from websockets.asyncio.server import serve

ROOT = Path(__file__).resolve().parents[2]
HEADED = False
FIXTURE = b'''<!doctype html><html><head><meta charset="utf-8"><title>Extension test</title>
<style>body{margin:0;background:#f6f8fc;min-height:2000px}#target{position:absolute;left:40px;top:80px;width:320px;height:160px;background:#136f63;color:white;font:24px sans-serif;display:grid;place-items:center}input{position:absolute;top:300px}</style></head>
<body><div id="target">Extension screenshot test</div><input id="password" type="password" value="do-not-export"></body></html>'''


@asynccontextmanager
async def fixture_server():
    async def handle(reader, writer):
        try:
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nConnection: close\r\nContent-Length: "
                         + str(len(FIXTURE)).encode() + b"\r\n\r\n" + FIXTURE)
            await writer.drain()
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/extension-test/index.html"


@unittest.skipUnless(__name__ == "__main__", "仅手动运行时启动扩展测试浏览器")
class ExtensionLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_extension_queries_and_screenshots(self):
        # 测试使用独立 Chrome for Testing，不接管用户的普通 Chrome。
        bundled = ROOT / "data" / "browsers"
        if bundled.is_dir():
            os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(bundled))
        from playwright.async_api import async_playwright
        from PIL import Image

        bridge = Bridge()
        output = ROOT / "data" / "extension-tests" / ("headed" if HEADED else "headless")
        output.mkdir(parents=True, exist_ok=True)
        async with serve(bridge.handler, "127.0.0.1", 0, max_size=24 * 1024 * 1024) as server:
            endpoint = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
            async with fixture_server() as url, async_playwright() as p:
                with TemporaryDirectory(prefix="profile-", dir=output) as profile:
                    extension = str(ROOT / "extension")
                    context = await p.chromium.launch_persistent_context(
                        profile, channel="chromium", headless=not HEADED,
                        viewport={"width": 1000, "height": 800},
                        args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}"],
                    )
                    try:
                        worker = context.service_workers[0] if context.service_workers else await context.wait_for_event("serviceworker")
                        extension_id = worker.url.split("/")[2]
                        options = await context.new_page()
                        await options.goto(f"chrome-extension://{extension_id}/options.html")
                        await options.locator("#endpoint").fill(endpoint)
                        await options.locator("button").click()
                        for _ in range(100):
                            if bridge.extension is not None:
                                break
                            await asyncio.sleep(0.1)
                        self.assertTrue(bridge.extension is not None, "插件未连接本机测试服务")
                        # 仅操作此临时配置中的合成 Cookie，验证真实 WS 和扩展 API。
                        await context.add_cookies([
                            {"name": "old", "value": "fixture", "domain": ".pinduoduo.com", "path": "/"},
                            {"name": "unrelated", "value": "fixture", "domain": "example.org", "path": "/"},
                        ])
                        exported = await request(endpoint, "cookies_export")
                        self.assertEqual(exported["count"], 1)
                        imported = await request(endpoint, "cookies_import", {"cookies": [
                            {"name": "roundtrip", "value": "fixture", "domain": ".pinduoduo.com",
                             "path": "/", "secure": True, "httpOnly": True, "sameSite": "Lax"},
                            {"name": "roundtrip", "value": "nested", "domain": "mobile.pinduoduo.com",
                             "hostOnly": True, "path": "/nested", "secure": True},
                        ]})
                        self.assertEqual(imported["cleared"], 1)
                        self.assertEqual(imported["imported"], 2)
                        snapshot = await request(endpoint, "cookies_export")
                        self.assertEqual(snapshot["count"], 2)
                        self.assertTrue(any(c["httpOnly"] for c in snapshot["cookies"]))
                        await request(endpoint, "cookies_import", {"cookies": snapshot["cookies"]})
                        cleared = await request(endpoint, "cookies_clear")
                        self.assertEqual(cleared["remaining"], 0)
                        self.assertEqual((await request(endpoint, "cookies_clear"))["cleared"], 0)
                        self.assertTrue(any(c["domain"] == "example.org" for c in await context.cookies()))
                        # 使用路由提供的合成商家页面，不访问真实站点或账号。
                        await context.route("https://mms.pinduoduo.com/**", lambda route: route.fulfill(
                            status=200, content_type="text/html", body="<!doctype html><title>Storage fixture</title>"))
                        storage_fixture_page = await context.new_page()
                        await storage_fixture_page.goto("https://mms.pinduoduo.com/home")
                        await storage_fixture_page.evaluate("localStorage.setItem('combined_fixture', 'test')")
                        await context.add_cookies([{"domain": ".pinduoduo.com", "name": "combined_cookie", "value": "test", "path": "/"}])
                        cookie_file = Path(profile) / "cookies-roundtrip.json"
                        for filename, arguments in (
                            ("test_export_cookies.py", ["--output", str(cookie_file)]),
                            ("test_import_cookies.py", ["--cookies", str(cookie_file)]),
                            ("test_clear_cookies.py", []),
                        ):
                            process = await asyncio.create_subprocess_exec(
                                sys.executable, str(ROOT / "tests" / "extension" / filename),
                                "--port", endpoint.split(":")[-1].strip("/"), *arguments,
                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                            )
                            _, stderr = await asyncio.wait_for(process.communicate(), 45)
                            self.assertEqual(process.returncode, 0, filename + " failed: " + stderr.decode(errors="replace")[-1500:])
                            if filename == "test_export_cookies.py":
                                backup = json.loads(cookie_file.read_text(encoding="utf-8"))
                                backup["cookies"].append({"domain": ".pinduoduo.com", "path": "/", "name": "expired_fixture", "value": "test", "expires": 1})
                                cookie_file.write_text(json.dumps(backup), encoding="utf-8")
                        from automation.pdd.storage.local_storage import import_local_storage, export_local_storage, clear_local_storage
                        storage_file = Path(profile) / "local-storage.json"
                        storage_file.write_text(json.dumps({"url": "https://mms.pinduoduo.com/home",
                            "local_storage": {"fixture": "value", "__proto__": "special"}}), encoding="utf-8")
                        stored = await import_local_storage(storage_file, port=int(endpoint.split(":")[-1].strip("/")))
                        self.assertEqual(stored["imported"], 2)
                        storage_tab = stored["tab_id"]
                        exported_file = Path(profile) / "storage-export.json"
                        await export_local_storage(exported_file, port=int(endpoint.split(":")[-1].strip("/")))
                        self.assertEqual(json.loads(exported_file.read_text(encoding="utf-8"))["local_storage"],
                                         {"fixture": "value", "__proto__": "special"})
                        for filename, arguments in (
                            ("test_export_local_storage.py", ["--output", str(exported_file)]),
                            ("test_import_local_storage.py", ["--input", str(exported_file)]),
                            ("test_clear_local_storage.py", []),
                        ):
                            process = await asyncio.create_subprocess_exec(
                                sys.executable, str(ROOT / "tests" / "extension" / filename),
                                "--port", endpoint.split(":")[-1].strip("/"), *arguments,
                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                            )
                            _, stderr = await asyncio.wait_for(process.communicate(), 45)
                            self.assertEqual(process.returncode, 0, filename + " failed: " + stderr.decode(errors="replace")[-1500:])
                        empty = await request(endpoint, "local_storage_export", {"tab_id": storage_tab})
                        self.assertEqual(empty["local_storage"], {})
                        for storage_page in context.pages:
                            if storage_page.url.startswith("https://mms.pinduoduo.com/"):
                                await storage_page.close()
                        print("local_storage 验证通过：复用合成商家页面、导入导出及清除；三个独立入口通过。", flush=True)
                        previous_connection = bridge.extension
                        await previous_connection.close(1001, "test_reconnect")
                        for _ in range(100):
                            if bridge.extension is not None and bridge.extension is not previous_connection:
                                break
                            await asyncio.sleep(0.1)
                        self.assertTrue(bridge.extension is not None and bridge.extension is not previous_connection,
                                        "插件断线后未自动重连")
                        page = await context.new_page()
                        await page.goto(url)
                        tabs = await request(endpoint, "tabs")
                        self.assertEqual(len(tabs["tabs"]), 1)
                        tab_id = tabs["tabs"][0]["tab_id"]
                        for selector, kind in (("#target", "css"), ('//*[@id="target"]', "xpath")):
                            data = await request(endpoint, "element", {"tab_id": tab_id, "selector": selector, "selector_type": kind})
                            self.assertEqual(data["elements"][0]["rect"]["width"], 320)
                            self.assertNotIn("text", data["elements"][0])
                        data = await request(endpoint, "element", {"tab_id": tab_id, "selector": "#password", "include_text": True})
                        self.assertNotIn("text", data["elements"][0])
                        for name, extra, expected in (
                            ("viewport", {}, (1000, 800)),
                            ("element", {"selector": "#target"}, (320, 160)),
                            ("full-page", {"full_page": True}, (1000, 2000)),
                        ):
                            image = await request(endpoint, "screenshot", {"tab_id": tab_id, **extra})
                            raw = base64.b64decode(image["data"], validate=True)
                            size = Image.open(BytesIO(raw)).size
                            if name in {"full-page", "viewport"}:
                                # CSS 内容宽度不包含系统滚动条，跨系统可能比视口窄。
                                self.assertEqual(size[1], 2000 if name == "full-page" else 800)
                                self.assertTrue(950 <= size[0] <= 1000)
                            else:
                                self.assertEqual(size, expected)
                            if name == "element":
                                self.assertEqual(Image.open(BytesIO(raw)).convert("RGB").getpixel((10, 10)), (19, 111, 99))
                            (output / f"{name}.png").write_bytes(raw)
                        print(f"扩展端到端验证通过：{'可见' if HEADED else 'headless'}，CSS/XPath、表单值排除、视口/元素/整页截图。结果：{output}", flush=True)
                        for filename, arguments, expected_file in (
                            ("test_get_element.py", ["--selector", "#target", "--output-dir", str(output / "element-probe")], output / "element-probe" / "elements.json"),
                            ("test_screenshot.py", ["--selector", "#target", "--output", str(output / "screenshot-probe.png")], output / "screenshot-probe.png"),
                        ):
                            process = await asyncio.create_subprocess_exec(
                                sys.executable, str(ROOT / "tests" / "extension" / filename),
                                "--port", endpoint.split(":")[-1].strip("/"), *arguments,
                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                            )
                            stdout, stderr = await asyncio.wait_for(process.communicate(), 45)
                            self.assertEqual(process.returncode, 0, filename + " 未通过：" + stderr.decode(errors="replace")[-2000:])
                            self.assertTrue(expected_file.is_file())
                        # 不依赖验证码固定 class 或 DOM 深度，只给按钮定位。
                        svg = base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg" width="320" height="160"><rect width="320" height="160" fill="#5c90bd"/></svg>').decode()
                        widget = f'''<style>body{{margin:0}}#widget{{margin:40px;width:320px;height:240px}}.scene{{position:relative;width:320px;height:160px}}.rail{{position:relative;margin-top:20px;width:320px;height:36px;background:#ddd}}#knob{{position:absolute;left:0;top:0;width:36px;height:36px;background:#18a}}</style>
                        <section id="widget"><div class="scene"><img id="bg" width="320" height="160" src="data:image/svg+xml;base64,{svg}"><img id="piece" style="position:absolute;left:0;top:0;width:50px;height:160px" src="data:image/svg+xml;base64,{svg}"></div><div class="rail"><div><div id="knob" class="movable handle"></div></div></div></section><div class="movable handle" id="old-knob" style="display:none;width:36px;height:36px"></div>
                        <script>window.operationCount=0;for(const type of ['click','mousedown','pointerdown','input'])document.addEventListener(type,()=>window.operationCount++);</script>'''
                        await page.set_content(widget)
                        for locator in ({"button_class": "movable"}, {"button_class": ".movable"},
                                        {"button_class": "movable handle"}, {"button_xpath": '//*[@id="knob"]'}):
                            detection = await request(endpoint, "captcha_detect", {"tab_id": tab_id, **locator})
                            self.assertEqual(detection["status"], "detected")
                            self.assertEqual(detection["backgrounds"][0]["tag"], "img")
                            self.assertEqual(len(detection["pieces"]), 1)
                            self.assertTrue(detection.get("screenshot", {}).get("data"))
                        await page.locator(".rail").evaluate("e=>{const fill=document.createElement('div');fill.style='position:absolute;left:0;top:0;width:320px;height:7px;background:#0a8';e.append(fill)}")
                        detection = await request(endpoint, "captcha_detect", {"tab_id": tab_id, "button_class": "movable"})
                        self.assertEqual(detection["status"], "detected")
                        self.assertEqual(len(detection["tracks"]), 1)
                        await page.locator(".rail").evaluate("e=>{const fill=document.createElement('div');fill.style='position:absolute;left:0;top:0;width:320px;height:7px;background:#0a8';e.append(fill)}")
                        detection = await request(endpoint, "captcha_detect", {"tab_id": tab_id, "button_class": "movable"})
                        self.assertEqual(detection["status"], "detected")
                        self.assertEqual(len(detection["tracks"]), 1)
                        process = await asyncio.create_subprocess_exec(
                            sys.executable, str(ROOT / "tests" / "pdd" / "captcha" / "test_detect.py"),
                            "--port", endpoint.split(":")[-1].strip("/"), "--button-class", "movable",
                            "--output-dir", str(output / "captcha-detect"),
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        )
                        stdout, stderr = await asyncio.wait_for(process.communicate(), 45)
                        self.assertEqual(process.returncode, 0, "验证码检测入口未通过：" + stderr.decode(errors="replace")[-2000:])
                        self.assertTrue((output / "captcha-detect" / "diagnostic.png").is_file())
                        await page.locator("#old-knob").evaluate("e=>e.style.display='block'")
                        detection = await request(endpoint, "captcha_detect", {"tab_id": tab_id, "button_class": "movable"})
                        self.assertEqual(detection["status"], "ambiguous_button")
                        self.assertIsNone(detection["container"])
                        await page.locator("#old-knob").evaluate("e=>e.style.display='none'")
                        detection = await request(endpoint, "captcha_detect", {"tab_id": tab_id, "button_class": "not-present"})
                        self.assertEqual(detection["status"], "button_not_found")
                        await page.locator(".scene").evaluate("e=>{const image=document.createElement('img');image.src=e.firstElementChild.src;image.style='position:absolute;left:0;top:8px;width:140px;height:60px';e.append(image)}")
                        detection = await request(endpoint, "captcha_detect", {"tab_id": tab_id, "button_class": "movable"})
                        self.assertEqual(detection["status"], "ambiguous")
                        self.assertEqual(len(detection["backgrounds"]), 2)
                        await page.set_content(widget)
                        await page.locator("#bg").evaluate("e=>{const replacement=document.createElement('div');replacement.style=`width:320px;height:160px;background-image:url('${e.src}')`;e.replaceWith(replacement)}")
                        detection = await request(endpoint, "captcha_detect", {"tab_id": tab_id, "button_class": "movable"})
                        self.assertEqual(detection["status"], "detected")
                        self.assertEqual(detection["backgrounds"][0]["tag"], "div")
                        await page.set_content(widget)
                        await page.locator("#bg").evaluate("e=>{const canvas=document.createElement('canvas');canvas.width=320;canvas.height=160;e.replaceWith(canvas);document.querySelector('#piece').remove()}")
                        detection = await request(endpoint, "captcha_detect", {"tab_id": tab_id, "button_class": "movable"})
                        self.assertEqual(detection["status"], "incomplete")
                        self.assertTrue(any("canvas" in warning for warning in detection["warnings"]))
                        self.assertEqual(await page.evaluate("window.operationCount"), 0)
                        print("按钮检测验证通过：class/XPath、隐藏副本、重复按钮、歧义背景、CSS 背景、canvas，不执行页面操作。", flush=True)
                        # 所有拼多多请求在浏览器内拦截，用假凭据测试，不访问真实登录服务。
                        from automation.pdd.login import submit_login
                        from automation.pdd.credentials import ShopCredential
                        login_fixture = '''<!doctype html><html><head><meta charset="utf-8"></head><body>
                        <div id="panel"><button id="switch" type="button"><span>账号登录</span></button>
                        <form id="form" hidden><input id="usernameId"><input id="passwordId" type="password"><button id="submit" disabled>登录</button></form></div>
                        <script>window.submitCount=0;window.switchCount=0;window.outcome='success';
                        document.querySelector('#switch').onclick=()=>{window.switchCount++;document.querySelector('#form').hidden=false};
                        document.querySelector('#form').oninput=()=>{document.querySelector('#submit').disabled=!(document.querySelector('#usernameId').value&&document.querySelector('#passwordId').value)};
                        document.querySelector('#form').onsubmit=e=>{e.preventDefault();window.submitCount++;
                        if(window.outcome==='verify'){document.querySelector('#panel').insertAdjacentHTML('beforeend','<div class="captcha-wrapper" style="width:300px;height:100px">请完成拼图验证</div>');return;}
                        if(window.outcome==='reject'){document.body.insertAdjacentHTML('beforeend','<p>账号或密码错误</p>');return;}
                        if(document.querySelector('#usernameId').value!=='fixture-account'||document.querySelector('#passwordId').value!=='fixture-password'){document.body.insertAdjacentHTML('beforeend','<p>账号或密码错误</p>');return;}
                        history.pushState(null,'','/home/');document.body.innerHTML='<div id="mms-header-next"><div class="user-info-top"><div class="user-name-name"><span>fixture</span></div></div></div><nav>商品管理 订单查询</nav>';
                        };</script></body></html>'''
                        async def mock_login(route):
                            await route.fulfill(status=200, content_type="text/html", body=login_fixture)
                        await context.route("https://mms.pinduoduo.com/**", mock_login)
                        await page.goto("https://mms.pinduoduo.com/login/")
                        preview = await request(endpoint, "login_probe", {"tab_id": tab_id})
                        self.assertEqual(preview["mode"], "needs_account_tab")
                        self.assertEqual(await page.evaluate("window.switchCount"), 0)
                        process = await asyncio.create_subprocess_exec(
                            sys.executable, str(ROOT / "tests" / "pdd" / "browser" / "test_login.py"),
                            "--port", endpoint.split(":")[-1].strip("/"), "--dry-run",
                            "--output-dir", str(output / "login-preview"),
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        )
                        stdout, stderr = await asyncio.wait_for(process.communicate(), 45)
                        self.assertEqual(process.returncode, 0, "登录预检查入口未通过：" + stderr.decode(errors="replace")[-2000:])
                        self.assertEqual(await page.evaluate("window.switchCount"), 0)
                        self.assertEqual(await page.evaluate("document.querySelector('#usernameId').value"), "")
                        credential = ShopCredential("fixture", "fixture-account", "fixture-password")
                        result = await submit_login(endpoint, tab_id, credential, timeout=3)
                        self.assertEqual(result["status"], "logged_in")
                        self.assertEqual(await page.evaluate("window.switchCount"), 1)
                        self.assertEqual(await page.evaluate("window.submitCount"), 1)
                        for outcome, expected in (("verify", "verification_other"), ("reject", "login_rejected")):
                            await page.goto("https://mms.pinduoduo.com/login/")
                            await page.evaluate("value=>window.outcome=value", outcome)
                            result = await submit_login(endpoint, tab_id, credential, timeout=3)
                            self.assertEqual(result["status"], expected)
                            self.assertEqual(await page.evaluate("window.submitCount"), 1)
                        await page.goto("https://mms.pinduoduo.com/login/")
                        await page.evaluate("document.body.insertAdjacentHTML('beforeend','<div class=\"captcha-wrapper\" style=\"width:300px;height:100px\">请完成验证</div>')")
                        result = await submit_login(endpoint, tab_id, credential, timeout=3)
                        self.assertEqual(result["status"], "verification_other")
                        self.assertFalse(result["submitted"])
                        self.assertEqual(await page.evaluate("window.switchCount"), 0)
                        self.assertEqual(await page.evaluate("window.submitCount"), 0)
                        print("模拟登录验证通过：切换账号模式、受控输入、单次提交、成功/拒绝/验证码状态，不访问真实账号登录。", flush=True)
                    finally:
                        await context.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headed", action="store_true")
    HEADED = parser.parse_args().headed
    unittest.main(argv=[__file__], verbosity=2)
