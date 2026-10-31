"""读取截图业务工作目录的 .env，独立启动 Cookie 服务。"""
import argparse

from automation.com.cookies_server.app import main as serve
from automation.pdd.screenshot.config import load_environment


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        root = load_environment()
    except (ValueError, OSError) as error:
        print(f"启动失败：{error}", flush=True)
        return 1
    return serve(root=root)


if __name__ == "__main__":
    raise SystemExit(main())
