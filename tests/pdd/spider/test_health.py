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


def test_check_outputs_only_environment_and_ranking_panels(monkeypatch, capsys):
    from contextlib import nullcontext
    db = Mock()
    db.execute.return_value.fetchall.return_value = []
    monkeypatch.setattr(health, 'connect', lambda **kwargs: nullcontext(db))
    monkeypatch.setattr(health, 'columns', lambda *args: {'product_count'})
    monkeypatch.setattr(health, 'environment_checks', lambda: [('iOS device', 'OK', 'Ready')])
    assert health.run(capture_date='2026-11-16') == 0
    output = capsys.readouterr().out
    assert output.count('PDD SPIDER CHECK / ENVIRONMENT') == 1
    assert output.count('COLLECTION STATUS') == 1
    assert 'PRODUCT STATUS' not in output and 'RESULT:' not in output


def test_collection_summary_reports_six_steps_and_missing_identity(database, capsys):
    from automation.pdd.spider.mobile_database import PRODUCT_COLUMNS
    path, rid = database
    with health.connect(path) as db:
        db.execute("UPDATE t_pdd_ranking_products SET " + ','.join(f"{c}='completed'" for c in PRODUCT_COLUMNS))
        health.collection_status(db)
        output = capsys.readouterr().out
        assert '1/1' in output and '0/1' in output
        assert 'Matched' in output and 'Identity' not in output and 'PRODUCT STATUS' not in output
        health.collection_status(db, ranking_id=rid + 1)
        assert '猫粮' not in capsys.readouterr().out


def test_matched_and_points_follow_data_instead_of_analysis_flags(database, tmp_path, monkeypatch):
    path, rid = database
    store = Store(path)
    try:
        first = store.items(rid)[0]
        second = store.save_item(rid, store.list(rid)['directory'], {'排行': 2, '标题': '第二个'})
        store.db.execute("UPDATE t_pdd_ranking_products SET platform_product_id='123',platform_product_link='link',listing_date='2026-01-01' WHERE id=%s", (first['id'],))
        store.db.execute("INSERT INTO t_pdd_product_analysis(ranking_product_id,match_state,point_state) VALUES(%s,'failed','completed'),(%s,'completed','failed')", (first['id'], second['id']))
        store.db.execute("""INSERT INTO t_pdd_selling_point(ranking_product_id,platform_product_id,main_point,secondary_point,raw_json)
            VALUES(%s,'','卖点','','{}')""", (second['id'],))
        panel = Mock()
        monkeypatch.setattr(health, 'table', panel)
        health.collection_status(store.db, ranking_id=rid)
        panel.assert_called_once()
        row = panel.call_args.args[2][0]
        assert row[4:6] == ('1/2', '1/2')
        assert row[-1] == 0  # Failed audit flags do not override existing business data.
    finally:
        store.close()


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
    assert len(rows) == 5 and rows[0][1] == 'FAIL' and rows[-1][1] == 'OK'
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
    monkeypatch.setattr(health, 'columns', lambda *args: {'id', 'state', *health.PRODUCT_COLUMNS})
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
