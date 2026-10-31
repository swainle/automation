"""cookies_server 的 .env 配置读取与目录解析。"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787


@dataclass(frozen=True)
class ServerConfig:
    cookies_dir: Path
    host: str
    port: int

    @classmethod
    def load(cls, root: Path) -> "ServerConfig":
        load_dotenv(root / ".env")
        raw_dir = (os.getenv("COOKIES_DIR") or "data/cookies").strip()
        host = (os.getenv("COOKIES_SERVER_HOST") or DEFAULT_HOST).strip() or DEFAULT_HOST
        raw_port = (os.getenv("COOKIES_SERVER_PORT") or str(DEFAULT_PORT)).strip()
        try:
            port = int(raw_port)
        except ValueError:
            raise ValueError("COOKIES_SERVER_PORT 必须为整数") from None
        if not 1024 <= port <= 65535:
            raise ValueError("COOKIES_SERVER_PORT 必须在 1024 到 65535 之间")
        return cls(cookies_dir=resolve_cookies_dir(raw_dir, root), host=host, port=port)


def resolve_cookies_dir(value: str, root: Path) -> Path:
    """COOKIES_DIR 支持绝对路径或相对项目根目录的相对路径。"""
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return path.resolve()
