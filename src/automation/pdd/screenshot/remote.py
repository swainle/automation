"""SSH 远程入口：stdin 心跳失联后停止 CS，日志走 stderr。"""
import argparse
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from automation.pdd.screenshot.services import stop_tree
from automation.pdd.screenshot.state import RunLock


def supervise(command, *, cwd, env, lease=30, stream=None):
    messages = queue.Queue()
    def read():
        try:
            for line in stream or sys.stdin:
                try:
                    kind = json.loads(line).get("type")
                except (ValueError, AttributeError):
                    continue
                if kind in {"ping", "stop"}:
                    messages.put(kind)
        finally:
            messages.put("stop")
    threading.Thread(target=read, daemon=True).start()
    child = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr)
    deadline = time.monotonic() + lease
    try:
        while child.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print("远程 CS 心跳超时，正在停止。", file=sys.stderr, flush=True)
                break
            try:
                message = messages.get(timeout=min(remaining, .5))
            except queue.Empty:
                continue
            if message == "stop":
                break
            deadline = time.monotonic() + lease
        return child.returncode if child.poll() is not None else 0
    finally:
        stop_tree(child)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--lease", type=float, default=30)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or args.lease <= 5:
        parser.error("端口须为 1024～65535，心跳超时须大于 5 秒")
    root = args.root.resolve()
    with RunLock(root / f"data/cs-remote-{args.port}.lock"):
        # 不连接已有服务，避免把别的 CS 误认为本次启动的 CS。
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", args.port))
            except OSError:
                print("远程 CS 端口已占用。", file=sys.stderr)
                return 1
        from dotenv import dotenv_values
        values = {key: value for key, value in dotenv_values(root / ".env").items() if value is not None}
        env = dict(os.environ, **values)
        env.update(COOKIES_SERVER_HOST="127.0.0.1", COOKIES_SERVER_PORT=str(args.port), PYTHONIOENCODING="utf-8")
        directory = Path(env.get("COOKIES_DIR") or "data/cookies")
        env["COOKIES_DIR"] = str(directory if directory.is_absolute() else root / directory)
        return supervise([sys.executable, "-u", "-m", "automation.com.cookies_server"],
                         cwd=root, env=env, lease=args.lease)


if __name__ == "__main__":
    raise SystemExit(main())
