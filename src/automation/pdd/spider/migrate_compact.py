"""Keep one capture date and fold product_info into ranking_products."""
import argparse
from contextlib import ExitStack
from datetime import date, datetime
import json
from pathlib import Path
import sqlite3


def compact_connection(source, day=None):
    from automation.pdd.spider.owner_states import ensure_schema, ITEM_STAGES, LIST_STAGES, canonical
    target = sqlite3.connect(':memory:')
    target.row_factory = sqlite3.Row
    target.executescript(Path(__file__).with_name('schema.sql').read_text(encoding='utf-8'))
    ensure_schema(target)
    source.row_factory = sqlite3.Row
    tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def insert(table, row):
        allowed = {r[1] for r in target.execute(f'PRAGMA table_info({table})')}
        values = {key: value for key, value in row.items() if key in allowed}
        target.execute(f'INSERT INTO {table}({",".join(values)}) VALUES({",".join("?" for _ in values)})', list(values.values()))

    try:
        rankings = source.execute('SELECT * FROM rankings' + (' WHERE capture_date=?' if day else ''), (day,) if day else ()).fetchall()
        ranking_ids = {row['id'] for row in rankings}
        for row in rankings:
            insert('rankings', dict(row))
        products = [dict(row) for row in source.execute('SELECT * FROM ranking_products') if row['ranking_id'] in ranking_ids]
        product_ids = {row['id'] for row in products}
        infos = {row['ranking_product_id']: dict(row) for row in source.execute('SELECT * FROM product_info')} if 'product_info' in tables else {}
        info_ids = {row['id']: row['ranking_product_id'] for row in infos.values()}
        for product in products:
            info = infos.get(product['id'])
            if info:
                for key in ('shop_id','shop_name','listing_date','sale_state','info_json','collected_at'):
                    product[key] = info[key]
                details = json.loads(info['raw_json'])
                product['files_json'] = json.dumps(details.get('files', []), ensure_ascii=False)
                raw_info = json.loads(product['info_json'])
                if info['title'] and info['title'] != product['title'] and not raw_info.get('goods_name'):
                    raw_info['goods_name'] = info['title']
                product['info_json'] = json.dumps(raw_info, ensure_ascii=False)
                stages = details.get('status', {}).get('stages', {})
                stored = json.loads(product.get('stages_json', '{}'))
                for stage, metadata in stages.items():
                    stored[stage] = {**metadata, **stored.get(stage, {})}
                product['stages_json'] = json.dumps(stored, ensure_ascii=False)
            from automation.pdd.spider.migrate_product_fields import RENAMES
            for old,new in RENAMES.items():
                if old in product and new not in product:
                    product[new] = product[old]
            insert('ranking_products', product)
        counts = {'rankings': len(rankings), 'ranking_products': len(products)}
        for table in ('product_images','product_skus','product_comments'):
            count = 0
            for row in source.execute(f'SELECT * FROM {table}'):
                values = dict(row)
                owner = values.get('ranking_product_id')
                if owner is None:
                    owner = info_ids.get(values.get('product_info_id'))
                if owner in product_ids:
                    values['ranking_product_id'] = owner
                    insert(table, values)
                    count += 1
            counts[table] = count
        target.commit()
        if target.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('Foreign key check failed')
        if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('Integrity check failed')
        target.backup(source)
        return counts
    finally:
        target.close()


def migrate(path, day):
    date.fromisoformat(day)
    path = Path(path).resolve()
    backup = path.with_name(path.name + '.before-compact-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.bak')
    with sqlite3.connect(path, timeout=30) as source:
        with sqlite3.connect(backup) as destination:
            source.backup(destination)
        counts = compact_connection(source, day)
    return {'backup': str(backup), 'retained_date': day, 'counts': counts}


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
