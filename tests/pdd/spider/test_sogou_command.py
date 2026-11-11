from unittest.mock import Mock, patch
import sys

from automation.pdd.spider import config
from automation.pdd.spider.runtime import Runtime


def test_launcher_detaches_only_its_new_bridge_and_keeps_workspace(tmp_path):
    executable = tmp_path / 'SogouExplorer.exe'
    executable.touch()
    runtime = Runtime()
    runtime.ranking_request = Mock(side_effect=[{'connected': False, 'protocol': 3}, {'connected': True, 'protocol': 3, 'extension_peer_port': 1234},
                                              {'tab_id': 10, 'persistent': True, 'protocol': 2}])
    with patch.object(config, 'SOGOU_EXECUTABLE', str(executable)), patch.object(config, 'BUSINESS_ROOT', tmp_path), \
            patch('automation.pdd.spider.runtime.port_ready', return_value=False), \
            patch('automation.pdd.spider.runtime.subprocess.Popen') as popen, \
            patch.object(runtime, '_verify_ranking_browser'), patch.object(runtime.processes, 'start') as managed:
        runtime.start_ranking_browser(persistent=True)
        assert popen.call_count == 2
        assert popen.call_args_list[0].args[0][3] == 'automation.com.extension_bridge'
        assert (popen.call_args_list[0].kwargs['creationflags'] != 0) if sys.platform == 'win32' else popen.call_args_list[0].kwargs['start_new_session']
        assert not any('debugging' in value for value in popen.call_args_list[1].args[0])
        managed.assert_not_called()
        runtime.close()
        assert runtime.ranking_request.call_count == 3


def test_sogou_module_prepares_browser_without_collecting_or_creating_tasks(tmp_path):
    from automation.pdd.spider.sogou import main
    with patch('sys.argv', ['sogou']), patch.object(config, 'BUSINESS_ROOT', tmp_path), \
            patch('automation.pdd.spider.runtime.Runtime') as factory, \
            patch('automation.pdd.spider.sogou.close_existing_sogou', return_value=2) as close:
        assert main() == 0
        close.assert_called_once_with()
        factory.return_value.start_ranking_browser.assert_called_once_with(persistent=True)
        factory.return_value.close.assert_called_once()
        assert not (tmp_path / 'data/sqlite.db').exists()


def test_close_sogou_closes_all_instances_and_children_but_not_chrome():
    from automation.pdd.spider.sogou import close_existing_sogou
    first = Mock(pid=10, info={'name': 'SogouExplorer.exe'})
    second = Mock(pid=20, info={'name': 'SogouExplorer.exe'})
    child = Mock(pid=11)
    chrome = Mock(pid=30, info={'name': 'chrome.exe'})
    first.children.return_value = [child]
    second.children.return_value = []
    with patch('automation.pdd.spider.sogou.psutil.process_iter', side_effect=[[first, second, chrome], [chrome]]), \
            patch('automation.pdd.spider.sogou.psutil.wait_procs', side_effect=[([first, second], [child]), ([child], [])]):
        assert close_existing_sogou() == 3
    for process in (first, second, child):
        process.terminate.assert_called_once_with()
    child.kill.assert_called_once_with()
    chrome.terminate.assert_not_called()


def test_failed_sogou_shutdown_prevents_reopening(tmp_path):
    from automation.pdd.spider.sogou import main
    with patch('sys.argv', ['sogou']), patch.object(config, 'BUSINESS_ROOT', tmp_path), \
            patch('automation.pdd.spider.runtime.Runtime') as factory, \
            patch('automation.pdd.spider.sogou.close_existing_sogou', side_effect=RuntimeError('无法关闭搜狗')):
        assert main() == 1
    factory.return_value.start_ranking_browser.assert_not_called()
