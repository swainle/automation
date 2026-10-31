"""Manual workflow checks; all settings come from the working directory .env."""
import asyncio
import math
import os
from pathlib import Path

import pytest
from PIL import Image

from automation.config import ROOT, env_bool, load_environment
from automation.com.browser import launch_chrome
from automation.com.extension.client import output_folder, save_png, select_tab
from automation.com.extension_bridge import request
from automation.com.extension_bridge import BridgeError
from automation.pdd.screenshot.state import VERIFICATIONS

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def environment():
    load_environment()


def test_open_browser():
    profile = os.getenv("BROWSER_USER_DATA_DIR", "").strip()
    port = os.getenv("BROWSER_DEBUG_PORT", "").strip()
    process = launch_chrome(
        url=os.getenv("BROWSER_URL") or None,
        headless=env_bool("BROWSER_HEADLESS"),
        debug_port=int(port) if port else None,
        user_data_dir=Path(profile) if profile else None,
    )
    assert process.pid > 0
    assert process.poll() in (None, 0), "Chrome 启动失败"


def test_extension_connection():
    port = int(os.getenv("AUTOMATION_WS_PORT", "8765"))
    result = asyncio.run(request(f"ws://127.0.0.1:{port}/", "status"))
    assert result.get("connected"), "Chrome 插件尚未连接"


def test_manual_captcha_completion():
    """检测当前验证码，等待人工完成后确认登录；不识别缺口或拖动滑块。"""
    async def verify():
        timeout = float(os.getenv("PDD_CAPTCHA_MANUAL_TIMEOUT", "180"))
        assert math.isfinite(timeout) and timeout > 0, "PDD_CAPTCHA_MANUAL_TIMEOUT 必须为正数"
        port = int(os.getenv("AUTOMATION_WS_PORT", "8765"))
        endpoint = f"ws://127.0.0.1:{port}/"
        tab = os.getenv("PDD_SCREENSHOT_TAB_ID", "").strip()
        tab_id = await select_tab(endpoint, int(tab) if tab else None)
        params = {"tab_id": tab_id}
        initial = await request(endpoint, "login_status", params)
        assert initial.get("status") in VERIFICATIONS, (
            f"当前状态为 {initial.get('status')}，未检测到验证码或身份验证；请先让验证码显示")
        print(f"已检测到验证页面，标签页 {tab_id}；请在浏览器手动完成验证，最多等待 {timeout:g} 秒。", flush=True)
        deadline = asyncio.get_running_loop().time() + timeout
        last_status = "verification_required"
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(.5)
            try:
                result = await request(endpoint, "login_status", params)
            except BridgeError as error:
                # 手动完成后页面跳转，内容脚本可能短暂不可用；不重发登录。
                if str(error) in {"content_script_unavailable", "login_page_not_allowed"}:
                    continue
                raise
            last_status = result.get("status")
            if last_status == "logged_in":
                print("手动验证完成，已确认后台登录成功。", flush=True)
                return
            assert last_status != "login_rejected", "验证后页面提示登录失败"
        pytest.fail(f"等待人工验证超时，最后状态：{last_status}")
    asyncio.run(verify())


def test_close_notification():
    """仅关闭已打开后台的通知，不登录、不切换 Cookie、不启动 Chrome。"""
    async def close():
        port = int(os.getenv("AUTOMATION_WS_PORT", "8765"))
        endpoint = f"ws://127.0.0.1:{port}/"
        tab = os.getenv("PDD_SCREENSHOT_TAB_ID", "").strip()
        tab_id = await select_tab(endpoint, int(tab) if tab else None)
        result = await request(endpoint, "notification_close", {"tab_id": tab_id})
        assert result.get("closed") is True, "通知关闭后仍可见"
        assert result.get("clicked") is True, "未检测到通知关闭按钮，未执行点击；请先让通知显示"
        print(f"标签页 {tab_id}：已点击通知关闭按钮，并复查通知已消失")
    asyncio.run(close())


def test_screenshot():
    async def capture():
        port = int(os.getenv("AUTOMATION_WS_PORT", "8765"))
        endpoint = f"ws://127.0.0.1:{port}/"
        tab = os.getenv("PDD_SCREENSHOT_TAB_ID", "").strip()
        tab_id = await select_tab(endpoint, int(tab) if tab else None)
        selector = os.getenv("PDD_SCREENSHOT_SELECTOR") or None
        full_page = env_bool("PDD_SCREENSHOT_FULL_PAGE")
        if selector and full_page:
            raise ValueError("PDD_SCREENSHOT_SELECTOR 与 PDD_SCREENSHOT_FULL_PAGE 不能同时设置")
        result = await request(endpoint, "screenshot", {
            "tab_id": tab_id, "selector": selector,
            "selector_type": os.getenv("PDD_SCREENSHOT_SELECTOR_TYPE", "css"), "full_page": full_page,
        })
        output = os.getenv("PDD_SCREENSHOT_OUTPUT", "").strip()
        path = Path(output) if output else output_folder("screenshot") / "screenshot.png"
        return save_png(result, path if path.is_absolute() else ROOT / path)

    saved = asyncio.run(capture())
    with Image.open(saved) as image:
        image.load()
        assert image.width > 0 and image.height > 0
    print(f"截图已保存：{saved}")
