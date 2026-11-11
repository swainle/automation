"""Run the mobile ranking and product pipeline."""
import argparse
import psycopg


def main(argv=None):
    parser = argparse.ArgumentParser(description='手机整榜采集；--check 查看状态')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='只读检查环境并显示采集状态 ASCII 表格')
    mode.add_argument('--switch-data', metavar='YYYY-MM-DD', help='将该日期的数据库采集数据迁至 --switch-to')
    parser.add_argument('--switch-to', metavar='YYYY-MM-DD', help='目标采集日期（须无数据）')
    parser.add_argument('--ranking-id', type=int)
    parser.add_argument('--ranking-product-id', type=int)
    args = parser.parse_args(argv)
    if any(value is not None and value < 1 for value in (args.ranking_id, args.ranking_product_id)):
        parser.error('数据库 ID 必须大于零')
    if bool(args.switch_data) != bool(args.switch_to):
        parser.error('--switch-data 和 --switch-to 必须同时使用')
    if args.switch_data and (args.ranking_id is not None or args.ranking_product_id is not None):
        parser.error('日期迁移不能与采集 ID 筛选同时使用')
    if args.switch_data:
        from automation.pdd.spider import config
        from automation.pdd.spider.switch_data import switch_data
        try:
            backup, count = switch_data(config.POSTGRES_URL, args.switch_data, args.switch_to)
        except (ValueError, OSError, psycopg.Error) as error:
            print(f'日期迁移失败：{error}')
            return 1
        print(f'备份：{backup}')
        print(f'已将 {count} 条榜单及关联采集数据从 {args.switch_data} 迁至 {args.switch_to}')
        return 0
    from automation.pdd.spider import config
    if args.check:
        from automation.pdd.spider.health import run
        return run(args.ranking_id, args.ranking_product_id)
    from automation.pdd.spider.unified import collect
    from types import SimpleNamespace
    from automation.pdd.spider.commands import execute
    return execute('手机整榜采集',
        lambda store, runtime: collect(store, runtime, args.ranking_id, args.ranking_product_id),
        args=SimpleNamespace(ios=True, mobile=True), role='rank')


if __name__ == '__main__':
    raise SystemExit(main())
