-- Manual migration: stop all spider commands and back up sqlite.db first.
-- Execute the ENTIRE script on the spider database, not on screenshot sqlite.db.
-- SQLite JSON1 is required. On any error: ROLLBACK; do not continue executing statements.
-- Source tables are removed only after copying their data. Original JSON is preserved.
PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;

            CREATE TABLE IF NOT EXISTS spider_runs (
                day TEXT PRIMARY KEY, state TEXT NOT NULL, started_at TEXT DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS spider_boards (
                id INTEGER PRIMARY KEY, day TEXT NOT NULL, name TEXT NOT NULL, url TEXT NOT NULL,
                position INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                reason TEXT NOT NULL DEFAULT '', UNIQUE(day,name)
            );
            CREATE TABLE IF NOT EXISTS spider_lists (
                id INTEGER PRIMARY KEY, board_id INTEGER NOT NULL REFERENCES spider_boards(id),
                tab TEXT NOT NULL, tab_index INTEGER NOT NULL, has_sub INTEGER NOT NULL,
                directory TEXT NOT NULL DEFAULT '', header_json TEXT NOT NULL DEFAULT '[]',
                device TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'pending',
                collect_state TEXT NOT NULL DEFAULT 'pending', export_state TEXT NOT NULL DEFAULT 'pending',
                reason TEXT NOT NULL DEFAULT '', UNIQUE(board_id,tab)
            );
            CREATE TABLE IF NOT EXISTS spider_items (
                id INTEGER PRIMARY KEY, list_id INTEGER NOT NULL REFERENCES spider_lists(id),
                rank INTEGER NOT NULL, row_json TEXT NOT NULL,
                screenshot_state TEXT NOT NULL DEFAULT 'pending', crop_state TEXT NOT NULL DEFAULT 'pending',
                link_state TEXT NOT NULL DEFAULT 'pending', reason TEXT NOT NULL DEFAULT '',
                product_state TEXT NOT NULL DEFAULT 'pending', product_attempts INTEGER NOT NULL DEFAULT 0,
                product_json TEXT NOT NULL DEFAULT '{}', UNIQUE(list_id,rank)
            );
            CREATE TABLE IF NOT EXISTS spider_events (
                id INTEGER PRIMARY KEY, day TEXT NOT NULL, board TEXT NOT NULL, tab TEXT NOT NULL DEFAULT '',
                device TEXT NOT NULL, stage TEXT NOT NULL, error TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        
CREATE TABLE IF NOT EXISTS rankings (
    id INTEGER PRIMARY KEY,
    capture_date TEXT NOT NULL,
    source_name TEXT NOT NULL,
    main_name TEXT NOT NULL DEFAULT '',
    tab_name TEXT NOT NULL,
    source_url TEXT NOT NULL DEFAULT '',
    source_order INTEGER NOT NULL DEFAULT 0,
    tab_index INTEGER NOT NULL DEFAULT 0,
    has_sub INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    directory TEXT NOT NULL DEFAULT '',
    header_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(header_json)),
    state TEXT NOT NULL DEFAULT 'pending',
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    UNIQUE(capture_date, source_name, tab_name)
);
CREATE TABLE IF NOT EXISTS ranking_products (
    id INTEGER PRIMARY KEY,
    ranking_id INTEGER NOT NULL REFERENCES rankings(id) ON DELETE CASCADE,
    rank INTEGER NOT NULL CHECK(rank > 0),
    goods_id TEXT,
    goods_url TEXT,
    title TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','running','completed','failed')),
    metrics_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(metrics_json)),
    raw_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(raw_json)),
    directory TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ranking_id, rank)
);
CREATE INDEX IF NOT EXISTS idx_ranking_products_goods ON ranking_products(goods_id);
CREATE TABLE IF NOT EXISTS product_info (
    id INTEGER PRIMARY KEY,
    ranking_product_id INTEGER NOT NULL UNIQUE REFERENCES ranking_products(id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT '',
    shop_id TEXT,
    shop_name TEXT,
    listing_date TEXT,
    sale_state TEXT NOT NULL DEFAULT 'unknown',
    info_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(info_json)),
    images_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(images_json)),
    raw_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(raw_json)),
    collected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS product_skus (
    id INTEGER PRIMARY KEY,
    product_info_id INTEGER NOT NULL REFERENCES product_info(id) ON DELETE CASCADE,
    sku_id TEXT,
    dedup_key TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    specs_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(specs_json)),
    price TEXT,
    stock INTEGER,
    available INTEGER,
    image TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(raw_json)),
    UNIQUE(product_info_id, dedup_key)
);
CREATE TABLE IF NOT EXISTS product_comments (
    id INTEGER PRIMARY KEY,
    product_info_id INTEGER NOT NULL REFERENCES product_info(id) ON DELETE CASCADE,
    comment_id TEXT,
    dedup_key TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    sku_text TEXT,
    rating TEXT,
    is_negative INTEGER NOT NULL DEFAULT 0,
    tags_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(tags_json)),
    media_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(media_json)),
    commented_at TEXT,
    collected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    raw_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(raw_json)),
    UNIQUE(product_info_id, dedup_key)
);
CREATE TABLE IF NOT EXISTS collection_tasks (
    id INTEGER PRIMARY KEY,
    task_key TEXT UNIQUE,
    kind TEXT NOT NULL DEFAULT 'step',
    capture_date TEXT NOT NULL,
    source_name TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    source_order INTEGER NOT NULL DEFAULT 0,
    ranking_id INTEGER REFERENCES rankings(id) ON DELETE CASCADE,
    ranking_product_id INTEGER REFERENCES ranking_products(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    device TEXT NOT NULL DEFAULT '',
    artifacts_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(artifacts_json)),
    last_error TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_tasks_scope ON collection_tasks(ranking_id, ranking_product_id, stage);
CREATE INDEX IF NOT EXISTS idx_tasks_day ON collection_tasks(capture_date, kind, state);
PRAGMA user_version=3;


-- Source and run tasks preserve sources with no successfully discovered tabs.
INSERT INTO collection_tasks(id,task_key,kind,capture_date,source_name,source_url,source_order,stage,state,last_error)
SELECT id,'source:'||day||':'||name,'source',day,name,url,position,'discover',state,reason FROM spider_boards;
INSERT INTO collection_tasks(task_key,kind,capture_date,stage,state,started_at,finished_at)
SELECT 'run:'||day,'run',day,'run',state,started_at,finished_at FROM spider_runs;
INSERT INTO rankings(id,capture_date,source_name,main_name,tab_name,source_url,source_order,tab_index,has_sub,
 description,directory,header_json,state,last_error)
SELECT l.id,b.day,b.name,
 COALESCE(NULLIF(json_extract((SELECT row_json FROM spider_items WHERE list_id=l.id ORDER BY rank LIMIT 1),'$.主榜单名称'),''),''),
 l.tab,b.url,b.position,l.tab_index,l.has_sub,
 COALESCE(json_extract((SELECT row_json FROM spider_items WHERE list_id=l.id ORDER BY rank LIMIT 1),'$.榜单统计说明'),''),
 l.directory,l.header_json,l.state,l.reason FROM spider_lists l JOIN spider_boards b ON b.id=l.board_id;
INSERT INTO ranking_products(id,ranking_id,rank,goods_id,goods_url,title,metrics_json,raw_json,directory)
SELECT i.id,i.list_id,i.rank,NULLIF(json_extract(i.row_json,'$.产品ID'),''),NULLIF(json_extract(i.row_json,'$.产品链接'),''),
 COALESCE(json_extract(i.row_json,'$.标题'),''),json_object('display_price',COALESCE(NULLIF(CAST(json_extract(i.row_json, '$.metrics.display_price') AS TEXT),''),NULLIF(CAST(json_extract(i.row_json, '$.展示价') AS TEXT),''),NULLIF(CAST(json_extract(i.row_json, '$.价格') AS TEXT),'')),'recent_sales',COALESCE(NULLIF(CAST(json_extract(i.row_json, '$.metrics.recent_sales') AS TEXT),''),NULLIF(CAST(json_extract(i.row_json, '$.metrics.sales_24h') AS TEXT),''),NULLIF(CAST(json_extract(i.row_json, '$.24小时内拼单') AS TEXT),'')),'total_sales',COALESCE(NULLIF(CAST(json_extract(i.row_json, '$.metrics.total_sales') AS TEXT),''),NULLIF(CAST(json_extract(i.row_json, '$.销售数量') AS TEXT),''),NULLIF(CAST(json_extract(i.row_json, '$.销量') AS TEXT),'')),'hot_index',COALESCE(NULLIF(CAST(json_extract(i.row_json, '$.metrics.hot_index') AS TEXT),''),NULLIF(CAST(json_extract(i.row_json, '$.热卖指数') AS TEXT),'')),'dominance_days',COALESCE(NULLIF(CAST(json_extract(i.row_json, '$.metrics.dominance_days') AS TEXT),''),NULLIF(CAST(json_extract(i.row_json, '$.霸榜天数') AS TEXT),'')),'positive_rate',COALESCE(NULLIF(CAST(json_extract(i.row_json, '$.metrics.positive_rate') AS TEXT),''),NULLIF(CAST(json_extract(i.row_json, '$.好评率') AS TEXT),'')),'positive_count',COALESCE(NULLIF(CAST(json_extract(i.row_json, '$.metrics.positive_count') AS TEXT),''),NULLIF(CAST(json_extract(i.row_json, '$.好评人数') AS TEXT),'')),'coupon_price',COALESCE(NULLIF(CAST(json_extract(i.row_json, '$.metrics.coupon_price') AS TEXT),''),NULLIF(CAST(json_extract(i.row_json, '$.券后价') AS TEXT),'')),'deal_price_30d',COALESCE(NULLIF(CAST(json_extract(i.row_json, '$.metrics.deal_price_30d') AS TEXT),''),NULLIF(CAST(json_extract(i.row_json, '$.近30天成交价') AS TEXT),'')),'price_drop',COALESCE(NULLIF(CAST(json_extract(i.row_json, '$.metrics.price_drop') AS TEXT),''),NULLIF(CAST(json_extract(i.row_json, '$.降价') AS TEXT),''))),i.row_json,
 CASE WHEN l.directory='' THEN '' ELSE rtrim(l.directory,'/\')||'/榜'||printf('%02d',i.rank) END
FROM spider_items i JOIN spider_lists l ON l.id=i.list_id;
INSERT INTO product_info(id,ranking_product_id,title,listing_date,info_json,raw_json)
SELECT id,id,COALESCE(json_extract(row_json,'$.标题'),''),
 COALESCE(NULLIF(json_extract(product_json,'$.status.listing_date'),''),NULLIF(json_extract(row_json,'$.上架日期'),'')),
 COALESCE(json_extract(product_json,'$.info'),'{}'),product_json FROM spider_items;
INSERT OR IGNORE INTO product_skus(product_info_id,sku_id,dedup_key,position,specs_json,price,raw_json)
SELECT i.id,json_extract(s.value,'$.sku_id'),s.value,CAST(s.key AS INTEGER),
 COALESCE((SELECT json_group_object(key,value) FROM json_each(s.value) WHERE key LIKE 'SPU%'),'{}'),
 COALESCE(json_extract(s.value,'$.卷后价'),json_extract(s.value,'$.券后价'),json_extract(s.value,'$.price')),s.value
FROM spider_items i,json_each(i.product_json,'$.sku') s;
INSERT INTO product_comments(product_info_id,comment_id,dedup_key,content,sku_text,is_negative,commented_at,raw_json)
SELECT i.id,COALESCE(json_extract(c.value,'$.review_id'),json_extract(c.value,'$.comment_id')),
 COALESCE(CAST(json_extract(c.value,'$.review_id') AS TEXT),CAST(json_extract(c.value,'$.comment_id') AS TEXT),
 json_remove(c.value,'$.序号','$.原因','$.negative_reason')),
 COALESCE(json_extract(c.value,'$.评论内容'),json_extract(c.value,'$.comment'),''),
 COALESCE(json_extract(c.value,'$.SKU'),json_extract(c.value,'$.specs'),''),
 CASE c.category WHEN 'negative_comments' THEN 1 ELSE 0 END,
 COALESCE(json_extract(c.value,'$.评论日期'),json_extract(c.value,'$.time')),c.value
FROM spider_items i JOIN (
 SELECT spider_items.id AS item_id,'comments' AS category,j.value FROM spider_items,json_each(product_json,'$.comments') j
 UNION ALL
 SELECT spider_items.id AS item_id,'negative_comments' AS category,j.value FROM spider_items,json_each(product_json,'$.negative_comments') j
) c ON c.item_id=i.id WHERE 1
ON CONFLICT(product_info_id,dedup_key) DO UPDATE SET is_negative=MAX(is_negative,excluded.is_negative);
INSERT INTO collection_tasks(task_key,capture_date,ranking_id,stage,state,device,last_error)
SELECT 'list:'||l.id||':collect',b.day,l.id,'collect',l.collect_state,l.device,l.reason FROM spider_lists l JOIN spider_boards b ON b.id=l.board_id;
INSERT INTO collection_tasks(task_key,capture_date,ranking_id,stage,state,device)
SELECT 'list:'||l.id||':export',b.day,l.id,'export',l.export_state,l.device FROM spider_lists l JOIN spider_boards b ON b.id=l.board_id;
INSERT INTO collection_tasks(task_key,capture_date,ranking_id,ranking_product_id,stage,state,attempts,last_error)
SELECT 'item:'||i.id||':product',b.day,l.id,i.id,'product',i.product_state,i.product_attempts,i.reason
FROM spider_items i JOIN spider_lists l ON l.id=i.list_id JOIN spider_boards b ON b.id=l.board_id;
INSERT INTO collection_tasks(task_key,capture_date,ranking_id,ranking_product_id,stage,state,device)
SELECT 'item:'||i.id||':'||s.stage,b.day,l.id,i.id,s.stage,
 CASE s.stage WHEN 'ranking_screenshot' THEN i.screenshot_state WHEN 'ranking_crop' THEN i.crop_state ELSE i.link_state END,l.device
FROM spider_items i JOIN spider_lists l ON l.id=i.list_id JOIN spider_boards b ON b.id=l.board_id
CROSS JOIN (SELECT 'ranking_screenshot' stage UNION ALL SELECT 'ranking_crop' UNION ALL SELECT 'share_link') s;
INSERT OR IGNORE INTO collection_tasks(task_key,capture_date,ranking_id,ranking_product_id,stage,state,last_error)
SELECT 'item:'||i.id||':'||CASE j.key WHEN 'screenshot' THEN 'product_screenshot' ELSE j.key END,
 b.day,l.id,i.id,CASE j.key WHEN 'screenshot' THEN 'product_screenshot' ELSE j.key END,
 COALESCE(json_extract(j.value,'$.state'),'pending'),COALESCE(json_extract(j.value,'$.reason'),'')
FROM spider_items i JOIN spider_lists l ON l.id=i.list_id JOIN spider_boards b ON b.id=l.board_id,
 json_each(i.product_json,'$.status.stages') j WHERE j.key!='product';
INSERT INTO collection_tasks(kind,capture_date,source_name,ranking_id,stage,state,device,last_error,artifacts_json,created_at)
SELECT 'event',e.day,e.board,r.id,e.stage,'failed',e.device,e.error,json_object('legacy_event_id',e.id,'tab',e.tab),e.created_at
FROM spider_events e LEFT JOIN rankings r ON r.capture_date=e.day AND r.source_name=e.board AND r.tab_name=e.tab;
-- Refuse to remove source tables if an orphaned row could not be copied.
CREATE TEMP TABLE _migration_20261102115136_checks(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO _migration_20261102115136_checks(ok)
SELECT CASE WHEN
 (SELECT count(*) FROM spider_lists) = (SELECT count(*) FROM rankings r JOIN spider_lists l ON l.id=r.id)
 AND (SELECT count(*) FROM spider_items) = (SELECT count(*) FROM ranking_products p JOIN spider_items i ON i.id=p.id)
 AND (SELECT count(*) FROM spider_items) = (SELECT count(*) FROM product_info p JOIN spider_items i ON i.id=p.ranking_product_id)
 THEN 1 ELSE 0 END;
DROP TABLE _migration_20261102115136_checks;
-- Original row_json/product_json are retained in ranking_products.raw_json/product_info.raw_json.
-- Only drop old tables after every copy above has succeeded in this transaction.
DROP TABLE spider_items;
DROP TABLE spider_lists;
DROP TABLE spider_boards;
DROP TABLE spider_events;
DROP TABLE spider_runs;
PRAGMA user_version=3;
COMMIT;
PRAGMA foreign_keys=ON;
PRAGMA foreign_key_check;
