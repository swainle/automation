"""Connections and owned external services for one CLI invocation."""
from automation.pdd.spider.logging_setup import timed_operation
import socket
import asyncio
import json
import logging
import math
import os
import re
import sys
import subprocess
import time
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import psutil

from automation.pdd.spider import config
from automation.pdd.spider.processes import ProcessManager
from automation.pdd.spider.workflow import FatalWorkflowError


def proxy_url(server, credentials=None):
    if not server:
        if credentials:
            raise ValueError("--proxy-credentials 必须与 --proxy 一起使用")
        return None
    parsed = urlsplit(server)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or not parsed.port:
        raise ValueError("--proxy 必须是带端口的 HTTP/HTTPS 地址")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("--proxy 不能包含路径、查询参数或片段")
    if credentials:
        if parsed.username is not None or ":" not in credentials:
            raise ValueError("代理认证格式为 user:pass，不能与 URL 内的认证重复指定")
        user, password = credentials.split(":", 1)
        if not user:
            raise ValueError("代理用户名不能为空")
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, "", "", ""))
    return server


def port_ready(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def verify_debug_browser(port, executable):
    """A ready Chrome CDP port is not proof that the configured Sogou owns it."""
    expected = Path(executable).resolve()
    for connection in psutil.net_connections(kind='tcp'):
        if connection.status != psutil.CONN_LISTEN or not connection.laddr or connection.laddr.port != port:
            continue
        if connection.pid is not None:
            owner = Path(psutil.Process(connection.pid).exe()).resolve()
            if owner == expected:
                return
            raise RuntimeError(f'调试端口 {port} 属于其他程序，请配置独立的 SPIDER_DEBUG_PORT；未关闭该程序')
    raise RuntimeError(f'无法确认调试端口 {port} 的搜狗进程，请检查浏览器和端口配置')


class Runtime:
    def __init__(self, *, proxy=None, port=8080, rotation=None, key=None,
                 password=None, private=False, rotate_every=None):
        self.processes = ProcessManager()
        self.proxy = proxy
        self.port = port
        self.rotation = rotation
        self.key = key
        self.password = password
        self.private = private
        self.rotate_every = rotate_every
        self.playwright = None
        self.browser = None
        self.page = None
        self.client = None
        self._mitm = False
        self._ios_used = False
        self._tunnel_checked = False
        self._image_checked = False
        self.ranking_tab = None
        self._ranking_started = False
        self._ranking_launcher = None
        self._ranking_keep_open = False

    def ranking_request(self, action, params=None, *, timeout=40):
        from automation.com.extension_bridge import request
        async def call():
            return await asyncio.wait_for(request(f'ws://127.0.0.1:{config.RANKING_WS_PORT}/', action, params, browser='sogou'), timeout)
        return asyncio.run(call())

    def _verify_ranking_browser(self, peer_port):
        if not isinstance(peer_port, int):
            raise RuntimeError('插件服务版本过旧，请停止旧 bridge 后重试')
        expected = Path(config.SOGOU_EXECUTABLE).resolve()
        for connection in psutil.net_connections(kind='tcp'):
            if (connection.pid is not None and connection.laddr and connection.raddr
                    and connection.laddr.port == peer_port and connection.raddr.port == config.RANKING_WS_PORT):
                if Path(psutil.Process(connection.pid).exe()).resolve() == expected:
                    return
                raise RuntimeError('榜单插件连接来自其他浏览器；请将榜单服务地址仅配置到搜狗插件')
        raise RuntimeError('无法确认搜狗插件连接的进程，请检查连接状态后重试')

    @timed_operation('[环境] 连接商品采集浏览器')
    def connect_product_browser(self):
        """Use a manually started bridge and Sogou without launching either."""
        if not 1024 <= config.RANKING_WS_PORT <= 65535:
            raise ValueError('请检查公共 WS 端口配置')
        if not port_ready(config.RANKING_WS_PORT):
            raise RuntimeError(f'公共 WS 未启动，请手动启动 ws://127.0.0.1:{config.RANKING_WS_PORT}/ 后重试')
        try:
            status = self.ranking_request('status', timeout=3)
        except Exception as error:
            raise RuntimeError(f'无法连接公共 WS，请检查手动启动的服务：{error}') from error
        if status.get('protocol') != 3:
            raise RuntimeError('公共 WS 服务版本过旧，请手动重启 automation.com.extension_bridge')
        if not status.get('connected'):
            raise RuntimeError('搜狗插件未连接，请手动打开搜狗并确认插件已连接公共 WS 后重试')
        self._verify_ranking_browser(status.get('extension_peer_port'))
        print(f'[商品] 已连接现有搜狗插件及 WS：ws://127.0.0.1:{config.RANKING_WS_PORT}/')

    @timed_operation('[环境] 启动或复用榜单浏览器')
    def start_ranking_browser(self, *, persistent=False):
        if self._ranking_started:
            return
        if (not 1024 <= config.RANKING_WS_PORT <= 65535 or not math.isfinite(config.RANKING_READY_TIMEOUT)
                or config.RANKING_READY_TIMEOUT <= 0):
            raise ValueError('请检查搜狗插件端口和启动超时配置')
        executable = Path(config.SOGOU_EXECUTABLE)
        if not executable.is_file():
            raise RuntimeError(f'搜狗浏览器不存在：{executable}')
        reuse_ws = port_ready(config.RANKING_WS_PORT)
        if reuse_ws:
            print(f'[WS] 复用现有公共服务：ws://127.0.0.1:{config.RANKING_WS_PORT}/')
        else:
            command = [sys.executable, '-u', '-m', 'automation.com.extension_bridge', '--port', str(config.RANKING_WS_PORT)]
            log_path = config.BUSINESS_ROOT / 'logs/sogou-bridge.log'
            log_path.parent.mkdir(parents=True, exist_ok=True)
            flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if sys.platform == 'win32' else 0
            with log_path.open('ab') as output:
                subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                 creationflags=flags, start_new_session=sys.platform != 'win32',
                                 env=dict(os.environ, PYTHONIOENCODING='utf-8'))
            print(f'[WS] 已启动公共后台服务：ws://127.0.0.1:{config.RANKING_WS_PORT}/')
        print(f'[搜狗] 使用插件连接 ws://127.0.0.1:{config.RANKING_WS_PORT}/；优先复用已打开的搜狗')
        deadline = time.monotonic() + config.RANKING_READY_TIMEOUT
        launched = False
        while time.monotonic() < deadline:
            self.processes.check()
            try:
                status = self.ranking_request('status', timeout=3)
            except (OSError, TimeoutError):
                status = {}
            if status and status.get('protocol') != 3:
                raise RuntimeError('公共 WS 服务版本过旧，请重启 automation.com.extension_bridge 后重试')
            if status.get('connected'):
                self._verify_ranking_browser(status.get('extension_peer_port'))
                result = self.ranking_request('ranking_prepare' if persistent else 'ranking_open',
                    {'reuse_workspace': True})
                self.ranking_tab = result['tab_id']
                self._ranking_keep_open = result.get('persistent', False)
                if result.get('protocol') != 2:
                    raise RuntimeError('请在搜狗扩展管理中重新加载本仓库插件（0.13.0）')
                self._ranking_started = True
                return
            if not launched:
                self._ranking_launcher = subprocess.Popen([str(executable), f'--user-data-dir={config.USER_DATA_DIR}', 'about:blank'],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
                launched = True
            time.sleep(.5)
        raise RuntimeError('搜狗插件未就绪，请在搜狗中安装/重新加载插件并核对连接地址')

    def _ios_command(self, *args, timeout=60, stream=False):
        from processkit import Command
        from automation.pdd.spider import ios
        if not ios.IOS_EXE.is_file():
            raise RuntimeError(f"找不到 go-ios: {ios.IOS_EXE}")
        command = Command(str(ios.IOS_EXE), ios.device_args(args)).timeout(timeout).cwd(str(config.TOOLS_ROOT))
        if stream:
            logger = logging.getLogger("automation.pdd.spider.image")
            command = command.on_stdout_line(logger.info).on_stderr_line(logger.info)
        result = command.output()
        if not result.is_success:
            detail = result.stderr.strip()[-2000:]
            diagnostic = (result.stdout + '\n' + result.stderr).lower()
            if sys.platform == 'win32' and '127.0.0.1:27015' in diagnostic and any(
                    marker in diagnostic for marker in ('connectex', 'connection refused', 'actively refused')):
                raise RuntimeError(
                    '无法连接本机 iOS USB 服务（127.0.0.1:27015）。'
                    '请打开 services.msc，启动或重启 Apple Mobile Device Service；'
                    '若没有该服务，请安装或修复 Apple 设备支持组件（Apple Devices / iTunes）。'
                    '然后重新连接 USB、解锁手机并点「信任」，再重试。'
                    '设备枚举需要先恢复 USB 服务，单独启动 go-ios tunnel 无法解决此错误。'
                    f' 原始错误：{detail}')
            raise RuntimeError(f"go-ios {' '.join(args[:2])} 失败，退出码 {result.code}: {detail}")
        return result.stdout + "\n" + result.stderr

    @staticmethod
    def _ios_records(output):
        for line in output.splitlines():
            try:
                yield json.loads(line)
            except ValueError:
                continue

    @classmethod
    def _has_image(cls, output):
        def mounted(value):
            if isinstance(value, list):
                return any(mounted(item) for item in value)
            if not isinstance(value, dict):
                return False
            if value.get("msg") == "image signature" and value.get("signature"):
                return True
            if value.get("ImageSignature") or value.get("ImageSignatureList") or value.get("imageSignatures"):
                return True
            return any(mounted(item) for item in value.values() if isinstance(item, (dict, list)))
        return any(mounted(record) for record in cls._ios_records(output))

    @timed_operation('[环境] 打开或复用浏览器')
    def open_browser(self):
        from automation.pdd.spider.sogou import cdp_ready, set_dpi_aware
        from playwright.sync_api import sync_playwright

        if self.page is not None:
            return self.page
        set_dpi_aware()
        if not cdp_ready(config.DEBUG_PORT, timeout=0.5):
            executable = Path(config.SOGOU_EXECUTABLE)
            if not executable.is_file():
                raise RuntimeError(f"浏览器程序不存在: {executable}，请修改 SOGOU_EXECUTABLE")
            # Only the configured browser executable is in scope for restart.
            targets = []
            for process in psutil.process_iter(["exe"]):
                try:
                    if process.info["exe"] and Path(process.info["exe"]).resolve() == executable.resolve():
                        targets.extend(process.children(recursive=True))
                        targets.append(process)
                except psutil.NoSuchProcess:
                    continue
            targets = list({p.pid: p for p in targets}.values())
            for process in reversed(targets):
                try:
                    process.terminate()
                except psutil.NoSuchProcess:
                    pass
            _, alive = psutil.wait_procs(targets, timeout=5)
            for process in alive:
                try:
                    process.kill()
                except psutil.NoSuchProcess:
                    pass
            _, alive = psutil.wait_procs(alive, timeout=5)
            if alive:
                raise RuntimeError("旧浏览器未退出，无法安全复用用户数据目录")
            self.processes.start("browser", [str(executable),
                f"--remote-debugging-port={config.DEBUG_PORT}",
                f"--user-data-dir={config.USER_DATA_DIR}"])
            if not cdp_ready(config.DEBUG_PORT, timeout=30):
                raise RuntimeError("浏览器调试端口未就绪")
        verify_debug_browser(config.DEBUG_PORT, config.SOGOU_EXECUTABLE)
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{config.DEBUG_PORT}")
        context = self.browser.contexts[0]
        self.page = context.pages[0] if context.pages else context.new_page()
        self.page.bring_to_front()
        return self.page

    def ensure_wda(self):
        from automation.pdd.spider import ios
        self._ios_used = True
        if self.client is None:
            if ios.wda_ready():
                import wda
                wda.HTTP_TIMEOUT = 30.0
                self.client = wda.Client(ios.WDA_URL)
            else:
                self.ensure_tunnel()
                self.ensure_image()
                self.ensure_driver()
                self.ensure_forward()
                import wda
                wda.HTTP_TIMEOUT = 30.0
                self.client = wda.Client(ios.WDA_URL)
        return self.client

    @timed_operation('[ios] 检查并准备隧道')
    def ensure_tunnel(self):
        from automation.pdd.spider import ios
        if self._tunnel_checked:
            return
        self._ios_used = True
        if not ios.device_connected():
            raise FatalWorkflowError("未检测到已连接的 iOS 设备")
        ios._WDA_PROCESSES.check()
        if not ios.tunnel_up():
            output = self._ios_command("lockdown", "get", "ProductVersion")
            versions = [value for value in self._ios_records(output)
                        if isinstance(value, str) and re.fullmatch(r"\d+(?:\.\d+)+", value)]
            if not versions:
                raise RuntimeError("未识别到 iOS 版本，无法判断是否需要隧道")
            if int(versions[-1].split(".")[0]) < 17:
                print(f"[ios] iOS {versions[-1]} 不需要 RemotePairing 隧道，跳过")
                self._tunnel_checked = True
                return
            ios._spawn("tunnel", ["tunnel", "start", "--userspace"])
            ios._wait(ios.tunnel_up, 15, "隧道建立")
        else:
            print("[ios] 复用已有隧道")
        self._tunnel_checked = True

    @timed_operation('[ios] 检查并挂载开发者镜像')
    def ensure_image(self):
        if self._image_checked:
            return
        from automation.pdd.spider import ios
        self._ios_used = True
        if self._has_image(self._ios_command("image", "list")):
            print("[ios] 开发者镜像已挂载，直接复用")
            self._image_checked = True
            return
        print("[ios] 挂载匹配设备版本的开发者镜像...")
        self._ios_command("image", "auto", f"--basedir={config.TOOLS_ROOT / 'devimages'}",
                          timeout=300, stream=True)
        if not self._has_image(self._ios_command("image", "list")):
            raise FatalWorkflowError("镜像命令已结束，但未确认开发者镜像挂载成功")
        self._image_checked = True

    @timed_operation('[ios] 检查并启动 WDA 驱动')
    def ensure_driver(self):
        from automation.pdd.spider import ios
        self._ios_used = True
        ios._WDA_PROCESSES.check()
        if ios.wda_ready():
            print("[ios] 复用已有 WDA 驱动")
            return
        if ios.wda_running():
            # 应用在跑但 8100 未就绪：可能是卡住/崩溃残留的 runner，必须终止，
            # 否则 forward 转发到的是一个不提供 HTTP 的僵尸进程，永远连不上。
            print("[ios] WDA 应用在运行但未就绪，终止卡住进程后重新启动")
            try:
                self._ios_command("kill", ios.wda_bundle_id(), timeout=20)
            except Exception as error:
                print(f"[ios] 终止卡住的 WDA 应用失败，继续尝试启动：{error}")
            time.sleep(2)
        self.ensure_tunnel()
        self.ensure_image()
        print("[ios] 启动 WDA 驱动...")
        bundle = ios.wda_bundle_id()
        for attempt in range(3):
            try:
                # Probe before launching: a tunnel record alone does not prove RSD works.
                self._ios_command("ps", "--apps", timeout=15)
                ios._spawn("runwda", ["runwda", f"--bundleid={bundle}", f"--testrunnerbundleid={bundle}",
                                      "--xctestconfig=WebDriverAgentRunner.xctest",
                                      f"--env=USE_PORT={ios.WDA_PORT}"])
                # HTTP polling avoids opening competing RSD sessions with `ps`
                # while runwda is establishing its connection.
                if not ios.wda_ready():
                    if "forward" not in ios._WDA_PROCESSES.groups:
                        ios._spawn("forward", ["forward", str(ios.WDA_PORT), str(ios.WDA_PORT)])
                ios._wait(ios.wda_ready, 30, "WDA HTTP 服务启动")
                return
            except RuntimeError as error:
                owned = "runwda" in ios._WDA_PROCESSES.groups
                retryable = owned or any(mark in str(error).lower() for mark in
                    ("rsd", "eof", "connection reset", "connection refused", "timeout"))
                ios._WDA_PROCESSES.stop("runwda")
                ios._WDA_PROCESSES.stop("forward")
                if not retryable or attempt == 2:
                    raise FatalWorkflowError(f"WDA 启动失败：{error}；请检查设备解锁状态、USB 连接及 go-ios 与 iOS 的兼容性") from error
                print(f"[ios] WDA 连接失败，准备第 {attempt + 2}/3 次尝试：{error}")
                if "tunnel" in ios._WDA_PROCESSES.groups:
                    ios._WDA_PROCESSES.stop("tunnel")
                    self._tunnel_checked = False
                time.sleep(2)
                self.ensure_tunnel()

    @timed_operation('[ios] 检查并启动端口转发')
    def ensure_forward(self):
        from automation.pdd.spider import ios
        self._ios_used = True
        ios._WDA_PROCESSES.check()
        if ios.wda_ready():
            print("[ios] 复用已有 WDA 端口转发")
            return
        self.ensure_driver()
        print("[ios] 启动 WDA 端口转发...")
        ios._spawn("forward", ["forward", str(ios.WDA_PORT), str(ios.WDA_PORT)])
        ios._wait(ios.wda_ready, 15, "端口转发")

    @timed_operation('[环境] 准备代理服务')
    def start_mitm(self):
        from automation.pdd.spider import ios
        if self._mitm:
            return
        if port_ready(self.port):
            # Reused mitm reads upstream settings dynamically; never terminate it.
            if self.key and not self.proxy:
                server = ios._fetch_qg_ip(self.key, self.password)
                if not server:
                    raise RuntimeError("未获取到青果代理地址")
                ios._update_proxy_config(server, f"{self.key}:{self.password}" if self.password else None)
                ios._write_mitm_config({"upstream_scheme": "http"})
            elif self.proxy:
                from urllib.parse import unquote
                parsed = urlsplit(self.proxy)
                host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
                auth = f"{unquote(parsed.username)}:{unquote(parsed.password or '')}" if parsed.username else None
                ios._update_proxy_config(f"{host}:{parsed.port}", auth)
                ios._write_mitm_config({"upstream_scheme": parsed.scheme})
            print(f"[mitm] 复用监听端口 {self.port}，退出时保留")
        else:
            command = [sys.executable, "-m", "automation.pdd.spider.mitm", str(self.port)]
            if self.proxy:
                command.extend(["--upstream", self.proxy])
            for option, value in (("--rotation", self.rotation), ("--key", self.key), ("--password", self.password)):
                if value:
                    command.extend([option, value])
            self.processes.start("mitm", command)
            deadline = time.monotonic() + 20
            while not port_ready(self.port):
                self.processes.check()
                if time.monotonic() >= deadline:
                    raise RuntimeError("mitm 启动超时")
                time.sleep(0.2)
        self._mitm = True

    def close_browser(self):
        # Disconnect CDP; owned processes are closed by close(), reused ones stay.
        try:
            if self.playwright is not None:
                self.playwright.stop()
        finally:
            self.playwright = self.browser = self.page = None
            self.processes.stop("browser")

    def stop_mitm(self):
        self.processes.stop("mitm")
        self._mitm = False

    def close(self):
        try:
            try:
                if self.ranking_tab is not None and not self._ranking_keep_open:
                    try:
                        self.ranking_request('ranking_close', {'tab_id': self.ranking_tab}, timeout=35)
                    except Exception:
                        print('[搜狗] 未能确认任务标签页关闭，请检查浏览器；本次启动的通信服务仍会清理')
            finally:
                self.ranking_tab = None
                self._ranking_started = False
                self.close_browser()
        finally:
            try:
                self.processes.close()
            finally:
                if self._ios_used:
                    from automation.pdd.spider import ios
                    try:
                        ios.stop_wda()
                    finally:
                        self._ios_used = False
                        self.client = None
                        self._tunnel_checked = False
                        self._image_checked = False
                self._mitm = False
