"""从本地数据导出当天 Excel。"""
from pathlib import Path

from automation.pdd.screenshot.workbook import export_receivables, parse_amount


def export_day(root, day):
    from automation.pdd.screenshot.format_xlsx import format_daily_xlsx
    from automation.pdd.screenshot.logs import report
    path = format_daily_xlsx(root, day, database=Path(root) / "state/sqlite.db")
    report("管理", f"当天报表已导出：{path}")
    return path


def main():
    import argparse
    from automation.pdd.screenshot.config import load_environment
    from automation.pdd.screenshot.logs import configure, close, report
    from automation.pdd.screenshot.state import RunLock, today
    argparse.ArgumentParser(description="从当天本地数据导出 Excel").parse_args()
    try:
        root = load_environment()
        configure(root / "logs")
        with RunLock(root / "state/automation.lock"):
            export_day(root, today())
        return 0
    except Exception as error:
        report("管理", f"导出失败：{error}" if isinstance(error, (ValueError, RuntimeError)) else f"导出失败（{type(error).__name__}）")
        return 1
    finally:
        close()


if __name__ == "__main__":
    raise SystemExit(main())
