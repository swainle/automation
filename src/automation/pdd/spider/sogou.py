"""搜狗浏览器连接、窗口定位、截图、页面冻结、滚动对齐。"""
import os
import ctypes
import time
import urllib.request
import psutil
from pathlib import Path

if os.name == "nt":
    import win32gui
else:
    win32gui = None
from PIL import Image, ImageGrab

from automation.pdd.spider.config import SCROLLBAR_WIDTH_PRESET


def close_existing_sogou():
    """Close all Sogou instances and their child processes before initialization."""
    from automation.pdd.spider import config
    names = {'sogouexplorer.exe', 'sogouexplorer', Path(config.SOGOU_EXECUTABLE).name.casefold()}
    targets = {}
    try:
        for process in psutil.process_iter(['name']):
            try:
                if (process.info.get('name') or '').casefold() in names:
                    targets[process.pid] = process
                    for child in process.children(recursive=True):
                        targets[child.pid] = child
            except psutil.NoSuchProcess:
                continue
        for process in targets.values():
            try:
                process.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(list(targets.values()), timeout=5)
        for process in alive:
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(alive, timeout=3)
        if alive:
            raise RuntimeError('搜狗进程尚未完全退出，已取消重新启动')
        for process in psutil.process_iter(['name']):
            if (process.info.get('name') or '').casefold() in names:
                raise RuntimeError('检测到搜狗再次运行，请关闭后重试')
    except psutil.AccessDenied:
        raise RuntimeError('无法关闭搜狗，请手动关闭所有搜狗窗口和后台进程后重试') from None
    return len(targets)


def cdp_ready(port: int, timeout: float) -> bool:
    """等待 CDP 调试端口就绪。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def set_dpi_aware() -> None:
    """设置进程 DPI 感知，保证 GetWindowRect / ImageGrab 坐标一致。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def find_sogou_hwnd() -> int | None:
    """用 pywin32 定位搜狗浏览器窗口句柄。"""
    if win32gui is None:
        return None
    matches = []

    def cb(hwnd, _):
        try:
            if win32gui.IsWindowVisible(hwnd) and win32gui.GetClassName(hwnd) == "SogouExplorer_WidgetWin_1":
                matches.append(hwnd)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return matches[0] if matches else None


_FREEZE_CSS = "*{animation:none!important;transition:none!important;-webkit-animation:none!important;scroll-behavior:auto!important;}"


def freeze_page(page) -> None:
    """禁用页面动画/过渡，避免滚动截图时闪动。"""
    try:
        page.evaluate("""(css) => {
            if (!document.getElementById('__freeze')) {
                const s = document.createElement('style');
                s.id = '__freeze';
                s.textContent = css;
                document.documentElement.appendChild(s);
            }
        }""", _FREEZE_CSS)
    except Exception:
        pass


def get_content_rect(page, hwnd: int) -> tuple[int, int, int, int]:
    """返回浏览器网页内容区(视口)在屏幕上的矩形(物理像素)，去除标题栏/标签栏/地址栏。"""
    cl, ct, cr, cb = win32gui.GetClientRect(hwnd)
    cl, ct = win32gui.ClientToScreen(hwnd, (cl, ct))
    cr, cb = win32gui.ClientToScreen(hwnd, (cr, cb))
    vw = page.evaluate("window.innerWidth") or 0
    vh = page.evaluate("window.innerHeight") or 0
    if vw <= 0 or vh <= 0:
        return (cl, ct, cr, cb)
    scale = (cr - cl) / vw  # 实际物理/CSS 比例（浏览器缩放可能让 DPR 与真实比例不一致）
    content_h = int(vh * scale)
    # 内容区 = 客户端区底部（顶部是标题栏/标签栏/地址栏）
    return (cl, cb - content_h, cr, cb)


def grab_window(page, path: Path) -> bool:
    """用 Pillow ImageGrab 截取浏览器网页内容区(去除浏览器原生 UI)并保存。"""
    hwnd = find_sogou_hwnd()
    if not hwnd:
        print("    未找到搜狗浏览器窗口，无法截图")
        return False
    try:
        win32gui.SetForegroundWindow(hwnd)  # 尽量把窗口带到前台，避免截到遮挡物
        time.sleep(0.3)
        left, top, right, bottom = get_content_rect(page, hwnd)
        im = ImageGrab.grab(bbox=(left, top, right, bottom))
        path.parent.mkdir(parents=True, exist_ok=True)
        im.save(str(path), quality=90)
        return True
    except Exception as ex:
        print(f"    窗口截图失败: {ex}")
        return False


def get_scrollbar_width(page, hwnd: int) -> int:
    """垂直滚动条宽度（物理像素）；获取不到时返回预设值。"""
    try:
        css_w = page.evaluate("window.innerWidth - document.documentElement.clientWidth") or 0
        if css_w > 0:
            cl, _, cr, _ = win32gui.GetClientRect(hwnd)
            vw = page.evaluate("window.innerWidth") or 1
            scale = (cr - cl) / vw
            return int(css_w * scale)
    except Exception:
        pass
    return SCROLLBAR_WIDTH_PRESET


def crop_center_top(path: Path, width: int, height: int, scrollbar: int = 0) -> None:
    """裁剪图片为 width x height：水平居中(内容区内)、垂直居上，原地覆盖保存。"""
    try:
        im = Image.open(path)
        w, h = im.size
        content_w = w - scrollbar  # 减去垂直滚动条后的内容宽
        if content_w < width or h < height:
            print(f"    图片 {w}x{h} 小于目标 {width}x{height}，跳过裁剪")
            return
        left = (content_w - width) // 2
        top = 0
        im.crop((left, top, left + width, top + height)).save(str(path), quality=90)
    except Exception as ex:
        print(f"    截图裁剪失败: {ex}")


def crop_width_center(path: Path, width: int, scrollbar: int = 0) -> None:
    """裁剪图片宽度为 width（水平居中，内容区内），高度不变，原地覆盖保存。"""
    try:
        im = Image.open(path)
        w, h = im.size
        content_w = w - scrollbar
        if content_w < width:
            print(f"    图片宽 {w} 小于目标 {width}，跳过裁剪")
            return
        left = (content_w - width) // 2
        im.crop((left, 0, left + width, h)).save(str(path), quality=90)
    except Exception as ex:
        print(f"    截图裁剪失败: {ex}")


def scroll_card_to_top(page, goods_id: str, ref_selector: str) -> bool:
    """滚动榜单页，使卡片上边缘对齐到参考元素(ref_selector)的底部。

    ref_selector 是每个榜单自己的「吸顶元素」CSS 选择器，卡片吸附在其最底部。
    """
    ok = page.evaluate("""(arg) => {
        const card = document.querySelector(`[data-ranking-goods-id="${arg.gid}"]`);
        if (!card) return false;
        const wrapper = document.querySelector(arg.ref);
        const refBottom = wrapper ? wrapper.getBoundingClientRect().bottom + window.scrollY : 0;
        const cardTop = card.getBoundingClientRect().top + window.scrollY;
        window.scrollTo(0, cardTop - refBottom);
        return true;
    }""", {"gid": goods_id, "ref": ref_selector})
    return bool(ok)


def read_viewport(page) -> dict:
    """读取浏览器当前窗口/视口/页面/缩放/滚动等实际值，并完成换算。"""
    info = page.evaluate("""() => {
        const de = document.documentElement;
        const vv = window.visualViewport || {};
        return {
            screen: { w: window.screen.width, h: window.screen.height },
            outer: { w: window.outerWidth, h: window.outerHeight },
            inner: { w: window.innerWidth, h: window.innerHeight },
            client: { w: de.clientWidth, h: de.clientHeight },
            page: { w: de.scrollWidth, h: de.scrollHeight },
            body: { w: document.body ? document.body.scrollWidth : 0 },
            dpr: window.devicePixelRatio,
            visual_scale: vv.scale || 1,
            scroll: { x: window.scrollX, y: window.scrollY },
        };
    }""")
    dpr = info.get("dpr", 1) or 1
    info["viewport_physical_w"] = info["inner"]["w"] * dpr
    info["viewport_physical_h"] = info["inner"]["h"] * dpr
    info["page_physical_h"] = info["page"]["h"] * dpr
    return info


def log_viewport(page, tag: str = "") -> None:
    """打印当前视口状态，方便核对换算关系。"""
    v = read_viewport(page)
    dpr = v["dpr"]
    print(f"[{tag}] 屏幕={v['screen']['w']}x{v['screen']['h']} "
          f"窗口={v['outer']['w']}x{v['outer']['h']} "
          f"视口={v['inner']['w']}x{v['inner']['h']}CSS "
          f"DPR={dpr} "
          f"视口物理={v['viewport_physical_w']:.0f}x{v['viewport_physical_h']:.0f} "
          f"页面高={v['page']['h']}CSS "
          f"滚动=({v['scroll']['x']},{v['scroll']['y']})")


def main():
    """关闭所有搜狗实例后重新打开，复用公共 WS 并准备移动视口。"""
    import argparse
    from automation.pdd.spider import config
    from automation.pdd.spider.initialize import initialize
    from automation.pdd.spider.logging_setup import console_logging
    from automation.pdd.spider.runtime import Runtime
    from automation.pdd.spider.concurrency import workflow_lock
    argparse.ArgumentParser(description=main.__doc__).parse_args()
    initialize(config.BUSINESS_ROOT)
    with console_logging(config.BUSINESS_ROOT):
        runtime = Runtime()
        try:
            with workflow_lock('device-browser'):
                print('[搜狗] 正在关闭所有搜狗窗口和后台进程')
                count = close_existing_sogou()
                print(f'[搜狗] 已关闭 {count} 个进程，正在重新初始化')
                runtime.start_ranking_browser(persistent=True)
            print(f'[搜狗] 已就绪：窗口 {config.RANKING_WIDTH}×{config.RANKING_HEIGHT} px，移动视口 {config.RANKING_WIDTH}×{config.RANKING_HEIGHT}，标签页缩放 {config.RANKING_SCALE:.0%}；浏览器和插件服务已保留')
            return 0
        except KeyboardInterrupt:
            return 0
        except Exception as error:
            print(f'[搜狗] 准备失败：{error}')
            return 1
        finally:
            runtime.close()


if __name__ == '__main__':
    raise SystemExit(main())
