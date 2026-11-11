"""Move legacy tasks to their owners, then reconcile today's saved artifacts."""
import argparse
from contextlib import ExitStack
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3

from automation.pdd.spider.owner_states import ITEM_STAGES, LIST_STAGES, canonical


def widen_detail_state(db):
    sql = db.execute("SELECT sql FROM sqlite_master WHERE name='ranking_products'").fetchone()[0]
    updated = re.sub(r"CHECK\s*\(\s*detail_state\s+IN\s*\([^)]*\)\s*\)", '', sql, flags=re.I)
    if updated == sql:
        return
    indexes = db.execute("SELECT sql FROM sqlite_master WHERE tbl_name='ranking_products' AND type='index' AND sql IS NOT NULL").fetchall()
    db.execute('PRAGMA foreign_keys=OFF')
    with db:
        create = re.sub(r'CREATE TABLE\s+[\w"`]+', 'CREATE TABLE __owner_products', updated, count=1, flags=re.I)
        db.execute(create)
        db.execute('INSERT INTO __owner_products SELECT * FROM ranking_products')
        db.execute('DROP TABLE ranking_products')
        db.execute('ALTER TABLE __owner_products RENAME TO ranking_products')
        for (index,) in indexes:
            db.execute(index)


def transfer(store):
    db = store.db
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='collection_tasks'").fetchone():
        return 0
    tasks = [dict(row) for row in db.execute('SELECT * FROM collection_tasks ORDER BY updated_at,id')]
    for task in tasks:
        if task['kind'] == 'step' and task['ranking_id']:
            state = 'pending' if task['state'] == 'running' else task['state']
            store._set_stage(task['ranking_id'], task['stage'], state, item_id=task['ranking_product_id'],
                             **{key: task[key] for key in ('attempts','device','last_error','artifacts_json')})
            # Preserve historical timestamps rather than making migration look like collection.
            table = 'ranking_products' if task['ranking_product_id'] else 'rankings'
            owner_id = task['ranking_product_id'] or task['ranking_id']
            row = db.execute(f'SELECT stages_json FROM {table} WHERE id=?', (owner_id,)).fetchone()
            stages = json.loads(row[0])
            stages[canonical(task['stage'])].update({key: task[key] for key in ('created_at','updated_at','started_at','finished_at')})
            db.execute(f'UPDATE {table} SET stages_json=?,updated_at=? WHERE id=?',
                       (json.dumps(stages, ensure_ascii=False), task['updated_at'], owner_id))
    db.execute('DROP TABLE collection_tasks')
    return len(tasks)


def reconcile_day(store, day):
    from automation.pdd.spider.native_product import NativeProduct, saved_image, product_info, UnreadableWebView
    import xml.etree.ElementTree as ET
    from automation.pdd.spider.product import artifacts
    from automation.pdd.spider.status import load_status
    from automation.pdd.spider.artifacts import read_share_link
    from automation.pdd.spider import longshot
    from urllib.parse import urlsplit

    report = []
    validator = NativeProduct(None)
    rows = store.db.execute('''SELECT p.* FROM ranking_products p JOIN rankings r ON r.id=p.ranking_id
                               WHERE r.capture_date=? ORDER BY p.ranking_id,p.rank''', (day,)).fetchall()
    for row in rows:
        folder = Path(row['directory'])
        try:
            status = load_status(folder)
            stages = status.get('stages', {})
            for stage in ('product_info','main_images','sku','negative_comments','detail_images'):
                entry = stages.get(stage, {})
                if stage == 'detail_images' and not entry:
                    entry = stages.get('details', {})
                if stage == 'product_info' and (folder / '产品信息.json').is_file():
                    entry = {**entry, 'state': 'completed'}
                if stage in {'main_images','detail_images'} and (folder / '图片清单.json').is_file():
                    records = json.loads((folder / '图片清单.json').read_text(encoding='utf-8'))
                    kind = 'main' if stage == 'main_images' else 'detail'
                    matching = [r for r in records if r.get('image_type') in {kind,kind+'_video'}]
                    totals = {r['page_counter'][1] for r in matching if r.get('page_counter')}
                    if len(totals) == 1:
                        entry = {**entry, 'state': 'completed', 'expected': next(iter(totals))}
                # Supplement old metadata from database before validating the actual files.
                valid = validator.stage_complete(folder, stage, entry=entry)
                state = 'completed' if valid else 'pending'
                if stage == 'negative_comments' and entry.get('state') == 'skipped':
                    state = 'skipped'
                reason = entry.get('reason', '') if state in {'completed','skipped'} else '核对产物：缺失或尚未确认完整'
                metadata = {key: value for key, value in entry.items() if key not in {'state','reason','updated_at','completed_at'}}
                store._set_stage(row['ranking_id'], stage, state, item_id=row['id'], last_error=reason, **metadata)
            for stage, filename in [('ranking_screenshot','排行截图.jpg'),('product_screenshot','产品页截图.jpg'),('ranking_crop','产品截图.jpg')]:
                store._set_stage(row['ranking_id'], stage, 'completed' if saved_image(folder / filename) else 'pending', item_id=row['id'])
            link = row['goods_url'] or ''
            try:
                share = read_share_link(folder, '')
                link = (share or {}).get('share_link') or link
            except (OSError, ValueError):
                pass
            parsed = urlsplit(link)
            linked = parsed.scheme in {'http','https'} and bool(parsed.hostname)
            store._set_stage(row['ranking_id'], 'share_link', 'completed' if linked else 'pending', item_id=row['id'])
            overall = 'completed' if all(store.stage(row['ranking_id'], stage, row['id'])['state'] in {'completed','skipped'}
                for stage in ('product_info','main_images','sku','negative_comments','detail_images')) else 'pending'
            jump_xml = folder / '网页跳转/App页面.xml'
            unreadable = False
            if jump_xml.is_file() and store.stage(row['ranking_id'], 'product_info', row['id'])['state'] != 'completed':
                try:
                    product_info(ET.fromstring(jump_xml.read_text(encoding='utf-8')))
                except UnreadableWebView:
                    unreadable = True
                except (RuntimeError, ET.ParseError):
                    pass
            if unreadable or stages.get('product', {}).get('state') == 'blocked_webview' or row['product_state'] == 'blocked_webview':
                overall = 'blocked_webview'
            details = artifacts(folder) if folder.is_dir() else {}
            # Persist data independently of legacy exported state, which may be stale.
            details.pop('status', None)
            if any(key in details for key in ('info','images','sku','negative_comments','comments')):
                existing = json.loads(store.item(row['id'])['product_json'])
                merged = {**existing, **details}
                merged.pop('status', None)
                store._save_details(row['id'], merged)
            store._set_stage(row['ranking_id'], 'product', overall, item_id=row['id'],
                             last_error='空 WebView，待统一处理' if overall == 'blocked_webview' else '')
            complete = all(store.stage(row['ranking_id'], stage, row['id'])['state'] == 'completed'
                           for stage in ('product_screenshot','ranking_crop','share_link'))
            store.db.execute('UPDATE ranking_products SET rank_state=? WHERE id=?', ('completed' if complete else 'pending', row['id']))
            report.append({'id': row['id'], 'directory': str(folder), 'product_state': overall,
                           **{column: store.stage(row['ranking_id'], stage, row['id'])['state'] for stage,column in ITEM_STAGES.items()}})
        except (OSError, ValueError, KeyError) as error:
            raise RuntimeError(f'核对商品 {row["id"]} 失败：{folder}: {error}') from error
    for entry in store.db.execute('SELECT id,directory FROM rankings WHERE capture_date=?', (day,)).fetchall():
        items = store.items(entry['id'])
        ranked = [json.loads(item['row_json']) for item in items]
        image_done = bool(ranked) and longshot.reusable(Path(entry['directory']), ranked)
        store._set_stage(entry['id'], 'ranking_long_screenshot', 'completed' if image_done else 'pending')
        done = bool(items) and all(item['state'] == 'completed' for item in items)
        store.db.execute('UPDATE rankings SET state=? WHERE id=?', ('completed' if done else 'pending', entry['id']))
    return report


def migrate(path, day):
    path = Path(path).resolve()
    backup = path.with_name(path.name + '.before-owner-states-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.bak')
    with sqlite3.connect(path) as db:
        with sqlite3.connect(backup) as destination:
            db.backup(destination)
        widen_detail_state(db)
    from automation.pdd.spider.database import Store
    store = Store(path, migrating=True)
    try:
        with store.db:
            store.db.execute('BEGIN IMMEDIATE')
            count = transfer(store)
        from automation.pdd.spider.migrate_compact import compact_connection
        compact_connection(store.db)
        with store.db:
            report = reconcile_day(store, day)
            if store.db.execute('PRAGMA foreign_key_check').fetchall():
                raise RuntimeError('Foreign key check failed')
            if store.db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Integrity check failed')
        report_path = backup.with_suffix('.report.json')
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        return {'backup': str(backup), 'legacy_tasks': count, 'today_products': len(report), 'report': str(report_path)}
    finally:
        store.close()


def main():
    from automation.pdd.spider.concurrency import workflow_lock
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('--day', required=True)
    args = parser.parse_args()
    with ExitStack() as locks:
        for role in ('rank','product','mobile','device-ios','device-android','device-browser'):
            locks.enter_context(workflow_lock(role, timeout=0))
        print(json.dumps(migrate(args.database, args.day), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
