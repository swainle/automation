"""Collect Excel leaderboard links or the current iOS leaderboard."""
from automation.pdd.spider.logging_setup import timed_operation
import argparse
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET
from wda.usbmux.exceptions import HTTPError

from automation.pdd.spider import config, ios
from automation.pdd.spider.database import today
from automation.pdd.spider.rank import TAB_TO_CLASS
from automation.pdd.spider.utils import sanitize


def save_diagnostics(xml, elements):
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S-%f')
    directory = config.BUSINESS_ROOT / 'data/diagnostics'
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / f'rank-{stamp}.xml'
    source.write_text(xml or '', encoding='utf-8')
    counts = {}
    if xml:
        for node in ET.fromstring(xml).iter():
            kind = node.tag.replace('XCUIElementType', '')
            counts[kind] = counts.get(kind, 0) + 1
    source.with_suffix('.json').write_text(json.dumps({'node_types': counts,
        'texts': [element['text'] for element in elements]}, ensure_ascii=False, indent=2), encoding='utf-8')
    return source


@timed_operation('[榜单] 识别当前榜单')
def current_board(client):
    foreground = client.http.get('/wda/activeAppInfo', timeout=3).value.get('bundleId')
    if foreground != ios.PDD_BUNDLE_ID:
        raise RuntimeError('请先手动打开拼多多 App 的榜单页面，并保持在前台')
    xml = ios._current_page_source(client)
    if not xml:
        raise RuntimeError('WDA 未返回页面元素树，请检查 WDA 日志与设备连接')
    elements = ios._xcui_texts(xml) if xml else []
    tab = ios._guess_tab(elements)
    nodes = list(ET.fromstring(xml).iter())
    if any(node.tag.endswith('WebView') for node in nodes) and not any(node.tag.endswith('StaticText') for node in nodes):
        path = save_diagnostics(xml, elements)
        title = next((element['text'] for element in elements if element.get('type') == 'NavigationBar'), tab or '')
        raise RuntimeError(f'当前榜单“{title}”的 WDA 元素树只有 WebView 容器，没有商品文本节点，当前 iOS 元素方式无法采集；诊断文件：{path}')
    if tab not in TAB_TO_CLASS:
        path = save_diagnostics(xml, elements)
        raise RuntimeError(f'当前页面未识别到榜单标题或指标，请检查诊断文件：{path}')
    details = ios._rank_details(elements, TAB_TO_CLASS[tab])
    main = details['主榜单名称']
    if not main:
        raise RuntimeError('当前页面未识别到主榜单名称，停止采集')
    client.__dict__['_rank_last_elements'] = elements
    client.__dict__['_rank_page_source'] = xml
    tabs = list(dict.fromkeys(e['text'] for e in elements if e['text'] in TAB_TO_CLASS)) or [tab]
    return main, tabs, bool(any(e['text'] in TAB_TO_CLASS for e in elements))


@timed_operation('[榜单] 解析当前子榜商品')
def read_tab(client, main, tab, *, initial_elements=None):
    """Parse the first complete snapshot; never drag or fetch it again if cached."""
    cls = TAB_TO_CLASS[tab]
    if initial_elements is None:
        xml = ios._current_page_source(client)
        if not xml:
            raise RuntimeError('WDA 未返回榜单元素树')
        elements = ios._xcui_texts(xml)
        client.__dict__['_rank_page_source'] = xml
    else:
        elements = initial_elements
        client.__dict__['_rank_page_source'] = client.__dict__.get('_last_page_source')
    client.__dict__['_rank_last_elements'] = elements
    details = ios._rank_details(elements, cls)
    if details['主榜单名称'] != main or ios._guess_tab(elements) != tab:
        raise RuntimeError('当前快照的榜单或 Tab 不一致，未写入数据库')
    if not any(any(marker in element['text'] for marker in ios._END_MARKS) for element in elements):
        raise RuntimeError('首次元素树未包含榜单结束分界，无法确认全榜已加载；本命令不拖动，未写入不完整数据')
    rows = ios._parse_rows(elements, cls)
    if not rows:
        raise RuntimeError('当前子榜未解析出商品')
    print(f'[榜单] {tab}：直接解析首次完整快照，共 {len(rows)} 个商品，未执行拖动')
    return ios._ios_rank_header(cls), rows


def completed(store, entry, product_collector):
    if entry['state'] != 'completed':
        return False
    if product_collector is None:
        return True
    items = store.items(entry['id'])
    return bool(items) and all(item['product_state'] == 'completed'
                               and item['screenshot_state'] == 'completed'
                               and json.loads(item['product_json'] or '{}').get('info', {}).get('image_method') == 'ios_enlarged_screenshot_crop'
                               for item in items)


def collect(store, runtime, *, source_name=None, full=False, refresh=False, entry=None, product_collector=None):
    client = runtime.ensure_wda()
    main, tabs, has_sub = current_board(client)
    source = entry['source_name'] if entry else source_name or main
    day = entry['capture_date'] if entry else today()
    if entry:
        if main != entry['main_name'] or entry['tab'] not in tabs:
            raise RuntimeError(f"页面主榜或子榜与 Excel C/D 不对应：{main}/{entry['tab']}")
        entries = [entry]
        board_id = entry['board_id']
    else:
        board = store.seed(day, [{'name': source, 'url': ''}])[0]
        from automation.pdd.spider.collectors import RankingItem
        store.discover(board['id'], [RankingItem(source, tab=tab, index=index, has_sub=has_sub) for index, tab in enumerate(tabs)])
        entries = store.lists(board['id'])
        board_id = board['id']
    failed = False
    elements = client.__dict__.get('_rank_last_elements')
    for entry in entries:
        from automation.pdd.spider.concurrency import ranking_lock
        with ranking_lock(entry['id']):
            entry = store.list(entry['id'])
            if completed(store, entry, product_collector) and not refresh:
                print(f"[榜单] {main} / {entry['tab']}：复用当天已完成记录")
                continue
            try:
                if has_sub and (not isinstance(elements, list) or ios._guess_tab(elements) != entry['tab']):
                    switched = ios._switch_rank_tab(client, entry['tab'], initial_elements=elements if isinstance(elements, list) else None)
                    elements = switched if isinstance(switched, list) else None
                header, rows = read_tab(client, main, entry['tab'], initial_elements=elements)
                elements = client.__dict__.get('_rank_last_elements')
                for row in rows:
                    row['主榜单名称'] = main
                    row['子榜单名称'] = entry['tab']
                directory = Path(entry['directory']) if entry.get('directory') else config.OUTPUT_ROOT / sanitize(main) / sanitize(entry['tab']) / day
                store.save_rows(entry['id'], directory, header, rows, 'ios', update_existing=True, latest_snapshot=True)
                if full:
                    from automation.pdd.spider.full_flow import IosFullFlow
                    IosFullFlow(store, client, refresh=refresh, latest_snapshot=True, **({'product_collector': product_collector} if product_collector else {})).run_list(entry['id'], rows, elements)
                    elements = client.__dict__.get('_rank_last_elements')
                store.list_state(entry['id'], 'completed')
                print(f"[榜单] {main} / {entry['tab']}：已入库 {len(rows)} 个商品")
            except Exception as error:
                failed = True
                store.list_state(entry['id'], 'failed', str(error))
                store.event(day, source, entry['tab'], 'ios', 'collect', error)
                print(f"[榜单] {entry['tab']} 采集失败：{error}")
                from automation.pdd.spider.workflow import FatalWorkflowError
                if isinstance(error, FatalWorkflowError):
                    raise
    store.board_state(board_id, 'failed' if failed else 'completed')
    if not failed:
        store.finish(day)
    return int(failed)


def main(argv=None):
    parser = argparse.ArgumentParser(description='循环 Excel 榜单链接，通过 Safari 打开拼多多并完整采集；一轮后退出')
    parser.add_argument('--ios', action='store_true', required=True)
    parser.add_argument('--ranking-id', type=int, help='只采集数据库中指定 ID 的榜单')
    parser.add_argument('--current', action='store_true', help='只采集手机当前打开的榜单')
    parser.add_argument('--once', action='store_true', default=True, help=argparse.SUPPRESS)
    parser.add_argument('--source-name', help='来源分类名，默认使用页面主榜名称')
    parser.add_argument('--full', action='store_true', default=True, help=argparse.SUPPRESS)
    parser.add_argument('--refresh', action='store_true', help='刷新全部榜单数据、图片及分享链接；默认按断点续采')
    options = parser.parse_args(argv)
    from automation.pdd.spider.commands import execute
    if options.ranking_id is not None:
        if options.ranking_id < 1:
            parser.error('--ranking-id 必须为正整数')
        if options.current or options.source_name:
            parser.error('--ranking-id 不能与 --current、--source-name 同时使用')
    if options.source_name and not options.current:
        parser.error('--source-name 仅用于 --current 模式')
    return execute(parser.description, lambda store, runtime: (
        collect(store, runtime, source_name=options.source_name, full=options.full,
                refresh=options.refresh) if options.current else
        collect_sources(store, runtime, refresh=options.refresh, ranking_id=options.ranking_id)), args=options, role='rank')


def collect_sources(store, runtime, *, refresh=False, ranking_id=None, product_collector=None):
    from automation.pdd.spider.rank import read_leaderboards
    from automation.pdd.spider.navigation import open_app_board
    from automation.pdd.spider.workflow import FatalWorkflowError
    if ranking_id is not None:
        if not store.db.execute('SELECT 1 FROM rankings WHERE id=?', (ranking_id,)).fetchone():
            raise ValueError(f'榜单 ID 不存在：{ranking_id}')
        entry = store.list(ranking_id)
        if not entry['source_url'].strip():
            raise ValueError(f'榜单 ID {ranking_id} 没有可用的来源链接')
        entries = [entry]
        sources = [{'name': entry['source_name'], 'main_name': entry['main_name'], 'url': entry['source_url']}]
        print(f'[榜单] 只采集榜单 ID {ranking_id}：{entry["main_name"]} / {entry["tab"]}', flush=True)
    else:
        sources = read_leaderboards(config.EXCEL_PATH)
        if not sources:
            raise ValueError(f'来源 Excel 没有有效榜单名称和链接：{config.EXCEL_PATH}')
        entries = store.seed_rankings(today(), sources, config.OUTPUT_ROOT)
    client = None
    failed = 0
    for index, source in enumerate(sources, 1):
        entry = entries[index - 1]
        print(f"[榜单] {index}/{len(sources)}：{source['name']}", flush=True)
        if not entry['tab']:
            failed += 1
            print('[榜单] D 列为空，已标记链接失效，跳过打开页面', flush=True)
            continue
        if completed(store, entry, product_collector) and not refresh:
            print('[榜单] 复用当天已完成子榜，跳过打开页面', flush=True)
            continue
        try:
            if client is None:
                client = runtime.ensure_wda()
            open_app_board(client, source)
            failed += bool(collect(store, runtime, full=True, refresh=refresh, entry=entry, **({'product_collector': product_collector} if product_collector else {})))
        except (FatalWorkflowError, OSError):
            raise
        except HTTPError as error:
            store.list_state(entry['id'], 'failed', str(error))
            raise FatalWorkflowError('WDA 通信异常，已停止本轮；请恢复 iOS 连接后续采') from error
        except Exception as error:
            failed += 1
            store.list_state(entry['id'], 'failed', str(error))
            store.event(today(), source['name'], entry['tab'], 'ios', 'open_board', error)
            print(f"[榜单] 打开或采集失败，继续下一条：{error}", flush=True)
    print(f'[榜单] 链接循环结束：共 {len(sources)} 条，失败 {failed} 条')
    return int(bool(failed))
