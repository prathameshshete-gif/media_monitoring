"""
Sitemap-driven news monitor.

Discovers articles from publisher news sitemaps (the feed publishers maintain
for machine consumption), filters to a date window, then matches keywords
against the URL slug and, optionally, the fetched article text.

Design notes:
  - robots.txt is consulted for every URL before it is fetched, using the
    configured User-Agent. Disallowed URLs are skipped, not worked around.
  - Requests are rate limited per domain with exponential backoff on 429/5xx.
  - Fetched pages are cached on disk so re-runs cost nothing.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import random
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
import requests
from protego import Protego
from bs4 import BeautifulSoup

IST = timezone(timedelta(hours=5, minutes=30))
CACHE_DIR = Path(os.getenv("PAGE_CACHE_DIR", ".cache/pages"))

# The "Mozilla/5.0 (compatible; ...)" shape is the conventional bot string, and
# some publishers' WAFs (Tarun Bharat's, for one) 403 anything without it. The
# MediaMonitor token and contact address still identify us to robots.txt.
UA = "Mozilla/5.0 (compatible; MediaMonitor/1.0; +contact: aiml@strelema.com)"


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------

@dataclass
class Source:
    name: str
    language: str
    # Either a sitemap index, or a strftime template for per-date shards.
    sitemap_index: str | None = None
    dated_sitemap: str | None = None
    # Only keep candidate URLs whose path contains one of these (empty = all).
    sections: tuple[str, ...] = ()
    # Only walk nested sitemaps whose URL contains one of these (empty = all).
    # For indexes that mix article shards with tag/category archive shards.
    nested_include: tuple[str, ...] = ()


SOURCES = [
    Source("ABP Live", "hi/en",
           dated_sitemap="https://www.abplive.com/news-%d-%m-%Y.xml",
           sections=("maharashtra", "politics", "india", "states")),
    Source("ABP Majha", "mr",
           sitemap_index="https://marathi.abplive.com/news-sitemap.xml",
           sections=("maharashtra", "politics", "india", "states")),
    Source("Maharashtra Times", "mr",
           sitemap_index="https://maharashtratimes.com/staticsitemap/mt/news/sitemap-48hours.xml"),
    Source("Loksatta", "mr",
           sitemap_index="https://www.loksatta.com/news-sitemap.xml"),
    Source("eSakal", "mr",
           sitemap_index="https://www.esakal.com/news_sitemap.xml"),
    Source("Lokmat", "mr",
           sitemap_index="https://www.lokmat.com/sitemap.xml"),
    Source("Sarkarnama", "mr",
           sitemap_index="https://www.sarkarnama.in/news_sitemap.xml"),
    Source("Saam TV", "mr",
           sitemap_index="https://www.saamtv.com/news_sitemap.xml"),
    Source("TV9 Marathi", "mr",
           sitemap_index="https://www.tv9marathi.com/news-sitemap.xml"),
    # marathi.news18.com's own sitemap paths 404; its index points at the
    # news18marathi.com feed host, which is where the news shard actually lives.
    Source("News18 Marathi", "mr",
           sitemap_index="https://news18marathi.com/commonfeeds/v1/mar/sitemap/google-news.xml"),
    # Daily shards go back further than news_sitemap.xml (recent items only).
    Source("Pudhari", "mr",
           dated_sitemap="https://pudhari.news/sitemap/sitemap-daily-%Y-%m-%d.xml",
           sitemap_index="https://pudhari.news/news_sitemap.xml"),
    Source("Navarashtra", "mr",
           sitemap_index="https://www.navarashtra.com/news-sitemap.xml"),
    # The Rank Math index stamps every shard with a stale <lastmod> (days behind
    # the articles inside), so discover() would skip them all. post-sitemap1 is
    # the newest-first shard, so target it directly.
    Source("Lokshahi", "mr",
           sitemap_index="https://lokshahilive.com/post-sitemap1.xml"),
    # Yoast index, 340 shards: dead post-sitemapN.xml ones carry an old
    # <lastmod> and get skipped, but the post_tag-* archive shards are stamped
    # fresh and would otherwise flood the run with tag pages posing as articles.
    Source("Tarun Bharat", "mr",
           sitemap_index="https://www.tarunbharat.com/sitemap.xml",
           nested_include=("post-sitemap",)),
    Source("Navbharat Times", "hi",
           sitemap_index="https://navbharattimes.indiatimes.com/staticsitemap/nbt/news/sitemap-48hours.xml",
           sections=("maharashtra", "politics", "india", "states")),
    Source("Times Now", "en",
           sitemap_index="https://www.timesnownews.com/google-news-sitemap-en.xml",
           sections=("maharashtra", "politics", "india", "states")),
    # NDTV sits behind an Akamai edge that returns 403 to this host (robots.txt
    # included), so these yield nothing from here; they work from networks the
    # edge does not block.
    Source("NDTV", "en",
           sitemap_index="https://www.ndtv.com/sitemap/google-news-sitemap",
           sections=("maharashtra", "politics", "india", "states")),
    Source("NDTV Marathi", "mr",
           sitemap_index="https://marathi.ndtv.com/sitemap/google-news-sitemap"),
]


# --------------------------------------------------------------------------
# Polite fetching
# --------------------------------------------------------------------------

class Fetcher:
    """Rate-limited, robots-respecting, disk-cached HTTP client."""

    def __init__(self, user_agent: str, delay: float = 2.0, jitter: float = 1.0,
                 timeout: int = 25, max_retries: int = 3, cache: bool = True):
        self.user_agent = user_agent
        self.delay = delay
        self.jitter = jitter
        self.timeout = timeout
        self.max_retries = max_retries
        self.cache = cache
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self._robots: dict = {}
        self._last_hit: dict[str, float] = {}
        self.stats = {"fetched": 0, "cached": 0, "blocked": 0, "failed": 0}
        if cache:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # -- robots ------------------------------------------------------------
    def _robots_for(self, url: str):
        """Protego, not urllib's RobotFileParser: the stdlib parser treats a
        leading '*' in a Disallow path as matching every URL, which turns a
        rule like `Disallow: */reporter/*` into a site-wide block."""
        parts = urlparse(url)
        host = parts.netloc
        if host not in self._robots:
            rp = None
            try:
                r = self.session.get(f"{parts.scheme}://{host}/robots.txt",
                                     timeout=self.timeout)
                if r.status_code == 200:
                    rp = Protego.parse(r.text)
            except Exception:
                pass  # unreachable robots.txt -> treat as permissive
            self._robots[host] = rp
        return self._robots[host]

    def allowed(self, url: str) -> bool:
        rp = self._robots_for(url)
        return True if rp is None else rp.can_fetch(url, self.user_agent)

    def crawl_delay(self, url: str) -> float:
        rp = self._robots_for(url)
        if rp is None:
            return self.delay
        try:
            cd = rp.crawl_delay(self.user_agent)
        except Exception:
            cd = None
        return max(self.delay, float(cd)) if cd else self.delay

    # -- fetching ----------------------------------------------------------
    def _wait(self, url: str) -> None:
        host = urlparse(url).netloc
        gap = self.crawl_delay(url) + random.uniform(0, self.jitter)
        elapsed = time.monotonic() - self._last_hit.get(host, 0.0)
        if elapsed < gap:
            time.sleep(gap - elapsed)
        self._last_hit[host] = time.monotonic()

    def _cache_path(self, url: str) -> Path:
        return CACHE_DIR / (hashlib.sha256(url.encode()).hexdigest()[:32] + ".bin")

    def get(self, url: str) -> bytes | None:
        if self.cache:
            cp = self._cache_path(url)
            if cp.exists():
                self.stats["cached"] += 1
                return cp.read_bytes()

        if not self.allowed(url):
            self.stats["blocked"] += 1
            return None

        for attempt in range(self.max_retries):
            self._wait(url)
            try:
                r = self.session.get(url, timeout=self.timeout)
            except requests.RequestException:
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 200:
                body = gzip.decompress(r.content) if url.endswith(".gz") else r.content
                if self.cache:
                    self._cache_path(url).write_bytes(body)
                self.stats["fetched"] += 1
                return body
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep((2 ** attempt) * 5)  # back off and retry
                continue
            break  # 403/404/etc -- do not hammer
        self.stats["failed"] += 1
        return None


# --------------------------------------------------------------------------
# Sitemap parsing
# --------------------------------------------------------------------------

@dataclass
class Article:
    url: str
    source: str
    published: datetime | None = None
    title: str = ""
    keywords: str = ""
    language: str = ""
    matched: list[str] = field(default_factory=list)
    where: str = ""
    body_text: str = ""


_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
       "news": "http://www.google.com/schemas/sitemap-news/0.9"}


def _parse_dt(text: str) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    return dt.astimezone(IST) if dt.tzinfo else dt.replace(tzinfo=IST)


def parse_sitemap(xml: bytes, source: str):
    """Yield (kind, payload). kind is 'index' (a nested sitemap URL) or 'url'."""
    soup = BeautifulSoup(xml, "xml")

    if soup.find("sitemapindex"):
        for sm in soup.find_all("sitemap"):
            loc = sm.find("loc")
            if loc and loc.text.strip():
                lastmod = _parse_dt(sm.find("lastmod").text) if sm.find("lastmod") else None
                yield "index", (loc.text.strip(), lastmod)
        return

    for u in soup.find_all("url"):
        loc = u.find("loc")
        if not loc or not loc.text.strip():
            continue
        news = u.find("news")
        title = news.find("title").text.strip() if news and news.find("title") else ""
        kw = news.find("keywords").text.strip() if news and news.find("keywords") else ""
        lang = news.find("language").text.strip() if news and news.find("language") else ""
        pub = None
        if news and news.find("publication_date"):
            pub = _parse_dt(news.find("publication_date").text)
        if pub is None and u.find("lastmod"):
            pub = _parse_dt(u.find("lastmod").text)
        yield "url", Article(url=loc.text.strip(), source=source, published=pub,
                             title=title, keywords=kw, language=lang)


# --------------------------------------------------------------------------
# Keyword matching
# --------------------------------------------------------------------------

def normalise(text: str) -> str:
    """Casefold + NFC so Devanagari and Latin compare consistently."""
    return unicodedata.normalize("NFC", text or "").casefold()


def slugify(url: str) -> str:
    return normalise(re.sub(r"[^a-z0-9]+", " ", urlparse(url).path.lower()))


def match(text: str, keywords: list[str]) -> list[str]:
    """Return the keywords present in text. Latin terms match on word bounds;
    Devanagari has no case or spacing conventions to rely on, so it's substring."""
    hay = normalise(text)
    hits = []
    for kw in keywords:
        k = normalise(kw)
        if not k:
            continue
        if re.search(r"[a-z0-9]", k):
            if re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", hay):
                hits.append(kw)
        elif k in hay:
            hits.append(kw)
    return hits


# Rolling liveblog / aggregator pages. They mention everyone in the news that
# day, so they match keywords without being coverage, and they inflate counts.
AGGREGATOR_PATTERNS = (
    "live-updates", "live-news", "liveblog", "live-blog", "news-live",
    "latest-marathi-news-live", "top-marathi-news", "breaking-news-live",
    "live-today", "news-today-live", "-live-",
)


def is_aggregator(article: "Article") -> bool:
    url = article.url.lower()
    title = normalise(article.title)
    if any(p in url for p in AGGREGATOR_PATTERNS):
        return True
    return any(p in title for p in ("live updates", "news live", "live blog"))


# --------------------------------------------------------------------------
# Discovery + extraction
# --------------------------------------------------------------------------

def discover(source: Source, fetcher: Fetcher, start: datetime, end: datetime,
             verbose: bool = True) -> list[Article]:
    """Collect candidate articles from a source's sitemaps within the window."""
    found: dict[str, Article] = {}

    targets: list[str] = []
    if source.dated_sitemap:
        day = start
        while day <= end:
            targets.append(day.strftime(source.dated_sitemap))
            day += timedelta(days=1)
    if source.sitemap_index:
        targets.append(source.sitemap_index)

    seen_sitemaps: set[str] = set()
    while targets:
        sm_url = targets.pop(0)
        if sm_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sm_url)

        xml = fetcher.get(sm_url)
        if not xml:
            continue

        for kind, payload in parse_sitemap(xml, source.name):
            if kind == "index":
                nested, lastmod = payload
                if (source.nested_include
                        and not any(p in nested for p in source.nested_include)):
                    continue
                # Only walk nested sitemaps that could hold in-window articles.
                if lastmod and not (start <= lastmod <= end + timedelta(days=1)):
                    continue
                if len(seen_sitemaps) < 60:
                    targets.append(nested)
            else:
                art = payload
                if art.published is None:
                    continue  # undated (section/pagination link) -- unusable
                if not (start <= art.published <= end):
                    continue
                if source.sections and not any(s in art.url.lower() for s in source.sections):
                    continue
                art.language = art.language or source.language
                found[art.url] = art

    if verbose:
        print(f"  {source.name:<20} {len(found):>5} candidates in window")
    return list(found.values())


def extract(html: bytes, article: Article) -> Article:
    """Pull title and body text out of a fetched article page."""
    soup = BeautifulSoup(html, "lxml")

    if not article.title:
        for sel in ("h1", "meta[property='og:title']", "title"):
            el = soup.select_one(sel)
            if el:
                article.title = (el.get("content") or el.get_text(" ", strip=True)).strip()
                break

    if article.published is None:
        meta = soup.select_one("meta[property='article:published_time']")
        if meta and meta.get("content"):
            article.published = _parse_dt(meta["content"])

    for junk in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
        junk.decompose()
    body = soup.find("article") or soup.find("main") or soup.body or soup

    # Paragraph-level assembly, not a flat get_text(): sidebars, related-article
    # widgets and tag clouds are <a> inside <li>/<div>, and a flat text dump
    # sweeps them in. Measured on this corpus, pages with zero mentions of the
    # subject in their prose carried 22-40 in that chrome -- every one of which
    # the deep pass counted as a body match.
    paras = [p.get_text(" ", strip=True)
             for p in body.find_all(["p", "h2", "h3"])]
    paras = [t for t in paras if len(t) >= 25]
    article.body_text = ("\n\n".join(paras) or body.get_text(" ", strip=True))[:20000]
    return article

# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------

def run(keywords: list[str], start_date: str, end_date: str,
        sources: list[Source] = None, user_agent: str = None,
        deep: bool = True, deep_limit: int = 400, delay: float = 2.0,
        verbose: bool = True):
    """Discover, match, and return a DataFrame of keyword-matching articles.

    deep=True fetches candidate articles to match keywords in the body as well
    as the slug/title, which is what catches passing mentions. deep_limit caps
    how many pages are fetched per run so a wide window stays polite.
    """
    import pandas as pd

    sources = sources or SOURCES
    user_agent = user_agent or UA
    start = datetime.strptime(start_date, "%m/%d/%Y").replace(tzinfo=IST)
    end = (datetime.strptime(end_date, "%m/%d/%Y").replace(tzinfo=IST)
           + timedelta(days=1) - timedelta(seconds=1))

    fetcher = Fetcher(user_agent=user_agent, delay=delay)

    if verbose:
        print(f"Window: {start:%Y-%m-%d} .. {end:%Y-%m-%d}   keywords: {len(keywords)}")
        print("Discovering from sitemaps...")

    candidates: list[Article] = []
    for src in sources:
        try:
            candidates.extend(discover(src, fetcher, start, end, verbose))
        except Exception as e:
            print(f"  {src.name:<20} ERROR: {e}")

    if verbose:
        print(f"\n{len(candidates)} total candidates.")

    # Cheap pass first: sitemap title, news keywords, and URL slug.
    shallow, unmatched = [], []
    for art in candidates:
        hits = match(f"{art.title} {art.keywords} {slugify(art.url)}", keywords)
        if hits:
            art.matched, art.where = hits, "title/slug"
            shallow.append(art)
        else:
            unmatched.append(art)

    if verbose:
        print(f"{len(shallow)} matched on title/slug.")

    # Some sitemaps (e.g. ABP's dated shards) carry no <news:title>, so fetch
    # the shallow matches to fill in title and publication time.
    for art in shallow:
        if art.title:
            continue
        html = fetcher.get(art.url)
        if html:
            extract(html, art)

    # Deep pass: fetch the rest and match against body text.
    deep_hits = []
    if deep and unmatched:
        budget = min(len(unmatched), deep_limit)
        if verbose:
            print(f"Deep-matching {budget} of {len(unmatched)} remaining articles...")
        for i, art in enumerate(unmatched[:budget], 1):
            html = fetcher.get(art.url)
            if not html:
                continue
            extract(html, art)
            hits = match(f"{art.title} {art.body_text}", keywords)
            if hits:
                # Classify by where the match actually lands, not by which pass
                # found it: sitemaps that omit <news:title> (ABP's dated shards)
                # push headline coverage into the deep pass, and calling that a
                # body mention understates prominence.
                art.matched = hits
                art.where = "title/slug" if match(art.title, keywords) else "body"
                deep_hits.append(art)
            if verbose and i % 25 == 0:
                print(f"    {i}/{budget}  ({len(deep_hits)} hits)")
        if verbose:
            print(f"{len(deep_hits)} matched on body text.")

    keep = [a for a in shallow + deep_hits if not is_aggregator(a)]
    dropped = len(shallow) + len(deep_hits) - len(keep)
    if verbose and dropped:
        print(f"Dropped {dropped} liveblog/aggregator pages.")

    rows = [{
        "Source": a.source,
        "Language": a.language,
        "Published": a.published.strftime("%Y-%m-%d %H:%M") if a.published else "",
        "Title": a.title,
        "Keyword": ", ".join(a.matched),
        "Matched In": a.where,
        "Links": a.url,
    } for a in keep]

    df = pd.DataFrame(rows, columns=["Source", "Language", "Published", "Title",
                                     "Keyword", "Matched In", "Links"])
    if not df.empty:
        df.drop_duplicates(subset=["Links"], inplace=True)
        df.sort_values(["Published", "Source"], ascending=[False, True], inplace=True)

    if verbose:
        print(f"\nFetch stats: {fetcher.stats}")
        print(f"{len(df)} unique articles.")
    return df
