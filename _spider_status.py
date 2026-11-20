"""Print current spider rankings state (ASCII only)."""
from automation.pdd.spider import config
from automation.pdd.spider.mobile_database import connect

db = connect(readonly=True)
rows = db.execute(
    "SELECT r.id, week_start, capture_date, product_count, collect_state, snapshot_state, "
    "count(p.id) FILTER (WHERE p.mobile_state='completed') AS mob_done, "
    "count(p.id) FILTER (WHERE p.mobile_state='failed') AS mob_fail, "
    "count(p.id) FILTER (WHERE p.mobile_state='running') AS mob_run, "
    "count(p.id) AS total_prods "
    "FROM t_pdd_rankings r LEFT JOIN t_pdd_ranking_products p ON p.ranking_id=r.id "
    "GROUP BY r.id ORDER BY r.id"
).fetchall()
print("id week_start capture_date product_count collect_state snapshot_state mob_done mob_fail mob_run total_prods")
for r in rows:
    print(
        r["id"], r["week_start"], r["capture_date"], r["product_count"],
        r["collect_state"], r["snapshot_state"],
        r["mob_done"], r["mob_fail"], r["mob_run"], r["total_prods"]
    )
db.close()
