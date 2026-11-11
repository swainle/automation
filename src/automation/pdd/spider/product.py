"""采集当天榜单商品；默认搜狗插件，--android 使用安卓拼多多 App。"""
import argparse
import csv
import json
import random
import time
from pathlib import Path

from automation.pdd.spider import config
from automation.pdd.spider.database import today


def artifacts(directory):
    from automation.pdd.spider.status import load_status
    result = {'status': load_status(directory), 'files': []}
    for path in sorted(directory.rglob('*')):
        if path.is_file() and path.suffix.lower() in {'.jpg', '.png', '.json', '.csv'}:
            result['files'].append(str(path.relative_to(directory)))
    for filename, key in [('SKU.csv', 'sku'), ('差评.csv', 'negative_comments')]:
        path = directory / filename
        if path.is_file():
            with path.open(encoding='utf-8-sig', newline='') as stream:
                result[key] = list(csv.DictReader(stream))
    info = directory / '产品信息.json'
    if info.is_file():
        result['info'] = json.loads(info.read_text(encoding='utf-8'))
    for filename, key in [('图片清单.json', 'images'), ('差评原始.json', 'negative_comments')]:
        path = directory / filename
        if path.is_file():
            result[key] = json.loads(path.read_text(encoding='utf-8'))
    return result


class Product:
    def __init__(self, store, runtime, *, delay=None, item_delay=(3, 8), ranking_id=None):
        self.store, self.runtime = store, runtime
        self.delay = config.PRODUCT_RETRY_DELAY if delay is None else delay
        self.item_delay = item_delay
        self.ranking_id = ranking_id

    def collect(self, task):
        if getattr(self.runtime, 'native_android', False) is True:
            from automation.pdd.spider.native_product import NativeProduct
            collector = NativeProduct(self.runtime.ensure_android())
            collector.bind_store(self.store, task)
            return collector.collect(task)
        from automation.pdd.spider.product_extension import ExtensionProduct
        from automation.pdd.spider.artifacts import write_share_link

        def identified(goods_id):
            self.store.fill_product_id(task['id'], goods_id)
            current = self.store.item(task['id'])
            task.update(goods_id=current['goods_id'], row_json=current['row_json'])
            write_share_link(Path(task['directory']) / f"榜{task['rank']:02d}", json.loads(task['row_json']))

        return ExtensionProduct(self.runtime, on_identity=identified,
                                on_image=lambda record: self.store.save_product_image(task['id'], record)).collect(task)

    def export(self, tasks):
        from automation.pdd.spider.utils import write_xlsx
        groups = {}
        for task in tasks:
            current = self.store.item(task['id'])
            row = json.loads(current['row_json'])
            details = json.loads(current['product_json'])
            row.update({'采集状态': current['product_state'], '异常原因': current['reason'],
                        'SKU数量': len(details.get('sku', [])), '评论数量': len(details.get('comments', [])),
                        '好评数量': len(details.get('positive_comments', [])),
                        '差评数量': len(details.get('negative_comments', [])),
                        '产品信息': str(Path(task['directory']) / f"榜{task['rank']:02d}/产品信息.json") if details.get('info') else ''})
            groups.setdefault(task['directory'], []).append(row)
        header = ['排行', '产品ID', '产品链接', '标题', '价格', '上架日期', '采集状态', '异常原因', 'SKU数量', '差评数量', '产品信息']
        for folder, rows in groups.items():
            write_xlsx(Path(folder) / '商品数据.xlsx', header, [[row.get(key, '') for key in header] for row in rows])

    def run(self):
        from contextlib import ExitStack
        from automation.pdd.spider.concurrency import ranking_active, ranking_lock
        day, connected, waiting = today(), False, False
        if self.ranking_id is not None:
            if not self.store.db.execute('SELECT 1 FROM rankings WHERE id=?', (self.ranking_id,)).fetchone():
                raise ValueError(f'榜单 ID 不存在：{self.ranking_id}')
            print(f'[商品] 只采集榜单 ID {self.ranking_id} 的商品')
        else:
            print(f'[商品] 持续读取 {day} 的榜单商品，榜单采集期间自动接收新链接')
        queue = {'ranking_id': self.ranking_id} if self.ranking_id is not None else {'day': day}
        try:
            while True:
                # Check producer before reading the queue, so its last committed rows are drained.
                producing = self.ranking_id is None and ranking_active()
                tasks = self.store.product_tasks(**queue)
                pending = False
                for task in tasks:
                    with ExitStack() as owned:
                        try:
                            owned.enter_context(ranking_lock(task['ranking_list_id'], timeout=0))
                        except TimeoutError:
                            pending = True
                            continue
                        # A ranking refresh may have replaced this position while we waited.
                        fresh = self.store.product_tasks(**queue)
                        current = next((row for row in fresh if row['id'] == task['id']), None)
                        if current is None:
                            continue
                        group = [row for row in fresh if row['ranking_list_id'] == current['ranking_list_id']]
                        if current['product_state'] in {'completed', 'skipped', 'blocked_webview'}:
                            self.export(group)
                            continue
                        if not connected:
                            self.runtime.connect_product_browser()
                            connected = True
                        self.runtime.processes.check()
                        self.store.product_state(current['id'], 'running')
                        try:
                            print(f"[商品] 榜单 {current['ranking_list_id']} 排行 {current['rank']} 产品链接：{json.loads(current['row_json'])['产品链接']}", flush=True)
                            state, details = self.collect(current)
                        except Exception as error:
                            directory = Path(current['directory']) / f"榜{current['rank']:02d}"
                            self.store.product_state(current['id'], 'pending', details=artifacts(directory), reason=str(error))
                            print(f"[商品] 排行 {current['rank']} 失败，稍后重试：{error}")
                            pending = True
                        else:
                            reason = details.get('status', {}).get('stages', {}).get('product', {}).get('reason', '') if state == 'skipped' else ''
                            if state == 'blocked_webview':
                                reason = '商品页面只有空 WebView，待统一处理'
                            self.store.product_state(current['id'], state, details=details, reason=reason)
                            pending |= state not in {'completed', 'skipped', 'blocked_webview'}
                            time.sleep(random.uniform(*self.item_delay))
                        self.export(group)
                if not pending and not producing:
                    # Re-read after work: rank may have added products while we collected.
                    remaining = self.store.product_tasks(**queue)
                    if not any(row['product_state'] not in {'completed', 'skipped', 'blocked_webview'} for row in remaining) and (self.ranking_id is not None or not ranking_active()):
                        print('[商品] 所选商品已处理完毕（含已标记待处理商品），退出')
                        return 0
                    continue
                if producing:
                    if not waiting:
                        print('[商品] 榜单仍在采集，等待新链接或子榜释放；每 5 秒检查')
                    waiting = True
                    time.sleep(5)
                else:
                    waiting = False
                    print(f'[商品] 等待 {self.delay:g} 秒后续采未完成商品，Ctrl+C 可停止')
                    time.sleep(self.delay)
        finally:
            self.runtime.close()


def main(argv=None):
    from automation.pdd.spider.commands import execute
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--android', action='store_true', help='使用安卓拼多多原生 App，复用 ADB 和 UiAutomator 连接')
    parser.add_argument('--ranking-id', type=int, help='只采集指定数据库榜单 ID 的商品，不限日期')
    parser.add_argument('--current', action='store_true', help='配合 --android 采集当前已打开商品，不遍历榜单')
    parser.add_argument('--jump', metavar='URL', help='用安卓 Chrome 打开链接、记录网页响应并跳转拼多多 App')
    parser.add_argument('--image-sku', action='store_true', help='--android --current 只采集 SKU 规格、价格和图片')
    parser.add_argument('--image-main', action='store_true', help='--android --current 只采集主图')
    parser.add_argument('--image-detail', action='store_true', help='--android --current 只采集详情图')
    parser.add_argument('--comment', action='store_true', help='--android --current 只采集差评，默认最多 100 条')
    parser.add_argument('--comment-limit', type=int, default=100, help='--current 最多读取差评条数（默认 100）')
    args = parser.parse_args(argv)
    if args.ranking_id is not None:
        if args.ranking_id < 1:
            parser.error('--ranking-id 必须为正整数')
        if args.current or args.jump:
            parser.error('--ranking-id 不能与 --current 或 --jump 同时使用')
    if args.jump:
        if not args.android or args.current or args.image_sku or args.image_main or args.image_detail or args.comment:
            parser.error('--jump 需要 --android，且不能与 --current 或分项采集参数同时使用')
        from automation.pdd.spider.android_jump import validate_url
        try:
            validate_url(args.jump)
        except ValueError as error:
            parser.error(str(error))
    if args.current and not args.android:
        parser.error('--current 需要配合 --android')
    if args.comment_limit < 1:
        parser.error('--comment-limit 必须为正整数')
    steps = {name for name, selected in (('sku', args.image_sku), ('image_main', args.image_main), ('image_detail', args.image_detail), ('comment', args.comment)) if selected}
    if steps and not (args.android and args.current):
        parser.error('--image-sku、--image-main、--image-detail、--comment 需要配合 --android --current')
    def run(store, runtime):
        if args.jump:
            from automation.pdd.spider.android_jump import jump
            jump(args.jump)
            return 0
        if args.current:
            from automation.pdd.spider.native_product import NativeProduct
            collector = NativeProduct(runtime.ensure_android(), comment_limit=args.comment_limit)
            if steps:
                collector.collect_current(steps=steps)
            else:
                collector.collect_current()
            return 0
        return Product(store, runtime, ranking_id=args.ranking_id).run()
    return execute(__doc__, run, args=args, role='product')


if __name__ == '__main__':
    raise SystemExit(main())
