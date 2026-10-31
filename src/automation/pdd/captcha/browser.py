"""易盾演示页业务：插件检测、缺口识别、单次拖动及结果检查。"""

import asyncio
import json
import math
from pathlib import Path
from urllib.parse import urlsplit

from automation.pdd.storage.cookies import _endpoint
from automation.pdd.captcha.yidun import analyze_gap
from automation.com.extension.client import output_folder, save_png
from automation.com.extension_bridge import BridgeError, request


def allowed_yidun_url(value):
    u = urlsplit(value)
    return (u.scheme == "https" and u.netloc == "dun.163.com" and u.path.rstrip("/") == "/trial/jigsaw") or (
        u.scheme == "http" and u.hostname == "127.0.0.1" and u.path.startswith("/extension-test/yidun/"))


async def _select(endpoint, tab_id):
    tabs = [t for t in (await request(endpoint, "tabs"))["tabs"] if allowed_yidun_url(t["url"])]
    if tab_id is not None:
        tabs = [t for t in tabs if t["tab_id"] == tab_id]
    if len(tabs) != 1:
        raise BridgeError("select_yidun_tab_id_required")
    return tabs[0]["tab_id"]


async def _run(mode, *, port=8765, tab_id=None, output_dir=None, threshold=100, timeout=5, selectors=None):
    if not isinstance(threshold, int) or isinstance(threshold, bool) or not 0 < threshold < 255:
        raise ValueError("threshold 必须为 1 到 254 的整数")
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 15:
        raise ValueError("timeout 必须在 0 到 15 秒之间")
    folder = Path(output_dir) if output_dir else output_folder("yidun-" + mode)
    folder.mkdir(parents=True, exist_ok=True)
    report = {"status": "starting", "mode": mode, "attempts": 0, "output_dir": str(folder.resolve())}
    try:
        endpoint = _endpoint(port)
        tab_id = await _select(endpoint, tab_id)
        params = {**(selectors or {}), "tab_id": tab_id}
        probe = await request(endpoint, "yidun_probe", params)
        save_png(probe["image"], folder / "background.png")
        report.update(tab_id=tab_id, probe={k:v for k,v in probe.items() if k != "image"}, status="detected")
        if mode == "detect":
            return report
        analysis = analyze_gap(folder / "background.png", probe, folder, threshold=threshold)
        report["analysis"] = analysis
        report["status"] = analysis["status"]
        if mode == "recognize" or analysis["status"] != "recognized":
            return report
        if probe["status"] != "pending":
            report["status"] = "challenge_not_pending"
            return report
        # 指纹核对由插件在按下鼠标前再次完成；不重试发送拖动命令。
        report.update(status="drag_outcome_unknown", attempts=1)
        await request(endpoint, "yidun_drag", {**params, **probe["selectors"],
                      "fingerprint": probe["fingerprint"], "distance": analysis["distance"]})
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            state = await request(endpoint, "yidun_status", params)
            if state["status"] in {"passed", "failed"}:
                report["status"] = state["status"]
                break
            await asyncio.sleep(0.2)
        else:
            report["status"] = "unconfirmed"
        return report
    except Exception as exc:
        report["error"] = str(exc) if isinstance(exc, BridgeError) else type(exc).__name__
        if report["attempts"] == 0:
            report["status"] = "error_before_drag"
        raise
    finally:
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


async def detect_yidun(**options):
    return await _run("detect", **options)


async def recognize_yidun(**options):
    return await _run("recognize", **options)


async def drag_yidun_once(**options):
    return await _run("drag", **options)
