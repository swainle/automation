-- Step 1: structure. Stop collectors and back up the database.
PRAGMA foreign_keys=ON;
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS product_images (
    id INTEGER PRIMARY KEY,
    product_info_id INTEGER NOT NULL REFERENCES product_info(id) ON DELETE CASCADE,
    image_type TEXT NOT NULL CHECK(image_type IN ('main','sku','detail','detail_merged')),
    position INTEGER NOT NULL CHECK(position > 0),
    source_url TEXT,
    local_path TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','completed','failed')),
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(product_info_id,image_type,position)
);
COMMIT;
