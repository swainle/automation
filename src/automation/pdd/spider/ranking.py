"""榜单采集：默认搜狗插件；--ios 使用手机元素。"""
import argparse
import json
import time
from pathlib import Path

from automation.pdd.spider import config
from automation.pdd.spider.database import today
from automation.pdd.spider.logging_setup import operation as log_operation


class Ranking:
    def __init__(self, store, runtime, device, *, attempts=None, delay=None):
        self.store, self.runtime, self.device = store, runtime, device
        self.attempts = config.RETRY_ATTEMPTS if attempts is None else attempts
        self.delay = config.RETRY_DELAY if delay is None else delay

    def retry(self, day, board, entry, device, stage, operation):
        for attempt in range(self.attempts):
            try:
                labels = {'discover': '发现子榜', 'collect': '采集榜单数据', 'store': '保存榜单数据',
                          'prepare_images': '准备截图页面', 'screenshot': '截取商品卡片',
                          'crop': '裁剪商品卡片', 'share_link': '获取分享链接', 'export': '导出榜单'}
                title = f"[榜单] {board['name']} / {entry['tab']}" if entry else f"[榜单] {board['name']}"
                with log_operation(f"{title}：{labels.get(stage, stage)}（{attempt + 1}/{self.attempts}）"):
                    return operation()
            except Exception as error:
                self.store.event(day, board['name'], entry['tab'] if entry else '', device.name, stage, error)
                if attempt + 1 == self.attempts:
                    raise
                time.sleep(self.delay)

    def process(self, day, board, entry, device):
        from automation.pdd.spider.concurrency import ranking_lock
        with ranking_lock(entry['id']):
            return self._process(day, board, self.store.list(entry['id']), device)

    def _process(self, day, board, entry, device):
        """Keep failures available for an explicit invocation on either device."""
        list_id = entry['id']
        if entry['state'] == 'completed':
            return True
        if entry['device'] and entry['device'] != device.name:
            self.store.reset_list_device(list_id)
            entry = self.store.list(list_id)
        if entry['collect_state'] != 'completed':
            try:
                header, rows, directory = self.retry(day, board, entry, device, 'collect', lambda: device.read(board, entry, day))
            except OSError:
                # A disk error is never a reason to switch devices.
                raise
            except Exception as error:
                self.store.list_state(list_id, 'access_failed', str(error))
                return False
            self.retry(day, board, entry, device, 'store', lambda: self.store.save_rows(list_id, directory, header, rows, device.name))
            entry = self.store.list(list_id)
        owner = device
        try:
            items = self.store.items(list_id)
            needs_page = any(item['screenshot_state'] != 'completed' or item['link_state'] != 'completed'
                             or item['crop_state'] != 'completed'
                             or not (Path(entry['directory']) / f"榜{item['rank']:02d}/排行截图.jpg").is_file()
                             or not (Path(entry['directory']) / f"榜{item['rank']:02d}/产品截图.jpg").is_file() for item in items)
            if needs_page:
                self.retry(day, board, entry, owner, 'prepare_images', lambda: owner.prepare(board, entry, day))
            for item in items:
                row = json.loads(item['row_json'])
                folder = Path(entry['directory']) / f"榜{item['rank']:02d}"
                folder.mkdir(parents=True, exist_ok=True)
                try:
                    fresh_image = item['screenshot_state'] != 'completed' or not (folder / '排行截图.jpg').is_file()
                    if fresh_image:
                        self.retry(day, board, entry, owner, 'screenshot', lambda: owner.screenshot(row))
                        self.store.item_stage(item['id'], 'screenshot')
                    if fresh_image or item['crop_state'] != 'completed' or not (folder / '产品截图.jpg').is_file():
                        self.retry(day, board, entry, owner, 'crop', lambda: owner.crop(row))
                        self.store.item_stage(item['id'], 'crop')
                    if item['link_state'] != 'completed':
                        row = self.retry(day, board, entry, owner, 'share_link', lambda: owner.link(row))
                        self.store.item_stage(item['id'], 'link', row=row)
                finally:
                    if needs_page:
                        owner.finish_item()
            self.retry(day, board, entry, owner, 'export', lambda: self.store.export(list_id))
            self.store.list_state(list_id, 'completed')
            print(f"[榜单] {board['name']} / {entry['tab']}：{owner.name} 完成")
        except Exception as error:
            self.store.list_state(list_id, 'failed', str(error))
            print(f"[榜单] {board['name']} / {entry['tab']}：步骤失败，保留断点：{error}")
        return True

    def discover(self, day, board, device):
        try:
            items = self.retry(day, board, None, device, 'discover', lambda: device.discover(board))
        except Exception as error:
            self.store.board_state(board['id'], 'access_failed', str(error))
            return False
        self.store.discover(board['id'], items)
        return True

    def update_board(self, board):
        states = {entry['state'] for entry in self.store.lists(board['id'])}
        state = 'failed' if 'failed' in states else 'access_failed' if 'access_failed' in states else 'completed'
        self.store.board_state(board['id'], state)

    def run(self, sources, *, day=None):
        day = today() if day is None else day
        if sources and 'main_name' in sources[0]:
            entries = self.store.seed_rankings(day, sources, config.OUTPUT_ROOT)
            try:
                if any(entry['tab'] and entry['state'] != 'completed' for entry in entries):
                    if self.device.name == 'ios':
                        self.runtime.ensure_tunnel()
                        self.runtime.ensure_image()
                        self.runtime.ensure_driver()
                        self.runtime.ensure_forward()
                    else:
                        self.runtime.start_ranking_browser()
                for source, entry in zip(sources, entries):
                    if not entry['tab'] or entry['state'] == 'completed':
                        continue
                    board = {'id': entry['board_id'], 'name': source['name'],
                             'main_name': source['main_name'], 'url': source['url']}
                    self.process(day, board, entry, self.device)
                self.store.finish(day)
                return int(any(self.store.list(entry['id'])['state'] != 'completed' for entry in entries))
            finally:
                self.runtime.close()
        boards = self.store.seed(day, sources)
        try:
            if self.device.name == 'ios':
                print('[环境] 准备 iOS 隧道、镜像、WDA 驱动和端口转发')
                self.runtime.ensure_tunnel()
                self.runtime.ensure_image()
                self.runtime.ensure_driver()
                self.runtime.ensure_forward()
            else:
                self.runtime.start_ranking_browser()
            for board in boards:
                if board['state'] == 'completed':
                    continue
                if not self.store.lists(board['id']) and not self.discover(day, board, self.device):
                    print(f"[榜单] {board['name']}：{self.device.name} 无法访问，记录后跳过")
                    continue
                for entry in self.store.lists(board['id']):
                    self.process(day, board, entry, self.device)
                self.update_board(board)
            while True:
                pending = [(board, entry) for board in boards for entry in self.store.lists(board['id']) if entry['state'] == 'failed']
                if not pending:
                    break
                print(f'[榜单] {len(pending)} 个子榜等待原步骤续采，Ctrl+C 可停止')
                time.sleep(self.delay)
                for board, entry in pending:
                    self.process(day, board, entry, self.device)
                    self.update_board(board)
            self.store.finish(day)
            failed = sum(board['state'] != 'completed' for board in self.store.boards(day))
            print(f'[榜单] 本轮处理结束；失败主榜 {failed} 个，详细状态见 sqlite.db')
            return 0
        finally:
            self.runtime.close()


def main(argv=None):
    from automation.pdd.spider.commands import execute
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ios', action='store_true', help='使用手机元素采集；默认使用搜狗浏览器插件')
    parser.add_argument('--ranking-id', type=int, help='配合 --ios 只采集数据库中指定 ID 的榜单')
    parser.add_argument('--current', action='store_true', help='配合 --ios 只采集手机当前打开的榜单')
    parser.add_argument('--source-name', help='--ios --current 的来源分类名')
    parser.add_argument('--refresh', action='store_true', help='配合 --ios 刷新全部榜单数据、图片及分享链接')
    args = parser.parse_args(argv)
    if args.ranking_id is not None:
        if args.ranking_id < 1:
            parser.error('--ranking-id 必须为正整数')
        if not args.ios or args.current or args.source_name:
            parser.error('--ranking-id 需要 --ios，且不能与 --current、--source-name 同时使用')
    if args.ios:
        from automation.pdd.spider.rank.current import collect, collect_sources
        if args.source_name and not args.current:
            parser.error('--source-name 仅用于 --current 模式')
        return execute(__doc__, lambda store, runtime: (
            collect(store, runtime, source_name=args.source_name, full=True,
                    refresh=args.refresh) if args.current else
            collect_sources(store, runtime, refresh=args.refresh, ranking_id=args.ranking_id)), args=args, role='rank')
    if args.current or args.source_name or args.refresh:
        parser.error('--current、--source-name、--refresh 需要配合 --ios')

    def collect(store, runtime):
        from automation.pdd.spider.devices import Sogou
        from automation.pdd.spider.rank import read_leaderboards
        sources = read_leaderboards(config.EXCEL_PATH)
        if not sources:
            raise ValueError('来源 Excel 没有有效榜单名称和链接')
        device = Sogou(runtime)
        return Ranking(store, runtime, device).run(sources)

    return execute(__doc__, collect, args=args, role='rank')


if __name__ == '__main__':
    raise SystemExit(main())
