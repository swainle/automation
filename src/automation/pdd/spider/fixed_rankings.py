"""Back up and compact dated rankings into stable source/tab identities."""
import sqlite3
from datetime import datetime
from pathlib import Path


def migrate(path, sources):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='idx_rankings_fixed'").fetchone():
            return None
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='rankings'").fetchone():
            return None
        rows = list(db.execute('SELECT * FROM rankings ORDER BY capture_date DESC,updated_at DESC,id DESC'))
        order = {(s['name'], s['tab']): i for i, s in enumerate(sources)}
        latest = {}
        for row in rows:
            latest.setdefault((row['source_name'], row['tab_name']), row)
        kept = sorted(latest.values(), key=lambda r: (order.get((r['source_name'], r['tab_name']), len(order)), r['source_order'], r['tab_index'], r['id']))
        backup = Path(str(path) + '.before-fixed-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.bak')
        with sqlite3.connect(backup) as destination:
            db.backup(destination)
        # Negative temporary IDs avoid collisions; cascade updates are explicit.
        db.execute('PRAGMA foreign_keys=OFF')
        with db:
            keep_ids = {r['id'] for r in kept}
            for row in rows:
                if row['id'] not in keep_ids:
                    for table in ('product_images', 'product_skus', 'product_comments'):
                        db.execute(f'DELETE FROM {table} WHERE ranking_product_id IN (SELECT id FROM ranking_products WHERE ranking_id=?)', (row['id'],))
                    db.execute('DELETE FROM ranking_products WHERE ranking_id=?', (row['id'],))
                    db.execute('DELETE FROM rankings WHERE id=?', (row['id'],))
            for i, row in enumerate(kept, 1):
                db.execute('UPDATE rankings SET id=? WHERE id=?', (-i, row['id']))
                db.execute('UPDATE ranking_products SET ranking_id=? WHERE ranking_id=?', (-i, row['id']))
            for i in range(1, len(kept) + 1):
                db.execute('UPDATE rankings SET id=? WHERE id=?', (i, -i))
                db.execute('UPDATE ranking_products SET ranking_id=? WHERE ranking_id=?', (i, -i))
            db.execute('CREATE UNIQUE INDEX idx_rankings_fixed ON rankings(source_name,tab_name)')
            if db.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('固定榜单迁移外键校验失败，已回滚')
        return backup
    finally:
        db.close()
