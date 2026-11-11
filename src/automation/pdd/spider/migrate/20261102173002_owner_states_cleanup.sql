-- STEP 3: structure cleanup. Only after successful data migration.
-- Stop all collectors; back up sqlite.db before starting this three-step upgrade.
-- Application code must be updated separately before resuming collection.
-- Execute with a client that stops on error; on any error issue ROLLBACK.
PRAGMA foreign_keys=ON;
BEGIN IMMEDIATE;
CREATE TEMP TABLE _upgrade_guard(ok INTEGER CHECK(ok=1));
INSERT INTO _upgrade_guard SELECT CASE WHEN user_version=401 THEN 1 ELSE 0 END FROM pragma_user_version;
-- Existing overall completion is not inferred from links alone: return validation can fail.
-- Guard the essential checkpoint counts before removing the source table.
INSERT INTO _upgrade_guard SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM collection_tasks t JOIN rankings b ON b.id=t.ranking_id WHERE t.kind='step' AND t.ranking_product_id IS NULL AND t.stage='collect' AND t.state='completed' AND b.collect_state!='completed') THEN 1 ELSE 0 END;
INSERT INTO _upgrade_guard SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM collection_tasks t JOIN rankings b ON b.id=t.ranking_id WHERE t.kind='step' AND t.ranking_product_id IS NULL AND t.stage='ranking_long_screenshot' AND t.state='completed' AND b.screenshot_state!='completed') THEN 1 ELSE 0 END;
INSERT INTO _upgrade_guard SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM collection_tasks t JOIN ranking_products b ON b.id=t.ranking_product_id WHERE t.kind='step' AND t.stage='ranking_crop' AND t.state='completed' AND b.crop_state!='completed') THEN 1 ELSE 0 END;
INSERT INTO _upgrade_guard SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM collection_tasks t JOIN ranking_products b ON b.id=t.ranking_product_id WHERE t.kind='step' AND t.stage='share_link' AND t.state='completed' AND b.link_state!='completed') THEN 1 ELSE 0 END;
INSERT INTO _upgrade_guard SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM collection_tasks t JOIN ranking_products b ON b.id=t.ranking_product_id WHERE t.kind='step' AND t.stage='product' AND t.state='completed' AND b.detail_state!='completed') THEN 1 ELSE 0 END;
INSERT INTO _upgrade_guard SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM collection_tasks t JOIN ranking_products b ON b.id=t.ranking_product_id WHERE t.kind='step' AND t.stage='product_screenshot' AND t.state='completed' AND b.screenshot_state!='completed') THEN 1 ELSE 0 END;
INSERT INTO _upgrade_guard SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM pragma_foreign_key_check) THEN 1 ELSE 0 END;
DROP TABLE collection_tasks;
DROP TABLE _upgrade_guard;
PRAGMA user_version=4;
COMMIT;
PRAGMA foreign_key_check;
