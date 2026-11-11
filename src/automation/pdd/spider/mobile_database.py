"""PostgreSQL storage for weekly ranking snapshots and product checkpoints."""
import hashlib
import json
import psycopg
from psycopg.rows import dict_row
from datetime import date, timedelta
from pathlib import Path

from automation.pdd.spider.database import Store as LegacyStore, encode
from automation.pdd.spider.metrics import FIELDS, from_row
from automation.pdd.spider.time_format import timestamp
from automation.pdd.spider.utils import sanitize

SQL_NOW = "(to_char(clock_timestamp() AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM-DD\"T\"HH24:MI:SS') || '+08:00')"
TABLES = ('t_pdd_rankings', 't_pdd_ranking_products', 't_pdd_product_skus',
          't_pdd_product_main_images', 't_pdd_products_detail_image', 't_pdd_product_comments', 't_pdd_selling_point')


def connect(dsn=None, *, readonly=False):
    from automation.pdd.spider import config
    dsn = dsn or config.POSTGRES_URL
    if not dsn:
        raise ValueError('请设置 SPIDER_POSTGRES_URL（数据库 pdd，命名空间 spider）')
    options = '-c search_path=spider -c timezone=Asia/Shanghai'
    if readonly:
        options += ' -c default_transaction_read_only=on -c statement_timeout=5000'
    return psycopg.connect(dsn, connect_timeout=5, autocommit=True, row_factory=dict_row, options=options)


def columns(db, table):
    return {row['column_name'] for row in db.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='spider' AND table_name=%s", (table,))}


WEBVIEW_SKIP = 'webview_element_skip'
PRODUCT_STAGES = {
    'product_screenshot': 'product_screenshot_state', 'main_images': 'main_images_state',
    'sku': 'sku_state', 'detail_images': 'detail_images_state',
    'negative_comments': 'comments_state', 'product': 'product_state',
}
LIST_STAGES = {'collect': 'snapshot_state', 'ranking_long_screenshot': 'screenshot_state', 'export': 'export_state'}


def week_start(day):
    value = date.fromisoformat(day)
    return (value - timedelta(days=value.weekday())).isoformat()


class Store:
    weekly_mobile = True
    data_image_path = LegacyStore.data_image_path
    worker_image_path = LegacyStore.worker_image_path

    def __init__(self, dsn=None):
        from automation.pdd.spider import config
        self.data_root = config.BUSINESS_ROOT / 'data'
        self.db = connect(dsn)
        try:
            with self.db.transaction():
                self.db.execute(Path(__file__).with_name('mobile_schema.sql').read_text(encoding='utf-8'), prepare=False)
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def seed_rankings(self, day, sources, root):
        start = week_start(day)
        ids = []
        with self.db.transaction():
            for position, source in enumerate(sources):
                main, tab = source['main_name'], source['tab']
                if sanitize(main) in {'', '.', '..'}:
                    raise ValueError(f'无效主榜目录名称：{main}')
                directory = str((Path(root) / sanitize(main) / sanitize(tab) / day).resolve())
                self.db.execute('''INSERT INTO t_pdd_rankings(capture_date,week_start,source_name,main_name,tab_name,
                    source_url,source_order,tab_index,directory) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(week_start,source_name,tab_name) DO UPDATE SET
                    source_url=excluded.source_url,source_order=excluded.source_order''',
                    (day, start, source['name'], main, tab, source['url'], position, source['tab_index'], directory))
                ids.append(self.db.execute('SELECT id FROM t_pdd_rankings WHERE week_start=%s AND source_name=%s AND tab_name=%s',
                                          (start, source['name'], tab)).fetchone()['id'])
        return [self.list(rid) for rid in ids]

    def list(self, list_id):
        row = dict(self.db.execute('SELECT * FROM t_pdd_rankings WHERE id=%s', (list_id,)).fetchone())
        row.update(tab=row['tab_name'], state=row['collect_state'])
        return row

    def list_state(self, list_id, state, reason=''):
        with self.db.transaction():
            self.db.execute(f'''UPDATE t_pdd_rankings SET collect_state=%s,updated_at={SQL_NOW},
                completed_at=CASE WHEN %s='completed' THEN {SQL_NOW} ELSE NULL END WHERE id=%s''', (state, state, list_id))

    def stage(self, list_id, stage, item_id=None):
        if item_id is None:
            row, column = self.list(list_id), LIST_STAGES[stage]
        else:
            row = self.db.execute('SELECT * FROM t_pdd_ranking_products WHERE id=%s', (item_id,)).fetchone()
            column = PRODUCT_STAGES[stage]
        return {'state': row[column]}

    def stage_state(self, list_id, stage, state, *, item_id=None, artifacts=None, reason=''):
        with self.db.transaction():
            if item_id is None:
                column = LIST_STAGES[stage]
                self.db.execute(f'UPDATE t_pdd_rankings SET {column}=%s,last_stage=%s,updated_at={SQL_NOW} WHERE id=%s', (state, stage, list_id))
            else:
                column = PRODUCT_STAGES[stage]
                # Product wrapper state must not hide the actual unfinished step.
                active = stage != 'product' or not self.db.execute('SELECT last_stage FROM t_pdd_ranking_products WHERE id=%s', (item_id,)).fetchone()['last_stage']
                self.db.execute(f'UPDATE t_pdd_ranking_products SET {column}=%s,last_error=%s,updated_at={SQL_NOW}' +
                                (',last_stage=%s' if active else '') + ' WHERE id=%s',
                                (state, reason, *([stage] if active else []), item_id))
                rank = self.db.execute('SELECT rank,last_stage FROM t_pdd_ranking_products WHERE id=%s', (item_id,)).fetchone()
                self.db.execute(f'UPDATE t_pdd_rankings SET last=%s,last_stage=%s,updated_at={SQL_NOW} WHERE id=%s', (rank['rank'], rank['last_stage'], list_id))

    def save_rows(self, list_id, directory, header, rows, device='ios', **kwargs):
        ranks = [int(row['排行']) for row in rows]
        if not ranks or min(ranks) < 1 or len(ranks) != len(set(ranks)):
            raise ValueError('榜单商品排行为空或重复')
        entry = self.list(list_id)
        if entry['snapshot_state'] == 'completed':
            return
        day = date.fromisoformat(entry['capture_date'])
        year, week, _ = day.isocalendar()
        columns = ['ranking_id', 'rank', 'title', 'directory', 'collection_month', 'collection_month_week',
                   'collection_week_year', 'collection_year_week', *FIELDS]
        with self.db.transaction():
            for row in rows:
                rank = int(row['排行'])
                metrics = from_row(row)
                values = [list_id, rank, row['标题'], str(Path(directory) / f'榜{rank:02d}'),
                          day.strftime('%Y-%m'), (day.day - 1) // 7 + 1, year, week, *(metrics[key] for key in FIELDS)]
                self.db.execute(f'INSERT INTO t_pdd_ranking_products({",".join(columns)}) VALUES({",".join("%s" for _ in columns)})', values)
            self.db.execute("UPDATE t_pdd_rankings SET snapshot_state='completed',directory=%s,description=%s WHERE id=%s",
                            (str(directory), rows[0].get('榜单统计说明', ''), list_id))

    def items(self, list_id):
        return [self.item(r['id']) for r in self.db.execute('SELECT id FROM t_pdd_ranking_products WHERE ranking_id=%s ORDER BY rank', (list_id,))]

    def requeue_undated_comments(self, list_id):
        with self.db.transaction():
            count = self.db.execute('''UPDATE t_pdd_ranking_products SET comments_state='pending',
                product_state='pending',rank_state='pending' WHERE ranking_id=%s
                AND product_state='completed' AND comments_state='completed'
                AND EXISTS(SELECT 1 FROM t_pdd_product_comments c WHERE c.ranking_product_id=t_pdd_ranking_products.id
                           AND (c.commented_at IS NULL OR c.commented_at=''))''', (list_id,)).rowcount
            if count:
                self.db.execute("UPDATE t_pdd_rankings SET collect_state='pending',completed_at=NULL WHERE id=%s", (list_id,))
        return count

    def item(self, item_id):
        row = dict(self.db.execute('SELECT * FROM t_pdd_ranking_products WHERE id=%s', (item_id,)).fetchone())
        images = []
        for table, kind in [('t_pdd_product_main_images', 'main'), ('t_pdd_products_detail_image', 'detail')]:
            for image in self.db.execute(f'SELECT * FROM {table} WHERE ranking_product_id=%s ORDER BY position', (item_id,)):
                images.append(dict(image, image_type=kind, local_path=self.worker_image_path(row['directory'], image['local_path'])))
        skus = []
        for sku in self.db.execute('SELECT * FROM t_pdd_product_skus WHERE ranking_product_id=%s ORDER BY position', (item_id,)):
            path = self.worker_image_path(row['directory'], sku['local_path']) if sku['local_path'] else ''
            skus.append({**json.loads(sku['specs_json']), 'price': sku['price'], '图片路径': path,
                         'capture_method': 'ios_enlarged_screenshot_crop'})
            if path:
                images.append({'image_type': 'sku', 'position': sku['position'], 'local_path': path, 'state': 'completed'})
        comments = [{'comment': c['content'], 'specs': c['sku_text'], 'negative_tags': [c['tag']],
                     'negative_reason': c['tag'], 'time': c['commented_at'], 'ui_fingerprint': c['dedup_key']}
                    for c in self.db.execute('SELECT * FROM t_pdd_product_comments WHERE ranking_product_id=%s ORDER BY id', (item_id,))]
        data = {'排行': row['rank'], '标题': row['title'], 'metrics': {key: row[key] for key in FIELDS}}
        product = {'images': images, 'sku': skus, 'negative_comments': comments,
                   'status': {'stages': {s: {'state': row[c]} for s, c in PRODUCT_STAGES.items()}}}
        row.update(list_id=row['ranking_id'], row_json=encode(data), product_json=encode(product))
        return row

    def item_state(self, item_id, state):
        with self.db.transaction():
            self.db.execute(f'UPDATE t_pdd_ranking_products SET rank_state=%s,updated_at={SQL_NOW} WHERE id=%s', (state, item_id))

    def product_state(self, item_id, state, *, details=None, reason=''):
        item = self.item(item_id)
        if details is not None:
            self.save_progress(item_id, details)
        self.stage_state(item['list_id'], 'product', state, item_id=item_id, reason=reason)

    def refresh_product_info(self, item_id, info):
        from automation.pdd.spider.metrics import from_text
        metrics = {k: v for k, v in from_text(info.get('page_text', '')).items() if v is not None}
        fields = {'title': info['goods_name'], **metrics}
        with self.db.transaction():
            self.db.execute(f'UPDATE t_pdd_ranking_products SET ' + ','.join(f'{key}=%s' for key in fields) +
                            f',collected_at={SQL_NOW},updated_at={SQL_NOW} WHERE id=%s', [*fields.values(), item_id])

    def save_product_image(self, item_id, record):
        table = {'main': 't_pdd_product_main_images', 'detail': 't_pdd_products_detail_image'}.get(record['image_type'])
        if not table:
            return  # SKU images are owned by t_pdd_product_skus.local_path.
        directory = self.db.execute('SELECT directory FROM t_pdd_ranking_products WHERE id=%s', (item_id,)).fetchone()['directory']
        path = self.data_image_path(directory, record['local_path'])
        with self.db.transaction():
            self.db.execute(f'''INSERT INTO {table}(ranking_product_id,position,local_path,state) VALUES(%s,%s,%s,%s)
                ON CONFLICT(ranking_product_id,position) DO UPDATE SET local_path=excluded.local_path,state=excluded.state''',
                (item_id, record['position'], path, record.get('state', 'completed')))

    def restart_images(self, item_id, kind):
        table = {'main': 't_pdd_product_main_images', 'detail': 't_pdd_products_detail_image'}[kind]
        with self.db.transaction():
            self.db.execute(f'DELETE FROM {table} WHERE ranking_product_id=%s', (item_id,))

    def save_progress(self, item_id, details):
        item = self.item(item_id)
        def fingerprint(value):
            return hashlib.sha256(encode(value).encode()).hexdigest()
        with self.db.transaction():
            if 'sku' in details:
                self.db.execute('DELETE FROM t_pdd_product_skus WHERE ranking_product_id=%s', (item_id,))
                for position, sku in enumerate(details['sku'], 1):
                    if sku.get('available') is False:
                        continue
                    specs = {k: v for k, v in sku.items() if k.startswith('SPU')}
                    path = sku.get('图片路径') or sku.get('image')
                    path = self.data_image_path(item['directory'], path) if path else None
                    self.db.execute('''INSERT INTO t_pdd_product_skus(ranking_product_id,dedup_key,position,specs_json,price,local_path)
                        VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT (ranking_product_id,dedup_key) DO NOTHING''', (item_id, fingerprint(specs), position, encode(specs),
                        sku.get('price') or sku.get('券后价') or sku.get('卷后价'), path))
            if 'negative_comments' in details:
                self.db.execute('DELETE FROM t_pdd_product_comments WHERE ranking_product_id=%s', (item_id,))
                for comment in details['negative_comments']:
                    content = comment.get('comment') or comment.get('评论内容') or ''
                    specs = comment.get('specs') or comment.get('SKU') or ''
                    tag = comment.get('negative_reason') or comment.get('原因') or next(iter(comment.get('negative_tags', [])), '')
                    identity = comment.get('ui_fingerprint') or fingerprint([content, specs])
                    self.db.execute('''INSERT INTO t_pdd_product_comments(ranking_product_id,dedup_key,content,sku_text,tag,commented_at)
                        VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT (ranking_product_id,dedup_key) DO NOTHING''', (item_id, identity, content, specs, tag, timestamp(comment.get('time') or comment.get('评论日期'))))

    def export(self, list_id):
        from automation.pdd.spider.utils import write_xlsx
        entry = self.list(list_id)
        items = self.items(list_id)
        header = ['排行', '标题', *FIELDS]
        write_xlsx(Path(entry['directory']) / '榜单数据.xlsx', header,
                   [[i['rank'], i['title'], *(i[k] for k in FIELDS)] for i in items])
        self.stage_state(list_id, 'export', 'completed')
