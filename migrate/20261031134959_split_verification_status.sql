-- Run after 20261031134259_merge_daily_shops.sql for old three-table databases.
-- Old verification records have no reliable subtype; retain reason for traceability.
BEGIN IMMEDIATE;
UPDATE daily_shops SET status='verification_other'
WHERE status='verification_required';
COMMIT;
