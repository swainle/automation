-- SQLite migration for the previous three-table schema (including manager/row_number).
-- Stop automation and back up the database before executing. Run this file ONCE.
-- Events are intentionally discarded; keep the backup to retain historical events.
-- On any error, stop execution and ROLLBACK; do not continue individual statements.
BEGIN IMMEDIATE;

CREATE TABLE daily_shops_merged (
    day TEXT NOT NULL,
    shop TEXT NOT NULL,
    row_number INTEGER,
    manager TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    cleanup_ok INTEGER,
    amount TEXT,
    image_path TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (day, shop)
);

INSERT INTO daily_shops_merged
    (day,shop,row_number,manager,status,reason,attempts,cleanup_ok,amount,image_path,updated_at)
SELECT d.day,d.shop,d.row_number,COALESCE(d.manager,''),
       CASE WHEN d.status='login_error' AND d.reason='verification_required'
            THEN 'verification_required' ELSE d.status END,
       COALESCE(d.reason,''),d.attempts,d.cleanup_ok,r.amount,COALESCE(r.image_path,''),
       CASE WHEN r.updated_at>d.updated_at THEN r.updated_at ELSE d.updated_at END
FROM daily_shops AS d
LEFT JOIN receivable_records AS r ON r.day=d.day AND r.shop=d.shop;

-- Preserve amount/image rows without a corresponding task record as pending tasks.
INSERT INTO daily_shops_merged (day,shop,status,amount,image_path,updated_at)
SELECT r.day,r.shop,'pending',r.amount,r.image_path,r.updated_at
FROM receivable_records AS r
WHERE NOT EXISTS (SELECT 1 FROM daily_shops AS d WHERE d.day=r.day AND d.shop=r.shop);

-- Abort and roll back if any shop identity was lost.
CREATE TEMP TABLE migration_check (valid INTEGER CHECK(valid=1));
INSERT OR ROLLBACK INTO migration_check
SELECT (SELECT COUNT(*) FROM daily_shops_merged) =
       (SELECT COUNT(*) FROM (SELECT day,shop FROM daily_shops UNION SELECT day,shop FROM receivable_records));
DROP TABLE migration_check;

DROP TABLE receivable_records;
DROP TABLE automation_events;
DROP TABLE daily_shops;
ALTER TABLE daily_shops_merged RENAME TO daily_shops;
COMMIT;
