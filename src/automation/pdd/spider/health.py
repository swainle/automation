"""Read-only environment checks and terminal status panels."""
import json
import os
from pathlib import Path
import re
import unicodedata
import urllib.request

import psutil
import psycopg

from automation.pdd.spider import config, ios
from automation.pdd.spider.runtime import Runtime, port_ready
from automation.pdd.spider.mobile_database import connect, columns, TABLES, PRODUCT_COLUMNS, PRODUCT_DONE_SQL, PRODUCT_SKIP_SQL, IDENTITY_DONE_SQL


def table(title, headers, rows, widths):
    def cell(value, width):
        text, used = '', 0
        for char in ' '.join(str(value).split()):
            if not char.isprintable():
                continue
            size = 2 if unicodedata.east_asian_width(char) in 'WF' else 1
            if used + size > width:
                break
            text, used = text + char, used + size
        return text + ' ' * (width - used)
    border = '+' + '+'.join('-' * (width + 2) for width in widths) + '+'
    print(border)
    print('| ' + cell(title, len(border) - 4) + ' |')
    print(border)
    for row in (headers, *rows):
        print('| ' + ' | '.join(cell(value, width) for value, width in zip(row, widths)) + ' |')
        if row is headers:
            print(border)
    print(border)


def business_paths():
    if config.OUTPUT_ROOT.exists() and not config.OUTPUT_ROOT.is_dir():
        return 'FAIL', 'Output path is not a directory'
    if (config.BUSINESS_ROOT / '.env').exists() and not (config.BUSINESS_ROOT / '.env').is_file():
        return 'FAIL', 'Config path is not a file'
    missing = [name for name, path in [('config', config.BUSINESS_ROOT / '.env'), ('output', config.OUTPUT_ROOT)]
               if not path.exists()]
    return ('WARN', 'Missing: ' + ', '.join(missing)) if missing else ('OK', 'Config / output paths ready')


def source_excel():
    from automation.pdd.spider.rank import read_leaderboards
    count = len(read_leaderboards(config.EXCEL_PATH))
    return ('OK', f'{count} rankings') if count else ('FAIL', 'No valid rankings')


def ios_device():
    output = ios._ios('list', timeout=5)
    selected = os.getenv('SPIDER_IOS_UDID', '').strip()
    connected = selected in output if selected else bool(re.search(
        r'"(?:deviceList|Devices)"\s*:\s*\[\s*[^\s\]]|"(?:Udid|udid|UDID)"\s*:\s*"[^" ]+', output))
    return ('OK', 'Device connected') if connected else ('FAIL', 'Selected device not connected' if selected else 'No device connected')


def wda_status():
    try:
        with urllib.request.urlopen(f'{ios.WDA_URL}/status', timeout=3) as response:
            ready = json.load(response).get('value', {}).get('ready') is True
    except (OSError, ValueError):
        ready = False
    return ('OK', 'WDA ready') if ready else ('WARN', 'Not ready; starts on collection')


def sogou_browser():
    expected = Path(config.SOGOU_EXECUTABLE).resolve()
    if not expected.is_file():
        return 'FAIL', 'Configured executable missing'
    for process in psutil.process_iter(['exe']):
        try:
            if process.info['exe'] and Path(process.info['exe']).resolve() == expected:
                return 'OK', 'Browser running'
        except psutil.Error:
            continue
    return 'WARN', 'Not running; starts on collection'


def websocket_status():
    if not 1024 <= config.RANKING_WS_PORT <= 65535:
        return 'FAIL', 'Invalid WebSocket port'
    return ('OK', f'127.0.0.1:{config.RANKING_WS_PORT}') if port_ready(config.RANKING_WS_PORT) else ('WARN', 'Not running; starts on collection')


def sogou_plugin():
    if not 1024 <= config.RANKING_WS_PORT <= 65535 or not port_ready(config.RANKING_WS_PORT):
        return 'WARN', 'Bridge unavailable; plugin not checked'
    runtime = Runtime()
    status = runtime.ranking_request('status', timeout=3)
    if status.get('protocol') != 3:
        return 'FAIL', 'Bridge protocol mismatch'
    if not status.get('connected'):
        return 'FAIL', 'Sogou plugin not connected'
    runtime._verify_ranking_browser(status.get('extension_peer_port'))
    return 'OK', 'Connected; Sogou process verified'


def postgres_connection():
    if not config.POSTGRES_URL:
        return 'FAIL', 'SPIDER_POSTGRES_URL not configured'
    with connect(readonly=True) as db:
        if db.execute('SELECT 1 AS value').fetchone()['value'] != 1:
            return 'FAIL', 'Unexpected connection test result'
        if any(not columns(db, table) for table in TABLES):
            return 'FAIL', 'ods.t_pdd_* tables missing'
        if not set(PRODUCT_COLUMNS) <= columns(db, 't_pdd_ranking_products') or 'state' not in columns(db, 't_pdd_rankings'):
            return 'FAIL', 'Collection state columns missing'
    return 'OK', 'Connected; ods.t_pdd_* tables ready'


def environment_checks():
    rows = []
    for name, probe in [('Business paths', business_paths), ('Source Excel', source_excel),
                        ('iOS device', ios_device), ('WDA', wda_status), ('PostgreSQL', postgres_connection)]:
        try:
            state, detail = probe()
        except Exception as error:
            # Connection errors can include credentials; expose only their type.
            state, detail = 'FAIL', f'Check failed ({type(error).__name__})'
        rows.append((name, state, detail))
    return rows


def collection_status(db, ranking_id=None, product_id=None, capture_date=None):
    total = 'GREATEST(COUNT(p.id),r.product_count)' if product_id is None and 'product_count' in columns(db, 't_pdd_rankings') else 'COUNT(p.id)'
    rows = db.execute(f"""SELECT r.id,r.main_name,r.tab_name,r.state,{total} AS total,
        COUNT(p.id) FILTER (WHERE {PRODUCT_DONE_SQL}) AS done,
        COUNT(p.id) FILTER (WHERE {PRODUCT_SKIP_SQL}) AS skipped,
        COUNT(p.id) FILTER (WHERE {IDENTITY_DONE_SQL}) AS matched_done,
        COUNT(p.id) FILTER (WHERE EXISTS(SELECT 1 FROM t_pdd_selling_point sp WHERE sp.ranking_product_id=p.id)) AS point_done,
        COUNT(p.id) FILTER (WHERE NOT {PRODUCT_SKIP_SQL} AND ({' OR '.join(f"p.{c}='failed'" for c in PRODUCT_COLUMNS)}
            OR (a.match_state='failed' AND NOT ({IDENTITY_DONE_SQL}))
            OR (a.point_state='failed' AND NOT EXISTS(SELECT 1 FROM t_pdd_selling_point sp WHERE sp.ranking_product_id=p.id)))) AS failed
        FROM t_pdd_rankings r LEFT JOIN t_pdd_ranking_products p ON p.ranking_id=r.id
        LEFT JOIN t_pdd_product_analysis a ON a.ranking_product_id=p.id
        WHERE (%s::bigint IS NULL OR r.id=%s) AND (%s::bigint IS NULL OR p.id=%s)
        AND (%s::text IS NULL OR r.capture_date=%s)
        GROUP BY r.id ORDER BY r.id""", (ranking_id, ranking_id, product_id, product_id, capture_date, capture_date)).fetchall()
    output = [(r['id'], f"{r['main_name']}/{r['tab_name']}", r['state'], f"{r['done']}/{r['total']}",
               f"{r['matched_done']}/{r['total']}",
               f"{r['point_done']}/{r['total']}", r['skipped'], r['failed']) for r in rows]
    table('COLLECTION STATUS', ('Rank ID', 'Name', 'State', 'Collected', 'Matched', 'Points', 'Skipped', 'Failed'),
          output, (7, 24, 13, 9, 9, 9, 7, 6))


def run(ranking_id=None, product_id=None, capture_date=None):
    rows = environment_checks()
    table('PDD SPIDER CHECK / ENVIRONMENT', ('Item', 'State', 'Detail'), rows, (16, 5, 46))
    failed = any(state == 'FAIL' for _, state, _ in rows)
    try:
        with connect(readonly=True) as db:
            collection_status(db, ranking_id, product_id, capture_date)
    except (psycopg.Error, ValueError) as error:
        table('COLLECTION STATUS', ('State', 'Detail'), [('FAIL', f'Cannot read status ({type(error).__name__})')], (5, 65))
        failed = True
    return int(failed)
