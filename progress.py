"""Print PDD spider PostgreSQL progress without exposing credentials."""
from datetime import datetime, timedelta

from automation.pdd.spider.mobile_database import connect
from automation.pdd.spider.time_format import BEIJING

def main():
    with connect(readonly=True) as db:
        rows = db.execute("""
            SELECT r.id, r.capture_date, r.week_start, r.source_name, r.main_name, r.tab_name,
                   r.collect_state, r.snapshot_state, r.screenshot_state, r.export_state,
                   r.web_collected_at,
                   COUNT(p.id) AS total,
                   COUNT(p.id) FILTER (WHERE p.mobile_state='completed') AS mobile_done,
                   COUNT(p.id) FILTER (WHERE p.sogou_state='completed') AS web_done,
                   COUNT(p.id) FILTER (WHERE p.mobile_state='failed') AS mobile_failed,
                   COUNT(p.id) FILTER (WHERE p.sogou_state='failed') AS web_failed
            FROM t_pdd_rankings r
            LEFT JOIN t_pdd_ranking_products p ON p.ranking_id = r.id
            GROUP BY r.id
            ORDER BY r.capture_date DESC, r.source_order, r.id
        """).fetchall()
    now = datetime.now(BEIJING)
    print(f"{'id':>4} {'date':<10} {'week':<10} {'src':<22} {'main/tab':<26} "
          f"{'coll':<9} {'snap':<9} {'shot':<9} {'exp':<9} {'mob':>7} {'web':>7} {'mf':>3} {'wf':>3} next_web")
    for r in rows:
        next_web = '-'
        if r['mobile_done'] > r['web_done'] and r['total']:
            attempted = r['web_collected_at']
            if attempted:
                due = datetime.fromisoformat(attempted) + timedelta(minutes=30)
                next_web = 'READY' if due <= now else due.astimezone(BEIJING).strftime('%m-%d %H:%M:%S')
            else:
                next_web = 'READY'
        name = f"{r['main_name']}/{r['tab_name']}"
        print(f"{r['id']:>4} {r['capture_date']:<10} {r['week_start']:<10} {r['source_name']:<22} {name:<26} "
              f"{r['collect_state']:<9} {r['snapshot_state']:<9} {r['screenshot_state']:<9} {r['export_state']:<9} "
              f"{r['mobile_done']:>3}/{r['total']:<3} {r['web_done']:>3}/{r['total']:<3} "
              f"{r['mobile_failed']:>3} {r['web_failed']:>3} {next_web}")

if __name__ == '__main__':
    main()
