"""运行配置，读取项目相邻的业务工作目录 .env；远程命令由用户显式配置。"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv
from automation.com.cookies_server.config import ServerConfig

from automation.config import ROOT


@dataclass(frozen=True)
class Config:
    root: Path
    project_root: Path = ROOT
    ws_port: int = 8765
    cs_port: int = 8787
    cs_dir: Path = Path("data/cookies")
    ready_timeout: float = 120
    page_timeout: float = 30
    restart_limit: int = 3
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_user: str = ""
    ssh_password: str = field(default="", repr=False)
    ssh_key: str = ""
    ssh_command: str = ""
    ssh_cs_port: int = 8787

    @property
    def endpoint(self):
        return f"ws://127.0.0.1:{self.ws_port}/"

    @property
    def db_path(self):
        return self.root / "state/sqlite.db"

    @classmethod
    def load(cls, root=None):
        project_root = Path.cwd() if root is None else Path(root)
        business_root = load_environment(project_root)
        cs = ServerConfig.load(business_root)
        key = os.getenv("CS_SSH_KEY", "").strip()
        if key:
            key_path = Path(key).expanduser()
            key = str((key_path if key_path.is_absolute() else business_root / key_path).resolve())
        result = cls(
            root=business_root, project_root=project_root.resolve(), ws_port=int(os.getenv("AUTOMATION_WS_PORT", "8765")),
            cs_port=cs.port, cs_dir=cs.cookies_dir,
            ready_timeout=float(os.getenv("AUTOMATION_READY_TIMEOUT", "120")),
            page_timeout=float(os.getenv("AUTOMATION_PAGE_TIMEOUT", "30")),
            restart_limit=int(os.getenv("AUTOMATION_RESTART_LIMIT", "3")),
            ssh_host=os.getenv("CS_SSH_HOST", "").strip(),
            ssh_port=int(os.getenv("CS_SSH_PORT", "22")),
            ssh_user=os.getenv("CS_SSH_USER", "").strip(),
            ssh_password=os.getenv("CS_SSH_PASSWORD", ""),
            ssh_key=key,
            ssh_command=os.getenv("CS_SSH_COMMAND", "").strip(),
            ssh_cs_port=int(os.getenv("CS_SSH_REMOTE_PORT", "8787")))
        if any(not 1024 <= port <= 65535 for port in (result.ws_port, result.cs_port, result.ssh_cs_port)):
            raise ValueError("CS/WS 端口必须在 1024 到 65535 之间")
        if not 1 <= result.ssh_port <= 65535 or result.restart_limit < 0:
            raise ValueError("SSH 端口或重启次数配置无效")
        if result.ready_timeout <= 0 or result.page_timeout <= 0:
            raise ValueError("超时必须为正数")
        if result.ssh_host and not result.ssh_command:
            raise ValueError("远程 CS 模式需要配置 CS_SSH_COMMAND")
        if not result.ssh_host and result.ws_port == result.cs_port:
            raise ValueError("WS 与 CS 不能使用同一端口")
        return result


def runtime_root(project_root=None):
    project_root = Path.cwd() if project_root is None else Path(project_root)
    return project_root.resolve().parent / "automation.pdd.screenshot"


def load_environment(project_root=None):
    from automation.config import load_environment as load_common_environment
    load_common_environment(Path.cwd() if project_root is None else project_root)
    root = runtime_root(project_root)
    path = root / ".env"
    if not path.is_file():
        raise ValueError(f"业务配置不存在：{path}；请先运行 uv run python -m automation.pdd.screenshot --init 并填写配置")
    load_dotenv(path)
    return root
