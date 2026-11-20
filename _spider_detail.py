"""Write current spider progress (with ranking names) to a UTF-8 file."""
from automation.pdd.spider import config
from automation.pdd.spider.mobile_database import connect

db = connect(readonly=True)
rows = db.execute(
    "SELECT r.id, r.main_name, r.tab_name, r.product_count, r.collect_state, r.snapshot_state, "
    "count(p.id) FILTER (WHERE p.mobile_state='completed') AS mob_done, "
    "count(p.id) FILTER (WHERE p.mobile_state='failed') AS mob_fail, "
    "count(p.id) FILTER (WHERE p.mobile_state='running') AS mob_run, "
    "count(p.id) FILTER (WHERE p.mobile_state='pending') AS mob_pend, "
    "count(p.id) AS total_prods "
    "FROM t_pdd_rankings r LEFT JOIN t_pdd_ranking_products p ON p.ranking_id=r.id "
    "WHERE r.week_start='2026-11-16' "
    "GROUP BY r.id ORDER BY r.id"
).fetchall()
lines = []
for r in rows:
    total = r["total_prods"]
    done = r["mob_done"]
    marker = "DONE" if (total > 0 and done == total) else ("EMPTY" if total == 0 else "PENDING")
    lines.append(
        f"{r['id']:>3} [{marker:7}] {r['main_name']}/{r['tab_name']} | "
        f"count={r['product_count']} collect={r['collect_state']} snap={r['snapshot_state']} | "
        f"done={done} fail={r['mob_fail']} run={r['mob_run']} pend={r['mob_pend']} total={total}"
    )
text = "\n".join(lines) + "\n"
with open(r"D:\repo\automation.pdd.spider\logs\_progress.txt", "w", encoding="utf-8") as f:
    f.write(text)
db.close()
print("written", len(lines), "rows")
