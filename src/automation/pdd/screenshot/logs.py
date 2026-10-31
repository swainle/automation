"""终端与按日期/来源拆分的运行日志。"""
import logging
import sys
from datetime import datetime
from pathlib import Path
from automation.pdd.screenshot.state import CHINA


class ChinaFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, CHINA).strftime(datefmt or "%H:%M:%S")


class DailyFiles(logging.Handler):
    def __init__(self, root, max_bytes=20 * 1024 * 1024):
        super().__init__()
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.indices = {}

    def rotate(self, path):
        index = self.indices.get(path, 0) + 1
        target = path.with_name(f"{path.stem}.{index}.log")
        while target.exists():
            index += 1
            target = path.with_name(f"{path.stem}.{index}.log")
        path.rename(target)
        self.indices[path] = index

    def write(self, path, payload):
        while payload:
            size = path.stat().st_size if path.exists() else 0
            available = self.max_bytes - size
            if available <= 0:
                self.rotate(path)
                continue
            cut = min(available, len(payload))
            # 每个分块均保持合法 UTF-8，不把中文字符切成两半。
            if cut < len(payload):
                while cut and payload[cut] & 0xC0 == 0x80:
                    cut -= 1
            if not cut:
                self.rotate(path)
                continue
            with path.open("ab") as output:
                output.write(payload[:cut])
            payload = payload[cut:]

    def emit(self, record):
        try:
            day = datetime.fromtimestamp(record.created, CHINA).date().isoformat()
            directory = self.root / day
            directory.mkdir(parents=True, exist_ok=True)
            name = {"CS": "cs", "WS": "ws", "业务": "business", "浏览器": "browser", "管理": "manager"}.get(record.source, "manager")
            self.write(directory / f"{name}.log", (self.format(record) + "\n").encode("utf-8"))
        except Exception:
            self.handleError(record)


def configure(root):
    close()
    logger = logging.getLogger("pdd.automation")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = ChinaFormatter("%(asctime)s [%(source)s] %(message)s")
    for handler in (logging.StreamHandler(sys.stdout), DailyFiles(root)):
        handler.setFormatter(formatter)
        logger.addHandler(handler)


def log(source, message, *, level=logging.INFO):
    logging.getLogger("pdd.automation").log(level, message, extra={"source": source})


def report(source, message):
    if logging.getLogger("pdd.automation").handlers:
        log(source, message)
    else:
        print(f"[{source}] {message}", flush=True)


def close():
    logger = logging.getLogger("pdd.automation")
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
