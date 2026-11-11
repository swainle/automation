-- Step 2: data. Requires the old product_info.images_json column and the new product_images table.
-- Unknown historical source URLs are NULL; files are pending until checked, never fabricated.
BEGIN IMMEDIATE;
CREATE TEMP TABLE _legacy_images(product_info_id INTEGER, image_type TEXT, local_path TEXT);
INSERT INTO _legacy_images
SELECT p.id, CASE WHEN replace(j.value,char(92),'/') LIKE '%/长图.jpg' THEN 'detail_merged' ELSE g.key END,
       replace(j.value,char(92),'/')
FROM product_info p,json_each(p.images_json) g,json_each(g.value) j
WHERE g.key IN ('main','sku','detail') AND j.type='text';
INSERT INTO _legacy_images
SELECT p.id, CASE WHEN replace(j.value,char(92),'/') LIKE '主图/%' THEN 'main'
                 WHEN replace(j.value,char(92),'/') LIKE 'SKU/%' THEN 'sku'
                 WHEN replace(j.value,char(92),'/') LIKE '%/长图.jpg' THEN 'detail_merged' ELSE 'detail' END,
       replace(j.value,char(92),'/')
FROM product_info p,json_each(p.raw_json,'$.files') j
WHERE j.type='text' AND (replace(j.value,char(92),'/') LIKE '主图/%'
 OR replace(j.value,char(92),'/') LIKE 'SKU/%' OR replace(j.value,char(92),'/') LIKE '详情图片/%');
INSERT OR IGNORE INTO product_images(product_info_id,image_type,position,source_url,local_path,state)
SELECT product_info_id,image_type,row_number() OVER(PARTITION BY product_info_id,image_type ORDER BY local_path),
       NULL,local_path,'pending'
FROM (SELECT DISTINCT product_info_id,image_type,local_path FROM _legacy_images);
-- New manifests may already be present in raw_json; prefer their actual URL and state.
INSERT INTO product_images(product_info_id,image_type,position,source_url,local_path,state,last_error)
SELECT p.id,json_extract(j.value,'$.image_type'),json_extract(j.value,'$.position'),
 json_extract(j.value,'$.source_url'),json_extract(j.value,'$.local_path'),
 coalesce(json_extract(j.value,'$.state'),'pending'),coalesce(json_extract(j.value,'$.last_error'),'')
FROM product_info p,json_each(p.raw_json,'$.images') j WHERE j.type='object'
ON CONFLICT(product_info_id,image_type,position) DO UPDATE SET
 source_url=excluded.source_url,local_path=excluded.local_path,state=excluded.state,last_error=excluded.last_error;
DROP TABLE _legacy_images;
COMMIT;
PRAGMA foreign_key_check;
