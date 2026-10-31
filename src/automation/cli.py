"""Unified command dispatcher. Runtime options are read from .env."""
import argparse
from importlib import import_module
import sys

from automation.config import load_environment

COMMANDS = {
    "com.cookies_server": "automation.com.cookies_server.app",
    "com.extension_bridge": "automation.com.extension_bridge",
}


def main():
    parser = argparse.ArgumentParser(description="automation：通用服务；配置读取当前目录 .env")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in COMMANDS:
        subparsers.add_parser(command, description=f"运行 {command}；参数从当前目录 .env 读取")
    args = parser.parse_args()
    load_environment()
    # Module entry points retain their own help and internal subprocess options.
    original = sys.argv
    try:
        sys.argv = [f"automation {args.command}"]
        return import_module(COMMANDS[args.command]).main()
    finally:
        sys.argv = original
