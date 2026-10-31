import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path

from automation.com.extension_bridge import BridgeError, request

from automation.config import ROOT


def output_folder(feature):
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d_%H-%M-%S_%f")
    return ROOT / "data" / "extension-tests" / feature / stamp


async def select_tab(endpoint, tab_id=None):
    tabs = (await request(endpoint, "tabs")).get("tabs", [])
    if tab_id is not None:
        tabs = [tab for tab in tabs if tab.get("tab_id") == tab_id]
    if len(tabs) != 1:
        raise BridgeError("select_tab_id_required_or_no_supported_page")
    return tabs[0]["tab_id"]


def save_png(result, path):
    raw = base64.b64decode(result["data"], validate=True)
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise BridgeError("invalid_png_response")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path.resolve()


def save_jpeg(result, path):
    raw = base64.b64decode(result["data"], validate=True)
    if result.get("format") != "jpeg" or not raw.startswith(b"\xff\xd8\xff") or not raw.endswith(b"\xff\xd9"):
        raise BridgeError("invalid_jpeg_response")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".jpg.tmp")
    temp.write_bytes(raw)
    temp.replace(path)
    return path.resolve()
