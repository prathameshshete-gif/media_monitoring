"""Run storage.

Each pipeline execution writes a self-contained directory under runs/, so the
frontend can list past runs and the dashboard stops depending on hard-coded
spreadsheet names.

    runs/2026-08-31T1904/
        meta.json      window, subject, counts, status, timings
        articles.jsonl one record per article, with relevance score
        corpus.txt     what was sent to the LLM
        report.md      the written report (absent until generated)
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

# Overridable so the container can point it at a mounted volume; runs are
# the one thing that must outlive the image.
RUNS = Path(os.getenv("RUNS_DIR", "runs"))


def new_run_id() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H%M%S")


def run_dir(run_id: str) -> Path:
    d = RUNS / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_meta(run_id: str, **fields) -> dict:
    """Merge fields into the run's meta.json."""
    p = run_dir(run_id) / "meta.json"
    meta = json.loads(p.read_text()) if p.exists() else {"id": run_id}
    meta.update(fields)
    p.write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
    return meta


def read_meta(run_id: str) -> dict:
    p = RUNS / run_id / "meta.json"
    return json.loads(p.read_text()) if p.exists() else {}


def write_articles(run_id: str, docs) -> None:
    import relevance
    p = run_dir(run_id) / "articles.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for d in docs:
            row = dict(d.__dict__)
            row["relevance"] = relevance.sigmoid(d.score)
            row.pop("text", None)          # keep the index small
            row["words"] = d.word_count()
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_articles(run_id: str) -> list[dict]:
    p = RUNS / run_id / "articles.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def read_report(run_id: str) -> str | None:
    p = RUNS / run_id / "report.md"
    return p.read_text(encoding="utf-8") if p.exists() else None


def list_runs() -> list[dict]:
    """Newest first. Each entry is the run's meta plus its article count."""
    if not RUNS.exists():
        return []
    out = []
    for d in sorted(RUNS.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        meta = read_meta(d.name)
        if not meta:
            continue
        meta["has_report"] = (d / "report.md").exists()
        out.append(meta)
    return out
