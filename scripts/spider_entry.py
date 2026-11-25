"""Frozen entry point for the spider command."""
from multiprocessing import freeze_support

from automation.pdd.spider.__main__ import main


if __name__ == '__main__':
    freeze_support()
    raise SystemExit(main())
