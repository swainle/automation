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


def test_init_cli_without_env_does_not_start_workflow(tmp_path, monkeypatch):
    from automation.pdd.screenshot import main
    from dotenv import dotenv_values
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr("sys.argv", ["automation.pdd.screenshot", "--init"])
    with patch.dict(os.environ, {"PDD_SCREENSHOT_ONCE": "invalid"}, clear=True), patch(
        "automation.pdd.screenshot.Config.load", side_effect=AssertionError("初始化不能加载配置")
    ), patch("automation.pdd.screenshot.run_interruptible") as run:
        assert main() == 0
        run.assert_not_called()
    business = runtime_root()
    assert all((business / name).is_dir() for name in ["data/cookies/Cookie", "logs", "state"])
    values = dotenv_values(business / ".env")
    assert values["FEISHU_APP_SECRET"] == ""
    assert values["COOKIES_DIR"] == "./data/cookies/Cookie"
    assert values["SHADOWBOT_CLI_DIR"] == "C:/Program Files/ShadowBot"
    assert not (business / "state/sqlite.db").exists()
    with patch.dict(os.environ, {}, clear=True):
        assert Config.load().root == business


def test_init_preserves_existing_configuration_and_data(tmp_path):
    from automation.pdd.screenshot.initialize import initialize
    project = tmp_path / "project"
    business = runtime_root(project)
    business.mkdir()
    original = b"# existing settings\r\nFEISHU_APP_SECRET=fixture\r\n"
    (business / ".env").write_bytes(original)
    (business / "data").mkdir()
    (business / "data/existing.txt").write_bytes(b"existing data")
    initialize(project)
    initialize(project)
    assert (business / ".env").read_bytes() == original
    assert (business / "data/existing.txt").read_bytes() == b"existing data"
    assert (business / "state").is_dir()


def test_business_cookie_server_uses_business_config(tmp_path, monkeypatch):
    from automation.pdd.screenshot.cookeis_server import main
    project = tmp_path / "project"
    project.mkdir()
    business = runtime_root(project)
    business.mkdir()
    (business / ".env").write_text("COOKIES_DIR=inputs/cookies\nCOOKIES_SERVER_PORT=9876\n", encoding="utf-8")
    monkeypatch.chdir(project)
    monkeypatch.setattr("sys.argv", ["cookeis_server"])
    app = Mock()
    with patch.dict(os.environ, {}, clear=True), patch(
        "automation.com.cookies_server.app.create_app", return_value=app
    ) as create:
        assert main() == 0
        create.assert_called_once_with(business / "inputs/cookies")
        app.run.assert_called_once_with(host="127.0.0.1", port=9876, debug=False)


def test_business_cookie_server_missing_config_does_not_start(tmp_path, monkeypatch):
    from automation.pdd.screenshot.cookeis_server import main
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr("sys.argv", ["cookeis_server"])
    with patch("automation.pdd.screenshot.cookeis_server.serve") as serve:
        assert main() == 1
        serve.assert_not_called()
