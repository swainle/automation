"""Resume mobile collection, historical title matching and selling point extraction."""
from pathlib import Path
import re
import shutil
import tempfile
import time

from automation.pdd.spider import config, ios, longshot
from automation.pdd.spider.metrics import FIELDS, from_row
from automation.pdd.spider.mobile_database import SQL_NOW, PRODUCT_COLUMNS, product_complete, identity_complete
from automation.pdd.spider.analysis import AnalysisRunner


def reset_mobile(store, item, row):
    from automation.pdd.spider.mobile_files import checked_paths
    root, _ = checked_paths(store.list(item['ranking_id'])['directory'])
    folder = Path(item['directory'])
    if folder.resolve() != root.resolve() / f"榜{item['rank']:02d}":
        raise ValueError(f'非法产品清理目录：{folder}')
    # Validate all descendants before removing the product's old artifacts.
    if folder.exists():
        for path in folder.iterdir():
            if path.name != '产品榜单截图.jpg':
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
    folder.mkdir(parents=True, exist_ok=True)
    metrics = from_row(row)
    fields = dict(title=row['标题'], **{k: metrics.get(k) for k in FIELDS})
    fields.update({column: 'pending' for column in PRODUCT_COLUMNS if column != 'ranking_product_screenshot_state'})
    with store.db.transaction():
        for table in ('t_pdd_product_skus', 't_pdd_product_main_images', 't_pdd_products_detail_image', 't_pdd_product_comments',
                      't_pdd_selling_point', 't_pdd_product_analysis'):
            store.db.execute(f'DELETE FROM {table} WHERE ranking_product_id=%s', (item['id'],))
        store.db.execute('UPDATE t_pdd_ranking_products SET ' + ','.join(f'{k}=%s' for k in fields) + ' WHERE id=%s',
                         [*fields.values(), item['id']])


def mobile_round(store, runtime, entry, product_id=None, *, refreshed=None, analysis=None):
    from automation.pdd.spider.navigation import BoardUnavailableError, open_app_board
    from automation.pdd.spider.mobile_page import wait_for_elements
    from automation.pdd.spider.rank import TAB_TO_CLASS
    from automation.pdd.spider.rank.current import read_tab
    from automation.pdd.spider.full_flow import prepare_top
    from automation.pdd.spider.weekly_mobile import run_products
    existing = store.items(entry['id'])
    client = runtime.ensure_wda()
    xml = open_app_board(client, {'name': entry['source_name'], 'main_name': entry['main_name'], 'url': entry['source_url']},
                         inspect_xml=lambda source: wait_for_elements(lambda: ios._current_page_source(client), initial_xml=source))
    elements = ios._xcui_texts(xml)
    if ios._guess_tab(elements) != entry['tab']:
        try:
            elements = ios._switch_rank_tab(client, entry['tab'], initial_elements=elements)
        except RuntimeError as error:
            raise BoardUnavailableError(f"无法进入目标子榜 {entry['tab']}：{error}") from error
    main = ios._rank_details(elements, TAB_TO_CLASS[entry['tab']])['主榜单名称']
    if main != entry['main_name'] or ios._guess_tab(elements) != entry['tab']:
        raise BoardUnavailableError(f"当前手机榜单与目标榜单不符：目标 {entry['main_name']}/{entry['tab']}，实际 {main}/{ios._guess_tab(elements)}")
    header, rows = read_tab(client, main, entry['tab'], initial_elements=elements)
    store.save_snapshot(entry['id'], rows)
    saved = {i['rank']: i for i in existing}
    targets = [dict(id=saved.get(int(row['排行']), {}).get('id'), rank=int(row['排行']), directory=str(Path(entry['directory']) / f"榜{int(row['排行']):02d}"))
               for row in rows if (int(row['排行']) not in saved or not product_complete(saved[int(row['排行'])]))
               and (product_id is None or saved.get(int(row['排行']), {}).get('id') == product_id)]
    by_rank = {int(r['排行']): r for r in rows}
    for item in existing:
        if not product_complete(item) and item['rank'] not in by_rank:
            raise RuntimeError(f"当前榜单缺少排名 {item['rank']}")
    # Keep the top coordinates paired with the live snapshot for title-based scroll correction.
    client.__dict__['_rank_top_elements'] = elements
    if (targets or not (Path(entry['directory']) / '榜单长图.jpg').is_file()) and (refreshed is None or entry['id'] not in refreshed):
        xml, elements = prepare_top(client, main, entry['tab'], rows, client.__dict__.get('_rank_last_elements', elements))
        client.__dict__['_rank_top_elements'] = elements
        Path(entry['directory']).mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=entry['directory'], prefix='.capture-') as temporary:
            longshot.capture(client, rows, xml, Path(temporary), compact=True)
            for item in targets:
                Path(item['directory']).mkdir(parents=True, exist_ok=True)
                crop = Path(temporary) / f"榜{item['rank']:02d}/产品榜单截图.jpg"
                from automation.pdd.spider.full_flow import valid_image
                if not valid_image(crop):
                    raise RuntimeError(f"排行 {item['rank']} 的榜单裁剪截图无效")
                shutil.copy2(crop, Path(item['directory']) / '产品榜单截图.jpg')
                if item['id'] is not None:
                    store.stage_state(entry['id'], 'ranking_product_screenshot', 'completed', item_id=item['id'])
            output = Path(entry['directory']) / '榜单长图.jpg'
            captured = Path(temporary) / '榜单长图.jpg'
            if not output.exists() or longshot.image_digest(output) != longshot.image_digest(captured):
                shutil.copy2(captured, output)
        if refreshed is not None:
            refreshed.add(entry['id'])
    from automation.pdd.spider.utils import write_xlsx
    write_xlsx(Path(entry['directory']) / '榜单数据.xlsx', header, [[row.get(k, '') for k in header] for row in rows])
    client.__dict__.pop('_rank_last_elements', None)
    run_products(store, client, entry, rows, single_round=True, product_id=product_id,
                 **({'analysis': analysis} if analysis is not None else {}))
    return len(rows)


def check(store, ranking_id=None, product_id=None, capture_date=None):
    rows = store.db.execute(f"""SELECT p.id,p.ranking_id,p.rank,{','.join('p.'+c for c in PRODUCT_COLUMNS)},
        p.platform_product_id,p.platform_product_link,p.listing_date,p.title,
        CASE WHEN a.match_state='completed' THEN a.match_result ELSE COALESCE(a.match_state,'pending') END AS matching,
        a.matched_product_id,COALESCE(a.point_state,'pending') AS points
        FROM t_pdd_ranking_products p LEFT JOIN t_pdd_product_analysis a ON a.ranking_product_id=p.id
        JOIN t_pdd_rankings r ON r.id=p.ranking_id
        WHERE (%s::bigint IS NULL OR p.ranking_id=%s) AND (%s::bigint IS NULL OR p.id=%s)
        AND (%s::text IS NULL OR r.capture_date=%s)
        ORDER BY p.ranking_id,p.rank""", (ranking_id, ranking_id, product_id, product_id, capture_date, capture_date)).fetchall()
    from automation.pdd.spider.health import table
    table('PRODUCT STATUS', ('product', 'ranking', 'rank', 'rank crop', 'screenshot', 'main', 'sku', 'detail', 'comments', 'identity', 'matching', 'source', 'points', 'title'),
          [(r['id'], r['ranking_id'], r['rank'], *(r[c] for c in PRODUCT_COLUMNS),
            'complete' if identity_complete(r) else 'missing', r['matching'], r['matched_product_id'] or '-', r['points'], r['title']) for r in rows],
          (8, 8, 4, 9, 10, 9, 9, 9, 9, 8, 12, 8, 9, 24))


def collect(store, runtime, ranking_id=None, product_id=None):
    analysis = AnalysisRunner(store)
    from automation.pdd.spider.full_flow import valid_image
    from automation.pdd.spider.navigation import BoardUnavailableError
    from automation.pdd.spider.rank import read_leaderboards
    from automation.pdd.spider.database import today
    from automation.pdd.spider.workflow import FatalWorkflowError
    from wda.usbmux.exceptions import HTTPError
    if product_id is not None:
        row = store.db.execute('SELECT ranking_id FROM t_pdd_ranking_products WHERE id=%s', (product_id,)).fetchone()
        if row is None or (ranking_id is not None and ranking_id != row['ranking_id']):
            raise ValueError('指定产品不存在或不属于指定榜单')
        ranking_id = row['ranking_id']
    if ranking_id is not None:
        if not store.db.execute('SELECT 1 FROM t_pdd_rankings WHERE id=%s', (ranking_id,)).fetchone():
            raise ValueError('指定榜单不存在')
        entries = [store.list(ranking_id)]
    else:
        sources = read_leaderboards(config.EXCEL_PATH)
        if not sources:
            raise ValueError('来源 Excel 没有有效榜单')
        entries = store.seed_rankings(today(), sources, config.OUTPUT_ROOT)

    def wait(delay):
        deadline = time.monotonic() + max(1, delay)
        print(f'[榜单] 未完成，等待 {max(1, delay):.0f} 秒后继续；Ctrl+C 可停止', flush=True)
        while time.monotonic() < deadline:
            time.sleep(min(30, max(0, deadline - time.monotonic())))

    for entry in entries:
        try:
            unavailable = False
            refreshed = set()
            expected = entry.get('product_count', 0)
            attempted = False
            store.list_state(entry['id'], 'running')
            while True:
                items = store.items(entry['id'])
                targets = [i for i in items if product_id is None or i['id'] == product_id]
                image_done = valid_image(Path(entry['directory']) / '榜单长图.jpg')
                if not items or not image_done or any(not product_complete(i) for i in targets) or (product_id is None and len(items) < expected):
                    try:
                        attempted = True
                        mobile_round(store, runtime, entry, product_id, refreshed=refreshed, analysis=analysis)
                    except BoardUnavailableError as error:
                        store.list_state(entry['id'], 'unavailable')
                        print(f"[榜单] {entry['main_name']}/{entry['tab']} 暂不可用，本次跳过，下次启动重试：{error}", flush=True)
                        unavailable = True
                        break
                    except (HTTPError, OSError, FatalWorkflowError):
                        raise
                    except Exception as error:
                        store.list_state(entry['id'], 'retry_pending')
                        print(f'[手机] 本轮未完成：{error}', flush=True)
                    expected = store.list(entry['id']).get('product_count', expected)
                items = store.items(entry['id'])
                targets = [i for i in items if product_id is None or i['id'] == product_id]
                image_done = valid_image(Path(entry['directory']) / '榜单长图.jpg')
                if targets and image_done and all(product_complete(i) for i in targets) and (product_id is not None or len(items) >= expected):
                    break
                store.list_state(entry['id'], 'retry_pending')
                wait(config.PRODUCT_RETRY_DELAY)
                store.list_state(entry['id'], 'running')
            if unavailable:
                continue
            if not attempted:
                store.export(entry['id'])
            items = store.items(entry['id'])
            for item in items:
                if product_complete(item) and (product_id is None or item['id'] == product_id):
                    analysis.process(item)
            complete = (bool(items) and all(product_complete(i) and analysis.is_complete(i) for i in items)
                        and len(items) >= store.list(entry['id'])['product_count'])
            failed = any(i[c] == 'failed' for i in items for c in PRODUCT_COLUMNS) or analysis.has_failed(items)
            store.list_state(entry['id'], 'completed' if complete else 'retry_pending' if failed else 'pending')
        except BaseException:
            store.list_state(entry['id'], 'retry_pending')
            raise
    return int(any(analysis.has_failed([i for i in store.items(entry['id']) if product_id is None or i['id'] == product_id])
                   for entry in entries))
