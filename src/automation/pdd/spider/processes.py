"""Manage processes started by this application; reused services stay external."""
import logging
import platform
import threading
from pathlib import Path

from processkit import Command, ProcessGroup


def go_ios_path(root: Path) -> Path:
    system, machine = platform.system(), platform.machine().lower()
    if system == "Windows" and machine in ("amd64", "x86_64"):
        name = "ios.exe"
    elif system == "Linux" and machine in ("amd64", "x86_64"):
        name = "ios-amd64"
    elif system == "Darwin":
        name = "ios"
    else:
        raise RuntimeError(f"不支持的 go-ios 平台：{system}/{machine}")
    return root / "tools" / "vendor" / "go-ios" / name


class _Process:
    def __init__(self, process, logger, manager):
        self.process = process
        self.pid = process.pid
        self.code = None
        self.done = threading.Event()

        def wait():
            try:
                result = process.finish()
                self.code = result.code if result.code is not None else -1
                if not manager.stopping:
                    logger.error("进程退出，退出码 %s", self.code)
            except Exception:
                self.code = -1
                logger.exception("读取进程退出状态失败")
            finally:
                self.done.set()

        self.reader = threading.Thread(target=wait, daemon=True)
        self.reader.start()

    def poll(self):
        return self.code if self.done.is_set() else None

    def wait(self, timeout=None):
        if not self.done.wait(timeout):
            raise TimeoutError("等待进程退出超时")
        return self.code


class ProcessManager:
    def __init__(self):
        self.processes = []
        self.stopping = False
        self.groups = {}

    def start(self, name, command, *, cwd=None):
        self.check()
        if name in self.groups:
            raise ValueError(f"进程已由本管理器启动: {name}")
        group = ProcessGroup()
        logger = logging.getLogger("automation.pdd.spider." + ("tunel" if name == "tunnel" else name))
        cmd = (Command(str(command[0]), [str(arg) for arg in command[1:]])
               .on_stdout_line(lambda line: logger.info(line))
               .on_stderr_line(lambda line: logger.info(line)))
        if cwd is not None:
            cmd = cmd.cwd(str(cwd))
        if platform.system() == 'Windows':
            cmd = cmd.create_no_window()
        try:
            proc = _Process(group.start(cmd), logger, self)
        except BaseException:
            group.shutdown()
            raise
        self.groups[name] = group
        self.processes.append((name, proc))
        return proc.pid

    def check(self):
        for name, proc in self.processes:
            if proc.done.is_set():
                raise RuntimeError(f"{name} 进程已退出，退出码 {proc.code}")

    def close(self):
        self.stopping = True
        errors = []
        try:
            for group in reversed(list(self.groups.values())):
                try:
                    group.shutdown()
                except Exception as error:
                    logging.getLogger(__name__).exception("进程组清理失败，继续清理其他进程组")
                    errors.append(error)
            for _, proc in self.processes:
                proc.reader.join(timeout=5)
        finally:
            self.processes.clear()
            self.groups.clear()
            self.stopping = False
        if errors:
            raise ExceptionGroup("部分进程组清理失败", errors)

    def stop(self, name):
        """Stop only a named owned process tree; external services are untouched."""
        group = self.groups.get(name)
        if group is None:
            return
        self.stopping = True
        try:
            group.shutdown()
            remaining = []
            for entry_name, proc in self.processes:
                if entry_name == name:
                    proc.reader.join(timeout=5)
                else:
                    remaining.append((entry_name, proc))
            self.processes = remaining
            del self.groups[name]
        finally:
            self.stopping = False
