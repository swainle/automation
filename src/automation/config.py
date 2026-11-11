"""Resolve runtime files from the working directory, including uvx installs."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path.cwd().resolve()


def load_environment(root=None):
    load_dotenv((ROOT if root is None else Path(root)) / ".env")


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    if value.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if value.strip().lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} 必须为 true 或 false")
