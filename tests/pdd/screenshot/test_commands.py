"""新命令的目录、编排及影刀错误边界。"""
import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from automation.pdd.screenshot import run_cycle
from automation.pdd.screenshot.config import Config, runtime_root, load_environment
from automation.pdd.screenshot.report import APP_ID, ReportError, run_report
from automation.pdd.screenshot.runner import ShopRunner


def test_runtime_paths_and_business_environment(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    business = tmp_path / "automation.pdd.screenshot"
    business.mkdir()
    (project / ".env").write_text("AUTOMATION_WS_PORT=9999\n", encoding="utf-8")
    (business / ".env").write_text("AUTOMATION_WS_PORT=9001\nCOOKIES_DIR=inputs/cookies\nCS_SSH_KEY=keys/id_ed25519\nPDD_SCREENSHOT_ONCE=true\n", encoding="utf-8")
    monkeypatch.chdir(project)
    with patch.dict(os.environ, {"RPA_ROOT": "ignored"}, clear=True):
        assert load_environment() == business
        config = Config.load()
        assert runtime_root() == business
        assert config.root == business
        assert config.db_path == business / "state/sqlite.db"
        assert config.cs_dir == business / "inputs/cookies"
        assert config.ssh_key == str(business / "keys/id_ed25519")
        assert config.ws_port == 9001
        assert os.environ["PDD_SCREENSHOT_ONCE"] == "true"
    with patch.dict(os.environ, {"AUTOMATION_WS_PORT": "9002"}, clear=True):
        assert Config.load().ws_port == 9002
    (business / ".env").unlink()
    with pytest.raises(ValueError, match="业务配置不存在"):
        load_environment()


def test_crawl_does_not_export(tmp_path):
    runner = ShopRunner(Config(root=tmp_path), Mock(), Mock(), export_on_finish=False)
    runner._run_day = AsyncMock()
    with patch("automation.pdd.screenshot.runner.export_receivables") as export:
        asyncio.run(runner.run_day("2026-10-31", []))
        export.assert_not_called()


def test_finish_runs_after_crawl_and_failure_does_not_report():
    async def scenario(fail=False):
        calls = []
        async def crawl(*args):
            calls.append("crawl")
            if fail:
                raise ValueError("crawl failed")
        async def finish(day):
            calls.extend(["export", "report"])
        async def wait(*args):
            await asyncio.Future()
        runner = Mock(run_day=crawl)
        services = Mock(monitor=wait)
        if fail:
            with pytest.raises(ValueError):
                await run_cycle(runner, services, "2026-10-31", [], once=True, date_waiter=wait, after_day=finish)
            assert calls == ["crawl"]
        else:
            assert await run_cycle(runner, services, "2026-10-31", [], once=True, date_waiter=wait, after_day=finish) == "finished"
            assert calls == ["crawl", "export", "report"]
    asyncio.run(scenario())
    asyncio.run(scenario(True))


def test_report_failure_and_path_with_spaces():
    process = Mock(returncode=1)
    process.stdout.readline = AsyncMock(return_value=b"")
    process.wait = AsyncMock(return_value=1)
    path = Path("C:/Program Files/ShadowBot/shadowbot.shell-cli.exe")
    with patch("automation.pdd.screenshot.report.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)) as spawn:
        with pytest.raises(ReportError):
            asyncio.run(run_report(path))
        assert spawn.call_args.args == (str(path), "console", "task", "run", "--app-id", APP_ID)
        assert spawn.call_args.kwargs["cwd"] == path.parent
