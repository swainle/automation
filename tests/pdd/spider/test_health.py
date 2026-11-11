from datetime import datetime, timedelta
from unittest.mock import Mock
import psycopg
import unicodedata

import pytest

from automation.pdd.spider import config, health
from automation.pdd.spider.mobile_database import Store
from automation.pdd.spider.time_format import BEIJING


@pytest.fixture
def database(tmp_path, monkeypatch, pg_database):
    path = pg_database
    store = Store(path)
    entry = store.seed_rankings('2026-11-10', [{'name': '猫粮', 'main_name': '猫粮', 'tab': '畅销榜',
        'url': 'https://example.com/rank', 'tab_index': 0}], tmp_path / 'data')[0]
    store.save_rows(entry['id'], entry['directory'], [], [{'排行': 1, '标题': '测试商品'}])
    store.close()
    return path, entry['id']


def test_ascii_panels_align_chinese_and_truncate_long_text(capsys):
    health.table('CHECK', ('Name', 'State'), [('中文榜单名称很长', 'WARN\nready')], (8, 5))
    lines = capsys.readouterr().out.splitlines()
    lengths = {sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in line) for line in lines}
    assert len(lengths) == 1
    assert '中文榜单' in '\n'.join(lines)


def test_collection_summary_reports_cooldown_and_filters(database, capsys):
    path, rid = database
    now = datetime.now(BEIJING)
    with health.connect(path) as db:
        db.execute("UPDATE t_pdd_ranking_products SET mobile_state='completed'")
        db.execute('UPDATE t_pdd_rankings SET web_collected_at=%s', ((now - timedelta(minutes=5)).isoformat(),))
    with health.connect(path, readonly=True) as db:
        health.collection_status(db)
        output = capsys.readouterr().out
        assert '1/1' in output and '0/1' in output
        assert (now + timedelta(minutes=25)).strftime('%m-%d %H:%M:%S') in output
        health.collection_status(db, ranking_id=rid + 1)
        assert '猫粮' not in capsys.readouterr().out


def test_no_web_attempt_shows_ready_only_for_mobile_completed(database, capsys):
    path, _ = database
    with health.connect(path) as db:
        health.collection_status(db)
        assert 'READY' not in capsys.readouterr().out
        db.execute("UPDATE t_pdd_ranking_products SET mobile_state='completed'")
        health.collection_status(db)
        assert 'READY' in capsys.readouterr().out


def test_unavailable_database_does_not_get_initialized(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(health, 'environment_checks', lambda: [('PostgreSQL', 'FAIL', 'Unavailable')])
    def connect(*args, **kwargs):
        raise psycopg.OperationalError('Database unavailable')
    monkeypatch.setattr(health, 'connect', connect)
    assert health.run() == 1
    assert not list(tmp_path.glob('*.db'))
    assert 'COLLECTION STATUS' in capsys.readouterr().out


def test_check_uses_only_readonly_connections(database, monkeypatch, capsys):
    original = health.connect
    calls = []
    def connect(*args, **kwargs):
        calls.append(kwargs.get('readonly'))
        return original(*args, **kwargs)
    monkeypatch.setattr(health, 'connect', connect)
    monkeypatch.setattr(health, 'environment_checks', lambda: [('PostgreSQL', 'OK', 'Ready')])
    assert health.run() == 0
    assert calls == [True]
    assert 'COLLECTION STATUS' in capsys.readouterr().out
    with original(readonly=True) as db:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            db.execute("UPDATE t_pdd_ranking_products SET title='should not write'")


def test_checks_continue_after_failure_and_do_not_print_credentials(monkeypatch):
    def fail():
        raise RuntimeError('postgresql://user:secret@example.com/db')
    monkeypatch.setattr(health, 'business_paths', fail)
    for name in ('source_excel', 'ios_device', 'wda_status', 'sogou_browser',
                 'websocket_status', 'sogou_plugin', 'postgres_connection'):
        monkeypatch.setattr(health, name, lambda: ('OK', 'Ready'))
    rows = health.environment_checks()
    assert len(rows) == 8 and rows[0][1] == 'FAIL' and rows[-1][1] == 'OK'
    assert 'secret' not in str(rows) and 'example.com' not in str(rows)


def test_plugin_check_uses_status_only(monkeypatch):
    monkeypatch.setattr(health, 'port_ready', lambda port: True)
    runtime = Mock()
    runtime.ranking_request.return_value = {'connected': True, 'protocol': 3, 'extension_peer_port': 1234}
    monkeypatch.setattr(health, 'Runtime', lambda: runtime)
    assert health.sogou_plugin()[0] == 'OK'
    runtime.ranking_request.assert_called_once_with('status', timeout=3)
    runtime._verify_ranking_browser.assert_called_once_with(1234)
    runtime.start_ranking_browser.assert_not_called()
    runtime.ensure_wda.assert_not_called()


def test_stopped_bridge_does_not_start_it(monkeypatch):
    monkeypatch.setattr(health, 'port_ready', lambda port: False)
    runtime = Mock()
    monkeypatch.setattr(health, 'Runtime', runtime)
    assert health.websocket_status()[0] == 'WARN'
    assert health.sogou_plugin()[0] == 'WARN'
    runtime.assert_not_called()


def test_postgres_only_executes_read_only_connection_test(monkeypatch):
    monkeypatch.setattr(config, 'POSTGRES_URL', 'test-dsn')
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.execute.return_value.fetchone.return_value = {'value': 1}
    connect = Mock(return_value=connection)
    monkeypatch.setattr(health, 'connect', connect)
    monkeypatch.setattr(health, 'columns', lambda *args: {'id', 'mobile_state', 'sogou_state'})
    assert health.postgres_connection()[0] == 'OK'
    connect.assert_called_once_with(readonly=True)
    connection.execute.assert_called_once_with('SELECT 1 AS value')


def test_usb_check_is_bounded_and_never_starts_wda(monkeypatch):
    command = Mock(return_value='{"deviceList":["device-1"]}')
    monkeypatch.setattr(health.ios, '_ios', command)
    monkeypatch.delenv('SPIDER_IOS_UDID', raising=False)
    assert health.ios_device()[0] == 'OK'
    command.assert_called_once_with('list', timeout=5)
    monkeypatch.setenv('SPIDER_IOS_UDID', 'another-device')
    assert health.ios_device()[0] == 'FAIL'


def test_exit_status_reports_failures_but_warnings_are_nonfatal(database, monkeypatch):
    monkeypatch.setattr(health, 'environment_checks', lambda: [('WDA', 'WARN', 'Stopped')])
    assert health.run() == 0
    monkeypatch.setattr(health, 'environment_checks', lambda: [('Plugin', 'FAIL', 'Disconnected')])
    assert health.run() == 1
