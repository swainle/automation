"""商家后台 localStorage 业务：文件读写、页面准备和插件调用。"""

import json
from pathlib import Path
from urllib.parse import urlsplit

from automation.com.extension.client import endpoint_for_port
from automation.com.extension_bridge import request

DEFAULT_URL = "https://mms.pinduoduo.com/home"


def validate_url(url):
    if not isinstance(url, str):
        raise ValueError("url 必须为商家后台地址")
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname != "mms.pinduoduo.com"
            or parts.port not in (None, 443) or parts.username is not None or parts.password is not None):
        raise ValueError("local_storage 仅允许 https://mms.pinduoduo.com")
    return url


def validate_data(data):
    if not isinstance(data, dict) or any(not isinstance(value, str) for value in data.values()):
        raise ValueError("local_storage 必须为字符串键值对象")
    return data


async def _operate(action, *, url, port, tab_id, data=None):
    endpoint = endpoint_for_port(port)
    params = {"url": validate_url(url)}
    if tab_id is not None:
        params["tab_id"] = tab_id
    page = await request(endpoint, "local_storage_open", params)
    params = {"tab_id": page["tab_id"]}
    if data is not None:
        params["local_storage"] = data
    return await request(endpoint, action, params)


async def import_local_storage(path: str | Path, *, port=8765, tab_id=None) -> dict:
    """先校验文件，再复用/打开页面，清空该来源存储后导入；保留页面。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError("文件必须包含 url 和 local_storage")
    url = validate_url(raw.get("url"))
    data = validate_data(raw.get("local_storage"))
    return await _operate("local_storage_import", url=url, port=port, tab_id=tab_id, data=data)


async def export_local_storage(path: str | Path, *, url=DEFAULT_URL, port=8765, tab_id=None) -> dict:
    """复用/打开页面，导出为包含 url、local_storage 的 JSON 文件。"""
    result = await _operate("local_storage_export", url=url, port=port, tab_id=tab_id)
    data = validate_data(result.get("local_storage"))
    if result.get("count") != len(data):
        raise ValueError("插件返回的 local_storage 数量不一致")
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"url": url, "local_storage": data}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"count": len(data), "output": output.resolve(), "tab_id": result["tab_id"]}


async def clear_local_storage(*, url=DEFAULT_URL, port=8765, tab_id=None) -> dict:
    """复用/打开页面，仅清空商家后台 localStorage，返回已清除的 key。"""
    return await _operate("local_storage_clear", url=url, port=port, tab_id=tab_id)
