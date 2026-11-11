import os

import pytest
from psycopg import sql

from automation.pdd.spider import config
from automation.pdd.spider.mobile_database import Store, TABLES, connect


@pytest.fixture
def pg_database(tmp_path, monkeypatch):
    dsn = os.getenv('PDD_TEST_POSTGRES_URL')
    if not dsn:
        pytest.skip('Set PDD_TEST_POSTGRES_URL to a dedicated pdd_spider_test_* database')
    monkeypatch.setattr(config, 'POSTGRES_URL', dsn)
    monkeypatch.setattr(config, 'BUSINESS_ROOT', tmp_path)
    monkeypatch.setattr(config, 'OUTPUT_ROOT', tmp_path / 'data/grab/pdd')
    with connect(dsn, readonly=True) as db:
        if not db.info.dbname.startswith('pdd_spider_test_'):
            raise ValueError('Tests require an isolated pdd_spider_test_* database')
    store = Store(dsn)
    try:
        if not store.db.info.dbname.startswith('pdd_spider_test_'):
            raise ValueError('Tests require an isolated pdd_spider_test_* database')
        store.db.execute(sql.SQL('TRUNCATE {} RESTART IDENTITY CASCADE').format(
            sql.SQL(',').join(sql.Identifier('spider', table) for table in TABLES)))
    finally:
        store.close()
    return dsn
