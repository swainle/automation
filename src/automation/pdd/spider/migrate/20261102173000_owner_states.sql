-- STEP 1: structure only. Add owner columns, retain collection_tasks for data transfer.
-- Stop all collectors; back up sqlite.db before starting this three-step upgrade.
-- Application code must be updated separately before resuming collection.
-- Execute with a client that stops on error; on any error issue ROLLBACK.
PRAGMA foreign_keys=ON;
BEGIN IMMEDIATE;
CREATE TEMP TABLE _upgrade_guard(ok INTEGER CHECK(ok=1));
INSERT INTO _upgrade_guard SELECT CASE WHEN user_version=3 THEN 1 ELSE 0 END FROM pragma_user_version;
ALTER TABLE rankings ADD COLUMN collect_state TEXT NOT NULL DEFAULT 'pending' CHECK(collect_state IN ('pending','running','completed','failed'));
ALTER TABLE rankings ADD COLUMN screenshot_state TEXT NOT NULL DEFAULT 'pending' CHECK(screenshot_state IN ('pending','running','completed','failed'));
ALTER TABLE rankings ADD COLUMN export_state TEXT NOT NULL DEFAULT 'pending' CHECK(export_state IN ('pending','running','completed','failed'));
ALTER TABLE rankings ADD COLUMN device TEXT NOT NULL DEFAULT '';
ALTER TABLE rankings ADD COLUMN last_stage TEXT NOT NULL DEFAULT '';
ALTER TABLE ranking_products ADD COLUMN crop_state TEXT NOT NULL DEFAULT 'pending' CHECK(crop_state IN ('pending','running','completed','failed'));
ALTER TABLE ranking_products ADD COLUMN screenshot_state TEXT NOT NULL DEFAULT 'pending' CHECK(screenshot_state IN ('pending','running','completed','failed'));
ALTER TABLE ranking_products ADD COLUMN link_state TEXT NOT NULL DEFAULT 'pending' CHECK(link_state IN ('pending','running','completed','failed'));
ALTER TABLE ranking_products ADD COLUMN detail_state TEXT NOT NULL DEFAULT 'pending' CHECK(detail_state IN ('pending','running','completed','failed'));
ALTER TABLE ranking_products ADD COLUMN detail_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ranking_products ADD COLUMN last_stage TEXT NOT NULL DEFAULT '';
ALTER TABLE ranking_products ADD COLUMN last_error TEXT NOT NULL DEFAULT '';
ALTER TABLE ranking_products ADD COLUMN completed_at TEXT;
DROP TABLE _upgrade_guard;
PRAGMA user_version=400;
COMMIT;
PRAGMA foreign_key_check;
