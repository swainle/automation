"""Stage state belongs to the source, ranking or ranking product."""
from automation.pdd.spider.time_format import SQL_NOW

ITEM_STAGES = {
    'ranking_screenshot': 'ranking_screenshot_state', 'ranking_crop': 'ranking_crop_state',
    'product_screenshot': 'product_screenshot_state', 'share_link': 'link_state',
    'product': 'product_state', 'product_info': 'info_state',
    'main_images': 'main_images_state', 'sku': 'sku_state',
    'negative_comments': 'negative_comments_state', 'detail_images': 'detail_images_state',
}
LIST_STAGES = {'collect': 'collect_state', 'ranking_long_screenshot': 'screenshot_state', 'export': 'export_state'}


def canonical(stage):
    return {'screenshot': 'ranking_screenshot', 'crop': 'ranking_crop',
            'link': 'share_link', 'details': 'detail_images'}.get(stage, stage)


def ensure_schema(db):
    for table, stages in [('rankings', LIST_STAGES), ('ranking_products', ITEM_STAGES)]:
        existing = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
        additions = {column: "TEXT NOT NULL DEFAULT 'pending'" for column in stages.values()}
        additions.update(stages_json="TEXT NOT NULL DEFAULT '{}'",
                         last_stage="TEXT NOT NULL DEFAULT ''", last_error="TEXT NOT NULL DEFAULT ''")
        if table == 'rankings':
            additions['device'] = "TEXT NOT NULL DEFAULT ''"
        if table == 'ranking_products':
            additions.update(shop_id='TEXT', shop_name='TEXT', listing_date='TEXT',
                             sale_state="TEXT NOT NULL DEFAULT 'unknown'",
                             info_json="TEXT NOT NULL DEFAULT '{}'", collected_at='TEXT')
            additions.update(product_attempts='INTEGER NOT NULL DEFAULT 0')
        for column, definition in additions.items():
            if column not in existing:
                db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
    db.commit()
