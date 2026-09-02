"""Daily news archive.

Publisher news sitemaps carry about 48 hours. Once an article falls out of that
window it cannot be discovered again, so a report for last June is impossible
unless the article was saved while it was still listed. This module saves them.

Layout -- one gzipped JSONL file per source per day:

    news_articles_for_media_monitoring/
        2026-09-01/
            loksatta.jsonl.gz
            pudhari.jsonl.gz

Article *text*, never the HTML page. The page is ~344 KB of markup, ads and
navigation; the extracted text is ~2 KB. At roughly 5,000 articles a day that
is the difference between 640 GB a year and 4 GB.

Files rather than a database, because the query is always a date range and a
date range is a directory listing. Reading a week means opening seven folders.
Nothing to run, nothing to back up separately, and a bad day can be deleted
with rm.
"""

from __future__ import annotations

import gzip
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import news_monitor as nm

ARCHIVE_DIR = Path(os.getenv("ARCHIVE_DIR", "news_articles_for_media_monitoring"))

# A harvest that finds nothing is far more likely to be a broken selector or a
# blocked fetch than a genuinely quiet news day, so it is recorded as a failure
# for the status check rather than a success with zero articles.
MIN_PLAUSIBLE_ARTICLES = 20

# How often a source's file is rewritten mid-harvest. Small enough that a kill
# loses little, large enough that rewriting the gzip is not the bottleneck.
FLUSH_EVERY = 50


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def day_dir(day: datetime | str) -> Path:
    key = day if isinstance(day, str) else day.strftime("%Y-%m-%d")
    return ARCHIVE_DIR / key


def _source_file(day: datetime | str, source: str) -> Path:
    return day_dir(day) / f"{_slug(source)}.jsonl.gz"


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def read_source_day(day: datetime | str, source: str) -> list[dict]:
    p = _source_file(day, source)
    if not p.exists():
        return []
    try:
        with gzip.open(p, "rt", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except (OSError, json.JSONDecodeError):
        return []


def write_source_day(day: datetime | str, source: str, records: list[dict]) -> Path:
    """Replace one source's file for one day.

    Written to a temporary file and renamed, so a harvest killed mid-write
    leaves the previous day's data intact rather than a half-file.
    """
    p = _source_file(day, source)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(p)
    _bump_count(day, source, len(records))
    return p


def _counts_path(day: datetime | str) -> Path:
    return day_dir(day) / "_counts.json"


def _bump_count(day: datetime | str, source: str, n: int) -> None:
    """Per-day article counts, written beside the data.

    status() reads these rather than decompressing every file: a year of
    archive is ~6,500 files and several GB, which is not an API response.
    """
    p = _counts_path(day)
    counts = {}
    if p.exists():
        try:
            counts = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            counts = {}
    counts[_slug(source)] = n
    p.write_text(json.dumps(counts, indent=1), encoding="utf-8")


def day_count(day: datetime | str) -> int:
    p = _counts_path(day)
    if not p.exists():
        return 0
    try:
        return sum(json.loads(p.read_text()).values())
    except (OSError, json.JSONDecodeError, AttributeError):
        return 0


def harvest_day(day: datetime, fetcher: nm.Fetcher | None = None,
                sources: list[nm.Source] | None = None,
                verbose: bool = True) -> dict:
    """Fetch and store every article a source listed for this day.

    Re-running is safe and cheap: articles already stored are skipped, so a
    harvest interrupted halfway can simply be run again.
    """
    # cache=False on purpose. The HTML page cache exists to make re-runs of the
    # same window free, but a harvest never re-reads a page, and at ~344 KB
    # each it would add ~1.7 GB of disk a day for nothing.
    fetcher = fetcher or nm.Fetcher(user_agent=nm.UA, delay=1.5, cache=False)
    sources = sources or nm.SOURCES

    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1) - timedelta(seconds=1)

    totals = {"day": start.strftime("%Y-%m-%d"), "sources": {},
              "articles": 0, "new": 0, "skipped": 0, "failed": 0}

    for source in sources:
        existing = {r["url"]: r for r in read_source_day(start, source.name)}
        try:
            found = nm.discover(source, fetcher, start, end, verbose=False)
        except Exception as e:                      # one bad source must not
            if verbose:                             # abort the whole night
                print(f"  {source.name:<20} discover failed: {e}")
            totals["sources"][source.name] = {"error": str(e)}
            continue

        new = 0
        for art in found:
            if art.url in existing:
                totals["skipped"] += 1
                continue
            html = fetcher.get(art.url)
            if not html:
                totals["failed"] += 1
                continue
            try:
                art = nm.extract(html, art)
            except Exception:
                totals["failed"] += 1
                continue
            text = (art.body_text or "").strip()
            if not text:
                totals["failed"] += 1
                continue
            existing[art.url] = {
                "url": art.url,
                "source": art.source,
                "language": art.language or source.language,
                "title": art.title or "",
                "published": art.published.isoformat() if art.published else None,
                "text": text,
                "words": len(text.split()),
                "fetched": datetime.now().isoformat(timespec="seconds"),
            }
            new += 1
            # Flush periodically. A full night is ~2.5 hours of fetching, and
            # without this a kill part-way through a source throws away
            # everything fetched for it -- which, for a day whose sitemap has
            # since rolled over, means losing it for good.
            if new % FLUSH_EVERY == 0:
                write_source_day(start, source.name, list(existing.values()))

        if existing:
            write_source_day(start, source.name, list(existing.values()))
        totals["sources"][source.name] = {"stored": len(existing), "new": new}
        totals["articles"] += len(existing)
        totals["new"] += new
        if verbose:
            print(f"  {source.name:<20} {len(existing):>5} stored ({new} new)")

    totals["ok"] = totals["articles"] >= MIN_PLAUSIBLE_ARTICLES
    _write_status(totals)
    return totals


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def read_range(start: datetime, end: datetime, sources: list[str] | None = None):
    """Every stored article published between two dates, oldest day first."""
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    last = end.replace(hour=0, minute=0, second=0, microsecond=0)
    want = {_slug(s) for s in sources} if sources else None
    while day <= last:
        d = day_dir(day)
        if d.is_dir():
            for f in sorted(d.glob("*.jsonl.gz")):
                if want and f.name.removesuffix(".jsonl.gz") not in want:
                    continue
                try:
                    with gzip.open(f, "rt", encoding="utf-8") as fh:
                        for line in fh:
                            if line.strip():
                                yield json.loads(line)
                except (OSError, json.JSONDecodeError):
                    continue
        day += timedelta(days=1)


def stored_days() -> list[str]:
    if not ARCHIVE_DIR.is_dir():
        return []
    return sorted(d.name for d in ARCHIVE_DIR.iterdir()
                  if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name))


# --------------------------------------------------------------------------
# Status -- what the alarm reads
# --------------------------------------------------------------------------

def _status_path() -> Path:
    return ARCHIVE_DIR / "_status.json"


def _write_status(totals: dict) -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    prev = {}
    p = _status_path()
    if p.exists():
        try:
            prev = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            prev = {}
    if totals.get("ok"):
        prev["last_success"] = datetime.now().isoformat(timespec="seconds")
        prev["last_success_day"] = totals["day"]
    prev["last_run"] = datetime.now().isoformat(timespec="seconds")
    prev["last_run_day"] = totals["day"]
    prev["last_run_articles"] = totals["articles"]
    prev["last_run_ok"] = bool(totals.get("ok"))
    p.write_text(json.dumps(prev, indent=1), encoding="utf-8")


def status() -> dict:
    """What was stored, and where the holes are.

    `missing` is the thing worth alerting on: a day with no directory is a day
    whose news is gone for good, because the sitemaps no longer list it.
    """
    days = stored_days()
    out: dict = {"archive_dir": str(ARCHIVE_DIR), "days": len(days),
                 "first_day": days[0] if days else None,
                 "last_day": days[-1] if days else None,
                 "articles": 0, "bytes": 0, "missing": []}
    if ARCHIVE_DIR.is_dir():
        out["bytes"] = sum(f.stat().st_size
                           for f in ARCHIVE_DIR.rglob("*.jsonl.gz"))
    out["articles"] = sum(day_count(d) for d in days)
    if days:
        have = set(days)
        d = datetime.strptime(days[0], "%Y-%m-%d")
        last = datetime.strptime(days[-1], "%Y-%m-%d")
        while d <= last:
            key = d.strftime("%Y-%m-%d")
            if key not in have:
                out["missing"].append(key)
            d += timedelta(days=1)
    p = _status_path()
    if p.exists():
        try:
            out.update(json.loads(p.read_text()))
        except (OSError, json.JSONDecodeError):
            pass
    return out


# --------------------------------------------------------------------------
# Searching -- what a report run reads instead of scraping
# --------------------------------------------------------------------------

def search(start: datetime, end: datetime, keywords: list[str],
           min_words: int = 25, drop_aggregators: bool = True,
           verbose: bool = False):
    """Stored articles in the window that mention one of the keywords.

    Returns `corpus.Doc` objects, the same type `corpus.build_docs` produces
    from live fetching, so the rest of the pipeline -- dedupe, rerank, report --
    does not know or care which path the articles came from.

    Matching runs over title plus body. Live discovery matches the URL slug
    first because fetching every candidate is expensive; here the text is
    already on disk, so the stricter check is also the cheaper one.
    """
    import corpus

    docs, seen, scanned = [], set(), 0
    for r in read_range(start, end):
        scanned += 1
        url = r.get("url") or ""
        if url in seen:
            continue
        text = (r.get("text") or "").strip()
        title = (r.get("title") or "").strip()
        if len(text.split()) < min_words:
            continue
        if keywords and not nm.match(f"{title}\n{text}", keywords):
            continue
        if drop_aggregators:
            art = nm.Article(url=url, source=r.get("source", ""), title=title)
            if nm.is_aggregator(art):
                continue
        seen.add(url)
        docs.append(corpus.Doc(
            url=url,
            source=r.get("source", ""),
            published=r.get("published") or "",
            title=title,
            text=text,
            lang=r.get("language", ""),
        ))

    if verbose:
        print(f"  archive: {scanned} stored articles scanned, {len(docs)} matched")
    return docs
