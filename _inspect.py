from automation.pdd.spider.mobile_database import connect

db = connect(readonly=True)
rows = db.execute(
    "SELECT id,rank,title,mobile_state,sogou_state,product_state,"
    "product_screenshot_state,main_images_state,sku_state,detail_images_state,"
    "comments_state,rank_state,directory FROM t_pdd_ranking_products "
    "WHERE ranking_id=24 ORDER BY rank"
).fetchall()
for r in rows:
    print(
        r['id'], '| rank', r['rank'], '|', r['title'],
        '| mobile', r['mobile_state'], '| sogou', r['sogou_state'],
        '| prod', r['product_state'], '| shot', r['product_screenshot_state'],
        '| img', r['main_images_state'], '| sku', r['sku_state'],
        '| det', r['detail_images_state'], '| cmt', r['comments_state'],
        '| rankst', r['rank_state'],
    )
print('count =', len(rows))
db.close()
