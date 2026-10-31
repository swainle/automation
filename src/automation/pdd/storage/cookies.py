"""Cookie 文件转换及通过扩展执行的业务操作；不会自行启动浏览器。"""

import json
from pathlib import Path

from automation.com.extension_bridge import request
from automation.com.extension.client import endpoint_for_port as _endpoint

def load_cookies(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    items = raw.get("cookies") if isinstance(raw, dict) else raw
    if not isinstance(items, list) or not items:
        raise ValueError("Cookie 文件需要包含非空 cookies 数组或直接为数组")
    cookies = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Cookie 条目必须为对象")
        domain = item.get("domain", "")
        if not isinstance(domain, str) or not (
            domain.lstrip(".") == "pinduoduo.com"
            or domain.lstrip(".").endswith(".pinduoduo.com")
        ):
            continue
        if not isinstance(item.get("name"), str) or not isinstance(item.get("value"), str):
            raise ValueError("Cookie name/value 必须为字符串")
        cookie = {key: item[key] for key in ("name", "value", "domain", "path", "secure", "httpOnly") if key in item}
        cookie.setdefault("path", "/")
        expires = item.get("expires", item.get("expirationDate"))
        if not item.get("sessionCookie") and isinstance(expires, (int, float)) and expires > 0:
            cookie["expires"] = expires
        same_site = str(item.get("sameSite", "")).lower()
        if same_site in {"strict", "lax", "none", "no_restriction"}:
            cookie["sameSite"] = {"strict": "Strict", "lax": "Lax", "none": "None", "no_restriction": "None"}[same_site]
        cookies.append(cookie)
    if not cookies:
        raise ValueError("没有可导入的拼多多 Cookie")
    return cookies


async def import_cookies(path: str | Path, *, port=8765) -> dict:
    """读取 JSON，通过插件完整校验、清除旧 Cookie 后导入；不自动重试。"""
    endpoint = _endpoint(port)
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    # 不沿用 CDP 转换函数，保留 hostOnly、partitionKey 等扩展元数据。
    return await request(endpoint, "cookies_import", {"cookies": raw})


async def export_cookies(path: str | Path, *, port=8765) -> dict:
    """通过插件读取拼多多 Cookie，并保存为可再次导入的 JSON 数组。"""
    result = await request(_endpoint(port), "cookies_export")
    cookies = result.get("cookies")
    if not isinstance(cookies, list) or result.get("count") != len(cookies):
        raise ValueError("插件返回的 Cookie 数据格式无效")
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"count": len(cookies), "output": output.resolve()}


async def clear_cookies(*, port=8765) -> dict:
    """通过插件清除当前 Cookie store 中的拼多多根域及子域 Cookie。"""
    return await request(_endpoint(port), "cookies_clear")


async def get_cookies(*, port=8765) -> dict:
    """重新读取当前拼多多 Cookie，不写文件。"""
    return await request(_endpoint(port), "cookies_export")
