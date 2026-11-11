import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from automation.pdd.spider.database import Store
from automation.pdd.spider.metrics import FIELDS, from_text


MIGRATION = Path(__file__).resolve().parents[3] / 'src/automation/pdd/spider/migrate/20261102115136_rankings_products.sql'


def test_migration_preserves_legacy_data_and_is_repeatable(tmp_path):
    sql = MIGRATION.read_text(encoding='utf-8')
    path = tmp_path / 'sqlite.db'
    db = sqlite3.connect(path)
    db.executescript(sql.split('CREATE TABLE IF NOT EXISTS rankings')[0] + '\nCOMMIT;')
    row = {'排行': 1, '产品ID': '123', '产品链接': 'https://example.com/123', '标题': '猫粮',
           '价格': '29.90', '主榜单名称': '全价猫粮', '热卖指数': '9856', '上架日期': '2026-10-01'}
    details = {'sku': [{'SPU1': '鸡肉', '卷后价': '29.90'}],
               'comments': [{'序号': 1, '评论内容': '好吃', 'SKU': '鸡肉'}],
               'negative_comments': [{'序号': 2, '评论内容': '好吃', 'SKU': '鸡肉', '原因': '标签'}],
               'status': {'listing_date': '2026-10-01'}, 'info': {'goods_id': '123'}}
    db.execute("INSERT INTO spider_runs(day,state) VALUES('2026-11-02','completed')")
    db.execute("INSERT INTO spider_boards(id,day,name,url,position,state) VALUES(1,'2026-11-02','来源猫粮','https://example.com/rank',0,'completed')")
    db.execute("INSERT INTO spider_lists(id,board_id,tab,tab_index,has_sub,directory,device,state,collect_state,export_state) VALUES(1,1,'畅销榜',0,1,'D:/test','ios','completed','completed','completed')")
    db.execute("INSERT INTO spider_items(id,list_id,rank,row_json,product_json,product_state,product_attempts) VALUES(1,1,1,?,?,'completed',2)",
               (json.dumps(row, ensure_ascii=False), json.dumps(details, ensure_ascii=False)))
    db.execute("INSERT INTO spider_events(day,board,tab,device,stage,error) VALUES('2026-11-02','来源猫粮','畅销榜','ios','collect','旧错误')")
    db.commit()
    db.close()
    with pytest.raises(RuntimeError, match='手动迁移'):
        Store(path)
    db = sqlite3.connect(path)
    db.executescript(sql)
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {'rankings', 'ranking_products', 'product_info', 'product_skus', 'product_comments', 'collection_tasks'}
    assert json.loads(db.execute('SELECT raw_json FROM ranking_products').fetchone()[0]) == row
    assert json.loads(db.execute('SELECT raw_json FROM product_info').fetchone()[0]) == details
    assert db.execute('SELECT main_name FROM rankings').fetchone()[0] == '全价猫粮'
    assert db.execute('SELECT count(*),max(is_negative) FROM product_comments').fetchone() == (1, 1)
    assert db.execute("SELECT last_error FROM collection_tasks WHERE kind='event'").fetchone()[0] == '旧错误'
    db.executescript(sql)
    assert db.execute('SELECT count(*) FROM ranking_products').fetchone()[0] == 1
    db.close()
    from automation.pdd.spider.migrate_owner_states import migrate
    migrate(path, '2099-01-01')
    store = Store(path)
    assert store.item(1)['product_attempts'] == 2
    assert len(store.product_tasks()) == 1
    store.close()


def test_native_storage_populates_details_skus_comments_and_unified_metrics(tmp_path):
    store = Store(tmp_path / 'sqlite.db')
    board = store.seed('2026-11-02', [{'name': '猫粮', 'url': ''}])[0]
    store.discover(board['id'], [SimpleNamespace(tab='畅销榜', index=0, has_sub=False)])
    metrics = from_text('¥39.90 券后 ¥29.90 热卖指数9856 蝉联榜首7天 疯抢2万件 24小时内1000+人拼单')
    store.save_rows(1, tmp_path, ['排行', '标题'], [{'排行': 1, '标题': '猫粮', '产品ID': '123', 'metrics': metrics}], 'ios')
    data = json.loads(store.db.execute('SELECT metrics_json FROM ranking_products').fetchone()[0])
    assert set(data) == set(FIELDS)
    assert data['hot_index'] == '热卖指数9856'
    assert data['coupon_price'] == '券后 ¥29.90'
    assert data['positive_rate'] is None
    store.product_state(1, 'running')
    store.product_state(1, 'completed', details={'info': {'goods_id': '123', 'goods_name': '完整猫粮', 'mall_id': 42},
        'files': ['主图\\01.jpg', '详情图片\\01.jpg'],
        'images': [{'image_type': 'main', 'position': 1, 'source_url': 'https://images.example/01.jpg',
                    'local_path': '主图/01.jpg', 'state': 'completed'}],
        'sku': [{'sku_id': 's1', 'SPU1': '鸡肉', '卷后价': '29.90'}],
        'comments': [{'review_id': 'r1', 'comment': '评价', 'specs': ['鸡肉']}],
        'negative_comments': [{'review_id': 'r1', 'comment': '评价', 'specs': ['鸡肉']}]})
    assert store.db.execute('SELECT shop_id FROM ranking_products WHERE id=1').fetchone()[0] == '42'
    assert tuple(store.db.execute('SELECT source_url,local_path,state FROM product_images').fetchone()) == (
        'https://images.example/01.jpg', '榜01/主图/01.jpg', 'completed')
    assert store.db.execute('SELECT price FROM product_skus').fetchone()[0] == '29.90'
    assert tuple(store.db.execute('SELECT count(*),max(is_negative) FROM product_comments').fetchone()) == (1, 1)
    assert store.item(1)['product_attempts'] == 1
    store.close()


def test_migration_error_can_be_rolled_back_without_removing_legacy_tables(tmp_path):
    sql = MIGRATION.read_text(encoding='utf-8')
    db = sqlite3.connect(tmp_path / 'broken.db')
    db.executescript(sql.split('CREATE TABLE IF NOT EXISTS rankings')[0] + '\nCOMMIT;')
    db.execute("INSERT INTO spider_runs(day,state) VALUES('2026-11-02','completed')")
    db.execute("INSERT INTO spider_boards(id,day,name,url,position) VALUES(1,'2026-11-02','猫粮','',0)")
    db.execute("INSERT INTO spider_lists(id,board_id,tab,tab_index,has_sub) VALUES(1,1,'畅销榜',0,0)")
    db.execute("INSERT INTO spider_items(list_id,rank,row_json) VALUES(1,1,'invalid json')")
    db.commit()
    with pytest.raises(sqlite3.OperationalError, match='malformed JSON'):
        db.executescript(sql)
    db.rollback()
    assert db.execute('SELECT count(*) FROM spider_items').fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM sqlite_master WHERE name='rankings'").fetchone()[0] == 0
    db.close()


def test_v2_state_migration_requires_manual_execution_and_keeps_rows(tmp_path):
    root = Path(__file__).resolve().parents[3]
    schema = (Path(__file__).with_name('fixtures') / 'schema_v3.sql').read_text(encoding='utf-8')
    schema = schema.replace("    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','running','completed','failed')),\n", '')
    path = tmp_path / 'v2.db'
    db = sqlite3.connect(path)
    db.executescript(schema)
    db.execute("INSERT INTO rankings(id,capture_date,source_name,tab_name,state) VALUES(1,'2026-11-02','猫粮','畅销榜','completed')")
    db.execute("INSERT INTO ranking_products(id,ranking_id,rank,title) VALUES(1,1,1,'保留的商品')")
    db.commit()
    db.close()
    with pytest.raises(RuntimeError, match='20261102142611_capture_state.sql'):
        Store(path)
    db = sqlite3.connect(path)
    db.executescript((root / 'src/automation/pdd/spider/migrate/20261102142611_capture_state.sql').read_text(encoding='utf-8'))
    assert db.execute('SELECT id,title,state FROM ranking_products').fetchone() == (1, '保留的商品', 'pending')
    assert db.execute('SELECT state FROM rankings').fetchone()[0] == 'pending'
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    db.close()
    from automation.pdd.spider.migrate_owner_states import migrate
    migrate(path, '2099-01-01')
    store = Store(path)
    store.close()
