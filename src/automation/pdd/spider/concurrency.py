"""Cross-process workflow and artifact ownership, separate from SQLite writes."""
from automation.pdd.spider import config
from automation.pdd.spider.utils import _xlsx_lock


def workflow_lock(role, *, timeout=None):
    return _xlsx_lock(config.BUSINESS_ROOT / f'state/{role}.session', timeout=timeout)


def ranking_active():
    for role in ('mobile', 'rank'):
        try:
            with workflow_lock(role, timeout=0):
                pass
        except TimeoutError:
            return True
    return False


def ranking_lock(list_id, *, timeout=None):
    return _xlsx_lock(config.BUSINESS_ROOT / f'state/ranking-{int(list_id)}.session', timeout=timeout)
