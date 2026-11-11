"""Supplement existing mobile products using the selected browser's extension."""
import argparse
import os
from pathlib import Path
import shutil
import time

from automation.pdd.spider import config
from automation.pdd.spider.database import today
from automation.pdd.spider.mobile_database import week_start
from automation.pdd.spider.unified import remaining_web_wait, web_round


def browser_settings(browser):
    if browser == 'sogou':
        return config.SOGOU_EXECUTABLE, config.USER_DATA_DIR
    if browser == 'chrome':
        from automation.com.browser import chrome_executable
        executable = os.getenv('SPIDER_CHROME_EXECUTABLE') or chrome_executable()
        directory = Path(os.environ['LOCALAPPDATA']) / 'Google/Chrome/User Data'
    else:
        candidates = [shutil.which('msedge')]
        candidates.extend(str(Path(os.environ[base]) / 'Microsoft/Edge/Application/msedge.exe')
                          for base in ('PROGRAMFILES(X86)', 'PROGRAMFILES', 'LOCALAPPDATA') if base in os.environ)
        executable = os.getenv('SPIDER_EDGE_EXECUTABLE') or next(
            (path for path in candidates if path and Path(path).is_file()), None)
        if not executable:
            raise FileNotFoundError('未找到 Microsoft Edge，请配置 SPIDER_EDGE_EXECUTABLE')
        directory = Path(os.environ['LOCALAPPDATA']) / 'Microsoft/Edge/User Data'
    return executable, os.getenv(f'SPIDER_{browser.upper()}_USER_DATA_DIR', str(directory))


def collect(store, runtime, ranking_id=None, product_id=None):
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
        entries = [store.list(row['id']) for row in store.db.execute(
            'SELECT id FROM t_pdd_rankings WHERE week_start=%s ORDER BY source_order,id',
            (week_start(today()),)).fetchall()]
    pending = entries
    while pending:
        waiting = []
        for entry in pending:
            entry = store.list(entry['id'])
            targets = [item for item in store.items(entry['id']) if item['sogou_state'] != 'completed'
                       and (product_id is None or item['id'] == product_id)]
            if targets:
                web_round(store, runtime, entry, product_id)
                store.export(entry['id'])
            items = store.items(entry['id'])
            complete = items and all(i['mobile_state'] == i['sogou_state'] == 'completed' for i in items)
            store.list_state(entry['id'], 'completed' if complete else 'pending')
            if any(i['sogou_state'] != 'completed'
                   and (product_id is None or i['id'] == product_id) for i in items):
                waiting.append(store.list(entry['id']))
        pending = waiting
        if pending:
            delay = max(1, min(remaining_web_wait(entry) for entry in pending))
            print(f'[网页] 等待 {delay:.0f} 秒后重试；Ctrl+C 可停止并保留窗口', flush=True)
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                time.sleep(min(30, max(0, deadline - time.monotonic())))
    runtime._ranking_keep_open = False
    return 0


def main(browser, argv=None):
    parser = argparse.ArgumentParser(description=f'通过 {browser} 补采产品 ID、链接和上架日期；成功后关闭任务窗口')
    parser.add_argument('--ranking-id', type=int)
    parser.add_argument('--ranking-product-id', type=int)
    args = parser.parse_args(argv)
    if any(value is not None and value < 1 for value in (args.ranking_id, args.ranking_product_id)):
        parser.error('数据库 ID 必须大于零')
    from automation.pdd.spider.commands import execute

    def run(store, runtime):
        runtime.ranking_browser = browser
        runtime.ranking_executable, runtime.ranking_user_data_dir = browser_settings(browser)
        runtime.ranking_window = True
        return collect(store, runtime, args.ranking_id, args.ranking_product_id)

    return execute(f'{browser} 网页产品信息采集', run, args=args, role='web')
