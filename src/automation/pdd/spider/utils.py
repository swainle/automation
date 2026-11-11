"""工具函数：下载、CSV、文本/销量/时间解析、文件名清理。"""
import csv
import json
import re
import urllib.request
from urllib.parse import urlsplit
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from automation.pdd.spider.config import HEADERS


def download(url: str, path: Path) -> bool:
    """下载图片到指定路径。"""
    if not url or urlsplit(url).scheme not in {'http', 'https'}:
        return False
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return True
    except Exception as ex:
        print(f"    下载失败 {url[:80]} -> {ex}")
        return False


def text(locator) -> str:
    """读取定位器文本。"""
    try:
        return locator.first.inner_text(timeout=3000).strip()
    except Exception:
        return ""


def sales_to_int(s: str) -> int:
    """销量文本转整数：疯抢200万件 -> 2000000。"""
    if not s:
        return 0
    m = re.search(r"([\d.]+)\s*万", s)
    if m:
        return int(float(m.group(1)) * 10000)
    m = re.search(r"([\d.]+)\s*亿", s)
    if m:
        return int(float(m.group(1)) * 100000000)
    m = re.search(r"(\d+)", s)
    if m:
        return int(m.group(1))
    return 0


def sanitize(name: str) -> str:
    """清理文件名中的非法字符。"""
    return re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip()


def write_csv(path: Path, header: list, rows: list) -> None:
    """写 CSV（utf-8-sig，Excel 直接打开不乱码）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


@contextmanager
def _xlsx_lock(path: Path, *, timeout=30):
    """协调榜单和产品两个采集进程的读改写。"""
    import os
    if os.name == "nt":
        import msvcrt
    else:
        import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a+b") as lock:
        lock.seek(0, 2)
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                lock.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(f"等待写表锁超时: {path}")
                time.sleep(0.1)
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def write_xlsx(path: Path, header: list, rows: list) -> None:
    with _xlsx_lock(path):
        _write_xlsx(path, header, rows)


def refresh_listing_dates(path: Path) -> None:
    """读取最新榜单后合并产品首图日期，不覆盖并发新增的链接。"""
    with _xlsx_lock(path):
        if path.exists():
            header, rows = read_xlsx(path)
            _write_xlsx(path, header, rows)


def _write_xlsx(path: Path, header: list, rows: list) -> None:
    """写 xlsx（Excel 原生格式）。

    原子写：先写临时文件再 os.replace 覆盖，避免并发读（如 product 轮询）读到半截文件。
    """
    import os

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for row in rows:
        values = list(row)
        if path.name == "榜单数据.xlsx" and "排行" in header and "上架日期" in header:
            rank = values[header.index("排行")]
            if rank is not None:
                status_file = path.parent / f"榜{int(rank):02d}" / "status.json"
                if status_file.exists():
                    status = json.loads(status_file.read_text(encoding="utf-8"))
                    if "listing_date" in status:
                        values[header.index("上架日期")] = status["listing_date"]
        ws.append(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    wb.save(str(tmp))
    os.replace(tmp, path)


def read_xlsx(path: Path) -> tuple[list, list]:
    """读 xlsx，返回 (表头, 行列表)。"""
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True)
    ws = wb.active
    rows = [[c.value for c in row] for row in ws.iter_rows()]
    wb.close()
    wb.close()
    if not rows:
        return [], []
    return list(rows[0]), rows[1:]


def ts_to_date(ts) -> str:
    """Unix 时间戳（秒）转 YYYY-MM-DD。"""
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
    except Exception:
        return ""


def format_specs(specs: str) -> str:
    """接口 specs JSON 字符串转 '口味：xxx 型号：xxx'。"""
    if not specs:
        return ""
    try:
        arr = json.loads(specs)
        return " ".join(f"{d.get('spec_key', '')}：{d.get('spec_value', '')}" for d in arr)
    except Exception:
        return specs
