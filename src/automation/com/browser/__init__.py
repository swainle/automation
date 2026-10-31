"""默认只打开本机 Chrome，其他浏览器操作按需调用。"""

import argparse
import os
from pathlib import Path
import shutil
import subprocess


def chrome_executable():
    candidates = [shutil.which("chrome"), shutil.which("google-chrome"), shutil.which("chromium"),
                  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.getenv(variable)
        if base:
            candidates.append(str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise FileNotFoundError("未找到本机 Google Chrome，请先安装 Chrome")


def launch_chrome(*, url=None, headless=False, debug_port=None, user_data_dir=None, quiet_startup=False):
    command = [str(chrome_executable())]
    if quiet_startup:
        command.extend(["--no-first-run", "--no-default-browser-check", "--hide-crash-restore-bubble"])
    if user_data_dir is not None:
        command.append(f"--user-data-dir={Path(user_data_dir).resolve()}")
    if headless:
        command.append("--headless")
    if debug_port is not None:
        if user_data_dir is None:
            raise ValueError("开启调试端口需要显式指定 --user-data-dir")
        if not 1024 <= debug_port <= 65535:
            raise ValueError("调试端口必须在 1024 到 65535 之间")
        command.extend([f"--remote-debugging-port={debug_port}", "--remote-debugging-address=127.0.0.1"])
    if url:
        command.append(url)
    return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="可选，启动时打开的地址")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--debug-port", type=int)
    parser.add_argument("--user-data-dir", type=Path)
    args = parser.parse_args()
    try:
        launch_chrome(url=args.url, headless=args.headless, debug_port=args.debug_port, user_data_dir=args.user_data_dir)
        print("已请求启动本机 Chrome。")
        return 0
    except (OSError, ValueError) as exc:
        print(str(exc))
        return 1
