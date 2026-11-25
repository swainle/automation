"""Hidden web entry points must never launch a browser or collection."""
from importlib import import_module
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize('browser', ['sogou', 'chrome', 'edge'])
def test_hidden_entry_does_not_run_browser(browser, monkeypatch, capsys):
    from automation.pdd.spider import commands
    execute = Mock(side_effect=AssertionError('hidden command must not collect'))
    monkeypatch.setattr(commands, 'execute', execute)
    module = import_module(f'automation.pdd.spider.{browser}')
    assert module.main(['--ranking-id', '1']) == 2
    assert '已隐藏' in capsys.readouterr().out
    execute.assert_not_called()
