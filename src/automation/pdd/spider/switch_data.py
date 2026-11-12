"""Move capture records, artifacts and collection timestamps to another day."""
from datetime import date, datetime, timezone, timedelta
from psycopg import sql
from automation.pdd.spider import config
from automation.pdd.spider.mobile_database import connect, TABLES, week_start
from pathlib import Path
import json
import os
import zipfile


CAPTURE_TIMES = {'created_at', 'updated_at', 'collected_at', 'captured_at',
                 'started_at', 'finished_at', 'completed_at', 'retry_at', 'web_collected_at'}


def retime(value, target, key=''):
    if isinstance(value, dict):
        return {name: retime(item, target, name) for name, item in value.items()}
    if isinstance(value, list):
        return [retime(item, target, key) for item in value]
    if key in CAPTURE_TIMES and isinstance(value, str) and value:
        datetime.fromisoformat(value)
        return target + value[10:]
    if key == 'retry_at_epoch' and isinstance(value, (int, float)) and value > 0:
        original = datetime.fromtimestamp(value, timezone(timedelta(hours=8)))
        return original.replace(year=int(target[:4]), month=int(target[5:7]), day=int(target[8:10])).timestamp()
    if key == 'capture_date' and isinstance(value, str):
        try:
            date.fromisoformat(value)
        except ValueError:
            return value
        return target
    return value


def relocate(value, directories):
    if isinstance(value, str):
        for old, new in directories:
            for start, end in ((str(old), str(new)), (old.as_posix(), new.as_posix())):
                if value == start or value.startswith(start + '/') or value.startswith(start + '\\'):
                    return end + value[len(start):]
        return value
    if isinstance(value, list):
        return [relocate(item, directories) for item in value]
    if isinstance(value, dict):
        return {key: relocate(item, directories) for key, item in value.items()}
    return value


def switch_data(dsn, source, target):
    for value in (source, target):
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError('日期须为 YYYY-MM-DD')
    if source == target:
        raise ValueError('源日期与目标日期不能相同')
    moved, edited, file_times = [], [], []
    db = connect(dsn)
    try:
        with db.transaction():
            db.execute(sql.SQL('LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE').format(
                sql.SQL(',').join(sql.Identifier('ods', table) for table in TABLES)))
            count = db.execute('SELECT count(*) AS count FROM t_pdd_rankings WHERE capture_date=%s', (source,)).fetchone()['count']
            if not count:
                raise ValueError(f'{source} 没有采集数据')
            if db.execute('SELECT 1 FROM t_pdd_rankings WHERE capture_date=%s LIMIT 1', (target,)).fetchone():
                raise ValueError(f'{target} 已有采集数据，未作修改')
            if db.execute('''SELECT 1 FROM t_pdd_rankings old JOIN t_pdd_rankings other
                ON old.source_name=other.source_name AND old.tab_name=other.tab_name
                WHERE old.capture_date=%s AND other.capture_date<>%s AND other.week_start=%s''',
                (source, source, week_start(target))).fetchone():
                raise ValueError('目标周已有同一榜单，未作修改')
            directories = []
            for row in db.execute('SELECT DISTINCT directory FROM t_pdd_rankings WHERE capture_date=%s', (source,)):
                old = Path(row['directory'])
                if not old.is_absolute() or old.name != source or old.is_symlink() or old.is_junction() or old.resolve() != old:
                    raise ValueError(f'榜单目录不是预期的绝对日期目录：{old}')
                if not old.is_relative_to(config.OUTPUT_ROOT.resolve()):
                    raise ValueError(f'榜单目录不在输出目录内：{old}')
                new = old.with_name(target)
                if not old.is_dir():
                    raise ValueError(f'源目录不存在：{old}')
                if new.exists() or new.is_symlink():
                    raise ValueError(f'目标目录已存在：{new}')
                directories.append((old, new))
            backup = config.BUSINESS_ROOT / 'data/backups' / ('before-switch-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.json')
            backup.parent.mkdir(parents=True, exist_ok=True)
            snapshot = {table: db.execute(sql.SQL('SELECT * FROM {} ORDER BY id').format(
                sql.Identifier('ods', table))).fetchall() for table in TABLES}
            with backup.open('x', encoding='utf-8') as stream:
                json.dump(snapshot, stream, ensure_ascii=False, indent=2, default=str)
            rankings = 'SELECT id FROM t_pdd_rankings WHERE capture_date=%s'
            products = f'SELECT id FROM t_pdd_ranking_products WHERE ranking_id IN ({rankings})'
            owners = [('t_pdd_rankings', 'capture_date=%s'), ('t_pdd_ranking_products', f'ranking_id IN ({rankings})'),
                      *((table, f'ranking_product_id IN ({products})') for table in TABLES[2:] if table != 't_pdd_selling_point')]
            for table, condition in owners:
                fields = [r['column_name'] for r in db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='ods' AND table_name=%s AND data_type='text'", (table,))]
                for row in db.execute(f'SELECT * FROM {table} WHERE {condition}', (source,)).fetchall():
                    changes = {}
                    for column in fields:
                        value = row[column]
                        if column == 'capture_date':
                            continue
                        if column.endswith('_json') and value is not None:
                            updated = json.dumps(retime(relocate(json.loads(value), directories), target), ensure_ascii=False)
                        else:
                            updated = retime(relocate(value, directories), target, column)
                            if column == 'local_path' and value:
                                relative = [(old.relative_to(config.BUSINESS_ROOT / 'data'), new.relative_to(config.BUSINESS_ROOT / 'data')) for old, new in directories]
                                updated = relocate(updated, relative)
                        if updated != value:
                            changes[column] = updated
                    if changes:
                        db.execute(f'UPDATE {table} SET ' + ','.join(f'{name}=%s' for name in changes) + ' WHERE id=%s', [*changes.values(), row['id']])
            with zipfile.ZipFile(str(backup) + '.files.zip', 'x', compression=zipfile.ZIP_DEFLATED) as archive:
                for old, new in directories:
                    for file in old.rglob('*'):
                        if file.is_symlink() or file.is_junction():
                            raise ValueError(f'采集目录中存在符号链接：{file}')
                        if not file.is_file():
                            continue
                        stat = file.stat()
                        file_times.append((file, stat.st_atime_ns, stat.st_mtime_ns))
                        if file.suffix.lower() == '.json':
                            content = file.read_bytes()
                            data = json.loads(content.decode('utf-8-sig'))
                            updated = retime(relocate(data, directories), target)
                            if file.name == 'status.json' and isinstance(updated, dict) and updated.get('date') == source:
                                updated['date'] = target
                            if updated != data:
                                archive.writestr(str(file.relative_to(old)), content)
                                edited.append((file, content))
                                file.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                        original = datetime.fromtimestamp(stat.st_mtime, timezone(timedelta(hours=8)))
                        modified = original.replace(year=int(target[:4]), month=int(target[5:7]), day=int(target[8:10])).timestamp()
                        os.utime(file, (modified, modified))
            for old, new in directories:
                old.rename(new)
                moved.append((old, new))
            day = date.fromisoformat(target)
            year, week, _ = day.isocalendar()
            db.execute(f'''UPDATE t_pdd_ranking_products SET collection_month=%s,collection_month_week=%s,
                collection_week_year=%s,collection_year_week=%s WHERE ranking_id IN ({rankings})''',
                (target[:7], (day.day - 1) // 7 + 1, year, week, source))
            db.execute('UPDATE t_pdd_rankings SET week_start=%s WHERE capture_date=%s', (week_start(target), source))
            db.execute('UPDATE t_pdd_rankings SET capture_date=%s WHERE capture_date=%s', (target, source))
        return backup, count
    except BaseException:
        for old, new in reversed(moved):
            new.rename(old)
        for file, content in reversed(edited):
            file.write_bytes(content)
        for file, accessed, modified in file_times:
            os.utime(file, ns=(accessed, modified))
        raise
    finally:
        db.close()
