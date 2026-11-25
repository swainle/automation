"""Hidden web collection entry point."""
from automation.pdd.spider.web import main as collect


def main(argv=None):
    return collect('edge', argv)


if __name__ == '__main__':
    raise SystemExit(main())
