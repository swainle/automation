"""Daily directories and size-limited UTF-8 logs using standard logging."""
import io
import logging
import sys
import threading
import time
import traceback
from functools import wraps
from contextlib import contextmanager
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

MAX_BYTES = 20 * 1024 * 1024


class ConsoleFormatter(logging.Formatter):
    def format(self, record):
        if getattr(record, "plain_output", False):
            return record.getMessage()
        return super().format(record)


class OperationConsoleHandler(logging.StreamHandler):
    """Keep an operation open, while allowing unrelated output to interrupt it."""
    pending = None

    def emit(self, record):
        try:
            event = getattr(record, "operation_event", None)
            token = getattr(record, "operation_token", None)
            if event == "start":
                self._break_line()
                self.stream.write(self.format(record))
                self.pending = (token, record.thread)
            elif event == "finish":
                if self.pending and self.pending[0] == token:
                    self.stream.write(record.operation_suffix + "\n")
                    self.pending = None
                else:
                    self._break_line()
                    self.stream.write(self.format(record) + "\n")
            elif (self.pending and self.pending[1] == record.thread
                  and record.levelno < logging.WARNING and not record.exc_info
                  and not getattr(record, "plain_output", False)
                  and "\n" not in record.getMessage()):
                self.stream.write(" " + record.getMessage())
            else:
                self._break_line()
                self.stream.write(self.format(record) + "\n")
            self.flush()
        except Exception:
            self.handleError(record)

    def _break_line(self):
        if self.pending:
            self.stream.write("\n")
            self.pending = None

    def close(self):
        self.acquire()
        try:
            self._break_line()
            self.flush()
        finally:
            self.release()
            super().close()


@contextmanager
def operation(description):
    """Print before work, append duration after work, and preserve exceptions."""
    logger = logging.getLogger("automation.pdd.spider.main")
    token = object()
    managed = isinstance(sys.stdout, LogStream)
    title = description.rstrip().rstrip(".") + "..."
    started = time.perf_counter()
    if managed:
        sys.stdout.flush()
        logger.info(title, extra={"operation_event": "start", "operation_token": token})
    else:
        print(title, end="", flush=True)
    error = None
    try:
        yield
    except BaseException as caught:
        error = caught
        raise
    finally:
        suffix = (f" 失败：{str(error) or type(error).__name__}" if error is not None else "")
        suffix += f" [{time.perf_counter() - started:.1f}s]"
        if managed:
            sys.stdout.flush()
            logger.log(logging.ERROR if error is not None else logging.INFO, title + suffix,
                       extra={"operation_event": "finish", "operation_token": token,
                              "operation_suffix": suffix})
        else:
            print(suffix, flush=True)


def timed_operation(description):
    """Decorate a synchronous operation; descriptions may depend on arguments."""
    def decorate(method):
        @wraps(method)
        def invoke(*args, **kwargs):
            title = description(*args, **kwargs) if callable(description) else description
            with operation(title):
                return method(*args, **kwargs)
        return invoke
    return decorate


class DailySizeHandler(RotatingFileHandler):
    def handleError(self, record):
        # stderr is redirected to this logger; reporting there would recurse.
        if logging.raiseExceptions and sys.__stderr__ is not None:
            traceback.print_exc(file=sys.__stderr__)

    def __init__(self, root, name, max_bytes=MAX_BYTES):
        self.root = Path(root)
        self.log_name = name
        self.day = datetime.now().strftime("%Y-%m-%d")
        folder = self.root / self.day
        folder.mkdir(parents=True, exist_ok=True)
        super().__init__(folder / f"{name}.log", maxBytes=max_bytes,
                         backupCount=1, encoding="utf-8", delay=True)

    def emit(self, record):
        day = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d")
        if day != self.day:
            if self.stream:
                self.stream.close()
                self.stream = None
            self.day = day
            folder = self.root / day
            folder.mkdir(parents=True, exist_ok=True)
            self.baseFilename = str((folder / f"{self.log_name}.log").resolve())
        super().emit(record)

    def shouldRollover(self, record):
        if self.stream is None:
            self.stream = self._open()
        self.stream.seek(0, 2)
        size = self.stream.tell()
        message = self.format(record) + "\n"
        return size > 0 and size + len(message.encode("utf-8")) > self.maxBytes

    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        path = Path(self.baseFilename)
        index = 1
        while path.with_name(f"{path.stem}.{index}.log").exists():
            index += 1
        if path.exists():
            path.rename(path.with_name(f"{path.stem}.{index}.log"))


class LogStream(io.TextIOBase):
    """Adapt existing print calls without changing their callers."""
    def __init__(self, logger, level):
        self.logger = logger
        self.level = level
        self.local = threading.local()

    @property
    def encoding(self):
        return "utf-8"

    def writable(self):
        return True

    def write(self, text):
        pending = getattr(self.local, "pending", "") + text
        lines = pending.split("\n")
        self.local.pending = lines.pop()
        for line in lines:
            if line.strip():
                self.logger.log(self.level, line.rstrip("\r"))
        return len(text)

    def flush(self):
        pending = getattr(self.local, "pending", "")
        if pending:
            self.local.pending = ""
            self.logger.log(self.level, pending)

    def write_plain(self, text):
        """Keep structured CLI output copyable while still recording it in logs."""
        self.flush()
        self.logger.log(self.level, text, extra={"plain_output": True})


@contextmanager
def console_logging(project_root):
    original_out, original_err = sys.stdout, sys.stderr
    parent = logging.getLogger("automation.pdd.spider")
    old_level, old_propagate = parent.level, parent.propagate
    parent.setLevel(logging.INFO)
    parent.propagate = False
    installed = []
    console = OperationConsoleHandler(original_out)
    console.setFormatter(ConsoleFormatter("%(asctime)s [%(name)s] %(message)s", "%H:%M:%S"))
    parent.addHandler(console)
    installed.append((parent, console))
    for name in ("main", "tunel", "image", "runwda", "forward"):
        logger = logging.getLogger(f"automation.pdd.spider.{name}")
        handler = DailySizeHandler(Path(project_root) / "logs", name)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        installed.append((logger, handler))
    logger = logging.getLogger("automation.pdd.spider.main")
    out, err = LogStream(logger, logging.INFO), LogStream(logger, logging.ERROR)
    sys.stdout, sys.stderr = out, err
    try:
        yield
    except KeyboardInterrupt:
        logger.info("用户中断")
        raise
    except Exception:
        logger.exception("主进程异常退出")
        raise
    finally:
        out.flush()
        err.flush()
        sys.stdout, sys.stderr = original_out, original_err
        for owner, handler in installed:
            owner.removeHandler(handler)
            handler.close()
        parent.setLevel(old_level)
        parent.propagate = old_propagate
