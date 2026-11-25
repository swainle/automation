"""Print ranking progress without opening devices or exposing credentials."""
from automation.pdd.spider.health import collection_status
from automation.pdd.spider.mobile_database import connect


def main():
    with connect(readonly=True) as db:
        collection_status(db)


if __name__ == '__main__':
    main()
