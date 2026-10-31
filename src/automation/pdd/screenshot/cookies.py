"""读取截图业务工作目录的 .env，独立启动 Cookie 服务。"""
import argparse
import math
import sys

from automation.com.cookies_server.config import ServerConfig

from automation.com.cookies_server.app import main as serve
from automation.pdd.screenshot.config import load_environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", action="store_true", help="SSH 管理模式：接收 stdin 心跳，断线或收到 stop 时清理服务")
    parser.add_argument("--lease", type=float, default=30, help="SSH 心跳超时秒数，默认 30；仅 --remote 生效")
    args = parser.parse_args()
    if not math.isfinite(args.lease) or args.lease <= 5:
        parser.error("心跳超时须为大于 5 的有限秒数")
    try:
        root = load_environment()
        if args.remote:
            from automation.pdd.screenshot.remote import run_server
            for stream in (sys.stdout, sys.stderr):
                if hasattr(stream, "reconfigure"):
                    stream.reconfigure(encoding="utf-8")
            config = ServerConfig.load(root)
            return run_server(root, port=config.port, lease=args.lease)
    except (ValueError, OSError, RuntimeError) as error:
        print(f"启动失败：{error}", flush=True)
        return 1
    return serve(root=root)


if __name__ == "__main__":
    raise SystemExit(main())
