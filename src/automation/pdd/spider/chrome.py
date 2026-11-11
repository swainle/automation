"""Collect web product information with Chrome."""
from automation.pdd.spider.web import main as collect


def main(argv=None):
    return collect('chrome', argv)


if __name__ == '__main__':
    raise SystemExit(main())
