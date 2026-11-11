import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from PIL import Image
import pytest

from automation.pdd.spider.database import Store
from automation.pdd.spider.migrate_owner_states import migrate
from automation.pdd.spider.native_product import NativeProduct


def test_stage_columns_drive_runtime_and_checkpoint_metadata(tmp_path):
    store = Store(tmp_path / 'sqlite.db')
    board = store.seed('2026-11-04', [{'name': '猫粮', 'url': ''}])[0]
    store.discover(board['id'], [SimpleNamespace(tab='畅销榜', index=0, has_sub=False)])
    store.save_rows(1, tmp_path, [], [{'排行': 1, '标题': '完整商品标题'}], 'ios')
    collector = NativeProduct(None).bind_store(store, {'id': 1, 'ranking_list_id': 1})
    folder = tmp_path / '榜01'
    folder.mkdir()
    Image.new('RGB', (20, 20)).save(folder / 'main.jpg')
    (folder / '图片清单.json').write_text(json.dumps([{'image_type': 'main', 'position': 1, 'local_path': 'main.jpg'}]), encoding='utf-8')
    collector.update_stage(folder, 'main_images', 'completed', count=1, expected=1)
    assert store.db.execute('SELECT main_images_state FROM ranking_products').fetchone()[0] == 'completed'
    assert collector.stage_complete(folder, 'main_images')
    store.db.execute("UPDATE ranking_products SET main_images_state='pending'")
    assert not collector.stage_complete(folder, 'main_images')  # Completed status.json cannot override DB.
    assert not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='collection_tasks'").fetchone()
    store.product_state(1, 'blocked_webview')
    assert store.item(1)['product_state'] == 'blocked_webview'
    store.close()


def test_legacy_tasks_migrate_to_owners_without_losing_attempts(tmp_path):
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as db:
        db.executescript((Path(__file__).with_name('fixtures') / 'schema_v3.sql').read_text(encoding='utf-8'))
        db.execute("INSERT INTO rankings(id,capture_date,source_name,tab_name) VALUES(1,'2026-11-03','猫粮','畅销榜')")
        db.execute('INSERT INTO ranking_products(id,ranking_id,rank) VALUES(1,1,1)')
        db.execute("INSERT INTO collection_tasks(task_key,kind,capture_date,source_name,stage,state) VALUES('source','source','2026-11-03','猫粮','discover','completed')")
        db.execute("INSERT INTO collection_tasks(task_key,kind,capture_date,ranking_id,ranking_product_id,stage,state,attempts) VALUES('product','step','2026-11-03',1,1,'product','blocked_webview',3)")
        db.execute("INSERT INTO collection_tasks(task_key,kind,capture_date,ranking_id,ranking_product_id,stage,state) VALUES('main','step','2026-11-03',1,1,'main_images','completed')")
    with pytest.raises(RuntimeError, match='migrate_owner_states'):
        Store(path)
    result = migrate(path, '2099-01-01')
    assert Path(result['backup']).is_file()
    store = Store(path)
    assert store.item(1)['product_state'] == 'blocked_webview'
    assert store.item(1)['product_attempts'] == 3
    assert store.item(1)['main_images_state'] == 'completed'
    assert len(store.lists(store.boards('2026-11-03')[0]['id'])) == 1
    assert not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='collection_tasks'").fetchone()
    assert not store.db.execute('PRAGMA foreign_key_check').fetchall()
    store.close()


def test_refresh_product_info_updates_same_record_and_keeps_other_stages(tmp_path):
    store = Store(tmp_path / 'sqlite.db')
    source = store.seed('2026-11-04', [{'name': '猫粮', 'url': ''}])[0]
    store.discover(source['id'], [SimpleNamespace(tab='畅销榜', index=0, has_sub=False)])
    store.save_rows(1, tmp_path, [], [{'排行': 1, '标题': '榜单标题'}], 'ios')
    store.stage_state(1,'main_images','completed',item_id=1)
    store.refresh_product_info(1, {'goods_name': '旧标题', 'display_price': '¥10'})
    store.refresh_product_info(1, {'goods_name': '新标题', 'display_price': '¥12'})
    assert store.db.execute('SELECT count(*) FROM ranking_products').fetchone()[0] == 1
    item = store.item(1)
    assert json.loads(item['product_json'])['info']['goods_name'] == '新标题'
    assert item['main_images_state'] == 'completed'
    assert item['title'] == '新标题'
    store.close()
