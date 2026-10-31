"""Cookie 文件存储的纯逻辑：路径安全、列表、校验与读写，不依赖 Flask。"""

import re
from datetime import datetime
from pathlib import Path, PurePosixPath


class StorageError(Exception):
    """带 HTTP 状态码的可安全输出错误。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def resolve_under(root: Path, rel: str) -> Path:
    """把相对路径安全地解析到 root 下；拒绝绝对路径、盘符、反斜杠与父目录穿越。"""
    if not isinstance(rel, str):
        raise StorageError(400, "路径必须为字符串")
    rel = rel.strip()
    if not rel:
        raise StorageError(400, "路径不能为空")
    if "\\" in rel:
        raise StorageError(400, "路径不能包含反斜杠")
    if rel.startswith("/") or PurePosixPath(rel).is_absolute():
        raise StorageError(400, "路径必须为相对路径")
    if re.match(r"^[a-zA-Z]:", rel):
        raise StorageError(400, "路径不能包含盘符")
    parts = PurePosixPath(rel).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise StorageError(400, "路径包含非法段")
    root = root.resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise StorageError(400, "路径越界")
    return target


def safe_filename(name: str) -> str:
    """校验上传文件名：非空、无路径分隔符、以 .json 结尾，保留中文。"""
    if not isinstance(name, str):
        raise StorageError(400, "文件名必须为字符串")
    name = name.strip()
    if not name:
        raise StorageError(400, "文件名不能为空")
    if "/" in name or "\\" in name:
        raise StorageError(400, "文件名不能包含路径分隔符")
    if not name.lower().endswith(".json"):
        raise StorageError(400, "文件名必须以 .json 结尾")
    return name


def list_files(directory: Path, root: Path) -> list[dict]:
    """列出目录下所有 .json 文件（不递归），返回 name/size/modified，不含 value。"""
    root = root.resolve()
    directory = directory.resolve()
    if not directory.is_dir():
        raise StorageError(404, "目录不存在")
    if directory != root and root not in directory.parents:
        raise StorageError(400, "目录越界")
    items = []
    for path in sorted(directory.iterdir(), key=lambda p: p.name):
        if not path.is_file() or path.suffix.lower() != ".json":
            continue
        stat = path.stat()
        items.append({
            "name": str(path.relative_to(root)).replace("\\", "/"),
            "size": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        })
    return items


def validate_cookies_data(obj) -> None:
    """校验 JSON 内容为 dict（含非空 cookies 数组）或非空 cookie 数组。"""
    if isinstance(obj, list):
        if not obj:
            raise StorageError(400, "Cookie 数组不能为空")
        if any(not isinstance(item, dict) for item in obj):
            raise StorageError(400, "Cookie 数组元素必须为对象")
        return
    if isinstance(obj, dict):
        cookies = obj.get("cookies")
        if not isinstance(cookies, list) or not cookies:
            raise StorageError(400, "文件必须包含非空 cookies 数组")
        if any(not isinstance(item, dict) for item in cookies):
            raise StorageError(400, "cookies 数组元素必须为对象")
        return
    raise StorageError(400, "文件必须为 JSON 对象或数组")


def read_bytes(path: Path) -> bytes:
    if not path.is_file():
        raise StorageError(404, "文件不存在")
    return path.read_bytes()


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
