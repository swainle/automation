from unittest.mock import Mock

import pytest

from automation.pdd.spider import __main__, health, unified


def test_cli_passes_exact_date_and_combined_ids(monkeypatch):
    run = Mock(return_value=0)
    monkeypatch.setattr(health, 'run', run)
    assert __main__.main(['--check', '--capture-date', '2026-11-16', '--ranking-id', '320']) == 0
    run.assert_called_once_with(320, None, '2026-11-16')


@pytest.mark.parametrize('args', [
    ['--check', '--capture-date', '2026-02-30'],
    ['--check', '--capture-date', '20261116'],
    ['--capture-date', '2026-11-16'],
])
def test_invalid_date_or_collection_mode_is_rejected_before_work(args, monkeypatch):
    run = Mock()
    monkeypatch.setattr(health, 'run', run)
    with pytest.raises(SystemExit) as error:
        __main__.main(args)
    assert error.value.code == 2
    run.assert_not_called()


@pytest.mark.parametrize('capture_date', [None, '2026-11-16'])
def test_summary_uses_optional_date_filter_without_product_panel(capture_date, monkeypatch):
    db = Mock()
    db.execute.return_value.fetchall.return_value = []
    monkeypatch.setattr(health, 'columns', lambda *args: {'product_count'})
    monkeypatch.setattr(health, 'table', Mock())
    health.collection_status(db, capture_date=capture_date)
    assert db.execute.call_count == 1
    for call in db.execute.call_args_list:
        sql, params = call.args
        assert 'r.capture_date=%s' in sql
        assert params == (None, None, None, None, capture_date, capture_date)


def test_date_filter_includes_only_products_owned_by_selected_rankings(pg_database, tmp_path, monkeypatch):
    from automation.pdd.spider.mobile_database import Store
    store = Store()
    try:
        source = dict(name='猫粮', main_name='猫粮', tab='畅销榜', url='https://example.com/rank', tab_index=0)
        entries = []
        for date in ('2026-11-09', '2026-11-16'):
            entry = store.seed_rankings(date, [source], tmp_path / 'data')[0]
            store.save_rows(entry['id'], entry['directory'], [], [{'排行': 1, '标题': date}])
            entries.append(entry)
        panels = []
        monkeypatch.setattr(health, 'table', lambda title, headers, rows, widths: panels.append((title, rows)))
        for capture_date, ranking_id, titles in [
            (None, None, ['2026-11-09', '2026-11-16']),
            ('2026-11-16', None, ['2026-11-16']),
            ('2026-11-16', entries[0]['id'], []),
            ('2026-11-23', None, []),
        ]:
            panels.clear()
            health.collection_status(store.db, ranking_id=ranking_id, capture_date=capture_date)
            assert len(panels[0][1]) == len(titles)
            assert len(panels) == 1
            expected_ids = [entry['id'] for entry in entries if entry['capture_date'] in titles]
            assert [row[0] for row in panels[0][1]] == expected_ids
    finally:
        store.close()
