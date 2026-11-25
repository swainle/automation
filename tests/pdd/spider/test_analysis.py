from pathlib import Path
import random
from unittest.mock import Mock

from PIL import Image
import pytest

from automation.pdd.spider import analysis, point, unified
from automation.pdd.spider.mobile_database import Store, PRODUCT_COLUMNS


SOURCE = dict(name='猫粮', main_name='猫粮', tab='畅销榜', url='https://example.com/rank', tab_index=0)


def write_images(directory, seed=1):
    for position, kind in enumerate(('主图', '主图', 'SKU', '详情')):
        folder = Path(directory) / kind
        folder.mkdir(parents=True, exist_ok=True)
        rng = random.Random(seed * 10 + position)
        image = Image.new('RGB', (32, 32))
        image.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(1024)])
        image.save(folder / f'{position}.png')


@pytest.fixture
def products(pg_database, tmp_path, monkeypatch):
    store = Store()
    old = store.seed_rankings('2026-11-09', [SOURCE], tmp_path / 'data')[0]
    new = store.seed_rankings('2026-11-16', [SOURCE], tmp_path / 'data')[0]
    for entry in (old, new):
        store.save_rows(entry['id'], entry['directory'], [], [{'排行': 1, '标题': '商品'}])
    old_item, item = store.items(old['id'])[0], store.items(new['id'])[0]
    for product in (old_item, item):
        store.db.execute('UPDATE t_pdd_ranking_products SET ' + ','.join(f"{c}='completed'" for c in PRODUCT_COLUMNS)
                         + ' WHERE id=%s', (product['id'],))
        write_images(product['directory'])
    store.db.execute("""UPDATE t_pdd_ranking_products SET platform_product_id='123',
        platform_product_link='https://example.com/goods/123',listing_date='2026-01-01' WHERE id=%s""", (old_item['id'],))
    extract = Mock(return_value=('ok', {'main_selling_points': [{'point': '柔软'}], 'secondary_selling_points': []}, 'result.json'))
    monkeypatch.setattr(point.Config, 'check', lambda self: None)
    monkeypatch.setattr(point, 'extract_one', extract)
    yield store, old_item, store.item(item['id']), extract
    store.close()


def test_matching_fills_identity_and_completed_steps_survive_restart(products):
    store, old_item, item, extract = products
    runner = analysis.AnalysisRunner(store)
    assert runner.process(item)
    saved = store.item(item['id'])
    assert (saved['platform_product_id'], saved['listing_date']) == ('123', '2026-01-01')
    status = runner.state(item['id'])
    assert status['matched_product_id'] == old_item['id']
    assert status['match_state'] == status['point_state'] == 'completed'
    assert analysis.AnalysisRunner(store).process(saved)
    extract.assert_called_once()


def test_title_matching_does_not_open_images(products, monkeypatch):
    store, old_item, item, extract = products
    store.db.execute("""INSERT INTO t_pdd_selling_point
        (ranking_product_id,platform_product_id,main_point,secondary_point,raw_json)
        VALUES(%s,'','已有卖点','','{}')""", (item['id'],))
    monkeypatch.setattr(Image, 'open', Mock(side_effect=AssertionError('title matching must not read pictures')))
    assert analysis.AnalysisRunner(store).process(item)
    assert store.item(item['id'])['platform_product_id'] == '123'
    extract.assert_not_called()


def test_unmatched_product_still_gets_selling_points_without_platform_id(products):
    store, old_item, item, extract = products
    store.db.execute("UPDATE t_pdd_ranking_products SET title='不同商品' WHERE id=%s", (item['id'],))
    item = store.item(item['id'])
    runner = analysis.AnalysisRunner(store)
    assert not runner.process(item)
    assert runner.state(item['id'])['match_result'] == 'unmatched'
    assert runner.state(item['id'])['match_state'] == 'pending'
    assert not store.item(item['id'])['platform_product_id']
    saved = store.db.execute('SELECT * FROM t_pdd_selling_point WHERE ranking_product_id=%s', (item['id'],)).fetchone()
    assert saved['platform_product_id'] == '' and saved['main_point'] == '柔软'


def test_failed_points_retry_without_repeating_successful_matching(products, monkeypatch):
    store, old_item, item, extract = products
    extract.side_effect = ValueError('timeout')
    runner = analysis.AnalysisRunner(store)
    assert not runner.process(item)
    assert runner.state(item['id'])['match_state'] == 'completed'
    assert runner.state(item['id'])['point_state'] == 'failed'
    extract.side_effect = None
    second = analysis.AnalysisRunner(store)
    second.match = Mock(side_effect=AssertionError('completed match repeated'))
    assert second.process(store.item(item['id']))
    assert extract.call_count == 2


def test_existing_identity_and_points_skip_analysis_even_if_images_change(products):
    store, old_item, item, extract = products
    assert analysis.AnalysisRunner(store).process(item)
    write_images(item['directory'], seed=88)
    runner = analysis.AnalysisRunner(store)
    runner.match = Mock(side_effect=AssertionError('complete data must skip matching'))
    assert runner.process(store.item(item['id']))
    assert store.item(item['id'])['platform_product_id'] == '123'
    extract.assert_called_once()


@pytest.mark.parametrize('missing', [None, 'platform_product_id', 'platform_product_link', 'listing_date', 'has_points'])
def test_completion_uses_actual_fields_and_point_record_only(missing):
    row = dict(id=1, platform_product_id='123', platform_product_link='link', listing_date='2026-01-01', has_points=True)
    if missing:
        row[missing] = False if missing == 'has_points' else '  '
    runner = analysis.AnalysisRunner.__new__(analysis.AnalysisRunner)
    runner.store = Mock()
    runner.store.db.execute.return_value.fetchone.return_value = row
    runner.state = Mock(return_value=dict(match_state='failed', point_state='failed'))
    assert runner.is_complete({'id': 1}) == (missing is None)
    assert runner.has_failed([{'id': 1}]) == (missing is not None)


def test_main_resumes_analysis_without_opening_phone(products, monkeypatch):
    store, old_item, item, extract = products
    entry = store.list(item['ranking_id'])
    Image.new('RGB', (20, 20)).save(Path(entry['directory']) / '榜单长图.jpg')
    mobile = Mock(side_effect=AssertionError('completed phone collection repeated'))
    monkeypatch.setattr(unified, 'mobile_round', mobile)
    assert unified.collect(store, Mock(), ranking_id=entry['id']) == 0
    assert store.list(entry['id'])['state'] == 'completed'
    assert unified.collect(store, Mock(), ranking_id=entry['id']) == 0
    mobile.assert_not_called()
    extract.assert_called_once()


def test_analysis_failure_does_not_reset_phone_checkpoints(products, monkeypatch):
    store, old_item, item, extract = products
    entry = store.list(item['ranking_id'])
    Image.new('RGB', (20, 20)).save(Path(entry['directory']) / '榜单长图.jpg')
    extract.side_effect = ValueError('API key missing')
    assert unified.collect(store, Mock(), ranking_id=entry['id']) == 1
    assert store.list(entry['id'])['state'] == 'retry_pending'
    assert all(store.item(item['id'])[c] == 'completed' for c in PRODUCT_COLUMNS)


def test_per_product_analysis_runs_before_next_product(tmp_path, monkeypatch):
    from automation.pdd.spider import weekly_mobile as flow
    store = Mock()
    items = [dict(id=n, list_id=1, ranking_id=1, rank=n, title=str(n), directory=str(tmp_path/f'榜{n:02}'),
                  **dict.fromkeys(PRODUCT_COLUMNS, 'completed')) for n in (1, 2)]
    for item in items:
        item['comments_state'] = 'pending'
    store.items.return_value = items
    store.item.side_effect = lambda pid: items[pid-1]
    events = []
    monkeypatch.setattr(flow, 'product_element', lambda *a, **k: Mock(displayed=True))
    monkeypatch.setattr('automation.pdd.spider.full_flow.prepare_top', Mock())
    monkeypatch.setattr(flow, 'wait_for_elements', lambda *a, **k: 'xml')
    monkeypatch.setattr(flow, 'product_page_checks', lambda xml, title: ([], {'title_candidates': [title], 'observed_title': title}))
    monkeypatch.setattr(flow, 'return_to_board', Mock())
    collector = Mock()
    collector.collect.side_effect = lambda item, *a, **k: events.append(('capture', item['id']))
    monkeypatch.setattr(flow, 'WeeklyProduct', Mock(return_value=collector))
    runner = Mock()
    runner.process.side_effect = lambda item: events.append(('analysis', item['id']))
    flow.run_products(store, Mock(), dict(id=1,main_name='猫粮',tab='畅销榜'),
                      [{'排行': n, '标题': str(n)} for n in (1, 2)], analysis=runner)
    assert events == [('capture', 1), ('analysis', 1), ('capture', 2), ('analysis', 2)]
