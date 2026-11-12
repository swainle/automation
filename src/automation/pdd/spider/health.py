"""Read-only environment checks and terminal status panels."""
from collections import Counter
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
import unicodedata
import urllib.request

import psutil
import psycopg

from automation.pdd.spider import config, ios
from automation.pdd.spider.runtime import Runtime, port_ready
from automation.pdd.spider.mobile_database import connect, columns, TABLES
from automation.pdd.spider.time_format import BEIJING


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
        if not {'mobile_state', 'sogou_state'} <= columns(db, 't_pdd_ranking_products'):
            return 'FAIL', 'Collection state columns missing'
    return 'OK', 'Connected; ods.t_pdd_* tables ready'


def environment_checks():
    rows = []
    for name, probe in [('Business paths', business_paths), ('Source Excel', source_excel),
                        ('iOS device', ios_device), ('WDA', wda_status), ('Sogou', sogou_browser),
                        ('WebSocket', websocket_status), ('Sogou plugin', sogou_plugin), ('PostgreSQL', postgres_connection)]:
        try:
            state, detail = probe()
        except Exception as error:
            # Connection errors can include credentials; expose only their type.
            state, detail = 'FAIL', f'Check failed ({type(error).__name__})'
        rows.append((name, state, detail))
    return rows


def collection_status(db, ranking_id=None, product_id=None):
    rows = db.execute('''SELECT r.id,r.main_name,r.tab_name,r.web_collected_at,COUNT(p.id) AS total,
        COUNT(p.id) FILTER (WHERE p.mobile_state='completed') AS mobile_done,
        COUNT(p.id) FILTER (WHERE p.sogou_state='completed') AS web_done
        FROM t_pdd_rankings r LEFT JOIN t_pdd_ranking_products p ON p.ranking_id=r.id
        WHERE (%s::bigint IS NULL OR r.id=%s) AND (%s::bigint IS NULL OR p.id=%s)
        GROUP BY r.id ORDER BY r.id''', (ranking_id, ranking_id, product_id, product_id)).fetchall()
    now = datetime.now(BEIJING)
    output = []
    for row in rows:
        rid, main, tab, attempted, total, mobile_done, web_done = row.values()
        next_web = '-'
        if mobile_done > web_done:
            due = datetime.fromisoformat(attempted) + timedelta(minutes=30) if attempted else now
            next_web = 'READY' if due <= now else due.astimezone(BEIJING).strftime('%m-%d %H:%M:%S')
        output.append((rid, f'{main}/{tab}', f'{mobile_done}/{total}', f'{web_done}/{total}', next_web))
    table('COLLECTION STATUS', ('Rank ID', 'Name', 'Mobile', 'Web', 'Next web'), output, (7, 24, 9, 9, 14))
    if ranking_id is not None or product_id is not None:
        from automation.pdd.spider.unified import check
        check(SimpleNamespace(db=db), ranking_id, product_id)


def run(ranking_id=None, product_id=None):
    rows = environment_checks()
    table('PDD SPIDER CHECK / ENVIRONMENT', ('Item', 'State', 'Detail'), rows, (16, 5, 46))
    counts = Counter(row[1] for row in rows)
    print(f"RESULT: {counts['OK']} OK / {counts['WARN']} WARN / {counts['FAIL']} FAIL\n")
    failed = bool(counts['FAIL'])
    try:
        with connect(readonly=True) as db:
            collection_status(db, ranking_id, product_id)
    except (psycopg.Error, ValueError) as error:
        table('COLLECTION STATUS', ('State', 'Detail'), [('FAIL', f'Cannot read status ({type(error).__name__})')], (5, 65))
        failed = True
    return int(failed)
