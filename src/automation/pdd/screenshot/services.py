"""只管理本流程启动的进程；SSH 日志实时转发，远程 CS 使用心跳租约。"""
import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
from urllib.request import urlopen

import psutil
from automation.com.browser import launch_chrome
from automation.com.extension_bridge import request
from automation.pdd.screenshot.blocking import blocking
from automation.pdd.screenshot.logs import log
from automation.pdd.screenshot.chrome import BrowserPreparationError, close_existing_chrome, prepare_default_profile


class InfrastructureError(RuntimeError):
    pass


class StartupError(RuntimeError):
    pass


def stop_tree(process):
    """只回收自己持有的 Popen 进程树，宽限后强制结束。"""
    if process is None:
        return
    try:
        parent = psutil.Process(process.pid)
        if process.poll() is not None:
            return
        children = parent.children(recursive=True)
        targets = children + [parent]
        for target in targets:
            with contextlib.suppress(psutil.NoSuchProcess):
                target.terminate()
        _, alive = psutil.wait_procs(targets, timeout=5)
        for target in alive:
            with contextlib.suppress(psutil.NoSuchProcess):
                target.kill()
        psutil.wait_procs(alive, timeout=3)
        process.wait(timeout=2)
    except (psutil.NoSuchProcess, subprocess.TimeoutExpired):
        pass


def port_free(port):
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def http_ready(base, *, startup=False):
    with urlopen(base + ("/swagger.json" if startup else "/health"), timeout=3) as response:
        document = json.load(response)
    return "/cookies" in document.get("paths", {}) if startup else document.get("status") == "ok"


class Services:
    def __init__(self, config):
        self.config = config
        self.processes = []
        self.browser = None
        self.conn = self.remote = self.tunnel = None
        self.tasks = []
        self.log_threads = []
        self.cs_base = f"http://127.0.0.1:{config.cs_port}"

    def spawn(self, module, *args, env=None):
        label = "CS" if module.endswith("cookies_server") else "WS"
        child_env = dict(os.environ if env is None else env, PYTHONIOENCODING="utf-8")
        child = subprocess.Popen([sys.executable, "-u", "-m", module, *args],
                                 cwd=self.config.root, env=child_env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding="utf-8", errors="replace", bufsize=1,
                                 start_new_session=os.name != "nt",
                                 creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
        child.log_label = label
        self.processes.append(child)
        def output():
            try:
                for line in child.stdout:
                    log(label, line.rstrip("\r\n"))
            finally:
                child.stdout.close()
        thread = threading.Thread(target=output, name=f"{label}-logs", daemon=True)
        self.log_threads.append(thread)
        thread.start()
        log("管理", f"{label} 已启动，PID={child.pid}")
        return child

    async def _remote_start(self):
        import asyncssh
        cfg = self.config
        options = {"port": cfg.ssh_port, "keepalive_interval": 5, "keepalive_count_max": 3,
                   "connect_timeout": 15, "login_timeout": 15}
        if cfg.ssh_user:
            options["username"] = cfg.ssh_user
        if cfg.ssh_password:
            options["password"] = cfg.ssh_password
        if cfg.ssh_key:
            options["client_keys"] = [os.path.expanduser(cfg.ssh_key)]
        # 保留 AsyncSSH 默认 known_hosts 检查，不自动信任未知主机。
        log("管理", "正在建立远程 CS 的 SSH 连接")
        self.conn = await asyncssh.connect(cfg.ssh_host, **options)
        self.remote = await asyncio.wait_for(self.conn.create_process(cfg.ssh_command, encoding="utf-8"), 15)
        async def output(reader):
            while line := await reader.readline():
                log("CS", line.rstrip("\r\n"))
        async def heartbeat():
            while True:
                self.remote.stdin.write('{"type":"ping"}\n')
                await self.remote.stdin.drain()
                await asyncio.sleep(5)
        self.tasks = [asyncio.create_task(output(self.remote.stdout)),
                      asyncio.create_task(output(self.remote.stderr)), asyncio.create_task(heartbeat())]
        self.tunnel = await self.conn.forward_local_port("127.0.0.1", 0, "127.0.0.1", cfg.ssh_cs_port)
        self.cs_base = f"http://127.0.0.1:{self.tunnel.get_port()}"

    def check_processes(self):
        if any(process.poll() is not None for process in self.processes):
            raise InfrastructureError("managed_process_exited")
        if self.browser is not None and self.browser.poll() not in (None, 0):
            raise InfrastructureError("browser_exited")
        if self.remote is not None and self.remote.exit_status is not None:
            raise InfrastructureError("remote_cs_exited")
        if self.conn is not None and self.conn.is_closed():
            raise InfrastructureError("ssh_disconnected")
        if any(task.done() and not task.cancelled() and task.exception() for task in self.tasks):
            raise InfrastructureError("ssh_stream_failed")

    async def wait_ready(self, probe, description):
        deadline = asyncio.get_running_loop().time() + self.config.ready_timeout
        while asyncio.get_running_loop().time() < deadline:
            self.check_processes()
            try:
                if await probe():
                    return
            except Exception:
                pass
            await asyncio.sleep(.5)
        raise InfrastructureError(description + "_not_ready")

    async def start(self):
        cfg = self.config
        if not port_free(cfg.ws_port):
            raise StartupError("WS 端口已占用；请停止单独启动的 bridge，或配置 AUTOMATION_WS_PORT 并修改插件连接地址")
        if not cfg.ssh_host and not port_free(cfg.cs_port):
            raise StartupError("CS 端口已占用；请停止已有 CS，或配置其他 COOKIES_SERVER_PORT")
        if cfg.ssh_host:
            await self._remote_start()
        else:
            env = dict(os.environ, COOKIES_DIR=str(cfg.cs_dir), COOKIES_SERVER_HOST="127.0.0.1",
                       COOKIES_SERVER_PORT=str(cfg.cs_port))
            self.spawn("automation.com.cookies_server", env=env)
        async def cs_ready():
            return await blocking(lambda: http_ready(self.cs_base, startup=True))
        await self.wait_ready(cs_ready, "cs")
        self.spawn("automation.com.extension_bridge", "--port", str(cfg.ws_port))
        async def ws_ready():
            await request(cfg.endpoint, "status")
            return True
        await self.wait_ready(ws_ready, "ws")
        log("浏览器", "正在关闭现有 Chrome 窗口及后台进程")
        try:
            count = await blocking(close_existing_chrome)
            prepared = await blocking(prepare_default_profile, cfg.root / "state/browser-preferences")
        except BrowserPreparationError as error:
            raise StartupError(str(error)) from None
        log("浏览器", f"已关闭 {count} 个 Chrome 进程，已准备启动提示设置（{prepared['profile']}）")
        if prepared["backup"]:
            log("浏览器", f"原 Preferences 已备份：{prepared['backup']}")
        self.browser = launch_chrome(url="about:blank", quiet_startup=True)
        log("浏览器", f"已请求打开默认 Chrome，复用已安装插件；连接地址 {cfg.endpoint}")
        await self.wait_ready(self.connected, "extension")
        log("浏览器", "插件已就绪，开始读取飞书店铺列表")

    async def connected(self):
        return (await request(self.config.endpoint, "status")).get("connected", False)

    async def health(self):
        self.check_processes()
        try:
            if not await self.connected() or not await blocking(http_ready, self.cs_base):
                raise InfrastructureError("service_unhealthy")
        except InfrastructureError:
            raise
        except Exception:
            raise InfrastructureError("service_unreachable") from None

    async def monitor(self):
        while True:
            await asyncio.sleep(2)
            await self.health()

    async def stop(self):
        # 默认 Chrome 可能属于用户；退出仅关闭插件登记的任务页，不结束 Chrome。
        if self.browser is not None:
            try:
                await asyncio.wait_for(request(self.config.endpoint, "automation_tabs_close"), 8)
            except Exception:
                log("浏览器", "未能确认任务标签页已关闭，请检查浏览器；未结束 Chrome。")
        self.browser = None
        for child in reversed(self.processes):
            await blocking(stop_tree, child)
            log("管理", f"{getattr(child, 'log_label', '服务')} 已停止，PID={child.pid}，退出码={child.poll()}")
        self.processes.clear()
        for thread in self.log_threads:
            await blocking(thread.join, 1)
        self.log_threads.clear()
        if self.remote is not None:
            with contextlib.suppress(Exception):
                self.remote.stdin.write('{"type":"stop"}\n')
                self.remote.stdin.write_eof()
                await asyncio.wait_for(self.remote.wait_closed(), 12)
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        if self.tunnel:
            self.tunnel.close()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.tunnel.wait_closed(), 3)
        if self.conn:
            self.conn.close()
            try:
                await asyncio.wait_for(self.conn.wait_closed(), 5)
            except TimeoutError:
                self.conn.abort()
        self.remote = self.conn = self.tunnel = None
