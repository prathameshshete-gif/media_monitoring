"""Fetch full article text for matched links and write an LLM-ready corpus file.

Stage 2 of the pipeline: news_monitor.run() produces links; this visits each one,
pulls clean body text, drops near-duplicates, and writes both a JSONL record file
and a plain-text corpus that can be handed to an LLM.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

from news_monitor import Fetcher

BOILERPLATE = re.compile(
    r"(also read|read more|संबंधित बातम्या|हेही वाचा|ताज्या बातम्या|"
    r"यह भी पढ़ें|और पढ़ें|follow us|डाउनलोड करा|subscribe)",
    re.I,
)


@dataclass
class Doc:
    url: str
    source: str
    published: str
    title: str
    text: str
    lang: str = ""
    score: float = 0.0
    dupe_of: str = ""
    partial: bool = False

    def word_count(self) -> int:
        return len(self.text.split())


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

def article_text(html: bytes) -> tuple[str, str]:
    """Return (title, body_text) from an article page."""
    soup = BeautifulSoup(html, "lxml")

    title = ""
    for sel in ("meta[property='og:title']", "h1", "title"):
        el = soup.select_one(sel)
        if el:
            title = (el.get("content") or el.get_text(" ", strip=True)).strip()
            if title:
                break

    for junk in soup(["script", "style", "nav", "header", "footer", "aside",
                      "form", "figure", "iframe", "noscript"]):
        junk.decompose()
    for sel in (".related", ".also-read", ".recommended", ".taboola",
                "[class*='related']", "[class*='trending']", "[id*='comment']"):
        for el in soup.select(sel):
            el.decompose()

    body = soup.find("article") or soup.find("main") or soup.body or soup

    # Paragraph-level assembly beats a flat get_text(): it keeps sentence
    # boundaries the LLM needs and makes boilerplate lines easy to drop.
    paras = []
    for p in body.find_all(["p", "h2", "h3"]):
        t = p.get_text(" ", strip=True)
        if len(t) < 25 or BOILERPLATE.search(t):
            continue
        paras.append(t)

    if not paras:  # some templates render the body without <p> tags
        paras = [body.get_text(" ", strip=True)]

    seen, out = set(), []
    for t in paras:
        if t not in seen:      # repeated nav/teaser lines
            seen.add(t)
            out.append(t)
    return title, "\n\n".join(out)


def strip_boilerplate(docs: list[Doc], min_share: float = 0.3,
                      verbose: bool = True) -> list[Doc]:
    """Remove lines that recur across articles from the same source.

    Nav menus, section lists and standing footers survive tag-based cleaning
    because publishers mark them up as ordinary paragraphs. But they are
    identical across that publisher's articles, and real article prose is not --
    so frequency within a source separates them without per-site selectors.
    """
    from collections import Counter, defaultdict

    per_source = defaultdict(list)
    for d in docs:
        per_source[d.source].append(d)

    removed = 0
    for source, group in per_source.items():
        if len(group) < 3:
            continue  # too few samples to tell boilerplate from coincidence
        freq = Counter()
        for d in group:
            freq.update(set(d.text.split("\n\n")))
        cut = {line for line, n in freq.items()
               if n / len(group) >= min_share and n > 2}
        for d in group:
            kept = [ln for ln in d.text.split("\n\n") if ln not in cut]
            removed += len(d.text.split("\n\n")) - len(kept)
            d.text = "\n\n".join(kept)

    if verbose and removed:
        print(f"Stripped {removed} boilerplate lines across sources.")
    return docs


def build_docs(df: pd.DataFrame, fetcher: Fetcher, min_words: int = 60,
               verbose: bool = True) -> list[Doc]:
    docs: list[Doc] = []
    for i, r in enumerate(df.itertuples(), 1):
        html = fetcher.get(r.Links)
        if not html:
            continue
        title, text = article_text(html)
        if len(text.split()) < 25:
            continue          # empty shell, gallery, or video page
        docs.append(Doc(
            url=r.Links,
            source=r.Source,
            published=str(r.Published),
            title=title or (r.Title if isinstance(r.Title, str) else ""),
            text=text,
            lang=getattr(r, "Language", ""),
        ))
        if verbose and i % 20 == 0:
            print(f"  extracted {len(docs)}/{i}")

    strip_boilerplate(docs, verbose=verbose)
    docs = [d for d in docs if d.word_count() >= min_words]

    # Some publishers (eSakal) serve only the lede to anonymous readers and put
    # the rest behind a login. That is their access control, so the lede is all
    # we get -- label it rather than pretend it is the whole article.
    for d in docs:
        d.partial = d.word_count() < 120
    n_partial = sum(d.partial for d in docs)
    if verbose and n_partial:
        print(f"{n_partial} articles are lede-only (paywalled beyond the intro).")
    if verbose:
        print(f"Extracted {len(docs)} articles with >= {min_words} words.")
    return docs


# --------------------------------------------------------------------------
# Near-duplicate removal
# --------------------------------------------------------------------------

def _shingles(text: str, k: int = 5) -> set[int]:
    """Hashed word k-grams. Works for Devanagari and Latin alike."""
    words = re.findall(r"\w+", unicodedata.normalize("NFC", text.lower()))
    if len(words) < k:
        return {hash(" ".join(words))} if words else set()
    return {hash(" ".join(words[i:i + k])) for i in range(len(words) - k + 1)}


def jaccard(a: set[int], b: set[int]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def dedupe(docs: list[Doc], threshold: float = 0.55,
           verbose: bool = True) -> tuple[list[Doc], list[Doc]]:
    """Drop syndicated re-runs of the same story.

    URL de-duplication is already done upstream; this catches the harder case --
    the same PTI/ANI copy republished by three mastheads under different URLs and
    slightly different headlines. Compares hashed 5-gram shingles, keeping the
    longest version of each cluster.
    """
    ordered = sorted(docs, key=lambda d: d.word_count(), reverse=True)
    sigs = {d.url: _shingles(d.text) for d in ordered}

    kept: list[Doc] = []
    dropped: list[Doc] = []
    for d in ordered:
        hit = next((k for k in kept if jaccard(sigs[d.url], sigs[k.url]) >= threshold), None)
        if hit:
            d.dupe_of = hit.url
            dropped.append(d)
        else:
            kept.append(d)

    kept.sort(key=lambda d: d.published, reverse=True)
    if verbose:
        print(f"Deduped: kept {len(kept)}, dropped {len(dropped)} near-duplicates.")
    return kept, dropped


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def write_corpus(docs: list[Doc], txt_path="corpus.txt", jsonl_path="corpus.jsonl",
                 max_chars: int = 6000, verbose: bool = True) -> str:
    """Write a numbered plain-text corpus for the LLM, plus JSONL for everything else.

    Each article is delimited and numbered so the model can cite [1], [2] ... and
    those indices resolve back to real URLs.
    """
    lines = []
    for i, d in enumerate(docs, 1):
        body = d.text[:max_chars]
        if len(d.text) > max_chars:
            body += " …[truncated]"
        lines.append(
            f"=== ARTICLE {i} ===\n"
            f"Source: {d.source}\n"
            f"Published: {d.published}\n"
            f"Language: {d.lang}\n"
            f"URL: {d.url}\n"
            f"Relevance: {d.score:.3f}\n"
            + ("Note: lede only -- full text is behind the publisher's paywall.\n"
               if d.partial else "")
            + f"Headline: {d.title}\n\n"
            f"{body}\n"
        )
    text = "\n".join(lines)
    Path(txt_path).write_text(text, encoding="utf-8")

    with open(jsonl_path, "w", encoding="utf-8") as f:
        for d in docs:
            f.write(json.dumps(d.__dict__, ensure_ascii=False) + "\n")

    if verbose:
        print(f"Wrote {txt_path} ({len(text):,} chars, {len(docs)} articles) "
              f"and {jsonl_path}")
    return text
