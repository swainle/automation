-- For databases already using the six-table schema (user_version 2).
-- Stop spider commands, back up sqlite.db, then execute the whole script once.
-- rankings already has state; only ranking_products needs a new column.
BEGIN IMMEDIATE;
ALTER TABLE ranking_products ADD COLUMN state TEXT NOT NULL DEFAULT 'pending'
    CHECK(state IN ('pending','running','completed','failed'));
UPDATE ranking_products SET state='completed'
WHERE EXISTS (SELECT 1 FROM collection_tasks t WHERE t.ranking_product_id=ranking_products.id AND t.stage='share_link' AND t.state='completed')
  AND EXISTS (SELECT 1 FROM collection_tasks t WHERE t.ranking_product_id=ranking_products.id AND t.stage='product_screenshot' AND t.state='completed')
  AND EXISTS (SELECT 1 FROM collection_tasks t WHERE t.ranking_product_id=ranking_products.id AND t.stage='ranking_crop' AND t.state='completed');
UPDATE rankings SET state=CASE
 WHEN EXISTS(SELECT 1 FROM ranking_products p WHERE p.ranking_id=rankings.id)
 AND NOT EXISTS(SELECT 1 FROM ranking_products p WHERE p.ranking_id=rankings.id AND p.state!='completed')
 AND EXISTS(SELECT 1 FROM collection_tasks t WHERE t.ranking_id=rankings.id AND t.ranking_product_id IS NULL AND t.stage='ranking_long_screenshot' AND t.state='completed')
 THEN 'completed' ELSE 'pending' END;
PRAGMA user_version=3;
COMMIT;
PRAGMA foreign_key_check;
