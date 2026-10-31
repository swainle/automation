"""通过 WS 执行一次账号登录；不导入 Cookie，不处理验证码。"""

import asyncio
import time
from urllib.parse import urlparse

from automation.com.extension_bridge import BridgeError, request
from automation.pdd.screenshot.state import VERIFICATIONS


async def select_login_tab(endpoint, tab_id=None):
    tabs = (await request(endpoint, "tabs")).get("tabs", [])
    tabs = [tab for tab in tabs if urlparse(tab["url"]).scheme == "https" and
            urlparse(tab["url"]).hostname == "mms.pinduoduo.com"]
    if tab_id is not None:
        tabs = [tab for tab in tabs if tab.get("tab_id") == tab_id]
    if len(tabs) != 1:
        raise BridgeError("select_one_pinduoduo_tab")
    return tabs[0]["tab_id"]


async def submit_login(endpoint, tab_id, credential, timeout=15):
    submitted = None
    try:
        result = await request(endpoint, "login_submit", {"tab_id": tab_id,
                               "account": credential.account, "password": credential.password})
        submitted = result.get("submitted", False)
        if result.get("status") in VERIFICATIONS:
            return {"status": result["status"], "submitted": submitted}
    except BridgeError as exc:
        # 导航可能使提交消息端口断开，此时只读取状态，绝不重发凭据或再次点击。
        if str(exc) != "login_submission_outcome_unknown":
            raise
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status = await request(endpoint, "login_status", {"tab_id": tab_id})
            if status.get("status") in {"logged_in", "login_rejected"} | VERIFICATIONS:
                return {"status": status["status"], "submitted": submitted}
        except BridgeError:
            pass
        await asyncio.sleep(0.3)
    return {"status": "result_unknown", "submitted": submitted}
