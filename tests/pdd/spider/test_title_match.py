from unittest.mock import Mock

import pytest

from automation.pdd.spider.analysis import AnalysisRunner
from automation.pdd.spider.title_match import title_key, choose_title_match


def product(pid='123', **fields):
    return dict(id=1, title='猫咪冻干 Ａ１２', platform_product_id=pid,
                platform_product_link=f'https://example.com/{pid}', listing_date='2026-01-01',
                capture_date='2026-11-09', **fields)


def test_title_normalizes_spacing_zero_width_and_full_width():
    result, source, evidence = choose_title_match('猫咪\u200b冻干A12\n', [product()])
    assert result == 'matched' and source['platform_product_id'] == '123'
    assert evidence['method'] == 'title_exact'


@pytest.mark.parametrize('title', ['', '   ', '猫咪冻干A13', '猫咪冻干A12 新品'])
def test_empty_different_or_partial_title_does_not_match(title):
    assert choose_title_match(title, [product()])[0] == 'unmatched'


def test_same_title_with_different_platform_ids_is_conflict():
    second = dict(product('456'), id=2)
    outcome, source, evidence = choose_title_match('猫咪冻干A12', [product(), second])
    assert outcome == 'conflict' and source is None
    assert evidence['candidate_ids'] == [1, 2]


def test_same_platform_id_uses_latest_complete_record():
    older = product()
    latest = dict(older, id=2, capture_date='2026-11-10')
    incomplete = dict(older, id=3, capture_date='2026-11-15', listing_date=None)
    assert choose_title_match('猫咪冻干A12', [older, latest, incomplete])[1]['id'] == 2


def test_history_is_title_indexed_and_loaded_once_per_week():
    store = Mock()
    store.db.execute.return_value = [product()]
    runner = AnalysisRunner(store)
    first = runner.history('2026-11-16')
    assert first[title_key('猫咪冻干A12')][0]['id'] == 1
    assert runner.history('2026-11-16') is first
    store.db.execute.assert_called_once()
    query, params = store.db.execute.call_args.args
    assert 'r.week_start<%s' in query and params == ('2026-11-16',)
