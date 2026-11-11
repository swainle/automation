from automation.pdd.spider import ranking
from unittest.mock import Mock, patch

import pytest


@pytest.mark.parametrize('command', ['ranking', 'product'])
def test_help_and_removed_flow_flags_do_not_start_services(command):
    from importlib import import_module
    module = import_module('automation.pdd.spider.' + command)
    with patch('automation.pdd.spider.runtime.Runtime') as runtime, patch('sys.argv', [command, '--help']):
        with pytest.raises(SystemExit) as stopped:
            module.main()
        assert stopped.value.code == 0
        runtime.assert_not_called()
    with patch('automation.pdd.spider.runtime.Runtime') as runtime, patch('sys.argv', [command, '--loop', 'collect_all']):
        with pytest.raises(SystemExit) as stopped:
            module.main()
        assert stopped.value.code == 2
        runtime.assert_not_called()


def test_initialize_preserves_existing_configuration(tmp_path):
    from automation.pdd.spider.initialize import initialize
    (tmp_path / '.env').write_text('SPIDER_DEBUG_PORT=9555\n', encoding='utf-8')
    initialize(tmp_path)
    assert (tmp_path / '.env').read_text(encoding='utf-8') == 'SPIDER_DEBUG_PORT=9555\n'
    assert (tmp_path / 'tools').is_dir()


@pytest.mark.parametrize('phone', [False])
def test_ranking_flag_selects_only_requested_device(phone):
    from automation.pdd.spider import ranking
    argv = ['ranking', '--ios'] if phone else ['ranking']
    with patch('sys.argv', argv), patch('automation.pdd.spider.commands.execute', return_value=0) as execute:
        assert ranking.main() == 0
    collect = execute.call_args.args[1]
    with patch('automation.pdd.spider.rank.read_leaderboards', return_value=[{'name': '主榜', 'url': 'https://example.com'}]), \
            patch('automation.pdd.spider.devices.Sogou') as browser, patch('automation.pdd.spider.devices.Ios') as ios, \
            patch('automation.pdd.spider.ranking.Ranking') as runner:
        store, runtime = Mock(), Mock()
        collect(store, runtime)
        selected, other = (ios, browser) if phone else (browser, ios)
        selected.assert_called_once_with(runtime)
        other.assert_not_called()
        assert runner.call_args.args[2] is selected.return_value


@pytest.mark.parametrize('flags,current_mode', [([], False), (['--current', '--source-name', '分类'], True)])
def test_ios_ranking_uses_full_collection(flags, current_mode):
    from automation.pdd.spider import ranking
    with patch('automation.pdd.spider.commands.execute', return_value=0) as execute, \
            patch('automation.pdd.spider.rank.current.collect') as current, \
            patch('automation.pdd.spider.rank.current.collect_sources') as sources:
        assert ranking.main(['--ios', '--refresh', *flags]) == 0
        store, runtime = Mock(), Mock()
        execute.call_args.args[1](store, runtime)
        if current_mode:
            current.assert_called_once_with(store, runtime, source_name='分类', full=True, refresh=True)
            sources.assert_not_called()
        else:
            sources.assert_called_once_with(store, runtime, refresh=True, ranking_id=None)
            current.assert_not_called()


def test_ios_ranking_id_cli_passes_selection():
    store, runtime = Mock(), Mock()
    with patch('automation.pdd.spider.commands.execute', side_effect=lambda description, callback, **kw: callback(store, runtime)), \
            patch('automation.pdd.spider.rank.current.collect_sources', return_value=0) as collect:
        assert ranking.main(['--ios', '--ranking-id', '12']) == 0
    collect.assert_called_once_with(store, runtime, refresh=False, ranking_id=12)


@pytest.mark.parametrize('args', [
    ['--ranking-id', '12'], ['--ios', '--ranking-id', '0'],
    ['--ios', '--ranking-id', '12', '--current'],
    ['--ios', '--ranking-id', '12', '--source-name', '分类'],
])
def test_ranking_id_rejects_invalid_arguments(args):
    with pytest.raises(SystemExit):
        ranking.main(args)
