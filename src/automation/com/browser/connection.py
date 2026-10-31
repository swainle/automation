"""只连接用户已启动的调试浏览器，不创建临时目录。"""

import re


def connect_context(playwright, port=9222):
    if not 1024 <= port <= 65535:
        raise ValueError("调试端口无效")
    browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=5000)
    if not browser.contexts:
        raise ValueError("调试浏览器没有可用的上下文")
    return browser.contexts[0]


def import_cookies(context, cookies):
    context.clear_cookies(domain=re.compile(r"(^|\.)pinduoduo\.com$"))
    context.add_cookies(cookies)


def export_cookies(context):
    return [cookie for cookie in context.cookies()
            if cookie["domain"].lstrip(".") == "pinduoduo.com"
            or cookie["domain"].lstrip(".").endswith(".pinduoduo.com")]
