"""Verify routing and .env configuration without starting services or browsers."""
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from automation.cli import COMMANDS, main
from automation.config import env_bool


@pytest.mark.parametrize("command,module", COMMANDS.items())
def test_dispatch(command, module):
    with patch("sys.argv", ["automation", command]), patch("automation.cli.load_environment"), patch(
        "automation.cli.import_module", return_value=SimpleNamespace(main=lambda: 7)
    ) as imported:
        assert main() == 7
    imported.assert_called_once_with(module)


def test_unknown_command_does_not_import():
    with patch("sys.argv", ["automation", "unknown"]), patch("automation.cli.import_module") as imported:
        with pytest.raises(SystemExit) as error:
            main()
    assert error.value.code == 2
    imported.assert_not_called()


def test_screenshot_command_removed_from_unified_cli():
    assert "pdd.screenshot" not in COMMANDS
    with patch("sys.argv", ["automation", "pdd.screenshot"]), patch("automation.cli.import_module") as imported:
        with pytest.raises(SystemExit) as error:
            main()
    assert error.value.code == 2
    imported.assert_not_called()


def test_screenshot_module_accepts_skip_sms_flag(tmp_path):
    from contextlib import ExitStack
    from unittest.mock import Mock
    from automation.pdd.screenshot import main as screenshot_main
    with ExitStack() as stack:
        stack.enter_context(patch("sys.argv", ["automation.pdd.screenshot", "--skip-sms-shop", "--once"]))
        stack.enter_context(patch("automation.pdd.screenshot.load_environment"))
        stack.enter_context(patch("automation.pdd.screenshot.Config.load", return_value=SimpleNamespace(root=tmp_path, db_path=tmp_path / 'db')))
        for name in ("configure", "close_logs", "RunLock", "StateStore", "run_interruptible"):
            stack.enter_context(patch(f"automation.pdd.screenshot.{name}"))
        run = stack.enter_context(patch("automation.pdd.screenshot.run", new=Mock(return_value=object())))
        assert screenshot_main() == 0
        assert run.call_args.kwargs == {"once": True, "skip_sms_shop": True}


def test_format_xlsx_does_not_load_business_config_or_start_services(tmp_path):
    from contextlib import ExitStack
    from automation.pdd.screenshot import main as screenshot_main
    with ExitStack() as stack:
        stack.enter_context(patch('sys.argv', ['automation.pdd.screenshot', '--format-xlsx']))
        stack.enter_context(patch('automation.pdd.screenshot.load_environment'))
        stack.enter_context(patch('automation.pdd.screenshot.ROOT', tmp_path))
        stack.enter_context(patch('automation.pdd.screenshot.today', return_value='2026-10-31'))
        stack.enter_context(patch('automation.pdd.screenshot.RunLock'))
        stack.enter_context(patch('automation.pdd.screenshot.report'))
        formatter = stack.enter_context(patch('automation.pdd.screenshot.format_xlsx.format_daily_xlsx', return_value=tmp_path / 'report.xlsx'))
        config = stack.enter_context(patch('automation.pdd.screenshot.Config.load'))
        run = stack.enter_context(patch('automation.pdd.screenshot.run'))
        store = stack.enter_context(patch('automation.pdd.screenshot.StateStore'))
        assert screenshot_main() == 0
        formatter.assert_called_once_with(tmp_path, '2026-10-31')
        config.assert_not_called()
        run.assert_not_called()
        store.assert_not_called()


def test_bridge_port_from_environment():
    from unittest.mock import AsyncMock
    from automation.com.extension_bridge import main as bridge_main
    with patch.dict(os.environ, {"AUTOMATION_WS_PORT": "9876"}), patch("sys.argv", ["automation"]), patch(
        "automation.com.extension_bridge.start_server", new_callable=AsyncMock
    ) as serve:
        assert bridge_main() == 0
    serve.assert_awaited_once_with(9876)


def test_invalid_boolean_is_rejected():
    with patch.dict(os.environ, {"PDD_SCREENSHOT_ONCE": "invalid"}):
        with pytest.raises(ValueError, match="PDD_SCREENSHOT_ONCE"):
            env_bool("PDD_SCREENSHOT_ONCE")
