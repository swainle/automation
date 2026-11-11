-- Step 3: structure cleanup. Run only after the data script succeeded.
BEGIN IMMEDIATE;
CREATE TEMP TABLE _image_guard(ok INTEGER CHECK(ok=1));
INSERT INTO _image_guard SELECT CASE WHEN NOT EXISTS(
 SELECT 1 FROM product_info p,json_each(p.images_json) g,json_each(g.value) j
 WHERE g.key IN ('main','sku','detail') AND j.type='text'
 AND NOT EXISTS(SELECT 1 FROM product_images i WHERE i.product_info_id=p.id AND i.local_path=replace(j.value,char(92),'/'))
) THEN 1 ELSE 0 END;
ALTER TABLE product_info DROP COLUMN images_json;
DROP TABLE _image_guard;
COMMIT;
PRAGMA foreign_key_check;
