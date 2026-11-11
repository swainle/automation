"""SQLite is the source of truth; spreadsheets and JSON are compatible exports."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from automation.pdd.spider.time_format import timestamp as format_timestamp, SQL_NOW
from automation.pdd.spider.owner_states import ITEM_STAGES, LIST_STAGES, canonical, ensure_schema


def today():
    return datetime.now(timezone(timedelta(hours=8))).date().isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False)


class Store:
    def __init__(self, path, *, migrating=False):
        self.data_root = Path(path).resolve().parent
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        if 'week_start' in {row[1] for row in self.db.execute('PRAGMA table_info(rankings)')}:
            self.db.close()
            raise RuntimeError('此数据库使用每周手机采集结构，请使用 python -m automation.pdd.spider --mobile')
        legacy = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name IN ('spider_runs','spider_boards','spider_lists','spider_items','spider_events')").fetchone()
        if legacy:
            self.db.close()
            raise RuntimeError('旧数据库需手动迁移，请先执行 src/automation/pdd/spider/migrate/20261102115136_rankings_products.sql')
        native = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ranking_products'").fetchone()
        if native and not {'state','rank_state'} & {row[1] for row in self.db.execute('PRAGMA table_info(ranking_products)')}:
            self.db.close()
            raise RuntimeError('请先手动执行 src/automation/pdd/spider/migrate/20261102142611_capture_state.sql，增加榜单产品完整采集状态')
        if not migrating and self.db.execute("SELECT 1 FROM sqlite_master WHERE name='collection_tasks'").fetchone():
            self.db.close()
            raise RuntimeError('请先执行 python -m automation.pdd.spider.migrate_owner_states DATABASE --day YYYY-MM-DD，迁移旧状态')
        if not migrating and self.db.execute("SELECT 1 FROM sqlite_master WHERE name='ranking_sources'").fetchone():
            self.db.close()
            raise RuntimeError('请先执行 python -m automation.pdd.spider.migrate_ranking_parents DATABASE，合并主榜来源')
        if not migrating and self.db.execute("SELECT 1 FROM sqlite_master WHERE name IN ('product_info','ranking_runs')").fetchone():
            self.db.close()
            raise RuntimeError('请先执行 python -m automation.pdd.spider.migrate_compact DATABASE --day YYYY-MM-DD，合并商品详情并清理日期')
        if native and not migrating and 'rank_state' not in {row[1] for row in self.db.execute('PRAGMA table_info(ranking_products)')}:
            self.db.close()
            raise RuntimeError('请先执行 python -m automation.pdd.spider.migrate_product_fields DATABASE，精简商品字段')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript(Path(__file__).with_name('schema.sql').read_text(encoding='utf-8'))
        ensure_schema(self.db)
        self._sources = {}

    def close(self):
        self.db.close()

    def _stage(self, list_id, stage, item_id=None):
        stage = canonical(stage)
        table, owner_id, columns = ('ranking_products', item_id, ITEM_STAGES) if item_id is not None else ('rankings', list_id, LIST_STAGES)
        row = self.db.execute(f'SELECT * FROM {table} WHERE id=?', (owner_id,)).fetchone()
        result = {'state': 'pending', 'attempts': 0, 'device': '', 'last_error': ''}
        if row:
            result.update(json.loads(row['stages_json']).get(stage, {}))
            if stage in columns:
                result['state'] = row[columns[stage]]
            if stage == 'collect':
                result['device'] = row['device']
            if stage == 'product':
                result['attempts'] = row['product_attempts']
        return result

    def _set_stage(self, list_id, stage, state, *, item_id=None, **values):
        stage = canonical(stage)
        table, owner_id, columns = ('ranking_products', item_id, ITEM_STAGES) if item_id is not None else ('rankings', list_id, LIST_STAGES)
        row = self.db.execute(f'SELECT * FROM {table} WHERE id=?', (owner_id,)).fetchone()
        if row is None:
            raise ValueError(f'Missing {table} owner {owner_id}')
        stages = json.loads(row['stages_json'])
        previous = self._stage(list_id, stage, item_id)
        now = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')
        values.setdefault('attempts', previous['attempts'] + int(state == 'running'))
        entry = {**previous, **values, 'state': state, 'updated_at': now}
        if state == 'running':
            entry.update(started_at=now, finished_at=None)
        elif state in {'completed', 'failed', 'skipped', 'blocked_webview'}:
            entry['finished_at'] = now
        stages[stage] = {key: value for key, value in entry.items()
                         if not (stage in columns and key == 'state')
                         and not (stage == 'product' and key == 'attempts')}
        fields = {'stages_json': encode(stages), 'updated_at': now}
        if stage in columns:
            fields[columns[stage]] = state
        if 'device' in values and item_id is None:
            fields['device'] = values['device']
        if state in {'failed', 'blocked_webview', 'retry_pending'} or values.get('last_error'):
            fields.update(last_stage=stage, last_error=values.get('last_error', ''))
        elif row['last_stage'] == stage:
            fields.update(last_stage='', last_error='')
        if item_id is not None and stage == 'product':
            fields['product_attempts'] = entry['attempts']
        self.db.execute(f'UPDATE {table} SET ' + ','.join(f'{name}=?' for name in fields) + ' WHERE id=?', [*fields.values(), owner_id])
        return entry

    def stage(self, list_id, stage, item_id=None):
        return self._stage(list_id, stage, item_id)

    def stage_state(self, list_id, stage, state, *, item_id=None, artifacts=None, reason=''):
        with self.db:
            values = {'last_error': reason}
            if artifacts is not None:
                values['artifacts_json'] = encode([str(path) for path in artifacts])
            self._set_stage(list_id, stage, state, item_id=item_id, **values)

    def _source(self, key):
        if not isinstance(key, tuple):
            row = self.db.execute('SELECT capture_date,source_name FROM rankings WHERE id=?', (key,)).fetchone()
            key = (row['capture_date'], row['source_name'])
        if key not in self._sources:
            row = self.db.execute('SELECT * FROM rankings WHERE capture_date=? AND source_name=? ORDER BY tab_index,id', key).fetchone()
            self._sources[key] = {'id': key, 'day': key[0], 'name': key[1],
                                  'url': row['source_url'] if row else '',
                                  'position': row['source_order'] if row else 0,
                                  'state': 'pending', 'reason': ''}
        return self._sources[key]

    def seed(self, day, sources):
        names = [source['name'] for source in sources]
        if len(names) != len(set(names)):
            raise ValueError('来源表存在重复主榜名称')
        result = []
        for position, source in enumerate(sources):
            key = (day, source['name'])
            previous = self._sources.get(key)
            if previous and previous['url'] != source['url']:
                raise ValueError(f"当天榜单链接已变更，请先核对任务：{source['name']}")
            rows = self.db.execute('SELECT state,source_url FROM rankings WHERE capture_date=? AND source_name=?', key).fetchall()
            if source['url'] and any(row['source_url'] and row['source_url'] != source['url'] for row in rows):
                raise ValueError(f"当天榜单链接已变更，请先核对任务：{source['name']}")
            self._sources[key] = {'id': key, 'day': day, 'name': source['name'], 'url': source['url'],
                                  'position': position, 'state': 'completed' if rows and all(row['state']=='completed' for row in rows) else 'pending', 'reason': ''}
            result.append(self._sources[key].copy())
        return result

    def boards(self, day):
        for row in self.db.execute('SELECT DISTINCT source_name FROM rankings WHERE capture_date=?', (day,)):
            self._source((day, row['source_name']))
        return [self._sources[key].copy() for key in sorted(self._sources, key=lambda key: self._sources[key]['position']) if key[0] == day]

    def seed_rankings(self, day, sources, root):
        """Initialize exact Excel rows without grouping different tab URLs under B."""
        from automation.pdd.spider.utils import sanitize
        result = []
        fixed = bool(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='idx_rankings_fixed'").fetchone())
        with self.db:
            for position, source in enumerate(sources):
                name, main, tab = source['name'], source['main_name'], source['tab']
                if fixed:
                    previous = self.db.execute('SELECT * FROM rankings WHERE source_name=? AND tab_name=?', (name, tab)).fetchone()
                    if previous and previous['capture_date'] != day:
                        self.db.execute("UPDATE rankings SET capture_date=?,directory='',state='pending',stages_json='{}',last_error='',completed_at=NULL,collect_state='pending',screenshot_state='pending',export_state='pending' WHERE id=?", (day, previous['id']))
                        for item in self.items(previous['id']):
                            self._reset_item_capture(item['id'])
                        self.db.execute("UPDATE ranking_products SET rank_state='pending' WHERE ranking_id=?", (previous['id'],))
                if sanitize(main) in {'', '.', '..'}:
                    raise ValueError(f'无效主榜目录名称：{main}')
                directory = str((Path(root) / sanitize(main) / sanitize(tab) / day).resolve())
                old = self.db.execute('SELECT * FROM rankings WHERE capture_date=? AND source_name=? AND tab_name=?', (day, name, tab)).fetchone()
                if self.db.execute('SELECT 1 FROM rankings WHERE capture_date=? AND main_name=? AND tab_name=? AND source_name<>?', (day, main, tab, name)).fetchone():
                    raise ValueError(f'已有同主榜/子榜的其他来源记录，请先合并：{main}/{tab}')
                if old and old['main_name'] not in ('', main):
                    raise ValueError(f'主榜名称已变更，请先迁移已有数据：{name}/{tab}')
                if old and old['directory'] and old['directory'] != directory:
                    raise ValueError(f'榜单目录已变更，请先迁移已有文件：{name}/{tab}')
                if old and old['source_url'] and old['source_url'] != source['url']:
                    raise ValueError(f'子榜链接已变更，请先核对任务：{name}/{tab}')
                self.db.execute('''INSERT INTO rankings(capture_date,source_name,main_name,tab_name,source_url,directory,source_order,tab_index)
                    VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(capture_date,source_name,tab_name) DO UPDATE SET
                    main_name=excluded.main_name,source_url=excluded.source_url,directory=excluded.directory,
                    source_order=excluded.source_order,tab_index=excluded.tab_index''',
                    (day, name, main, tab, source['url'], directory, position, source['tab_index']))
                rid = self.db.execute('SELECT id FROM rankings WHERE capture_date=? AND source_name=? AND tab_name=?', (day, name, tab)).fetchone()[0]
                if not tab:
                    Path(directory).mkdir(parents=True, exist_ok=True)
                    self._set_stage(rid, 'collect', 'failed', last_error='链接失效')
                    self.db.execute("UPDATE rankings SET state='failed',last_error='链接失效',completed_at=NULL WHERE id=?", (rid,))
                result.append(rid)
        self._sources.clear()
        return [self.list(rid) for rid in result]

    def lists(self, source):
        board = self._source(source)
        return [self.list(row['id']) for row in self.db.execute(
            'SELECT id FROM rankings WHERE capture_date=? AND source_name=? ORDER BY tab_index,id', (board['day'], board['name']))]

    def discover(self, source, items):
        if not items or len({item.tab for item in items}) != len(items):
            raise ValueError('未找到唯一有效的子榜列表')
        board = self._source(source)
        with self.db:
            for item in items:
                if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='idx_rankings_fixed'").fetchone():
                    previous = self.db.execute('SELECT * FROM rankings WHERE source_name=? AND tab_name=?', (board['name'], item.tab)).fetchone()
                    if previous and previous['capture_date'] != board['day']:
                        self.db.execute("UPDATE rankings SET capture_date=?,directory='',state='pending',stages_json='{}',last_error='',completed_at=NULL,collect_state='pending',screenshot_state='pending',export_state='pending' WHERE id=?", (board['day'], previous['id']))
                        for product in self.items(previous['id']):
                            self._reset_item_capture(product['id'])
                        self.db.execute("UPDATE ranking_products SET rank_state='pending' WHERE ranking_id=?", (previous['id'],))
                self.db.execute("""INSERT INTO rankings(capture_date,source_name,main_name,tab_name,source_url,source_order,tab_index,has_sub)
                    VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(capture_date,source_name,tab_name) DO NOTHING""",
                    (board['day'], board['name'], '', item.tab, board['url'],board['position'],item.index,int(item.has_sub)))

    def board_state(self, source, state, reason=''):
        # A source is only an input grouping, not a separate database record.
        self._source(source).update(state=state, reason=reason)

    def list_state(self, list_id, state, reason=''):
        with self.db:
            self.db.execute('''UPDATE rankings SET state=?,last_error=?,updated_at=(strftime(\'%Y-%m-%dT%H:%M:%S\',\'now\',\'+8 hours\') || \'+08:00\'),
                completed_at=CASE WHEN ?='completed' THEN (strftime(\'%Y-%m-%dT%H:%M:%S\',\'now\',\'+8 hours\') || \'+08:00\') ELSE NULL END WHERE id=?''',
                (state, reason, state, list_id))

    def list(self, list_id):
        row = dict(self.db.execute('SELECT * FROM rankings WHERE id=?', (list_id,)).fetchone())
        row.update(board_id=(row['capture_date'],row['source_name']), tab=row['tab_name'], reason=row['last_error'],
                   device=self._stage(list_id, 'collect')['device'],
                   collect_state=self._stage(list_id, 'collect')['state'], export_state=self._stage(list_id, 'export')['state'])
        return row

    def reset_list_device(self, list_id):
        with self.db:
            if self.db.execute("SELECT 1 FROM ranking_products WHERE ranking_id=? AND product_attempts>0", (list_id,)).fetchone():
                raise ValueError('此子榜已开始商品采集，不能重置设备')
            self.db.execute('DELETE FROM ranking_products WHERE ranking_id=?', (list_id,))
            self.db.execute("UPDATE rankings SET stages_json='{}',collect_state='pending',screenshot_state='pending',export_state='pending',device='' WHERE id=?", (list_id,))
            self.db.execute("UPDATE rankings SET state='pending',header_json='[]',completed_at=NULL WHERE id=?", (list_id,))

    def reset_item_capture(self, item_id):
        """Discard checkpoints belonging to an unverified or replaced product."""
        with self.db:
            self._reset_item_capture(item_id)

    def _reset_item_capture(self, item_id):
        for table in ('product_images', 'product_skus', 'product_comments'):
            self.db.execute(f'DELETE FROM {table} WHERE ranking_product_id=?', (item_id,))
        self.db.execute("UPDATE ranking_products SET shop_id=NULL,shop_name=NULL,listing_date=NULL,sale_state='unknown',info_json='{}',collected_at=NULL WHERE id=?", (item_id,))
        fields = {column: 'pending' for column in ITEM_STAGES.values()}
        fields.update(stages_json='{}', product_attempts=0, last_stage='', last_error='')
        self.db.execute('UPDATE ranking_products SET ' + ','.join(f'{key}=?' for key in fields) + ' WHERE id=?', [*fields.values(), item_id])
        self.db.execute("UPDATE ranking_products SET rank_state='pending',goods_id=NULL,goods_url=NULL WHERE id=?", (item_id,))

    def save_rows(self, list_id, directory, header, rows, device, *, update_existing=False, refresh_completed=False,
                  latest_snapshot=False):
        from automation.pdd.spider.metrics import from_row
        ranks = [int(row['排行']) for row in rows]
        if not ranks or min(ranks) < 1 or len(ranks) != len(set(ranks)):
            raise ValueError('榜单商品排行为空或重复')
        directory = Path(directory).resolve()
        with self.db:
            if latest_snapshot:
                placeholders = ','.join('?' for _ in ranks)
                self.db.execute(f'DELETE FROM ranking_products WHERE ranking_id=? AND rank NOT IN ({placeholders})',
                                (list_id, *ranks))
            if update_existing and not latest_snapshot and len(rows) < self.db.execute('SELECT count(*) FROM ranking_products WHERE ranking_id=?', (list_id,)).fetchone()[0]:
                raise ValueError('新快照商品数少于已有记录，保留旧数据，请核对榜单完整性')
            for row in rows:
                rank = int(row['排行'])
                goods_id, goods_url = row.get('产品ID') or None, row.get('产品链接') or None
                previous = self.db.execute('SELECT * FROM ranking_products WHERE ranking_id=? AND rank=?', (list_id, rank)).fetchone()
                if previous and latest_snapshot:
                    old_raw = json.loads(previous['raw_json'])
                    board_title = row.get('榜单标题') or row.get('标题', '')
                    old_board_title = old_raw.get('榜单标题') or previous['title']
                    # The position is scoped to this day's subranking. Its card
                    # title, not the decorated product-page title, identifies it.
                    same_product = bool(board_title) and board_title == old_board_title
                    if goods_id and previous['goods_id'] and goods_id != previous['goods_id']:
                        same_product = False
                    if not same_product:
                        self._reset_item_capture(previous['id'])
                    else:
                        goods_id = goods_id or previous['goods_id']
                        goods_url = goods_url or previous['goods_url']
                    if same_product and from_row(row) != json.loads(previous['metrics_json']):
                        self._set_stage(list_id, 'product_screenshot', 'pending', item_id=previous['id'])
                        self.db.execute("UPDATE ranking_products SET rank_state='pending' WHERE id=?", (previous['id'],))
                    raw = dict(row, 榜单标题=board_title, 产品ID=goods_id or '', 产品链接=goods_url or '')
                    if same_product:
                        page_title = old_raw.get('商品页标题') or (previous['title'] if old_raw.get('榜单标题') else '')
                        if page_title:
                            raw['商品页标题'] = page_title
                    self.db.execute('''UPDATE ranking_products SET goods_id=?,goods_url=?,title=?,metrics_json=?,raw_json=?,directory=?,
                        updated_at=(strftime(\'%Y-%m-%dT%H:%M:%S\',\'now\',\'+8 hours\') || \'+08:00\') WHERE id=?''', (goods_id, goods_url, row.get('标题', ''), encode(from_row(row)),
                        encode(raw), str(directory / f'榜{rank:02d}'), previous['id']))
                    continue
                if previous and update_existing:
                    if previous['rank_state'] == 'completed' and not refresh_completed:
                        continue
                    same_goods = bool(goods_id and goods_id == previous['goods_id'])
                    same_title = row.get('标题', '') == previous['title']
                    if not same_goods and not same_title:
                        if previous['goods_id'] or self._stage(list_id, 'product', previous['id'])['attempts']:
                            raise ValueError(f'排行 {rank} 的商品身份发生变化，无法安全覆盖已有详情或链接')
                        self._reset_item_capture(previous['id'])
                    else:
                        goods_id = goods_id or previous['goods_id']
                        goods_url = goods_url or previous['goods_url']
                    raw = dict(row, 产品ID=goods_id or '', 产品链接=goods_url or '')
                    self.db.execute('''UPDATE ranking_products SET goods_id=?,goods_url=?,title=?,metrics_json=?,raw_json=?,directory=?,
                        updated_at=(strftime(\'%Y-%m-%dT%H:%M:%S\',\'now\',\'+8 hours\') || \'+08:00\') WHERE id=?''', (goods_id, goods_url, row.get('标题', ''), encode(from_row(row)),
                        encode(raw), str(directory / f'榜{rank:02d}'), previous['id']))
                    continue
                self.db.execute('''INSERT INTO ranking_products(ranking_id,rank,goods_id,goods_url,title,metrics_json,raw_json,directory)
                    VALUES(?,?,?,?,?,?,?,?)''', (list_id, rank, goods_id, goods_url,
                    row.get('标题', ''), encode(from_row(row)), encode(row), str(directory / f'榜{rank:02d}')))
            self.db.execute("UPDATE rankings SET directory=?,header_json=?,state='running',last_error='',updated_at=(strftime(\'%Y-%m-%dT%H:%M:%S\',\'now\',\'+8 hours\') || \'+08:00\') WHERE id=?",
                            (str(directory), encode(header), list_id))
            if rows:
                self.db.execute('UPDATE rankings SET main_name=?,description=? WHERE id=?',
                    (rows[0].get('主榜单名称') or self.list(list_id)['main_name'], rows[0].get('榜单统计说明', ''), list_id))
            self._set_stage(list_id, 'collect', 'completed', device=device)

    def _item(self, row):
        result = dict(row)
        result['state'] = row['rank_state']  # Compatibility for ranking collectors.
        data = json.loads(row['raw_json'])
        data.update({'排行': row['rank'], '产品ID': row['goods_id'] or '', '产品链接': row['goods_url'] or '', '标题': row['title']})
        product = {'info': json.loads(row['info_json']), 'files': [],
                   'images': [dict(image) for image in self.db.execute('SELECT * FROM product_images WHERE ranking_product_id=? ORDER BY image_type,position', (row['id'],))],
                   'sku': [json.loads(sku['raw_json']) for sku in self.db.execute('SELECT raw_json FROM product_skus WHERE ranking_product_id=? ORDER BY position', (row['id'],))]}
        # Collectors still resolve their manifests from the product directory.
        for image in product['images']:
            image['local_path'] = self.worker_image_path(row['directory'], image['local_path'])
        for sku in product['sku']:
            for key in ('图片路径', 'image'):
                if sku.get(key) and not str(sku[key]).startswith(('http://', 'https://')):
                    sku[key] = self.worker_image_path(row['directory'], sku[key])
        comments = self.db.execute('SELECT raw_json,is_negative FROM product_comments WHERE ranking_product_id=? ORDER BY id', (row['id'],)).fetchall()
        product['comments'] = [json.loads(comment['raw_json']) for comment in comments if not comment['is_negative']]
        product['negative_comments'] = [json.loads(comment['raw_json']) for comment in comments if comment['is_negative']]
        stage_names = set(json.loads(row['stages_json'])) | set(ITEM_STAGES)
        product['status'] = {'stages': {stage: self._stage(row['ranking_id'], stage, row['id']) for stage in stage_names}}
        if row['listing_date']:
            data['上架日期'] = row['listing_date']
            product['status']['listing_date'] = row['listing_date']
        if not row['collected_at'] and not any(product.get(key) for key in ('info','files','images','sku','comments','negative_comments')):
            product = {}
        for stage in ('screenshot', 'crop', 'link', 'product'):
            result[f'{stage}_state'] = self._stage(row['ranking_id'], stage, row['id'])['state']
        state = self._stage(row['ranking_id'], 'product', row['id'])
        result.update(list_id=row['ranking_id'], row_json=encode(data), product_json=encode(product),
                      product_attempts=state['attempts'], reason=state['last_error'])
        return result

    def item(self, item_id):
        return self._item(self.db.execute('SELECT * FROM ranking_products WHERE id=?', (item_id,)).fetchone())

    def item_state(self, item_id, state):
        if state not in {'pending', 'running', 'completed', 'failed'}:
            raise ValueError(state)
        with self.db:
            self.db.execute('UPDATE ranking_products SET rank_state=?,updated_at=(strftime(\'%Y-%m-%dT%H:%M:%S\',\'now\',\'+8 hours\') || \'+08:00\') WHERE id=?', (state, item_id))

    def full_list_completed(self, list_id):
        return (self.list(list_id)['state'] == 'completed'
                and self.stage(list_id, 'ranking_long_screenshot')['state'] == 'completed'
                and bool(self.db.execute('SELECT 1 FROM ranking_products WHERE ranking_id=?', (list_id,)).fetchone())
                and not self.db.execute("SELECT 1 FROM ranking_products WHERE ranking_id=? AND rank_state!='completed'", (list_id,)).fetchone())

    def items(self, list_id):
        return [self._item(row) for row in self.db.execute('SELECT * FROM ranking_products WHERE ranking_id=? ORDER BY rank', (list_id,))]

    def item_stage(self, item_id, stage, *, row=None):
        if stage not in {'screenshot', 'crop', 'link'}:
            raise ValueError(stage)
        item = self.item(item_id)
        with self.db:
            self._set_stage(item['list_id'], stage, 'completed', item_id=item_id, last_error='')
            if row is not None:
                from automation.pdd.spider.metrics import from_row
                self.db.execute('UPDATE ranking_products SET goods_id=?,goods_url=?,title=?,metrics_json=?,raw_json=?,updated_at=(strftime(\'%Y-%m-%dT%H:%M:%S\',\'now\',\'+8 hours\') || \'+08:00\') WHERE id=?',
                    (row.get('产品ID') or None, row.get('产品链接') or None, row.get('标题', ''), encode(from_row(row)), encode(row), item_id))

    def export(self, list_id):
        from automation.pdd.spider.utils import write_xlsx
        from automation.pdd.spider.artifacts import write_rank_share_links
        from automation.pdd.spider.status import update_stage
        entry = self.list(list_id)
        header = json.loads(entry['header_json'])
        rows = [json.loads(item['row_json']) for item in self.items(list_id)]
        directory = Path(entry['directory'])
        write_rank_share_links(directory, rows)
        for row in rows:
            update_stage(directory / f"榜{int(row['排行']):02d}", 'rank', 'completed', data=row)
        write_xlsx(directory / '榜单数据.xlsx', header, [[row.get(key, '') for key in header] for row in rows])
        with self.db:
            self._set_stage(list_id, 'export', 'completed')

    def event(self, day, board, tab, device, stage, error):
        import logging
        logging.getLogger(__name__).warning('[%s] %s / %s [%s:%s] %s', day, board, tab, device, stage, error)

    def finish(self, day):
        pass  # Completion is owned by rankings; no separate run record.

    def product_tasks(self, *, day=None, ranking_id=None):
        tasks = []
        filters, params = '', []
        if day is not None:
            filters += ' AND r.capture_date=?'
            params.append(day)
        if ranking_id is not None:
            filters += ' AND r.id=?'
            params.append(ranking_id)
        for row in self.db.execute('''SELECT p.*,r.directory AS list_directory FROM ranking_products p
                JOIN rankings r ON r.id=p.ranking_id
                WHERE p.goods_url IS NOT NULL AND TRIM(p.goods_url)!='' ''' + filters + '''
                ORDER BY r.source_order,r.capture_date,r.tab_index,p.rank''', params):
            try:
                url = urlsplit(row['goods_url'])
                if url.scheme not in {'http', 'https'} or not url.hostname:
                    continue
            except ValueError:
                continue
            item = self._item(row)
            item.update(directory=row['list_directory'], ranking_list_id=row['ranking_id'])
            tasks.append(item)
        return tasks

    def fill_product_id(self, item_id, goods_id):
        item = self.item(item_id)
        goods_id = str(goods_id)
        if not goods_id.isdecimal() or int(goods_id) <= 0:
            raise ValueError('未取得有效商品 ID')
        if item['goods_id'] and str(item['goods_id']) != goods_id:
            raise ValueError('补全 ID 与已记录的商品 ID 不一致')
        row = json.loads(item['row_json'])
        row['产品ID'] = goods_id
        with self.db:
            self.db.execute('UPDATE ranking_products SET goods_id=?,raw_json=?,updated_at=(strftime(\'%Y-%m-%dT%H:%M:%S\',\'now\',\'+8 hours\') || \'+08:00\') WHERE id=?',
                            (goods_id, encode(row), item_id))

    def product_state(self, item_id, state, *, details=None, reason=''):
        item = self.item(item_id)
        with self.db:
            self._set_stage(item['list_id'], 'product', state, item_id=item_id, last_error=reason)
            if details is not None:
                self._save_details(item_id, details)

    def refresh_product_info(self, item_id, info):
        with self.db:
            cursor = self.db.execute(f"UPDATE ranking_products SET title=COALESCE(NULLIF(?,''),title),info_json=?,collected_at={SQL_NOW},updated_at={SQL_NOW} WHERE id=?",
                                     (info.get('goods_name'), encode(info), item_id))
            if cursor.rowcount != 1:
                raise ValueError(f'商品记录不存在：{item_id}')

    def save_product_image(self, item_id, record):
        with self.db:
            self._save_image_record(item_id, record)

    def data_image_path(self, directory, value):
        path = Path(value)
        if path.is_absolute() or '..' in path.parts:
            raise ValueError('图片路径必须是 data 内的相对路径')
        prefix = Path(directory).resolve().relative_to(self.data_root)
        if path.is_relative_to(prefix):
            return path.as_posix()
        return (prefix / path).as_posix()

    def worker_image_path(self, directory, value):
        if not directory:
            return value
        path = Path(value)
        prefix = Path(directory).resolve().relative_to(self.data_root)
        return path.relative_to(prefix).as_posix() if path.is_relative_to(prefix) else value

    def _save_image_record(self, item_id, record):
        path = Path(record['local_path'])
        if path.is_absolute() or '..' in path.parts:
            raise ValueError('图片路径必须相对商品目录')
        directory = self.db.execute('SELECT directory FROM ranking_products WHERE id=?', (item_id,)).fetchone()[0]
        relative = self.data_image_path(directory, record['local_path'])
        self.db.execute('''INSERT INTO product_images(ranking_product_id,image_type,position,source_url,local_path,state,last_error)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(ranking_product_id,image_type,position) DO UPDATE SET
            source_url=excluded.source_url,local_path=excluded.local_path,state=excluded.state,
            last_error=excluded.last_error,updated_at=(strftime(\'%Y-%m-%dT%H:%M:%S\',\'now\',\'+8 hours\') || \'+08:00\')''',
            (item_id, record['image_type'], record['position'], record.get('source_url'), relative,
             record.get('state', 'pending'), record.get('last_error', '')))

    def _save_details(self, item_id, details):
        import hashlib
        previous = self.db.execute('SELECT info_json,sale_state FROM ranking_products WHERE id=?', (item_id,)).fetchone()
        info = details.get('info', json.loads(previous['info_json']))
        listing = details.get('status', {}).get('listing_date')
        item = self.item(item_id)
        self.db.execute(f'UPDATE ranking_products SET listing_date=COALESCE(?,listing_date),info_json=?,collected_at={SQL_NOW} WHERE id=?',
                        (listing, encode(info), item_id))
        def find_goods(value):
            if isinstance(value, dict):
                if str(value.get('goods_id', '')) == str(item['goods_id']) and any(k in value for k in ('goods_name', 'shop_id', 'mall_id')):
                    return value
                for child in value.values():
                    found = find_goods(child)
                    if found:
                        return found
            elif isinstance(value, list):
                for child in value:
                    found = find_goods(child)
                    if found:
                        return found
            return None
        goods = find_goods(info) or info
        outcome = details.get('status', {}).get('stages', {}).get('product', {}).get('outcome')
        sale_state = {'available': 'on_sale', 'sold_out': 'sold_out'}.get(outcome, previous['sale_state'])
        self.db.execute('UPDATE ranking_products SET shop_id=?,shop_name=?,sale_state=? WHERE id=?',
            (goods.get('shop_id') or goods.get('mall_id'),
             goods.get('shop_name') or goods.get('mall_name'), sale_state, item_id))
        if 'images' in details:
            for stage, types in [('main_images', ('main',)), ('sku', ('sku',)), ('details', ('detail', 'detail_merged'))]:
                stages = details.get('status', {}).get('stages', {})
                if stages.get(stage, stages.get(canonical(stage), {})).get('state') in {'completed', 'skipped'}:
                    placeholders = ','.join('?' for _ in types)
                    self.db.execute(f'DELETE FROM product_images WHERE ranking_product_id=? AND image_type IN ({placeholders})', (item_id, *types))
        for record in details.get('images', []):
            if record.get('image_type', '').endswith('_video'):
                continue
            self._save_image_record(item_id, record)
        def fingerprint(value):
            return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if 'sku' in details:
            self.db.execute('DELETE FROM product_skus WHERE ranking_product_id=?', (item_id,))
            for position, sku in enumerate(details['sku']):
                sku = dict(sku)
                for key in ('图片路径', 'image'):
                    if sku.get(key) and not str(sku[key]).startswith(('http://', 'https://')):
                        sku[key] = self.data_image_path(item['directory'], sku[key])
                specs = {key: value for key, value in sku.items() if key.startswith('SPU')}
                self.db.execute('''INSERT OR IGNORE INTO product_skus(ranking_product_id,sku_id,dedup_key,position,specs_json,price,image,raw_json)
                    VALUES(?,?,?,?,?,?,?,?)''', (item_id, sku.get('sku_id'), fingerprint(sku), position, encode(specs),
                    sku.get('卷后价') or sku.get('券后价') or sku.get('price'), sku.get('图片路径') or sku.get('image'), encode(sku)))
        if any(key in details for key in ('comments', 'positive_comments', 'negative_comments')):
            self.db.execute('DELETE FROM product_comments WHERE ranking_product_id=?', (item_id,))
            for negative, key in ((False, 'comments'), (False, 'positive_comments'), (True, 'negative_comments')):
                for comment in details.get(key, []):
                    comment_id = comment.get('review_id') or comment.get('comment_id')
                    identity = str(comment_id) if comment_id else comment.get('ui_fingerprint') or fingerprint({k: v for k, v in comment.items() if k not in ('序号', '原因', 'negative_reason')})
                    specs = comment.get('SKU') or comment.get('specs') or ''
                    specs = specs if isinstance(specs, str) else encode(specs)
                    previous = self.db.execute('SELECT tags_json,raw_json FROM product_comments WHERE ranking_product_id=? AND dedup_key=?', (item_id, identity)).fetchone()
                    tags = json.loads(previous['tags_json']) if previous else []
                    for tag in [*comment.get('positive_tags', []), *comment.get('negative_tags', [])] or ([comment.get('原因') or comment.get('negative_reason')] if negative else []):
                        if tag and tag not in tags:
                            tags.append(tag)
                    raw_comment = {**(json.loads(previous['raw_json']) if previous else {}), **comment}
                    for tag_key in ('positive_tags', 'negative_tags'):
                        old_tags = json.loads(previous['raw_json']).get(tag_key, []) if previous else []
                        if old_tags or comment.get(tag_key):
                            raw_comment[tag_key] = list(dict.fromkeys([*old_tags, *comment.get(tag_key, [])]))
                    self.db.execute('''INSERT INTO product_comments(ranking_product_id,comment_id,dedup_key,content,sku_text,
                        is_negative,tags_json,commented_at,raw_json) VALUES(?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(ranking_product_id,dedup_key) DO UPDATE SET is_negative=MAX(is_negative,excluded.is_negative),
                        tags_json=excluded.tags_json,sku_text=excluded.sku_text,
                        commented_at=COALESCE(excluded.commented_at,product_comments.commented_at),raw_json=excluded.raw_json''',
                        (item_id, comment_id, identity, comment.get('评论内容') or comment.get('comment') or '',
                         specs, int(negative), encode(tags),
                         format_timestamp(comment.get('评论日期') or comment.get('time')), encode(raw_comment)))
        for stage, data in details.get('status', {}).get('stages', {}).items():
            if stage != 'product':
                self._set_stage(item['list_id'], 'product_screenshot' if stage == 'screenshot' else stage, data.get('state', 'pending'), item_id=item_id,
                                last_error=data.get('reason', ''))

