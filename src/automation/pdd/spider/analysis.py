"""Per-product, resumable historical identity matching and selling point extraction."""
import json

from automation.pdd.spider import point
from automation.pdd.spider.title_match import title_key, choose_title_match
from automation.pdd.spider.mobile_database import SQL_NOW, identity_complete, product_complete, product_skipped


class AnalysisRunner:
    def __init__(self, store):
        self.store = store
        self.attempted = {}
        self.histories = {}

    def history(self, week):
        if week not in self.histories:
            titles = {}
            for row in self.store.db.execute("""SELECT p.id,p.title,r.capture_date,
                p.platform_product_id,p.platform_product_link,p.listing_date,p.updated_at
                FROM t_pdd_ranking_products p JOIN t_pdd_rankings r ON r.id=p.ranking_id
                LEFT JOIN t_pdd_product_analysis a ON a.ranking_product_id=p.id
                WHERE r.week_start<%s AND btrim(p.platform_product_id)<>'' AND btrim(p.platform_product_link)<>''
                AND COALESCE(a.match_result,'')<>'matched' ORDER BY p.id""", (week,)):
                titles.setdefault(title_key(row['title']), []).append(dict(row))
            self.histories[week] = titles
        return self.histories[week]

    def state(self, item_id):
        return self.store.db.execute('SELECT * FROM t_pdd_product_analysis WHERE ranking_product_id=%s', (item_id,)).fetchone()

    def completion(self, item_id):
        row = self.store.db.execute("""SELECT p.*,EXISTS(
            SELECT 1 FROM t_pdd_selling_point sp WHERE sp.ranking_product_id=p.id) AS has_points
            FROM t_pdd_ranking_products p WHERE p.id=%s""", (item_id,)).fetchone()
        return row, identity_complete(row), row['has_points']

    def is_complete(self, item):
        current, matched, points = self.completion(item['id'])
        return product_skipped(current) or (matched and points)

    def has_failed(self, items):
        for item in items:
            current, matched, points = self.completion(item['id'])
            if product_skipped(current):
                continue
            row = self.state(item['id'])
            if row and ((not matched and row['match_state'] == 'failed') or (not points and row['point_state'] == 'failed')):
                return True
        return False

    def update(self, item_id, **fields):
        self.store.db.execute('UPDATE t_pdd_product_analysis SET ' + ','.join(f'{c}=%s' for c in fields)
                              + f',updated_at={SQL_NOW} WHERE ranking_product_id=%s', [*fields.values(), item_id])

    def match(self, item):
        self.update(item['id'], match_state='running', last_error='')
        if identity_complete(item):
            outcome, candidate, evidence = 'existing', None, {}
        else:
            history = self.history(self.store.list(item['ranking_id'])['week_start'])
            outcome, candidate, evidence = choose_title_match(item['title'], history.get(title_key(item['title']), []))
        with self.store.db.transaction():
            if candidate:
                # Lock and recheck to preserve directly collected identity fields and reject conflicts.
                current = self.store.db.execute('SELECT * FROM t_pdd_ranking_products WHERE id=%s FOR UPDATE', (item['id'],)).fetchone()
                values = {c: candidate[c] for c in ('platform_product_id', 'platform_product_link', 'listing_date')}
                if any(current[c] and values[c] and str(current[c]) != str(values[c]) for c in values):
                    outcome, candidate = 'conflict', None
                else:
                    evidence['filled_fields'] = {c: values[c] for c in values if not current[c] and values[c]}
                    self.store.db.execute(f'''UPDATE t_pdd_ranking_products SET
                        platform_product_id=CASE WHEN btrim(platform_product_id)='' THEN %s ELSE platform_product_id END,
                        platform_product_link=CASE WHEN btrim(platform_product_link)='' THEN %s ELSE platform_product_link END,
                        listing_date=COALESCE(NULLIF(listing_date,''),%s),updated_at={SQL_NOW} WHERE id=%s''',
                        (*values.values(), item['id']))
                    self.store.db.execute('UPDATE t_pdd_selling_point SET platform_product_id=%s WHERE ranking_product_id=%s',
                                          (current['platform_product_id'] or candidate['platform_product_id'], item['id']))
            self.update(item['id'], match_state='completed' if identity_complete(self.completion(item['id'])[0]) else 'pending', match_result=outcome,
                        matched_product_id=candidate['id'] if candidate else None,
                        match_evidence=json.dumps(evidence))
        print(f"[分析] 商品 {item['id']} 历史标题匹配：{outcome}", flush=True)

    def process(self, item):
        if product_skipped(item):
            return True
        if not product_complete(item):
            return False
        if item['id'] in self.attempted:
            return self.attempted[item['id']]
        self.attempted[item['id']] = False
        db = self.store.db
        db.execute('INSERT INTO t_pdd_product_analysis(ranking_product_id) VALUES(%s) ON CONFLICT(ranking_product_id) DO NOTHING', (item['id'],))
        item, matched, points = self.completion(item['id'])
        self.update(item['id'], match_state='completed' if matched else 'pending',
                    point_state='completed' if points else 'pending', last_error='')
        if not matched:
            try:
                self.match(item)
            except Exception as error:
                self.update(item['id'], match_state='failed', last_error=f'匹配：{error}')
                print(f"[分析] 商品 {item['id']} 匹配失败：{error}", flush=True)
        if not points:
            self.update(item['id'], point_state='running')
            try:
                point.process_product(self.store, self.store.item(item['id']))
                _, matched, points = self.completion(item['id'])
                row = self.state(item['id'])
                self.update(item['id'], point_state='completed' if points else 'pending',
                            last_error='' if matched else row['last_error'])
                print(f"[分析] 商品 {item['id']} 卖点{'完成' if points else '未完成'}", flush=True)
            except Exception as error:
                self.update(item['id'], point_state='failed', last_error=f'卖点：{error}')
                print(f"[分析] 商品 {item['id']} 卖点失败：{error}", flush=True)
        self.attempted[item['id']] = self.is_complete(item)
        return self.attempted[item['id']]
