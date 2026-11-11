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
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','+8 hours') || '+08:00'),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','+8 hours') || '+08:00'),
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
    shop_id TEXT,
    shop_name TEXT,
    listing_date TEXT,
    sale_state TEXT NOT NULL DEFAULT 'unknown',
    info_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(info_json)),
    collected_at TEXT,
    rank_state TEXT NOT NULL DEFAULT 'pending' CHECK(rank_state IN ('pending','running','completed','failed')),
    metrics_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(metrics_json)),
    raw_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(raw_json)),
    directory TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','+8 hours') || '+08:00'),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','+8 hours') || '+08:00'),
    UNIQUE(ranking_id, rank)
);
CREATE INDEX IF NOT EXISTS idx_ranking_products_goods ON ranking_products(goods_id);
CREATE TABLE IF NOT EXISTS product_images (
    id INTEGER PRIMARY KEY,
    ranking_product_id INTEGER NOT NULL REFERENCES ranking_products(id) ON DELETE CASCADE,
    image_type TEXT NOT NULL CHECK(image_type IN ('main','sku','detail','detail_merged')),
    position INTEGER NOT NULL CHECK(position > 0),
    source_url TEXT,
    local_path TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','completed','failed')),
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','+8 hours') || '+08:00'),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','+8 hours') || '+08:00'),
    UNIQUE(ranking_product_id,image_type,position)
);
CREATE TABLE IF NOT EXISTS product_skus (
    id INTEGER PRIMARY KEY,
    ranking_product_id INTEGER NOT NULL REFERENCES ranking_products(id) ON DELETE CASCADE,
    sku_id TEXT,
    dedup_key TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    specs_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(specs_json)),
    price TEXT,
    stock INTEGER,
    available INTEGER,
    image TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(raw_json)),
    UNIQUE(ranking_product_id, dedup_key)
);
CREATE TABLE IF NOT EXISTS product_comments (
    id INTEGER PRIMARY KEY,
    ranking_product_id INTEGER NOT NULL REFERENCES ranking_products(id) ON DELETE CASCADE,
    comment_id TEXT,
    dedup_key TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    sku_text TEXT,
    rating TEXT,
    is_negative INTEGER NOT NULL DEFAULT 0,
    tags_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(tags_json)),
    media_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(media_json)),
    commented_at TEXT,
    collected_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','+8 hours') || '+08:00'),
    raw_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(raw_json)),
    UNIQUE(ranking_product_id, dedup_key)
);
PRAGMA user_version=9;
