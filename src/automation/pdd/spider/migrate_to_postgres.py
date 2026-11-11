"""One-time import of backed-up SQLite and legacy PostgreSQL data."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

from psycopg import sql

from automation.pdd.spider.mobile_database import Store, TABLES, columns


def read_sqlite(path):
    path = Path(path).resolve(strict=True)
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('SQLite 完整性或外键检查失败，未迁移')
        return {table: [dict(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY id')]
                for table in (name.removeprefix('t_pdd_') for name in TABLES)}
    finally:
        db.close()


def merge(latest, legacy):
    result = deepcopy(latest)
    ranking_ids, product_ids, imported_products = {}, {}, set()
    for table, keys, owner in [('rankings', ('week_start', 'source_name', 'tab_name'), None),
                               ('ranking_products', ('ranking_id', 'rank'), 'ranking_id')]:
        index = {tuple(row[key] for key in keys): row['id'] for row in result[table]}
        next_id = max((row['id'] for row in result[table]), default=0)
        mapping = ranking_ids if table == 'rankings' else product_ids
        for source in legacy.get(table, []):
            row = dict(source)
            if owner:
                row[owner] = ranking_ids[row[owner]]
            identity = tuple(row[key] for key in keys)
            if identity not in index:
                next_id += 1
                row['id'] = index[identity] = next_id
                result[table].append(row)
                if owner:
                    imported_products.add(source['id'])
            mapping[source['id']] = index[identity]
    for table in (name.removeprefix('t_pdd_') for name in TABLES[2:]):
        next_id = max((row['id'] for row in result[table]), default=0)
        points = {tuple(row[key] for key in ('主卖点', '副卖点', '痛点')) for row in result[table]} if table == 'selling_point' else set()
        for source in legacy.get(table, []):
            row = dict(source)
            if table == 'selling_point':
                key = tuple(row[key] for key in ('主卖点', '副卖点', '痛点'))
                if key in points:
                    continue
                points.add(key)
            elif row['ranking_product_id'] not in imported_products:
                # A position may now belong to another product: keep its latest whole capture.
                continue
            else:
                row['ranking_product_id'] = product_ids[row['ranking_product_id']]
            next_id += 1
            row['id'] = next_id
            result[table].append(row)
    for row in result['ranking_products']:
        row.setdefault('mobile_state', 'completed' if row['rank_state'] == row['product_state'] == 'completed' else 'pending')
        row.setdefault('sogou_state', 'completed' if all(row.get(key) for key in
            ('platform_product_id', 'platform_product_link', 'listing_date')) else 'pending')
    for row in result['rankings']:
        row.setdefault('web_collected_at', None)
        products = [item for item in result['ranking_products'] if item['ranking_id'] == row['id']]
        row['collect_state'] = 'completed' if products and all(
            item['mobile_state'] == item['sogou_state'] == 'completed' for item in products) else 'pending'
        if row['collect_state'] != 'completed':
            row['completed_at'] = None
    return result


def migrate(sqlite_path, legacy_backup, dsn):
    rows = merge(read_sqlite(sqlite_path), json.loads(Path(legacy_backup).read_text(encoding='utf-8')))
    store = Store(dsn)
    try:
        with store.db.transaction():
            store.db.execute(sql.SQL('LOCK TABLE {} IN EXCLUSIVE MODE').format(
                sql.SQL(',').join(sql.Identifier('spider', table) for table in TABLES)))
            existing = {table: store.db.execute(sql.SQL('SELECT * FROM {} ORDER BY id').format(
                sql.Identifier('spider', table))).fetchall() for table in TABLES}
            if any(existing.values()):
                if all(existing[table] == sorted(rows[table.removeprefix('t_pdd_')], key=lambda row: row['id']) for table in TABLES):
                    return {table: len(existing[table]) for table in TABLES}
                raise ValueError('目标表已有不同数据，未覆盖；请先核对迁移冲突')
            for table in TABLES:
                allowed = columns(store.db, table)
                values = rows[table.removeprefix('t_pdd_')]
                for row in values:
                    if not set(row) <= allowed:
                        raise ValueError(f'{table} 存在未支持字段，未迁移')
                    fields = list(row)
                    store.db.execute(sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(
                        sql.Identifier('spider', table), sql.SQL(',').join(map(sql.Identifier, fields)),
                        sql.SQL(',').join(sql.Placeholder() for _ in fields)), list(row.values()))
                actual = store.db.execute(sql.SQL('SELECT * FROM {} ORDER BY id').format(sql.Identifier('spider', table))).fetchall()
                if actual != sorted(values, key=lambda row: row['id']):
                    raise ValueError(f'{table} 迁移后逐字段核对失败，已回滚')
            for table in TABLES:
                values = rows[table.removeprefix('t_pdd_')]
                store.db.execute('SELECT setval(pg_get_serial_sequence(%s,%s),%s,%s)',
                    ('spider.' + table, 'id', max((row['id'] for row in values), default=1), bool(values)))
        return {table: len(rows[table.removeprefix('t_pdd_')]) for table in TABLES}
    finally:
        store.close()
