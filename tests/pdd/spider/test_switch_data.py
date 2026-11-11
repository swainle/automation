from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from automation.pdd.spider import config
from automation.pdd.spider.mobile_database import Store, connect
from automation.pdd.spider.switch_data import switch_data


@pytest.fixture
def database(pg_database):
    store = Store()
    entry = store.seed_rankings('2026-11-04', [dict(name='分类', main_name='分类', tab='畅销榜',
        url='https://example.com/rank', tab_index=0)], config.OUTPUT_ROOT)[0]
    Path(entry['directory']).mkdir(parents=True)
    store.save_rows(entry['id'], entry['directory'], [], [{'排行': 1, '标题': '商品'}])
    item = store.items(entry['id'])[0]
    store.save_progress(item['id'], {'negative_comments': [{'comment': '评论', 'time': '2026-10-02'}]})
    store.close()
    return pg_database, Path(entry['directory'])


def test_switch_preserves_ids_and_creates_full_backup(database):
    dsn, old = database
    with connect(dsn, readonly=True) as db:
        before = db.execute('SELECT * FROM t_pdd_rankings').fetchall()
    backup, count = switch_data(dsn, '2026-11-04', '2026-11-05')
    assert count == 1 and old.with_name('2026-11-05').is_dir()
    assert json.loads(backup.read_text(encoding='utf-8'))['t_pdd_rankings'] == before
    with connect(dsn, readonly=True) as db:
        assert db.execute('SELECT id,capture_date FROM t_pdd_rankings').fetchone() == {'id': 1, 'capture_date': '2026-11-05'}
        assert db.execute('SELECT id,title FROM t_pdd_ranking_products').fetchone() == {'id': 1, 'title': '商品'}
        assert db.execute('SELECT commented_at FROM t_pdd_product_comments').fetchone()['commented_at'] == '2026-10-02'


@pytest.mark.parametrize('source,target', [('2026-11-04', '2026-11-04'), ('2026-11-03', '2026-11-05'),
    ('2026-11-04', '2026-02-30'), ('20261104', '2026-11-05')])
def test_invalid_switch_does_not_modify_database(database, source, target):
    dsn, old = database
    with pytest.raises(ValueError):
        switch_data(dsn, source, target)
    with connect(dsn, readonly=True) as db:
        assert db.execute('SELECT capture_date FROM t_pdd_rankings').fetchone()['capture_date'] == '2026-11-04'
    assert old.is_dir()


def test_existing_target_is_not_overwritten(database):
    dsn, _ = database
    store = Store()
    store.seed_rankings('2026-11-05', [dict(name='其他', main_name='其他', tab='畅销榜',
        url='https://example.com/rank', tab_index=0)], config.OUTPUT_ROOT)
    store.close()
    with pytest.raises(ValueError, match='已有采集数据'):
        switch_data(dsn, '2026-11-04', '2026-11-05')


def test_cli_switch_does_not_start_collection(database, monkeypatch):
    from automation.pdd.spider import commands
    from automation.pdd.spider.__main__ import main
    execute = Mock()
    monkeypatch.setattr(commands, 'execute', execute)
    assert main(['--switch-data', '2026-11-04', '--switch-to', '2026-11-05']) == 0
    assert main(['--switch-data', '2026-11-04', '--switch-to', '2026-11-05']) == 1
    execute.assert_not_called()


@pytest.mark.parametrize('args', [['--switch-data', '2026-11-04'], ['--mobile', '--switch-to', '2026-11-05'], ['--switch-to', '2026-11-05']])
def test_cli_requires_paired_dates(args):
    from automation.pdd.spider.__main__ import main
    with pytest.raises(SystemExit) as stopped:
        main(args)
    assert stopped.value.code == 2


def test_switch_moves_relative_image_paths_and_status_times(database):
    dsn, old = database
    status = {'date': '2026-11-04', 'created_at': '2026-11-03T09:12:13+08:00',
              'stages': {'product': {'completed_at': '2026-11-04T11:22:33+08:00'}},
              'listing_date': '2026-10-01', 'commented_at': '2026-10-02'}
    (old / 'status.json').write_text(json.dumps(status), encoding='utf-8')
    store = Store()
    store.save_product_image(1, dict(image_type='main', position=1, local_path='主图/001.jpg'))
    store.db.execute("UPDATE t_pdd_rankings SET created_at='2026-11-04T09:12:13+08:00'")
    store.close()
    backup, _ = switch_data(dsn, '2026-11-04', '2026-11-05')
    file = old.with_name('2026-11-05') / 'status.json'
    result = json.loads(file.read_text(encoding='utf-8'))
    assert result['date'] == '2026-11-05'
    assert result['created_at'] == '2026-11-05T09:12:13+08:00'
    assert result['listing_date'] == '2026-10-01' and result['commented_at'] == '2026-10-02'
    assert datetime.fromtimestamp(file.stat().st_mtime, timezone(timedelta(hours=8))).date().isoformat() == '2026-11-05'
    assert Path(str(backup) + '.files.zip').is_file()
    with connect(dsn, readonly=True) as db:
        assert '/2026-11-05/' in db.execute('SELECT local_path FROM t_pdd_product_main_images').fetchone()['local_path']
        assert db.execute('SELECT created_at FROM t_pdd_rankings').fetchone()['created_at'] == '2026-11-05T09:12:13+08:00'


def test_failed_move_restores_database_status_and_file_times(database, monkeypatch):
    dsn, old = database
    file = old / 'status.json'
    content = b'{"created_at":"2026-11-04T09:00:00+08:00"}'
    file.write_bytes(content)
    modified = file.stat().st_mtime_ns
    def fail(self, target):
        raise OSError('rename failed')
    monkeypatch.setattr(Path, 'rename', fail)
    with pytest.raises(OSError):
        switch_data(dsn, '2026-11-04', '2026-11-05')
    assert old.is_dir() and file.read_bytes() == content and file.stat().st_mtime_ns == modified
    with connect(dsn, readonly=True) as db:
        assert db.execute('SELECT capture_date FROM t_pdd_rankings').fetchone()['capture_date'] == '2026-11-04'
