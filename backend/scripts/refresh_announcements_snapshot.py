"""Refresh data/snapshots/announcements.json from the live exchanges (run locally, then commit it).

    cd backend && python -m scripts.refresh_announcements_snapshot [--tickers A B] [--days 30] [-v]

Tickers whose live fetch fails keep their previous snapshot rows. Nothing is written if all fail.
"""
import sys

from ingest.announcements import main

raise SystemExit(main(["refresh-snapshot", *sys.argv[1:]]))
