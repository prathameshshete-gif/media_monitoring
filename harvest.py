"""Save one day of news into the archive. Meant for a nightly cron.

    python3 harvest.py                      # yesterday
    python3 harvest.py --date 2026-09-01
    python3 harvest.py --days-back 3        # the last 3 days, oldest first
    python3 harvest.py --status             # what is stored, and what is missing

Exits non-zero when a day stores implausibly few articles, so cron mail or a
Grafana alert notices. This matters more than it looks: a news sitemap lists
about 48 hours, so a harvest that quietly fails for a week loses that week
permanently.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

import archive
import news_monitor as nm


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--days-back", type=int, default=1,
                    help="harvest this many days ending yesterday (default 1)")
    ap.add_argument("--delay", type=float, default=1.5,
                    help="seconds between requests to one host (default 1.5)")
    ap.add_argument("--source", action="append",
                    help="limit to a source by name; repeatable")
    ap.add_argument("--status", action="store_true",
                    help="print what is stored and exit")
    args = ap.parse_args()

    if args.status:
        print(json.dumps(archive.status(), indent=1, ensure_ascii=False))
        return 0

    sources = nm.SOURCES
    if args.source:
        want = {s.lower() for s in args.source}
        sources = [s for s in nm.SOURCES if s.name.lower() in want]
        if not sources:
            print(f"No source matched {args.source}. Known: "
                  f"{[s.name for s in nm.SOURCES]}", file=sys.stderr)
            return 2

    if args.date:
        days = [datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=nm.IST)]
    else:
        today = datetime.now(nm.IST).replace(hour=0, minute=0, second=0,
                                             microsecond=0)
        days = [today - timedelta(days=n)
                for n in range(args.days_back, 0, -1)]

    # One fetcher across all days: it holds the per-host rate limiter and the
    # robots.txt cache, and both should persist for the whole night.
    fetcher = nm.Fetcher(user_agent=nm.UA, delay=args.delay, cache=False)

    failed = []
    for day in days:
        print(f"{day:%Y-%m-%d}")
        totals = archive.harvest_day(day, fetcher=fetcher, sources=sources)
        print(f"  -> {totals['articles']} stored, {totals['new']} new, "
              f"{totals['failed']} failed"
              f"{'' if totals['ok'] else '   *** IMPLAUSIBLY LOW ***'}")
        if not totals["ok"]:
            failed.append(totals["day"])

    if failed:
        print(f"\nDays that stored too little: {failed}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
