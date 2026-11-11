import json
from pathlib import Path
import sqlite3

from automation.pdd.spider.database import Store
from automation.pdd.spider.migrate_compact import migrate


def test_compact_keeps_only_requested_day_and_relinks_children(tmp_path):
    path = tmp_path / 'legacy.db'
    db = sqlite3.connect(path)
    db.executescript((Path(__file__).with_name('fixtures') / 'schema_v3.sql').read_text(encoding='utf-8'))
    db.execute("INSERT INTO rankings(id,capture_date,source_name,tab_name) VALUES(1,'2026-11-04','猫粮','畅销榜')")
    db.execute("INSERT INTO rankings(id,capture_date,source_name,tab_name) VALUES(2,'2026-11-03','旧榜','畅销榜')")
    db.execute("INSERT INTO ranking_products(id,ranking_id,rank,title) VALUES(7,1,1,'榜单标题')")
    db.execute("INSERT INTO ranking_products(id,ranking_id,rank,title) VALUES(8,2,1,'昨天商品')")
    db.execute('INSERT INTO product_info(id,ranking_product_id,title,shop_id,info_json,raw_json) VALUES(101,7,?,?,?,?)',
               ('商品完整标题','42',json.dumps({'goods_name':'商品完整标题'}),json.dumps({'files':['产品信息.json']})))
    db.execute('INSERT INTO product_info(id,ranking_product_id) VALUES(102,8)')
    db.execute("INSERT INTO product_images(product_info_id,image_type,position,local_path,state) VALUES(101,'main',1,'主图/01.jpg','completed')")
    db.execute("INSERT INTO product_images(product_info_id,image_type,position,local_path) VALUES(102,'main',1,'旧主图.jpg')")
    db.execute("INSERT INTO product_skus(product_info_id,dedup_key,price,raw_json) VALUES(101,'sku','19.9','{\"price\":\"19.9\"}')")
    db.execute("INSERT INTO product_comments(product_info_id,dedup_key,content) VALUES(101,'review','评论')")
    db.execute('CREATE TABLE ranking_runs(capture_date TEXT)')
    db.commit()
    db.close()
    result = migrate(path, '2026-11-04')
    assert Path(result['backup']).is_file()
    store = Store(path)
    assert store.db.execute('SELECT DISTINCT capture_date FROM rankings').fetchall()[0][0] == '2026-11-04'
    assert store.db.execute('SELECT id,title,shop_id FROM ranking_products').fetchone()[:] == (7,'榜单标题','42')
    assert json.loads(store.item(7)['product_json'])['info']['goods_name'] == '商品完整标题'
    for table in ('product_images','product_skus','product_comments'):
        assert store.db.execute(f'SELECT ranking_product_id FROM {table}').fetchone()[0] == 7
        assert 'product_info_id' not in {row[1] for row in store.db.execute(f'PRAGMA table_info({table})')}
    assert not store.db.execute("SELECT 1 FROM sqlite_master WHERE name IN ('product_info','ranking_runs')").fetchall()
    assert not store.db.execute('PRAGMA foreign_key_check').fetchall()
    store.close()


def test_new_store_never_creates_duplicate_detail_or_run_table(tmp_path):
    store = Store(tmp_path / 'new.db')
    assert not store.db.execute("SELECT 1 FROM sqlite_master WHERE name IN ('product_info','ranking_runs')").fetchall()
    store.seed('2026-11-04', [{'name':'猫粮','url':''}])
    store.finish('2026-11-04')
    assert not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='ranking_runs'").fetchall()
    store.close()
