"""统一处理拼多多 cookies 和商家后台 local_storage，报告不包含 value。"""

import json
from pathlib import Path

from automation.com.extension.client import endpoint_for_port
from automation.pdd.storage.local_storage import DEFAULT_URL, validate_data, validate_url
from automation.com.extension_bridge import request


def summary(snapshot):
    return {"cookies": [{key: item.get(key, "") for key in ("domain", "name", "path")}
                        for item in snapshot["cookies"]],
            "local_storage_keys": list(snapshot["local_storage"])}


def notify(report, stage, data):
    if report is not None:
        report(stage, data)


async def _page(endpoint, url, tab_id):
    params = {"url": validate_url(url)}
    if tab_id is not None:
        params["tab_id"] = tab_id
    return (await request(endpoint, "local_storage_open", params))["tab_id"]


async def _snapshot(endpoint, tab_id, url):
    cookies = await request(endpoint, "cookies_export")
    storage = await request(endpoint, "local_storage_export", {"tab_id": tab_id})
    if not isinstance(cookies.get("cookies"), list) or cookies.get("count") != len(cookies["cookies"]):
        raise ValueError("Cookie 响应格式无效")
    data = validate_data(storage.get("local_storage"))
    if storage.get("count") != len(data):
        raise ValueError("local_storage 响应数量无效")
    return {"url": url, "cookies": cookies["cookies"], "local_storage": data}


async def import_state(path: str | Path, *, port=8765, tab_id=None, report=None):
    endpoint = endpoint_for_port(port)
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict) or not isinstance(raw.get("cookies"), list):
        raise ValueError("文件必须包含 url、cookies 数组、local_storage 对象")
    url = validate_url(raw.get("url"))
    data = validate_data(raw.get("local_storage"))
    # 插件先完整校验所有 Cookie；任何字段无效时，不清除两类数据。
    await request(endpoint, "cookies_validate", {"cookies": raw["cookies"]})
    tab_id = await _page(endpoint, url, tab_id)
    before = summary(await _snapshot(endpoint, tab_id, url))
    notify(report, "before", before)
    cookies = await request(endpoint, "cookies_import", {"cookies": raw["cookies"]})
    storage = await request(endpoint, "local_storage_import", {"tab_id": tab_id, "local_storage": data})
    after = summary(await _snapshot(endpoint, tab_id, url))
    notify(report, "after", after)
    return {"before": before, "after": after, "cookies": cookies, "local_storage": storage, "tab_id": tab_id}


async def export_state(path: str | Path, *, url=DEFAULT_URL, port=8765, tab_id=None, report=None):
    endpoint = endpoint_for_port(port)
    tab_id = await _page(endpoint, url, tab_id)
    snapshot = await _snapshot(endpoint, tab_id, url)
    before = summary(snapshot)
    notify(report, "before", before)
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    after = summary(await _snapshot(endpoint, tab_id, url))
    notify(report, "after", after)
    return {"before": before, "after": after, "output": output.resolve(), "tab_id": tab_id}


async def clear_state(*, url=DEFAULT_URL, port=8765, tab_id=None, report=None):
    endpoint = endpoint_for_port(port)
    tab_id = await _page(endpoint, url, tab_id)
    before = summary(await _snapshot(endpoint, tab_id, url))
    notify(report, "before", before)
    cookies = await request(endpoint, "cookies_clear")
    storage = await request(endpoint, "local_storage_clear", {"tab_id": tab_id})
    notify(report, "cleared", {"cookies": cookies, "local_storage": storage})
    # 明细展示之后，再次独立读取两类数据，供测试确认均为空。
    after = summary(await _snapshot(endpoint, tab_id, url))
    notify(report, "after", after)
    return {"before": before, "after": after, "cookies": cookies, "local_storage": storage, "tab_id": tab_id}


def print_state_report(stage, data):
    """只打印域名、名称、路径和 key，不打印任何 value。"""
    if stage == "cleared":
        cookies = data["cookies"]
        print(f"\n已清除 Cookie：{cookies['cleared']} 条")
        for item in cookies.get("cleared_cookies", []):
            print(f"  domain={item['domain']!r} name={item['name']!r} path={item['path']!r}")
        for item in cookies.get("failed_cookies", []):
            print(f"  删除失败：domain={item['domain']!r} name={item['name']!r} path={item['path']!r}")
        print(f"已清除 local_storage：{data['local_storage']['cleared']} 项")
        for key in data['local_storage']['cleared_keys']:
            print(f"  key={key!r}")
        return
    print("\n处理前：" if stage == "before" else "\n处理后（重新读取）：")
    print(f"cookies：{len(data['cookies'])} 条")
    for item in data['cookies']:
        print(f"  domain={item['domain']!r} name={item['name']!r} path={item['path']!r}")
    print(f"local_storage：{len(data['local_storage_keys'])} 项")
    for key in data['local_storage_keys']:
        print(f"  key={key!r}")
