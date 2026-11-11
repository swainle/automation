-- STEP 2: data only. Run after step 1; migrate actual current rows, no hard-coded IDs.
-- Stop all collectors; back up sqlite.db before starting this three-step upgrade.
-- Application code must be updated separately before resuming collection.
-- Execute with a client that stops on error; on any error issue ROLLBACK.
PRAGMA foreign_keys=ON;
BEGIN IMMEDIATE;
CREATE TEMP TABLE _upgrade_guard(ok INTEGER CHECK(ok=1));
INSERT INTO _upgrade_guard SELECT CASE WHEN user_version=400 THEN 1 ELSE 0 END FROM pragma_user_version;
UPDATE rankings SET collect_state=COALESCE((SELECT CASE state WHEN 'running' THEN 'pending' WHEN 'skipped' THEN 'completed' ELSE state END FROM collection_tasks WHERE kind='step' AND ranking_id=rankings.id AND ranking_product_id IS NULL AND stage IN ('collect') ORDER BY updated_at DESC,id DESC LIMIT 1),'pending');
UPDATE rankings SET screenshot_state=COALESCE((SELECT CASE state WHEN 'running' THEN 'pending' WHEN 'skipped' THEN 'completed' ELSE state END FROM collection_tasks WHERE kind='step' AND ranking_id=rankings.id AND ranking_product_id IS NULL AND stage IN ('ranking_long_screenshot') ORDER BY updated_at DESC,id DESC LIMIT 1),'pending');
UPDATE rankings SET export_state=COALESCE((SELECT CASE state WHEN 'running' THEN 'pending' WHEN 'skipped' THEN 'completed' ELSE state END FROM collection_tasks WHERE kind='step' AND ranking_id=rankings.id AND ranking_product_id IS NULL AND stage IN ('export') ORDER BY updated_at DESC,id DESC LIMIT 1),'pending');
UPDATE rankings SET device=COALESCE((SELECT device FROM collection_tasks WHERE kind='step' AND ranking_id=rankings.id AND ranking_product_id IS NULL AND stage IN ('collect') ORDER BY updated_at DESC,id DESC LIMIT 1),'');
UPDATE ranking_products SET crop_state=COALESCE((SELECT CASE state WHEN 'running' THEN 'pending' WHEN 'skipped' THEN 'completed' ELSE state END FROM collection_tasks WHERE kind='step' AND ranking_product_id=ranking_products.id AND stage IN ('ranking_crop') ORDER BY updated_at DESC,id DESC LIMIT 1),'pending');
UPDATE ranking_products SET screenshot_state=COALESCE((SELECT CASE state WHEN 'running' THEN 'pending' WHEN 'skipped' THEN 'completed' ELSE state END FROM collection_tasks WHERE kind='step' AND ranking_product_id=ranking_products.id AND stage IN ('product_screenshot','ranking_screenshot') ORDER BY updated_at DESC,id DESC LIMIT 1),'pending');
UPDATE ranking_products SET link_state=COALESCE((SELECT CASE state WHEN 'running' THEN 'pending' WHEN 'skipped' THEN 'completed' ELSE state END FROM collection_tasks WHERE kind='step' AND ranking_product_id=ranking_products.id AND stage IN ('share_link') ORDER BY updated_at DESC,id DESC LIMIT 1),'pending');
UPDATE ranking_products SET detail_state=COALESCE((SELECT CASE state WHEN 'running' THEN 'pending' WHEN 'skipped' THEN 'completed' ELSE state END FROM collection_tasks WHERE kind='step' AND ranking_product_id=ranking_products.id AND stage IN ('product') ORDER BY updated_at DESC,id DESC LIMIT 1),'pending');
UPDATE ranking_products SET detail_attempts=COALESCE((SELECT attempts FROM collection_tasks WHERE kind='step' AND ranking_product_id=ranking_products.id AND stage IN ('product') ORDER BY updated_at DESC,id DESC LIMIT 1),0);
UPDATE rankings SET last_stage=COALESCE((SELECT CASE WHEN state='failed' THEN stage ELSE '' END FROM collection_tasks WHERE kind='step' AND ranking_id=rankings.id AND ranking_product_id IS NULL ORDER BY updated_at DESC,id DESC LIMIT 1),'');
UPDATE rankings SET last_error=CASE WHEN state='failed' AND last_error!='' THEN last_error ELSE COALESCE((SELECT CASE WHEN state='failed' THEN last_error ELSE '' END FROM collection_tasks WHERE kind='step' AND ranking_id=rankings.id AND ranking_product_id IS NULL ORDER BY updated_at DESC,id DESC LIMIT 1),last_error) END;
UPDATE ranking_products SET last_stage=COALESCE((SELECT CASE WHEN state='failed' THEN stage ELSE '' END FROM collection_tasks WHERE kind='step' AND ranking_product_id=ranking_products.id ORDER BY updated_at DESC,id DESC LIMIT 1),'');
UPDATE ranking_products SET last_error=COALESCE((SELECT CASE WHEN state='failed' THEN last_error ELSE '' END FROM collection_tasks WHERE kind='step' AND ranking_product_id=ranking_products.id ORDER BY updated_at DESC,id DESC LIMIT 1),'');
-- An interrupted process is runnable again; preserve failures (including return failures).
UPDATE ranking_products SET state='pending' WHERE state='running';
UPDATE rankings SET state='pending',completed_at=NULL WHERE state='running';
UPDATE ranking_products SET completed_at=updated_at WHERE state='completed';
DROP TABLE _upgrade_guard;
PRAGMA user_version=401;
COMMIT;
PRAGMA foreign_key_check;
