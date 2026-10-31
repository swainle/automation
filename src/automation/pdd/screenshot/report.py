"""调用影刀 automation.pdd.screenshot.report，等待任务完成。"""
import asyncio
import os
from pathlib import Path

from automation.pdd.screenshot.logs import log
from automation.pdd.screenshot.services import StartupError

APP_ID = "f00b7263-9e1b-4e17-af4c-cc35676fc44d"


class ReportError(StartupError):
    pass


def cli_path():
    directory = os.getenv("SHADOWBOT_CLI_DIR", "").strip()
    if not directory or not Path(directory).is_absolute():
        raise ValueError("请在 .env 中配置绝对路径 SHADOWBOT_CLI_DIR")
    path = Path(directory) / "shadowbot.shell-cli.exe"
    if not path.is_file():
        raise ValueError(f"影刀 CLI 不存在：{path}")
    return path


async def run_report(path):
    log("影刀", "开始运行 automation.pdd.screenshot.report")
    try:
        process = await asyncio.create_subprocess_exec(
            str(path), "console", "task", "run", "--app-id", APP_ID,
            cwd=path.parent, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except OSError as error:
        raise ReportError(f"无法启动影刀 CLI（{type(error).__name__}）") from None
    try:
        while line := await process.stdout.readline():
            log("影刀", line.decode("utf-8", errors="replace").rstrip())
        code = await process.wait()
        if code:
            raise ReportError(f"影刀报表流程失败，CLI 退出码：{code}")
        log("影刀", "影刀 CLI 同步调用成功结束")
    finally:
        if process.returncode is None:
            process.terminate()
            await process.wait()


def main():
    import argparse
    from automation.pdd.screenshot.runtime import run_interruptible
    from automation.pdd.screenshot.config import load_environment
    from automation.pdd.screenshot.logs import configure, close, report
    from automation.pdd.screenshot.state import RunLock

    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        root = load_environment()
        path = cli_path()
        configure(root / "logs")
        with RunLock(root / "state/automation.lock"):
            run_interruptible(run_report(path))
        return 0
    except KeyboardInterrupt:
        report("管理", "影刀 CLI 调用已停止。")
        return 0
    except (ValueError, RuntimeError) as error:
        report("管理", f"影刀报表运行失败：{error}")
        return 1
    except Exception as error:
        report("管理", f"影刀报表运行失败（{type(error).__name__}）")
        return 1
    finally:
        close()


if __name__ == "__main__":
    raise SystemExit(main())
