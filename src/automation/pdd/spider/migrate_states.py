"""Preview or explicitly apply the ranking/product checkpoint simplification."""
import argparse
from contextlib import ExitStack
from datetime import datetime
import json
from pathlib import Path

from PIL import Image
from psycopg import sql

from automation.pdd.spider import config
from automation.pdd.spider.mobile_database import PRODUCT_COLUMNS, columns, connect, product_complete, product_skipped


TABLES = ('t_pdd_rankings', 't_pdd_ranking_products')
OLD_TIMES = {'completed_at', 'collected_at', 'web_collected_at', 'captured_at',
             'started_at', 'finished_at', 'retry_at'}
OLD_PROGRESS = {'last', 'last_stage', 'last_error'}


def removed_columns(table, names):
    keep = {'state'} if table == TABLES[0] else set(PRODUCT_COLUMNS)
    return sorted(name for name in names if (
        (name.endswith('_state') or name == 'state') and name not in keep
        or name in OLD_TIMES or name in OLD_PROGRESS))


def valid_image(path):
    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except (OSError, ValueError):
        return False


def migrate(db, backup_dir):
    """Back up both tables before any DDL; validate row identities before committing."""
    with db.transaction():
        db.execute("SET LOCAL lock_timeout='5s'")
        db.execute('LOCK TABLE ods.t_pdd_rankings,ods.t_pdd_ranking_products IN ACCESS EXCLUSIVE MODE')
        snapshot = {table: [dict(row) for row in db.execute(sql.SQL('SELECT * FROM ods.{} ORDER BY id').format(
            sql.Identifier(table)))] for table in TABLES}
        names = {table: columns(db, table) for table in TABLES}
        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f'states-before-{datetime.now():%Y%m%d-%H%M%S-%f}.json'
        with backup.open('x', encoding='utf-8') as stream:
            json.dump({'columns': {t: sorted(c) for t, c in names.items()}, 'tables': snapshot},
                      stream, ensure_ascii=False, indent=2, default=str)
        db.execute("ALTER TABLE ods.t_pdd_rankings ADD COLUMN IF NOT EXISTS state TEXT NOT NULL DEFAULT 'pending'")
        db.execute("""ALTER TABLE ods.t_pdd_ranking_products ADD COLUMN IF NOT EXISTS
            ranking_product_screenshot_state TEXT NOT NULL DEFAULT 'pending'""")
        for row in snapshot[TABLES[1]]:
            states = {c: row.get(c, 'pending') for c in PRODUCT_COLUMNS}
            states['ranking_product_screenshot_state'] = (
                'completed' if valid_image(Path(row['directory']) / '产品榜单截图.jpg') else 'skipped' if product_skipped(row) else 'pending')
            states = {c: value if value in {'pending', 'completed', 'failed', 'skipped'} else 'pending' for c, value in states.items()}
            db.execute(sql.SQL('UPDATE ods.t_pdd_ranking_products SET {} WHERE id=%s').format(
                sql.SQL(',').join(sql.SQL('{}=%s').format(sql.Identifier(c)) for c in states)),
                [*states.values(), row['id']])
        for row in snapshot[TABLES[0]]:
            old = row.get('state', row.get('collect_state', 'pending'))
            state = {'failed': 'retry_pending', 'running': 'retry_pending',
                     'webview_element_skip': 'unavailable'}.get(old, old)
            if state not in {'unavailable', 'pending', 'completed', 'retry_pending'}:
                state = 'pending'
            items = db.execute('SELECT * FROM ods.t_pdd_ranking_products WHERE ranking_id=%s', (row['id'],)).fetchall()
            complete = (items and len(items) >= max(1, row.get('product_count', len(items)))
                        and all(product_complete(p) for p in items)
                        and valid_image(Path(row['directory']) / '榜单长图.jpg')
                        and (Path(row['directory']) / '榜单数据.xlsx').is_file())
            if state == 'completed' and not complete:
                state = 'retry_pending'
            db.execute('UPDATE ods.t_pdd_rankings SET state=%s WHERE id=%s', (state, row['id']))
        for table in TABLES:
            for name in removed_columns(table, names[table]):
                # No CASCADE: unexpected dependencies abort the entire transaction.
                db.execute(sql.SQL('ALTER TABLE ods.{} DROP COLUMN {}').format(sql.Identifier(table), sql.Identifier(name)))
            actual = db.execute(sql.SQL('SELECT id FROM ods.{} ORDER BY id').format(sql.Identifier(table))).fetchall()
            if [r['id'] for r in actual] != [r['id'] for r in snapshot[table]]:
                raise ValueError('迁移后记录 ID 校验失败，已回滚')
            after = columns(db, table)
            for original in snapshot[table]:
                row = db.execute(sql.SQL('SELECT * FROM ods.{} WHERE id=%s').format(sql.Identifier(table)), (original['id'],)).fetchone()
                changed = {'state'} if table == TABLES[0] else set(PRODUCT_COLUMNS)
                if any(row[c] != original[c] for c in after & names[table] - changed):
                    raise ValueError(f'{table} 业务数据校验失败，已回滚')
        return backup, {t: len(rows) for t, rows in snapshot.items()}


def main(argv=None):
    parser = argparse.ArgumentParser(description='精简榜单和商品状态，默认只读预览；执行前停止采集任务')
    parser.add_argument('--apply', action='store_true', help='备份两张表后执行事务迁移')
    args = parser.parse_args(argv)
    with connect(readonly=not args.apply) as db:
        if not args.apply:
            for table in TABLES:
                print(f'{table}: 删除列 {", ".join(removed_columns(table, columns(db, table))) or "无"}')
            print('预览完成，未修改数据库。执行前停止采集任务，再加 --apply。')
            return 0
        from automation.pdd.spider.concurrency import workflow_lock
        with ExitStack() as locks:
            for role in ('rank', 'mobile', 'product', 'web'):
                locks.enter_context(workflow_lock(role, timeout=0))
            backup, counts = migrate(db, config.BUSINESS_ROOT / 'data/backups')
        print(f'迁移完成：{counts}；迁移前备份：{backup}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
