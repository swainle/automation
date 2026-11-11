"""Back up image paths, make them relative to data, and repair evidenced states."""
import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from automation.pdd.spider.database import Store, encode


def migrate(path):
    from automation.pdd.spider import longshot
    store = Store(path)
    backup = Path(str(path) + '.before-enlarged-paths-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.bak')
    try:
        with sqlite3.connect(backup) as target:
            store.db.backup(target)
        counts = {'images': 0, 'sku': 0, 'ranking_screenshots': 0, 'failed_ranking_screenshots': 0, 'recapture_products': 0}

        def convert(directory, value):
            if not value or str(value).startswith(('http://', 'https://')):
                return value
            image = Path(value)
            if image.is_absolute():
                return image.resolve().relative_to(store.data_root).as_posix()
            return store.data_image_path(directory, value)

        with store.db:
            for row in store.db.execute('SELECT i.id,i.local_path,p.directory FROM product_images i JOIN ranking_products p ON p.id=i.ranking_product_id').fetchall():
                relative = convert(row['directory'], row['local_path'])
                if relative != row['local_path']:
                    store.db.execute('UPDATE product_images SET local_path=? WHERE id=?', (relative, row['id']))
                    counts['images'] += 1
            for row in store.db.execute('SELECT s.id,s.image,s.raw_json,p.directory FROM product_skus s JOIN ranking_products p ON p.id=s.ranking_product_id').fetchall():
                data = json.loads(row['raw_json'])
                for key in ('图片路径', 'image'):
                    if data.get(key):
                        data[key] = convert(row['directory'], data[key])
                relative = convert(row['directory'], row['image'])
                if relative != row['image'] or data != json.loads(row['raw_json']):
                    store.db.execute('UPDATE product_skus SET image=?,raw_json=? WHERE id=?', (relative, encode(data), row['id']))
                    counts['sku'] += 1
            for entry in store.db.execute('SELECT id,directory,screenshot_state,last_error FROM rankings').fetchall():
                items = store.db.execute('SELECT * FROM ranking_products WHERE ranking_id=? ORDER BY rank', (entry['id'],)).fetchall()
                rows = [json.loads(item['raw_json']) for item in items]
                directory = Path(entry['directory'])
                if rows and directory.is_dir() and longshot.reusable(directory, rows):
                    for item in items:
                        if item['ranking_screenshot_state'] != 'completed':
                            store._set_stage(entry['id'], 'ranking_screenshot', 'completed', item_id=item['id'],
                                             artifacts_json=encode([str(directory / '榜单长图.jpg'), str(directory / '榜单长图.meta.json')]))
                            counts['ranking_screenshots'] += 1
                elif entry['screenshot_state'] == 'failed':
                    reason = store._stage(entry['id'], 'ranking_long_screenshot')['last_error'] or entry['last_error'] or '榜单长图未成功生成'
                    for item in items:
                        if item['ranking_screenshot_state'] not in {'completed', 'failed'}:
                            store._set_stage(entry['id'], 'ranking_screenshot', 'failed', item_id=item['id'], last_error=reason)
                            counts['failed_ranking_screenshots'] += 1
                for item in items:
                    info = json.loads(item['info_json'])
                    if info.get('source') == 'ios_native_ui' and info.get('image_method') != 'ios_enlarged_screenshot_crop':
                        for stage in ('sku', 'detail_images', 'product'):
                            store._set_stage(entry['id'], stage, 'pending', item_id=item['id'], last_error='升级为放大图片采集，等待重新采集')
                        store.db.execute("UPDATE ranking_products SET rank_state='pending' WHERE id=?", (item['id'],))
                        store.db.execute("UPDATE rankings SET state='pending',completed_at=NULL WHERE id=?", (entry['id'],))
                        counts['recapture_products'] += 1
            if store.db.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('迁移外键校验失败，已回滚')
        return {'backup': str(backup), **counts}
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    args = parser.parse_args()
    from automation.pdd.spider.concurrency import workflow_lock
    with workflow_lock('device-ios'), workflow_lock('device-android'), workflow_lock('rank'), workflow_lock('product'):
        print(encode(migrate(args.database)))


if __name__ == '__main__':
    main()
