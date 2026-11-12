"""iOS 自动化管理模块（升级版）。

职责：
1. 设备检测（usbmuxd 层面）
2. 管理完整 WDA 生命周期：隧道 → runwda → 端口转发（断了自动拉起）
3. 驱动 Safari 打开链接（facebook-wda）
4. 读取 App 元素、截图及获取分享链接

用法：
    from automation.pdd.spider.ios import ensure_wda

    client = ensure_wda()                    # 自动检查/拉起三层，返回 WDA 客户端

独立准备：uv run python -m automation.pdd.spider.ios

前置条件：
- 首次安装需要 tools/ios/signing/certificate_设备UDID.zip 和 WDA 预编译包
- 手机：开发者模式开、证书已信任、解锁（锁屏会断 USB，需在「面容ID与密码」里打开 USB 配件）
"""
from __future__ import annotations

import json
import atexit
import os
import re
import subprocess
import time
from pathlib import Path

from automation.pdd.spider.config import BUSINESS_ROOT, TOOLS_ROOT

PROJECT_ROOT = BUSINESS_ROOT

from automation.pdd.spider.processes import ProcessManager, go_ios_path

IOS_EXE = TOOLS_ROOT / 'vendor/go-ios' / go_ios_path(PROJECT_ROOT).name
_WDA_PROCESSES = ProcessManager()
atexit.register(_WDA_PROCESSES.close)
WDA_PORT = 8100
WDA_URL = f"http://127.0.0.1:{WDA_PORT}"
SAFARI_BUNDLE_ID = "com.apple.mobilesafari"
PDD_BUNDLE_ID = "com.xunmeng.pinduoduo"
_WDA_BUNDLE_ID = os.environ.get("WDA_BUNDLE_ID") or "com.facebook.WebDriverAgentRunner.xctrunner"
DATA_DIR = PROJECT_ROOT / "data"


MITM_CONFIG_FILE = DATA_DIR / "mitm_config.json"


# ---------------------------------------------------------------- go-ios 基础
def device_args(args):
    """Keep device operations on the explicitly selected USB device."""
    udid = os.getenv('SPIDER_IOS_UDID', '').strip()
    if udid and args and args[0] not in {'list', 'tunnel', 'version', 'help'} and not any(arg.startswith('--udid') for arg in args):
        return [*args, f'--udid={udid}']
    return list(args)


def _ios(*args: str, timeout: float = 60) -> str:
    """运行 go-ios 并返回 stdout。显式按 UTF-8 解码（go-ios 输出 UTF-8 JSON），
    避免 Windows 默认 gbk 解码失败把 stdout 置空。"""
    return subprocess.run(
        [str(IOS_EXE), *device_args(args)], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, cwd=TOOLS_ROOT
    ).stdout


def _spawn(name: str, args: list[str]) -> int:
    """启动受主管理的 go-ios 进程，日志输出到控制台和文件。"""
    if not IOS_EXE.is_file():
        raise RuntimeError(f"找不到 go-ios：{IOS_EXE}")
    if os.name != "nt" and not os.access(IOS_EXE, os.X_OK):
        raise RuntimeError(f"go-ios 没有执行权限，请执行 chmod +x {IOS_EXE}")
    return _WDA_PROCESSES.start(name, [str(IOS_EXE), *device_args(args)], cwd=TOOLS_ROOT)


def _wait(predicate, timeout: float, desc: str) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _WDA_PROCESSES.check()
        if predicate():
            return True
        time.sleep(1)
    raise RuntimeError(f"等待超时: {desc}")


# ---------------------------------------------------------------- 检测
def device_connected() -> bool:
    """设备是否在 usbmuxd 层面可见。"""
    output = _ios('list')
    selected = os.getenv('SPIDER_IOS_UDID', '').strip()
    if selected:
        return selected in output
    return bool(re.search(r'"(?:deviceList|Devices)"\s*:\s*\[\s*[^\s\]]|"(?:Udid|udid|UDID)"\s*:\s*"[^" ]+', output))


def tunnel_up() -> bool:
    """RemotePairing 隧道是否建立。"""
    out = _ios("tunnel", "ls")
    selected = os.getenv('SPIDER_IOS_UDID', '').strip()
    if selected and selected not in out:
        return False
    return "rsdPort" in out or "udid" in out


def wda_running() -> bool:
    """设备上 WebDriverAgentRunner 是否在跑。"""
    return "WebDriverAgent" in _ios("ps", "--apps")


def wda_ready() -> bool:
    """WDA HTTP 服务是否可访问（localhost:8100）。"""
    import urllib.request
    try:
        urllib.request.urlopen(f"{WDA_URL}/status", timeout=3).read()
        return True
    except Exception:
        return False


_DETECTED_WDA_BUNDLE_ID = None


def wda_bundle_id() -> str:
    """WDA 的实际 Bundle ID：WDA_BUNDLE_ID 环境变量优先，否则查设备应用列表（结果缓存）。

    重新签名安装的 WDA 不一定是 go-ios 默认的 com.facebook.WebDriverAgentRunner.xctrunner；
    runwda 启动与剪贴板前台切换都必须使用实际 ID。
    """
    global _DETECTED_WDA_BUNDLE_ID
    configured = os.environ.get("WDA_BUNDLE_ID")
    if configured:
        return configured
    if _DETECTED_WDA_BUNDLE_ID is None:
        _DETECTED_WDA_BUNDLE_ID = _detect_wda_bundle_id()
    return _DETECTED_WDA_BUNDLE_ID


def _detect_wda_bundle_id() -> str:
    """按应用名称在设备应用列表里查找 WebDriverAgent 的 Bundle ID。"""
    try:
        output = _ios("apps", "--list") or ""
        candidates = list(dict.fromkeys(
            match.group(1) for line in output.splitlines()
            if (match := re.match(r"^\s*(\S+)\s+(\S*WebDriverAgent\S*)", line))))
    except Exception as error:
        print(f"[ios] 读取设备应用列表失败，使用默认 WDA Bundle ID {_WDA_BUNDLE_ID}: {error}")
        return _WDA_BUNDLE_ID
    if not candidates:
        print(f"[ios] 未在设备上找到 WebDriverAgent 应用，使用默认 Bundle ID: {_WDA_BUNDLE_ID}")
        return _WDA_BUNDLE_ID
    if len(candidates) == 1:
        print(f"[ios] 检测到 WDA Bundle ID: {candidates[0]}")
        return candidates[0]
    raise RuntimeError("设备上安装有多个 WebDriverAgent 应用，请用环境变量 WDA_BUNDLE_ID 指定："
                       + ", ".join(candidates))


# ---------------------------------------------------------------- 生命周期管理
def ensure_wda(debug: bool = False):
    """确保 WDA 三层（隧道 → runwda → 转发）都在跑，返回 wda.Client。

    debug=True 时跳过自动拉起，仅检查 8100 就绪（三层需手动启动）。
    """
    import wda

    # 单次 WDA HTTP 请求超时上限。wda 默认 180s，太长了——真机大 WebView 的
    # source() 慢或偶发阻塞时会无输出地卡 3 分钟，看起来像"卡死"。压到 30s。
    wda.HTTP_TIMEOUT = 30.0

    if debug:
        if not wda_ready():
            raise RuntimeError(
                "[--debug] 8100 未就绪：请手动启动 隧道/runwda/转发 三个进程后重试"
            )
        print("[ios] WDA 已就绪（--debug 手动模式）")
        return wda.Client(WDA_URL)

    if not device_connected():
        raise RuntimeError("未检测到 iOS 设备：请插 USB、解锁手机、点「信任」")

    _WDA_PROCESSES.check()

    # 1. 隧道
    if not tunnel_up():
        print("[ios] 隧道未建立，启动隧道...")
        _spawn("tunnel", ["tunnel", "start", "--userspace"])
        _wait(tunnel_up, 15, "隧道建立")

    # 2. WDA 进程
    if not wda_running():
        print("[ios] WDA 未运行，启动 runwda...")
        bundle = wda_bundle_id()
        _spawn("runwda", ["runwda", f"--bundleid={bundle}", f"--testrunnerbundleid={bundle}",
                          "--xctestconfig=WebDriverAgentRunner.xctest",
                          f"--env=USE_PORT={WDA_PORT}"])
        _wait(wda_running, 30, "WDA 启动")

    # 3. 端口转发
    if not wda_ready():
        print("[ios] 8100 未通，启动端口转发...")
        _spawn("forward", ["forward", str(WDA_PORT), str(WDA_PORT)])
        _wait(wda_ready, 15, "端口转发")

    if not wda_ready():
        raise RuntimeError(
            "WDA 仍不可用：请确认手机已解锁、开发者模式已开、证书已信任"
        )

    print("[ios] WDA 已就绪")
    return wda.Client(WDA_URL)


def stop_wda() -> None:
    """停掉隧道/runwda/转发进程。"""
    _WDA_PROCESSES.close()
    print("[ios] 本次启动的 WDA 相关进程已停止")


# ---------------------------------------------------------------- Safari
# 无痕模式 UI 定位候选（不同 iOS 版本/语言可能不同，真机按需增改）
_PRIVATE_TAB_BTN = ["Tabs", "Show Tabs", "Tab Overview", "标签页"]
_PRIVATE_MENU = ["New Private Tab", "Private", "新建无痕标签页", "无痕浏览", "无痕标签页"]


def enable_safari_private(s) -> bool:
    """在已启动的 Safari session 上开启无痕模式：长按标签页按钮 → 点「新建无痕标签页」。"""
    try:
        tabs_btn = None
        for label in _PRIVATE_TAB_BTN:
            el = s(label=label, timeout=2)
            if el.exists:
                tabs_btn = el
                break
        if tabs_btn is None:
            print("  [隐私模式] 未找到标签页按钮，保持普通模式")
            return False
        tabs_btn.tap_hold(2.0)  # 长按弹出菜单
        time.sleep(1)
        for label in _PRIVATE_MENU:
            el = s(label=label, timeout=2)
            if el.exists:
                el.tap()
                time.sleep(1)
                print("  [隐私模式] 已开启无痕浏览")
                return True
        print("  [隐私模式] 未找到无痕菜单项，保持普通模式")
        return False
    except Exception as ex:
        print(f"  [隐私模式] 开启失败: {ex}")
        return False


def open_link(url: str, client=None, private: bool = False, wait: bool = True) -> None:
    """在手机 Safari 打开链接。client 不传则自动 ensure_wda。

    打开前先终止 Safari 并确认已退出，再冷启动（带 -u 参数），避免 Safari 停留前台
    导致「停在上一页」不跳转。private=True 时进入无痕模式。
    """
    if client is None:
        client = ensure_wda()

    # 终止 Safari，并轮询确认已退出（app_state.value == 1 表示未运行）
    for _ in range(5):
        try:
            client.app_terminate(SAFARI_BUNDLE_ID)
        except Exception as ex:
            print(f"  [ios] app_terminate 异常: {ex}")
        time.sleep(1)
        try:
            v = client.app_state(SAFARI_BUNDLE_ID).value
            print(f"  [ios] Safari 状态: {v} (1=已退出)")
            if v == 1:
                break
        except Exception as ex:
            print(f"  [ios] app_state 查询异常: {ex}")

    time.sleep(1)  # 等 Safari 完全退出

    if private:
        s = client.session(SAFARI_BUNDLE_ID)
        enable_safari_private(s)
        s.open_url(url)
    else:
        client.session(SAFARI_BUNDLE_ID, arguments=["-u", url])
    print(f"已在 Safari 打开: {url}")
    if wait:
        from automation.pdd.spider.config import PAGE_LOAD_WAIT
        time.sleep(PAGE_LOAD_WAIT)  # 接口采集仍保留原等待行为


# ---------------------------------------------------------------- 榜单接口数据


# ---------------------------------------------------------------- mitmproxy 监听进程


def _write_mitm_config(updates: dict) -> None:
    """增量更新 data/mitm_config.json（供 mitm.py 的 DynamicUpstream addon 动态读取）。"""
    cfg = {}
    try:
        cfg = json.loads(MITM_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    cfg.update(updates)
    MITM_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    MITM_CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


def _fetch_qg_ip(key: str, password: str | None = None) -> str | None:
    """通过青果 key 提取一个短效 IP，返回 server（ip:port）。"""
    import urllib.request
    url = f"https://share.proxy.qg.net/get?key={key}"
    if password:
        url += f"&pwd={password}"
    try:
        resp = urllib.request.urlopen(url, timeout=15).read()
        data = json.loads(resp.decode("utf-8"))
        if data.get("code") == "SUCCESS" and data.get("data"):
            item = data["data"][0]
            server = item.get("server")
            print(f"[ios] 提取IP: {item.get('proxy_ip')} -> {server} "
                  f"({item.get('area')} {item.get('isp')})")
            return server
        print(f"[ios] 提取IP 失败: {data.get('code')}")
    except Exception as ex:
        print(f"[ios] 提取IP 异常: {ex}")
    return None


def _update_proxy_config(upstream: str, auth: str | None) -> None:
    """更新 data/mitm_config.json 里的上游信息（供 mitm.py 的 DynamicUpstream addon 读取）。"""
    cfg = {}
    try:
        cfg = json.loads(MITM_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    cfg["upstream"] = upstream
    if auth:
        cfg["upstream_auth"] = auth
    else:
        cfg.pop("upstream_auth", None)
    MITM_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    MITM_CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    print(f"[ios] 已切换上游: {upstream}")


# ---------------------------------------------------------------- iOS 榜单采集（接口版）


# ---------------------------------------------------------------- iOS 榜单采集（element 版）
def _configure_source(client):
    """Disable idle waits once, without fetching a slow element snapshot."""
    _WDA_PROCESSES.check()
    if not client.__dict__.get("_source_configured", False):
        caps = {"shouldWaitForQuiescence": False, "waitForIdleTimeout": 0}
        result = client.http.post("session", {
            "capabilities": {"alwaysMatch": caps}, "desiredCapabilities": caps,
        })
        if isinstance(result.sessionId, str) and result.sessionId:
            client.session_id = result.sessionId
        client._session_http.post("/appium/settings", {"settings": {
            "waitForIdleTimeout": 0, "animationCoolOffTimeout": 0,
        }})
        client._source_configured = True


def _current_page_source(client) -> str | None:
    """读当前 Safari 页面的 source（XCUI 元素树）。

    首次读取前创建关闭页面静止等待的 session；后续复用，失败时重新建会话兜底。
    WebView 页面常有持续动画（倒计时/跑马灯/直播标签），WDA 默认等待 quiescence
    会让 source()/tap() 一直卡着不返回，这里显式关掉。
    """
    _configure_source(client)
    errors = []
    try:
        from automation.pdd.spider.logging_setup import operation
        with operation("[ios] 读取页面元素"):
            source = client.source()
        client.__dict__['_last_page_source'] = source
        return source
    except Exception as e:
        errors.append(f"global: {e}")
    try:
        # Session recreation resets idle settings; apply the same fast setup again.
        with operation("[ios] 重建会话并重试读取页面元素"):
            client.__dict__.pop('_source_configured', None)
            _configure_source(client)
            source = client.source()
        client.__dict__['_last_page_source'] = source
        return source
    except Exception as e:
        errors.append(f"session: {e}")
    print(f"[ios] 读取页面 source 失败: {' | '.join(errors)}")
    return None


def _xcui_texts(xml: str) -> list[dict]:
    """解析 XCUI source XML，返回按 y 坐标排序的文本元素列表。

    每个元素含 type / text（label 或 name 或 value）/ x / y。
    """
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml)
    elems = []
    for order, el in enumerate(root.iter()):
        typ = el.tag.replace("XCUIElementType", "")
        text = ((el.get("label") or "").strip()
                or (el.get("name") or "").strip()
                or (el.get("value") or "").strip())
        if not text:
            continue
        try:
            x = float(el.get("x") or 0)
            y = float(el.get("y") or 0)
            w = float(el.get("width") or 0)
            h = float(el.get("height") or 0)
        except ValueError:
            x = y = w = h = 0.0
        elems.append({"type": typ, "text": text, "x": x, "y": y, "w": w, "h": h, "order": order})
    elems.sort(key=lambda e: (e["y"], e["x"]))
    return elems


def _guess_tab(elems: list[dict]) -> str | None:
    """从页面文本里猜当前子榜单名（畅销榜/好评榜/降价榜）。"""
    joined = " ".join(e["text"] for e in elems)
    # 用卡片内容特征判断（比 tab 栏文本可靠，tab 栏三个词同时存在）
    if "热卖指数" in joined:
        return "畅销榜"
    if "好评率" in joined:
        return "好评榜"
    if "成交价" in joined or "直降" in joined:
        return "降价榜"
    # Native navigation titles remain readable even when WebView content is opaque.
    for element in elems:
        if element.get('type') == 'NavigationBar':
            for tab in ('畅销榜', '好评榜', '降价榜'):
                if element['text'].endswith(tab):
                    return tab
    return None


def _dump_diag(xml: str, elems: list[dict]) -> None:
    """诊断落盘：source + 提取的文本 + 元素类型统计，便于排查解析问题。"""
    import xml.etree.ElementTree as ET

    draft = Path("data/draft/畅销榜")
    draft.mkdir(parents=True, exist_ok=True)
    (draft / "source.xml").write_text(xml, encoding="utf-8")

    types: dict[str, int] = {}
    for el in ET.fromstring(xml).iter():
        t = el.tag.replace("XCUIElementType", "")
        types[t] = types.get(t, 0) + 1

    with (draft / "texts.txt").open("w", encoding="utf-8") as f:
        for e in elems:
            f.write(f"{e['type']:12} | ({e['x']:.0f},{e['y']:.0f}) | {e['text'][:80]!r}\n")

    print(f"[ios] 诊断已落盘 -> {draft}")
    print(f"[ios] 元素类型: {types}")
    print(f"[ios] 文本元素共 {len(elems)} 个，前 20 条：")
    for e in elems[:20]:
        print(f"[ios]   [{e['type']}] ({e['x']:.0f},{e['y']:.0f}) {e['text'][:60]!r}")


# 各榜单的卡片起始锚点（每张卡片以该特征文本开头）
_ANCHOR_BY_TAB = {
    "畅销榜": "热卖指数",
    "好评榜": "好评率",
    "降价榜": "成交价",
}
# 主榜单结束标记（之后是「更多推荐」双列商品区，不算榜单卡片）
_END_MARKS = ("更多上榜商品推荐", "更多降价商品推荐", "更多好评商品推荐", "更多推荐", "为你推荐")


def _cluster_cards(elems: list[dict], cls) -> list[list[dict]]:
    """按榜单锚点把平铺文本切成卡片，返回 [[元素, ...], ...]。"""
    document_order = bool(elems) and all('order' in element for element in elems)
    if document_order:
        elems = sorted(elems, key=lambda element: element['order'])
    anchor = _ANCHOR_BY_TAB.get(getattr(cls, "name", ""), "热卖指数")
    # 降价榜的成交价位于卡片底部，不能当作下一张卡片的起点。
    if getattr(cls, "name", "") == "降价榜":
        if document_order:
            end = next((index for index, element in enumerate(elems)
                        if element['type'] == 'StaticText' and any(marker in element['text'] for marker in _END_MARKS)), len(elems))
            anchors = [index for index, element in enumerate(elems[:end])
                       if element['type'] == 'StaticText' and re.search(r'[日天]成交价', element['text'])]
            cards = []
            start = 0
            for anchor in anchors:
                stop = anchor + 1
                # Native text may split "近", "30", "日成交价", currency and amount.
                if not re.search(r'成交价\s*[¥￥]?\s*\d', elems[anchor]['text']):
                    for index in range(anchor + 1, min(end, anchor + 8)):
                        element = elems[index]
                        if element['type'] != 'StaticText':
                            continue
                        if re.fullmatch(r'[¥￥]', element['text']):
                            stop = index + 1
                            continue
                        if re.fullmatch(r'[¥￥]?\s*\d+(?:\.\d+)?', element['text']):
                            stop = index + 1
                        break
                # Some WDA builds expose the reduction after the reference price.
                following = next((index for index in range(stop, end) if elems[index]['type'] == 'StaticText'), None)
                if following is not None and elems[following]['text'].startswith('直降'):
                    stop = following + 1
                    for index in range(stop, min(end, stop + 6)):
                        if elems[index]['type'] != 'StaticText':
                            continue
                        if re.fullmatch(r'[¥￥]', elems[index]['text']):
                            stop = index + 1
                            continue
                        if re.fullmatch(r'[¥￥]?\s*\d+(?:\.\d+)?', elems[index]['text']):
                            stop = index + 1
                        break
                card = [element for element in elems[start:stop]
                        if element['text'] not in ('畅销榜', '好评榜', '降价榜')
                        and '每日更新' not in element['text'] and not element['text'].endswith('降价榜')]
                if card:
                    cards.append(card)
                start = stop
            if cards:
                return cards
        texts = [e for e in elems if e["type"] == "StaticText"]
        end_y = min((e["y"] for e in texts if any(m in e["text"] for m in _END_MARKS)),
                    default=float("inf"))
        anchors = sorted({e["y"] for e in texts if "日成交价" in e["text"] and e["y"] < end_y})
        cards = []
        lower = float("-inf")
        for y in anchors:
            card = [e for e in texts if lower < e["y"] <= y + 12 and e["y"] < end_y
                    and e["text"] not in ("畅销榜", "好评榜", "降价榜")
                    and "每日更新" not in e["text"] and not e["text"].endswith("降价榜")]
            if card:
                cards.append(card)
            lower = y + 12
        return cards
    idxs = [i for i, e in enumerate(elems) if e["type"] == "StaticText"
            and re.search(re.escape(anchor) + r"\s*\d", e["text"])]

    end = len(elems)
    for i, e in enumerate(elems):
        if any(m in e["text"] for m in _END_MARKS):
            end = i
            break

    cards = []
    for k, idx in enumerate(idxs):
        if idx >= end:
            break
        stop = idxs[k + 1] if k + 1 < len(idxs) else end
        cards.append(elems[idx:stop])
    return cards


def _extract_title(card: list[dict]) -> str:
    """标题 = 卡片内最长文本（指标/标签/价格等都比标题短）。"""
    best = ""
    for e in card:
        t = e["text"].replace("\u200b", "").strip()
        if len(t) > len(best):
            best = t
    return best


def _extract_price(card: list[dict]) -> str:
    """价格 = 卡片内带小数点的纯数字（货币符号 ¥/￥ 是独立元素）。"""
    for e in card:
        t = e["text"].strip()
        if re.fullmatch(r"\d+\.\d+", t):
            return t
    return ""


_IOS_RANK_FIELDS = ["券后价", "展示价", "近期拼单", "销售数量",
                    "主榜单名称", "子榜单名称", "榜单统计说明"]


def _ios_rank_header(cls) -> list[str]:
    """Extra fields for iOS element output; preserve browser/API schemas."""
    return [key for key in cls("").header if key not in ("产品ID", "产品链接")] + _IOS_RANK_FIELDS


def _card_details(card: list[dict]) -> dict:
    """Keep displayed qualifiers and associate split price labels by position."""
    texts = [e for e in card if e["type"] == "StaticText"]
    result = {key: "" for key in _IOS_RANK_FIELDS[:4]}
    amount = r"\d+(?:\.\d+)?"
    for element in texts:
        value = element["text"].strip()
        combined = re.fullmatch(rf"(?:(券后|卷后)\s*)?([¥￥])\s*({amount})", value)
        if combined:
            coupon, currency, number = combined.groups()
            coupon = coupon or ""
        elif value in ("¥", "￥"):
            candidates = [e for e in texts if re.fullmatch(amount, e["text"].strip())
                          and e["x"] >= element["x"] + element["w"] - 4
                          and abs(e["y"] + e["h"] / 2 - element["y"] - element["h"] / 2) <= 16
                          and e["x"] - element["x"] <= 80]
            if not candidates:
                continue
            number = min(candidates, key=lambda e: e["x"])["text"].strip()
            coupon, currency = "", value
        else:
            continue
        markers = [e for e in texts if e["text"].strip() in ("券后", "卷后")
                   and 0 <= element["x"] - e["x"] - e["w"] <= 20
                   and abs(e["y"] + e["h"] / 2 - element["y"] - element["h"] / 2) <= 16]
        if markers and not coupon:
            coupon = min(markers, key=lambda e: element["x"] - e["x"])["text"].strip()
        field = "券后价" if coupon else "展示价"
        if not result[field]:
            result[field] = f"{coupon} {currency}{number}".strip()
    joined = re.sub(r"\s+", "", " ".join(e["text"] for e in texts))
    orders = re.search(r"(?:近|过去)?(?:24小时|一周|7天|30[天日]|一个月)(?:内)?\d+(?:\.\d+)?[万亿]?\+?人拼单", joined)
    if orders:
        result["近期拼单"] = orders.group(0)
    quantity = rf"{amount}\s*[万亿]?\s*\+?\s*件"
    sales = re.search(rf"(?:疯抢|已抢|总销|总售|已售|已销|累计售出|售出)\s*{quantity}", joined)
    if not sales:
        for element in texts:
            sales = re.fullmatch(quantity, element["text"].strip())
            if sales:
                break
    if sales:
        result["销售数量"] = sales.group(0)
    return result


def _rank_details(elems: list[dict], cls) -> dict:
    tab = getattr(cls, "name", "")
    headings = [e for e in elems if e["type"] in ("NavigationBar", "StaticText")
                and e["text"].endswith(tab) and e["text"] != tab] if tab else []
    main = ""
    if headings:
        heading = min(headings, key=lambda e: (e["type"] != "NavigationBar", len(e["text"])))
        main = heading["text"][:-len(tab)].strip()
    descriptions = [e for e in elems if e["type"] == "StaticText"
                    and "根据" in e["text"] and "更新" in e["text"]]
    description = min(descriptions, key=lambda e: e["y"])["text"] if descriptions else ""
    return {"主榜单名称": main, "子榜单名称": tab, "榜单统计说明": description}


def _fill_extra(cls, text: str, row: dict) -> None:
    """按榜单类型从卡片文本提取特有字段（正则与 rank/dom.py 对齐，补 iOS 的「万+件」写法）。"""
    name = getattr(cls, "name", "")
    if name == "畅销榜":
        mb = re.search(r"蝉联榜首\d+天|霸榜前三\d+天|霸榜\d+天|蝉联\d+天", text)
        ms = re.search(r"([\d.]+万?\+?件)", text)
        mh = re.search(r"热卖指数\s*(\d+)", text)
        from automation.pdd.spider.utils import sales_to_int
        row["霸榜天数"] = mb.group(0) if mb else ""
        row["销量"] = sales_to_int(ms.group(1)) if ms else 0
        row["热卖指数"] = mh.group(1) if mh else ""
    elif name == "好评榜":
        m = re.search(r"好评率\s*([\d.]+%)", text)
        row["好评率"] = m.group(1) if m else ""
    elif name == "降价榜":
        m1 = re.search(r"直降[\s\S]*?([¥￥]?[\d.]+)", text)
        m2 = re.search(r"近\s*30\s*日成交价\s*([¥￥]?\s*[\d.]+)", text)
        row["降价"] = m1.group(1) if m1 else ""
        row["近30天成交价"] = m2.group(1) if m2 else ""


def collect_leaderboards_ios_element(debug: bool = False, private: bool = False,
                                     rotation: str | None = None, key: str | None = None,
                                     password: str | None = None,
                                     rotate_every: int | None = None) -> None:
    """采集用户手动打开的 App 榜单，不打开 Safari 或跳转其他主榜。"""
    from automation.pdd.spider.config import OUTPUT_ROOT
    from automation.pdd.spider.status import reconcile_board, scan_output, set_worker

    scan_output(OUTPUT_ROOT)

    client = ensure_wda(debug=debug)
    xml = _current_page_source(client)
    elems = _xcui_texts(xml) if xml else []
    headings = [e for e in elems if e["type"] in ("NavigationBar", "StaticText")
                and e["text"].endswith(("畅销榜", "好评榜", "降价榜"))
                and e["text"] not in ("畅销榜", "好评榜", "降价榜")]
    if not headings or not _guess_tab(elems):
        raise RuntimeError("请先手动打开 App 榜单页面，再执行 rank 指令")
    heading = min(headings, key=lambda e: (e["type"] != "NavigationBar", len(e["text"])))
    main_name = re.sub(r"(?:畅销榜|好评榜|降价榜)$", "", heading["text"]).strip()
    if not main_name:
        raise RuntimeError("未识别到主榜名称，请检查当前榜单页面")
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    set_worker(OUTPUT_ROOT, "running", main_name)
    print(f"[ios] 采集当前 App 榜单：{main_name}")
    from automation.pdd.spider.utils import sanitize
    board_dir = OUTPUT_ROOT / sanitize(main_name)
    if board_dir.exists():
        status = reconcile_board(board_dir)
        print(f"[ios] 待续采榜单链接：{status['pending_rank']}；待采产品子榜：{status['pending_subranks']}")
    try:
        _collect_current_rank_ios(client, main_name, initial_xml=xml)
    except BaseException as ex:
        set_worker(OUTPUT_ROOT, "failed", main_name, str(ex) or "用户中断")
        raise
    else:
        set_worker(OUTPUT_ROOT, "completed", main_name)
    finally:
        if board_dir.exists():
            reconcile_board(board_dir)


def _collect_current_rank_ios(client, main_name: str, initial_xml: str | None = None) -> None:
    """iOS 榜单采集（element 版）：读当前 Safari 页面元素，解析出榜单数据写 xlsx。

    榜单字段（标题/价格/销量/热卖指数等）从 XCUI 元素树解析；
    产品链接通过点卡片进产品页 → 分享 → 复制链接 → 剪贴板获取，
    产品ID 从链接的 goods_id 参数提取。全程在手机上用 WDA 操作，不涉及电脑浏览器。

    调用前需确认已进入目标 App 榜单页。

    输出目录与其它后端一致：data/grab/pdd/{主榜单名}/{子榜单名}/{日期}/榜单数据.xlsx
    """
    from datetime import date

    from automation.pdd.spider.config import OUTPUT_ROOT
    from automation.pdd.spider.rank.dom import TAB_TO_CLASS
    from automation.pdd.spider.utils import sanitize
    from automation.pdd.spider.status import reconcile_subrank, stage_done, load_status, update_stage

    xml = initial_xml or _current_page_source(client)
    if not xml:
        raise RuntimeError("未读取到 App 榜单页面 source，请检查 WDA 和手机当前页面")

    elems = _xcui_texts(xml)
    if not elems:
        raise RuntimeError(
            "页面 source 无文本元素（WebView 黑盒，WDA 读不到 DOM）——"
            "element 版在 iOS 上需改走注入 JS 方案"
        )

    _dump_diag(xml, elems)

    # 主榜单名（提前算，供截图目录/xlsx 用）
    today = date.today().isoformat()
    tabs = [name for name in TAB_TO_CLASS
            if any(e["text"] == name for e in elems)]
    has_tabs = bool(tabs)
    if not has_tabs:
        current = _guess_tab(elems)
        if not current:
            raise RuntimeError("无法识别当前榜单，也未找到子榜标签")
        tabs = [current]

    # 提前建立所有子榜任务，避免 product 采完第一个就提前退出。
    jobs = []
    for tab_name in tabs:
        base_dir = OUTPUT_ROOT / sanitize(main_name) / sanitize(tab_name) / today
        state = reconcile_subrank(base_dir) if (base_dir / "榜单数据.xlsx").exists() else {}
        if state.get("rank_state") == "completed":
            print(f"[ios] 跳过已完成子榜：{tab_name}（{today}）")
            continue
        base_dir.mkdir(parents=True, exist_ok=True)
        reconcile_subrank(base_dir)
        jobs.append((tab_name, base_dir))

    for tab_name, base_dir in jobs:
        print(f"[ios] 切换子榜：{tab_name}")
        if has_tabs and _guess_tab(elems) != tab_name:
            elems = _switch_rank_tab(client, tab_name)
        cls = TAB_TO_CLASS[tab_name]
        rows = _parse_rows(elems, cls)
        if not rows:
            raise RuntimeError(f"{tab_name} 未解析出商品，停止以免采错榜单")
        try:
            _fill_product_links(client, rows, base_dir, _ios_rank_header(cls))
        except BaseException as ex:
            for row in rows:
                directory = base_dir / f"榜{int(row['排行']):02d}"
                if load_status(directory).get("stages", {}).get("rank", {}).get("state") == "running":
                    update_stage(directory, "rank", "failed", str(ex) or "用户中断")
            raise
        finally:
            reconcile_subrank(base_dir)
        if any(not stage_done(base_dir / f"榜{int(row['排行']):02d}", "rank") for row in rows):
            raise RuntimeError(f"{tab_name} 仍有商品未获取链接，未标记完成，请检查日志后重试")
        reconcile_subrank(base_dir)
        print(f"[ios] 子榜完成：{tab_name}，{len(rows)} 条")


def _switch_rank_tab(client, tab_name: str, *, initial_elements=None) -> list[dict]:
    """精确点击可见标签，并等待卡片内容确认切换成功。"""
    w, h = _get_window_size(client)
    if initial_elements is None:
        xml = _current_page_source(client)
        elems = _xcui_texts(xml) if xml else []
    else:
        elems = initial_elements
    candidates = [e for e in elems if e["text"] == tab_name
                  and e["type"] in ("StaticText", "Button")
                  and e["w"] > 0 and e["h"] > 0
                  and 0 <= e["x"] < w and 0 <= e["y"] < h * 0.4]
    if not candidates:
        raise RuntimeError(f"未找到可点击的子榜标签：{tab_name}")
    target = min(candidates, key=lambda e: e["w"] * e["h"])
    if not _tap_xy(client, target["x"] + target["w"] / 2,
                   target["y"] + target["h"] / 2, tab_name):
        raise RuntimeError(f"点击子榜失败：{tab_name}")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        time.sleep(0.8)
        xml = _current_page_source(client)
        elems = _xcui_texts(xml) if xml else []
        # 页面标题与商品指标同时验证，防止旧卡片残留时误判。
        heading = any(e["text"].endswith(tab_name) and e["text"] != tab_name
                      and e["type"] in ("StaticText", "NavigationBar") for e in elems)
        if heading and _guess_tab(elems) == tab_name:
            return elems
    raise RuntimeError(f"切换到{tab_name}后未确认到对应商品内容")


def _parse_rows(elems: list[dict], cls) -> list[dict]:
    """聚类卡片并提取字段（产品ID/产品链接/上架日期留空）。"""
    cards = _cluster_cards(elems, cls)
    rows = []
    rank_details = _rank_details(elems, cls)
    for i, card in enumerate(cards, 1):
        text = " ".join(e["text"] for e in card)
        title = _extract_title(card)
        row = {
            "排行": i,
            "产品ID": "",
            "产品链接": "",
            "标题": title,
            "价格": _extract_price(card),
            "上架日期": "",
        }
        for f in cls.extra_fields:
            row.setdefault(f, "")
        _fill_extra(cls, text, row)
        details = _card_details(card)
        row.update(details)
        row.update(rank_details)
        from automation.pdd.spider.metrics import from_text
        row["metrics"] = from_text(text)
        displayed = details["券后价"] or details["展示价"]
        if displayed:
            row["价格"] = re.search(r"\d+(?:\.\d+)?", displayed).group(0)
        if details["销售数量"] and "销量" in cls.extra_fields:
            from automation.pdd.spider.utils import sales_to_int
            row["销量"] = sales_to_int(details["销售数量"])
        if getattr(cls, "name", "") == "降价榜":
            # 价格元素 y 值不完全一致，按位置取对应金额，避免正则串到下一行。
            for marker, field in (("直降", "降价"), ("日成交价", "近30天成交价")):
                anchor = next((e for e in card if marker in e["text"]), None)
                if anchor:
                    numbers = [e for e in card if re.fullmatch(r"\d+(?:\.\d+)?", e["text"])
                               and e["x"] > anchor["x"]
                               and abs(e["y"] - anchor["y"]) <= (30 if marker == "直降" else 12)]
                    if numbers:
                        value = min(numbers, key=lambda e: abs(e["x"] - anchor["x"]))["text"]
                        row[field] = ("¥" if field == "近30天成交价" else "") + value
        rows.append(row)
        print(f"[ios] 排行{i} | {title[:20]} | {row['价格']}")
    return rows


# ---------------------------------------------------------------- 产品链接回填（点卡片→分享→复制链接→剪贴板）
def _find_center(client, label: str | tuple[str, ...], timeout: float = 3.0):
    """读 source 找 label 含指定文本的元素，返回其中心坐标。

    复用 _current_page_source（带 session 兜底 + 关闭 quiescence），避免 Selector
    空 session 定位失败，也避免裸 client.source() 走无 session 端点导致内容不一致。
    """
    labels = (label,) if isinstance(label, str) else label
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        xml = _current_page_source(client)
        if xml:
            elements = _xcui_texts(xml)
            for text in labels:
                candidates = [e for e in elements if text in e["text"]
                              and e["type"] in ("StaticText", "Button")
                              and e["w"] > 0 and e["h"] > 0
                              and e["x"] >= 0 and e["y"] >= 0
                              and (not _WINDOW_SIZE_CACHE or
                                   (e["x"] < _WINDOW_SIZE_CACHE[0] and e["y"] < _WINDOW_SIZE_CACHE[1]))
                              and (text not in ("复制", "拷贝") or e["text"] == text)]
                if candidates:
                    e = min(candidates, key=lambda e: (e["text"] != text, len(e["text"]), e["w"] * e["h"]))
                    return (e["x"] + e["w"] / 2, e["y"] + e["h"] / 2)
        time.sleep(0.4)
    return None


def _tap_label(client, label: str, timeout: float = 3.0) -> bool:
    """按 label 包含文本点击元素（读 source 定位后点坐标）。"""
    center = _find_center(client, label, timeout)
    if center:
        return _tap_xy(client, center[0], center[1], f"label含{label}")
    return False


def _tap_xy(client, x: float, y: float, desc: str = "") -> bool:
    """按屏幕坐标点击。"""
    try:
        from automation.pdd.spider.logging_setup import operation
        with operation(f"[ios] 点击 {desc or (x, y)}"):
            client.tap(int(x), int(y))
        return True
    except Exception as e:
        print(f"[ios] 点击 {desc or (x, y)} 失败: {e}")
        return False


def _foreground_app(bundle_id: str, timeout: float = 30) -> None:
    """Activate through go-ios; retry transient Instruments timeouts only."""
    for attempt in range(3):
        print(f"[ios]   go-ios 激活 {bundle_id}（{attempt + 1}/3，超时 {timeout:g}s）...", flush=True)
        try:
            result = subprocess.run(
                [str(IOS_EXE), *device_args(['launch', bundle_id])], capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired as error:
            failure = f'激活 {bundle_id} 超时（{timeout:g}s）'
            cause = error
        else:
            if not result.returncode:
                time.sleep(1)
                print(f"[ios]   激活命令完成：{bundle_id}", flush=True)
                return
            output = (result.stderr or result.stdout).strip()
            failure = f'激活 {bundle_id} 失败：{output[-1500:]}'
            cause = None
            if not any(word in output.lower() for word in ('timed out', 'timeout', 'connection reset', 'broken pipe')):
                raise RuntimeError(failure)
        if attempt == 2:
            raise RuntimeError(failure) from cause
        print(f'[ios]   激活通道暂未就绪，等待 2 秒后重试：{failure}', flush=True)
        time.sleep(2)


def _activate_clipboard_app(client, bundle_id):
    """Prefer the live WDA connection over the Instruments launch channel."""
    try:
        client._session_http.post('/wda/apps/activate', {'bundleId': bundle_id}, timeout=5)
        if _wait_foreground(client, bundle_id):
            time.sleep(1)
            return
    except Exception as error:
        print(f'[ios]   WDA 前台切换失败，尝试 go-ios：{error}', flush=True)
    _foreground_app(bundle_id)
    if not _wait_foreground(client, bundle_id):
        raise RuntimeError(f'未确认 {bundle_id} 进入前台')


def _read_clipboard(client) -> str:
    """读剪贴板文本。

    iOS 16+ 后台读剪贴板会被系统拦截：需要先把 WDA 切到前台再读。
    首次前台读会弹「允许粘贴」，用户设成「始终允许」后就不弹了。
    每一步都打日志，便于看出是哪个 WDA 调用超时。
    """
    import base64

    def _get_pasteboard(timeout: float = 3.0, content_type: str = "plaintext") -> str:
        try:
            print(f"[ios]   读取剪贴板 {content_type}（超时 {timeout:g}s）...", flush=True)
            v = client._session_http.post("/wda/getPasteboard", {"contentType": content_type}, timeout=timeout).value
            text = base64.b64decode(v).decode("utf-8", errors="replace")
            print(f"[ios]   剪贴板返回 {len(text)} 个字符", flush=True)
            return text
        except Exception as e:
            print(f"[ios]   直接读剪贴板失败: {e}")
            return ""

    # 1. 直接读（WDA 在后台，可能被系统拦截，此时会超时/为空）
    if not client.__dict__.get("_clipboard_requires_foreground", False):
        text = _get_pasteboard()
        if text:
            return text
        client._clipboard_requires_foreground = True

    # 2. 使用独立 go-ios 通道，不让 WDA 同步等待自身激活。
    current = ""
    switching = False
    try:
        print("[ios]   直接读剪贴板为空，切 WDA 前台再读...")
        current = client.http.get("/wda/activeAppInfo", timeout=5).value.get("bundleId", "")
        if current != PDD_BUNDLE_ID:
            raise RuntimeError(f"读剪贴板前前台不是拼多多（{current}），停止切换")
        print(f"[ios]   当前前台 App: {current}")
        switching = True
        wda_bundle = wda_bundle_id()
        _activate_clipboard_app(client, wda_bundle)
        if not _wait_foreground(client, wda_bundle):
            raise RuntimeError("激活命令结束，但 WDA 未进入前台")
        text = ""
        for attempt in range(3):
            # App 已前台不代表剪贴板服务/授权弹窗已准备完毕。
            time.sleep(0.5)
            text = _get_pasteboard()
            if text:
                break
            allowed = _allow_clipboard_paste(client)
            print(f"[ios]   前台剪贴板第 {attempt + 1}/3 次为空，"
                  f"{'已允许粘贴，重试读取' if allowed else '等待剪贴板就绪后重试'}", flush=True)
        if not text:
            text = _get_pasteboard(content_type="url")
        if not text:
            print("[ios]   前台多次读取仍为空，将切回产品页重新复制链接", flush=True)
        return text
    except Exception as e:
        print(f"[ios] 读剪贴板失败: {e}")
        return ""
    finally:
        if switching and current:
            print(f"[ios]   切回前台 App: {current}", flush=True)
            try:
                _restore_foreground(client, current)
            except Exception as ex:
                # 不在错误前台页面继续点击分享/返回。
                raise RuntimeError(f"无法恢复 {current}，请手动切回 App 后重试") from ex


def _wait_foreground(client, bundle_id: str, attempts: int = 8) -> bool:
    consecutive = 0
    for attempt in range(attempts):
        try:
            info = client.http.get("/wda/activeAppInfo", timeout=2).value
            if info.get("bundleId") == bundle_id:
                if bundle_id == PDD_BUNDLE_ID:
                    # 大商品页 source 可能耗时数秒甚至阻塞 WDA。
                    # 使用轻量应用状态；XCUIApplicationState=4 表示前台运行。
                    state = client._session_http.post(
                        "/wda/apps/state", {"bundleId": bundle_id}, timeout=3).value
                    if state != 4:
                        consecutive = 0
                        print(f"[ios]   拼多多尚未稳定在前台，应用状态={state}", flush=True)
                        time.sleep(0.5)
                        continue
                consecutive += 1
                if consecutive >= 2:
                    return True
            else:
                consecutive = 0
                print(f"[ios]   等待前台 {bundle_id}，实际为 {info.get('bundleId')}", flush=True)
        except Exception as ex:
            consecutive = 0
            print(f"[ios]   前台确认失败：{ex}", flush=True)
        if attempt + 1 < attempts:
            time.sleep(0.5)
    return False


def _restore_foreground(client, bundle_id: str) -> None:
    """激活已运行 App 并检查前台；启动命令成功不等于已切换。"""
    try:
        client._session_http.post("/wda/apps/activate", {"bundleId": bundle_id}, timeout=5)
        if _wait_foreground(client, bundle_id):
            print(f"[ios]   已确认切回 {bundle_id}", flush=True)
            return
    except Exception as ex:
        print(f"[ios]   WDA 激活失败，改用 go-ios：{ex}", flush=True)
    _foreground_app(bundle_id)
    if not _wait_foreground(client, bundle_id):
        raise RuntimeError(f"激活后前台仍不是 {bundle_id}")
    print(f"[ios]   已确认切回 {bundle_id}", flush=True)


def _allow_clipboard_paste(client) -> bool:
    """直接查系统弹窗，避免四次全文本树扫描拖延切回 App。"""
    try:
        message = client._session_http.get("/alert/text", timeout=2).value
        if not any(key in message.lower() for key in ("粘贴", "paste")):
            return False
        buttons = client._session_http.get("/wda/alert/buttons", timeout=2).value
        for label in ("允许粘贴", "Allow Paste", "允许", "Allow"):
            if label in buttons:
                client._session_http.post("/alert/accept", {"name": label}, timeout=2)
                return True
    except Exception:
        pass
    return False


def _extract_goods_id(link: str) -> str:
    """Extract an actual goods ID; an opaque ps/share token is not a goods ID."""
    m = re.search(r"goods_id=(\d+)", link)
    if m:
        return m.group(1)
    m = re.search(r"goods/(\d+)", link)
    if m:
        return m.group(1)
    return ""


_WINDOW_SIZE_CACHE = None


def require_pdd_foreground(client):
    from automation.pdd.spider.workflow import FatalWorkflowError
    info = client.http.get('/wda/activeAppInfo', timeout=3).value
    bundle = info.get('bundleId')
    if bundle == PDD_BUNDLE_ID:
        return
    # 来电/系统弹窗/锁屏可能把拼多多挤到后台：先尝试恢复一次，避免整轮白白中断。
    print(f'[ios] 拼多多已不在前台（当前 {bundle or "未知"}），尝试恢复前台...')
    try:
        try:
            if client.locked():
                print('[ios] 设备已锁屏，先解锁再恢复...')
                client.unlock()
                time.sleep(1)
        except Exception as wake_error:
            print(f'[ios] 解锁检查/解锁失败（继续尝试恢复前台）：{wake_error}')
        _restore_foreground(client, PDD_BUNDLE_ID)
    except Exception as error:
        raise FatalWorkflowError(
            f'拼多多已不在前台（当前 {bundle or "未知"}），恢复失败：{error}；请手动回到榜单后重试') from error
    info = client.http.get('/wda/activeAppInfo', timeout=3).value
    if info.get('bundleId') != PDD_BUNDLE_ID:
        raise FatalWorkflowError(
            f'拼多多已不在前台（当前 {bundle or "未知"}），恢复后仍为 {info.get("bundleId") or "未知"}；请手动回到榜单后重试')
    print('[ios] 已恢复拼多多前台，继续')


def _get_window_size(client):
    """获取窗口尺寸（缓存，避免频繁请求 window_size）。

    window_size() 是 element 流程里第一个走 session 端点的调用（source() 走的是
    无 session 的 /source），首次会触发建 session，真机上可能较慢。这里打日志，
    便于看出是不是卡在这一步。
    """
    global _WINDOW_SIZE_CACHE
    if _WINDOW_SIZE_CACHE:
        return _WINDOW_SIZE_CACHE
    print("[ios]   获取窗口尺寸（首次，走 /window/size）...")
    try:
        size = client.window_size()
        _WINDOW_SIZE_CACHE = (size.width, size.height)
        print(f"[ios]   窗口尺寸 = {size.width}x{size.height}")
    except Exception as e:
        _WINDOW_SIZE_CACHE = (390, 844)
        print(f"[ios]   获取窗口尺寸失败，用兜底 390x844: {e}")
    return _WINDOW_SIZE_CACHE


def _swipe(client, x1, y1, x2, y2, duration=0.25, *, velocity=220) -> bool:
    """优先速度可控拖动；旧 WDA 自动回退到传统拖动接口。

    旧 swipe 的 duration 仅控制起点按住时间，不控制移动速度。
    """
    from automation.pdd.spider.workflow import FatalWorkflowError

    def unsupported(error):
        detail = str(error).lower()
        return any(marker in detail for marker in (
            "unknown command", "unknowncommand", "unhandled endpoint", "unsupported operation"))

    try:
        from automation.pdd.spider.logging_setup import operation
        with operation(f"[ios] 拖动 {int(y2 - y1):+d}pt"):
            if not client.__dict__.get("_use_legacy_drag", False):
                try:
                    client._session_http.post("/wda/drag", data={
                        "fromX": int(x1), "fromY": int(y1), "toX": int(x2), "toY": int(y2),
                        "velocity": velocity,
                    })
                except Exception as error:
                    if not unsupported(error):
                        raise
                    client._use_legacy_drag = True
                    print("[ios] 当前 WDA 不支持 /wda/drag，改用传统拖动接口；后续复用此选择", flush=True)
            if client.__dict__.get("_use_legacy_drag", False):
                client.swipe(int(x1), int(y1), int(x2), int(y2), duration=duration)
        return True
    except Exception as e:
        if unsupported(e):
            raise FatalWorkflowError(
                f"WDA 不支持当前滑动接口，兼容回退失败，停止采集；请检查设备上的 WDA 版本。原始错误：{e}"
            ) from e
        print(f"[ios] 滑动失败: {e}")
        return False


def _scroll_to_top(client, first_title: str | None = None, times: int = 8,
                   initial_xml: str | None = None, force: bool = False) -> str | None:
    """把榜单页滚回顶部（连续向下滑，内容下移）。

    榜单页可能在中断时停留在中间滚动位置，先回顶再按顺序处理。
    若第一个卡片已可见（页面已在顶部附近），直接跳过，避免顶部橡皮筋回弹。
    """
    w, h = _get_window_size(client)
    xml = initial_xml or _current_page_source(client)
    if xml and not first_title and not force:
        from automation.pdd.spider.rank import TAB_TO_CLASS
        elements = _xcui_texts(xml)
        cls = TAB_TO_CLASS.get(_guess_tab(elements))
        cards = _cluster_cards(elements, cls) if cls else []
        if cards:
            first_title = _extract_title(cards[0])
    previous = None
    stalled = 0
    for _ in range(times):
        if not xml:
            return
        elems = _xcui_texts(xml)
        if not force and first_title and any(first_title[:12] in e["text"]
                               and h * 0.2 <= e["y"] < h * 0.6 for e in elems):
            print("[ios]   首卡已在顶部区域，跳过回顶")
            return xml
        signature = _scroll_signature(elems)
        stalled = stalled + 1 if signature == previous else 0
        if stalled >= (2 if force else 1):
            print("[ios]   回顶停止：连续滑动无位移")
            return xml
        previous = signature
        if not _swipe(client, w / 2, h * 0.45, w / 2, h * 0.63, duration=0.6):
            return
        time.sleep(0.25)
        xml = _current_page_source(client)
    if force:
        raise RuntimeError('未确认榜单已回到顶部，停止采集以免排行错位')
    return xml


def _scroll_signature(elems):
    """以长文本的位置判断实际位移，忽略倒计时和容器文本。"""
    return tuple((e["text"], round(e["y"] / 5)) for e in elems
                 if e["type"] == "StaticText" and len(e["text"]) >= 12)


def _locate_card_centered(client, title: str, max_attempts: int = 20,
                          safe_top: float | None = None):
    """滚动榜单页，让目标卡片出现在屏幕中间区域，返回其中心坐标。

    小幅逐次修正；提供 safe_top 时按裁剪所用的完整卡片边界确认可见。
    """
    key = title[:12]
    if not key.strip():
        return None
    w, h = _get_window_size(client)
    # 只要卡片标题在屏幕内（留少量边距）就直接返回，不强制居中——顶部/底部的卡片不要硬滚，
    # 否则第一张卡片在顶部会被反复下滑→橡皮筋回弹。
    lo, hi = h * 0.28, h * 0.85
    previous = None
    stalled = 0
    for attempt in range(1, max_attempts + 1):
        xml = _current_page_source(client)
        if not xml:
            print(f"[ios]   读 source 失败（第 {attempt} 次）")
            return None
        elems = _xcui_texts(xml)
        candidates = [e for e in elems if key in e["text"]
                      and e["w"] > 0 and e["h"] > 0
                      and e["type"] not in ("Application", "Window", "NavigationBar")]
        target = min(candidates, key=lambda e: (e["text"] != title, len(e["text"]),
                                               e["w"] * e["h"]), default=None)
        signature = _scroll_signature(elems)
        stalled = stalled + 1 if signature == previous else 0
        previous = signature
        if target:
            cx = target["x"] + target["w"] / 2
            cy = target["y"] + target["h"] / 2
            delta = cy - h / 2
            fits = lo <= cy <= hi
            if safe_top is not None:
                from automation.pdd.spider.crop import card_rectangle, elements_from_xml
                # Use the same badge and fixed card height as the crop step.
                geometry = elements_from_xml(xml)
                box, _ = card_rectangle((int(w), int(h)), geometry, title, allow_outside=True)
                fits = box[1] >= safe_top and box[3] <= h - 20
                desired_top = safe_top + max(8, (h - 20 - safe_top - (box[3] - box[1])) / 2)
                delta = box[1] - desired_top
            if 0 < cx < w and fits:
                print(f"[ios]   第 {attempt} 次 定位到「{key}」@ y={cy:.0f}")
                return (cx, cy)
            if stalled >= 2:
                print(f"[ios]   定位「{key}」停止：连续滑动无位移，避免边界回弹")
                return None
            # 精确滚动：让卡片中心对齐屏幕中心（正 delta=卡片在下方，手指上滑）
            delta = max(-h * 0.12, min(h * 0.12, delta * 0.6))
            y2 = h / 2 - delta
            y2 = max(h * 0.08, min(h * 0.92, y2))  # clamp 到屏幕内
            print(f"[ios]   第 {attempt} 次 找到「{key}」@ y={cy:.0f}，滚动对齐中心...")
            if not _swipe(client, w / 2, h / 2, w / 2, y2, duration=0.6):
                return None
            time.sleep(0.25)
            continue
        if stalled >= 2:
            print(f"[ios]   定位「{key}」停止：连续滑动无位移")
            return None
        # 顺序处理卡片时，尚未加载的下一张通常在下方。
        print(f"[ios]   第 {attempt} 次 未找到「{key}」，上滑浏览后续商品...")
        if not _swipe(client, w / 2, h * 0.65, w / 2, h * 0.5, duration=0.6):
            return None
        time.sleep(0.25)
    print(f"[ios]   已尝试 {max_attempts} 次仍未定位「{key}」")
    return None


def _restore_rank_rows(rows: list[dict], base_dir) -> None:
    """恢复已保存字段；排行和标题一致才回填，避免榜单变化后串商品。"""
    from automation.pdd.spider.utils import read_xlsx
    from automation.pdd.spider.status import reconcile_product, stage_done

    saved = {}
    xlsx_path = base_dir / "榜单数据.xlsx"
    if xlsx_path.exists():
        header, values = read_xlsx(xlsx_path)
        for values_row in values:
            previous = dict(zip(header, values_row))
            saved[str(previous.get("排行"))] = previous
    for row in rows:
        rank_dir = base_dir / f"榜{int(row['排行']):02d}"
        previous = saved.get(str(row["排行"]), {})
        state = reconcile_product(rank_dir)
        if state.get("rank_data", {}).get("标题"):
            previous = state["rank_data"]
        if previous.get("标题") == row["标题"]:
            for field in ("产品ID", "产品链接", "上架日期"):
                if previous.get(field):
                    row[field] = previous[field]
        elif stage_done(rank_dir, "rank"):
            if previous:
                row.update(previous)
            print(f"[ios]   排行{row['排行']}已标记完成，但旧记录缺失或标题不同，保留标记并跳过")


def _copy_product_link(client) -> str:
    for attempt in range(2):
        if attempt:
            print("[ios]   剪贴板未取得链接，在当前产品页重新分享复制（2/2）", flush=True)
        share = _find_center(client, "分享")
        if not share or not _tap_xy(client, *share, "分享按钮"):
            raise RuntimeError("未找到或无法点击分享按钮，保留当前商品待采")
        print(f"[ios]   已点击分享 @ ({share[0]:.0f},{share[1]:.0f})（{attempt + 1}/2）", flush=True)
        time.sleep(0.8)
        copy = _find_center(client, ("复制链接", "复制", "拷贝"))
        if not copy or not _tap_xy(client, *copy, "复制链接"):
            raise RuntimeError("未找到或无法点击复制链接，停止以免记录旧剪贴板")
        print(f"[ios]   已点击复制链接 @ ({copy[0]:.0f},{copy[1]:.0f})", flush=True)
        time.sleep(1)  # Allow the copy action to complete before backgrounding PDD.
        link = _read_clipboard(client)
        match = re.search(r'https?://[^\s<>"\u3000]+', link or "")
        if match:
            from urllib.parse import urlsplit
            url = match.group(0).rstrip("，。；）】")
            if urlsplit(url).hostname:
                print(f"[ios]   已获取分享链接：{urlsplit(url).hostname}（{len(url)} 字符）", flush=True)
                return url
        print("[ios]   本次复制未取得有效网址（剪贴板为空或内容不是链接）", flush=True)
    return ""


def _fill_product_links(client, rows: list[dict], base_dir, header: list[str]) -> None:
    """逐张卡片：滚动居中 → 截图 → 点卡片进产品页 → 分享复制链接 → 读剪贴板。

    全程在拼多多 APP 里通过 WDA 操作，不涉及电脑浏览器。
    产品ID 从分享链接的 ps 参数提取。
    每处理完一个产品，立即把当前所有 rows 写入 榜单数据.xlsx（增量保存）。
    """
    from automation.pdd.spider.utils import write_xlsx
    from automation.pdd.spider.status import stage_done, update_stage, reconcile_product

    xlsx_path = base_dir / "榜单数据.xlsx"
    _restore_rank_rows(rows, base_dir)
    pending_rows = [row for row in rows if not stage_done(base_dir / f"榜{int(row['排行']):02d}", "rank")]
    if not pending_rows:
        print("[ios] status.json 显示所有商品榜单采集已完成，跳过")
        return
    # 恢复旧链接后再增量写入，避免重跑清空已经完成的商品。
    write_xlsx(xlsx_path, header, [[r.get(f, "") for f in header] for r in rows])

    # 先把榜单页滚回顶部（中断可能让页面停留在中间滚动位置；已在顶部则自动跳过）
    print("[ios] 滚动榜单页回到顶部 ...")
    _scroll_to_top(client, rows[0]["标题"] if rows else None)

    for row in rows:
        title = row["标题"]
        rank_dir = base_dir / f"榜{int(row['排行']):02d}"
        if stage_done(rank_dir, "rank"):
            print(f"[ios] 跳过已完成商品：榜{int(row['排行']):02d}")
            continue
        rank_dir.mkdir(parents=True, exist_ok=True)
        update_stage(rank_dir, "rank", "running", data=dict(row))
        print(f"[ios] 定位排行{row['排行']} 卡片并居中 ...")
        center = _locate_card_centered(client, title)
        if not center:
            print(f"[ios]   ⚠ 未定位到标题，跳过")
            update_stage(rank_dir, "rank", "failed", "未定位到商品标题")
            continue

        # 截图（卡片已居中）
        try:
            client.screenshot(str(rank_dir / "排行截图.jpg"))
        except Exception as e:
            print(f"[ios]   ⚠ 截图失败: {e}")

        # 点击进产品页
        if not _tap_xy(client, center[0], center[1], f"排行{row['排行']}卡片"):
            raise RuntimeError(f"排行{row['排行']}卡片点击失败")
        time.sleep(3)  # 等产品页加载

        link = _copy_product_link(client)
        if not link:
            raise RuntimeError(f"排行{row['排行']}重新复制后仍未读取到分享链接，请检查手机粘贴权限")
        row["产品链接"] = link
        row["产品ID"] = _extract_goods_id(link)
        print(f"[ios]   排行{row['排行']} | 剪贴板: {link[:50]!r} | 产品ID: {row['产品ID']}")

        # 返回榜单页（保持滚动位置）
        # 返回按钮在系统导航栏，accessibility label 是英文 "back"（诊断 dump 里是
        # [Button] (20,48) 'back'），但也可能是中文「返回」，都试一遍。
        if not (_tap_label(client, "返回") or _tap_label(client, "back")):
            _tap_xy(client, 26, 68, "返回按钮")
        time.sleep(1.5)

        # 增量保存：每处理完一个产品，立即写当前所有 rows 到 xlsx
        from automation.pdd.spider.artifacts import write_share_link
        write_share_link(rank_dir, row)
        write_xlsx(xlsx_path, header, [[r.get(f, "") for f in header] for r in rows])
        update_stage(rank_dir, "rank", "completed", data=dict(row))
        reconcile_product(rank_dir, row)
        print(f"[ios] 已更新 {xlsx_path.name}（{row['排行']}/{len(rows)}）")


def main(argv=None):
    from automation.pdd.spider.ios_prepare import main as prepare
    return prepare(argv)


if __name__ == '__main__':
    raise SystemExit(main())
