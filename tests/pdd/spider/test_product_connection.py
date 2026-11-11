from unittest.mock import Mock, patch

import pytest

from automation.pdd.spider.runtime import Runtime


@pytest.mark.parametrize(('ready', 'status', 'error'), [
    (False, {}, 'WS 未启动'),
    (True, {'protocol': 2, 'connected': True}, '版本过旧'),
    (True, {'protocol': 3, 'connected': False}, '搜狗插件未连接'),
])
def test_manual_connection_fails_without_launching_services(ready, status, error):
    runtime = Runtime()
    with patch('automation.pdd.spider.runtime.port_ready', return_value=ready), \
            patch('automation.pdd.spider.runtime.subprocess.Popen') as launch, \
            patch.object(runtime, 'ranking_request', return_value=status), \
            patch.object(runtime.processes, 'start') as start, \
            pytest.raises(RuntimeError, match=error):
        runtime.connect_product_browser()
    launch.assert_not_called()
    start.assert_not_called()


def test_manual_connection_verifies_sogou_and_retains_external_services_on_close():
    runtime = Runtime()
    status = {'protocol': 3, 'connected': True, 'extension_peer_port': 45678}
    with patch('automation.pdd.spider.runtime.port_ready', return_value=True), \
            patch('automation.pdd.spider.runtime.subprocess.Popen') as launch, \
            patch.object(runtime, 'ranking_request', return_value=status) as request, \
            patch.object(runtime, '_verify_ranking_browser') as verify, \
            patch.object(runtime.processes, 'start') as start:
        runtime.connect_product_browser()
        runtime.close()
    request.assert_called_once_with('status', timeout=3)
    verify.assert_called_once_with(45678)
    launch.assert_not_called()
    start.assert_not_called()
    assert runtime.ranking_tab is None


def test_manual_connection_rejects_other_browsers():
    runtime = Runtime()
    with patch('automation.pdd.spider.runtime.port_ready', return_value=True), \
            patch.object(runtime, 'ranking_request', return_value={'protocol': 3, 'connected': True}), \
            patch.object(runtime, '_verify_ranking_browser', side_effect=RuntimeError('其他浏览器')), \
            pytest.raises(RuntimeError, match='其他浏览器'):
        runtime.connect_product_browser()
