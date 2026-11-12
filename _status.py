from automation.pdd.spider.mobile_database import connect

db = connect(readonly=True)
rows = db.execute(
    "SELECT mobile_state, count(*) AS n FROM t_pdd_ranking_products "
    "WHERE ranking_id=24 GROUP BY mobile_state ORDER BY mobile_state"
).fetchall()
print('ranking 24 mobile_state counts:', [(r['mobile_state'], r['n']) for r in rows])
r2 = db.execute(
    'SELECT collect_state, snapshot_state FROM t_pdd_rankings WHERE id=24'
).fetchone()
print('ranking 24 state:', dict(r2))
db.close()
