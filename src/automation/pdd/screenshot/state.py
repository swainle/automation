"""每日店铺结果；过程事件写日志，不写入 Cookie 或账号密码。"""
import json
import sqlite3
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from automation.pdd.verification import VERIFICATIONS, VERIFICATION_LABELS

CHINA = timezone(timedelta(hours=8))
DONE = {"success", "login_error"}


def today():
    return datetime.now(CHINA).date().isoformat()


def timestamp():
    return datetime.now(CHINA).isoformat(timespec="seconds")


class RunLock(AbstractContextManager):
    """进程崩溃也会释放的操作系统文件锁，防止两轮同时控制浏览器。"""
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            import os
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("已有自动化流程运行，请勿同时启动") from None
        return self

    def __exit__(self, *args):
        self.file.close()


class StateStore:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(daily_shops)")}
        if "step" in columns:
            self.db.close()
            raise ValueError(f"数据库需要迁移：请先备份并执行 {Path(__file__).parent / 'migrate/20261031134259_merge_daily_shops.sql'}")
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS daily_shops (
                day TEXT NOT NULL, shop TEXT NOT NULL, status TEXT NOT NULL,
                row_number INTEGER, manager TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                reason TEXT NOT NULL DEFAULT '', cleanup_ok INTEGER,
                amount TEXT, image_path TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
                PRIMARY KEY(day, shop)
            );
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(daily_shops)")}
        if "row_number" not in columns:
            self.db.execute("ALTER TABLE daily_shops ADD COLUMN row_number INTEGER")
        self.db.commit()

    def save_shop_details(self, day, shops):
        with self.db:
            self.db.executemany(
                "UPDATE daily_shops SET manager=?,row_number=? WHERE day=? AND shop=?",
                [(shop.manager, shop.row_number, day, shop.shop_name) for shop in shops])

    def seed(self, day, shops):
        with self.db:
            self.db.executemany(
                "INSERT OR IGNORE INTO daily_shops(day,shop,status,updated_at) VALUES(?,?,'pending',?)",
                [(day, shop, timestamp()) for shop in shops])
            self.db.execute(
                "UPDATE daily_shops SET status='interrupted',reason='previous_process_interrupted',updated_at=? "
                "WHERE day=? AND status='running'", (timestamp(), day))

    def get(self, day, shop):
        row = self.db.execute("SELECT * FROM daily_shops WHERE day=? AND shop=?", (day, shop)).fetchone()
        return dict(row) if row else None

    def completed(self, day, shop):
        row = self.get(day, shop)
        return bool(row and row["status"] in DONE)

    def begin(self, day, shop):
        now = timestamp()
        with self.db:
            self.db.execute("UPDATE daily_shops SET status='running',attempts=attempts+1,"
                            "reason='',cleanup_ok=NULL,updated_at=? "
                            "WHERE day=? AND shop=?", (now, day, shop))

    def step(self, day, shop, step):
        with self.db:
            self.db.execute("UPDATE daily_shops SET updated_at=? WHERE day=? AND shop=?", (timestamp(), day, shop))

    def finish(self, day, shop, status, reason="", cleanup_ok=None):
        now = timestamp()
        with self.db:
            self.db.execute("UPDATE daily_shops SET status=?,reason=?,cleanup_ok=?,updated_at=? "
                            "WHERE day=? AND shop=?", (status, reason, cleanup_ok, now, day, shop))

    def event(self, day, shop, kind, detail):
        from automation.pdd.screenshot.logs import log
        log("管理", f"{day} {shop or ''} {kind} {json.dumps(detail, ensure_ascii=False)}")

    def close(self):
        self.db.close()

    def save_receivable(self, day, shop, image_path, amount=None):
        with self.db:
            self.db.execute(
                "INSERT INTO daily_shops(day,shop,status,amount,image_path,updated_at) VALUES(?,?,'pending',?,?,?) "
                "ON CONFLICT(day,shop) DO UPDATE SET amount=excluded.amount,"
                "image_path=excluded.image_path,updated_at=excluded.updated_at",
                (day, shop, amount, str(image_path), timestamp()))

    def receivables(self, day):
        return [dict(row) for row in self.db.execute(
            "SELECT shop,amount,image_path FROM daily_shops WHERE day=? AND image_path<>'' ORDER BY rowid", (day,))]
