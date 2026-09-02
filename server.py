"""Web frontend for the media monitor.

    python3 server.py            # then open http://127.0.0.1:8000

Browsing past runs is always available. Starting a new run is a background job
whose progress streams to the page over SSE -- a run takes minutes, mostly in
the cross-encoder, so it cannot be a blocking request.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import traceback
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import profiles
import store

app = FastAPI(title="Media Monitor")

# One job at a time: the reranker is the bottleneck and two concurrent runs
# would just contend for the same CPU.
JOBS: dict[str, dict] = {}
_lock = threading.Lock()


class RunRequest(BaseModel):
    start: str                      # MM/DD/YYYY
    end: str
    subject: str = "Devendra Fadnavis, Chief Minister of Maharashtra"
    terms: list[str] = ["Devendra Fadnavis", "Fadnavis", "देवेंद्र फडणवीस", "फडणवीस"]
    min_relevance: float = 0.02
    deep: bool = False
    delay: float = 1.0
    report: bool = False
    # The brief for this entity, sent explicitly by the caller. There is no
    # server-side default: an API caller that omits it and asks for a report
    # gets a clear failure on the report step rather than someone else's brief.
    system_prompt: str | None = None


# --------------------------------------------------------------------------
# Job runner
# --------------------------------------------------------------------------

def _emit(job: dict, stage: str, message: str, **extra) -> None:
    job["events"].put({"stage": stage, "message": message,
                       "t": datetime.now().strftime("%H:%M:%S"), **extra})


def _run_pipeline(job: dict, req: RunRequest) -> None:
    """Executed on a worker thread; pushes progress events as it goes."""
    import corpus
    import news_monitor as nm
    import relevance

    run_id = job["id"]
    try:
        store.write_meta(run_id, status="running", subject=req.subject,
                         window=f"{req.start} – {req.end}",
                         started=datetime.now().isoformat(timespec="seconds"),
                         min_relevance=req.min_relevance, deep=req.deep)

        _emit(job, "discover", "Reading publisher sitemaps…")
        df = nm.run(req.terms, req.start, req.end, deep=req.deep,
                    delay=req.delay, verbose=False)
        if df.empty:
            _emit(job, "done", "No articles matched in this window.")
            store.write_meta(run_id, status="empty", matched=0)
            job["state"] = "done"
            return
        _emit(job, "discover", f"{len(df)} articles matched.", count=len(df))

        _emit(job, "extract", f"Fetching text for {len(df)} articles…")
        fetcher = nm.Fetcher(user_agent=nm.__dict__.get("UA", "MediaMonitor/1.0"),
                             delay=req.delay)
        docs = corpus.build_docs(df, fetcher, verbose=False)
        _emit(job, "extract", f"{len(docs)} articles with usable text.", count=len(docs))

        _emit(job, "dedupe", "Checking for near-duplicates…")
        docs, dropped = corpus.dedupe(docs, verbose=False)
        _emit(job, "dedupe", f"{len(dropped)} near-duplicates removed.",
              count=len(docs))

        _emit(job, "rank", f"Scoring {len(docs)} articles with the cross-encoder "
                           f"(~{len(docs) * 3.5 / 60:.0f} min)…")
        relevance.score_docs(docs, req.subject, verbose=False)
        kept = [d for d in docs if relevance.sigmoid(d.score) >= req.min_relevance]
        _emit(job, "rank", f"{len(kept)} of {len(docs)} above relevance "
                           f"{req.min_relevance}.", count=len(kept))

        d = store.run_dir(run_id)
        store.write_articles(run_id, docs)          # all of them, with scores
        text = corpus.write_corpus(kept, txt_path=str(d / "corpus.txt"),
                                   jsonl_path=str(d / "corpus.jsonl"),
                                   verbose=False)
        store.write_meta(run_id, matched=len(df), extracted=len(docs),
                         duplicates=len(dropped), ranked=len(kept),
                         corpus_chars=len(text))

        if req.report:
            _emit(job, "report", "Writing the report with Gemini…")
            try:
                import report_llm
                md = report_llm.build_report(text, req.subject,
                                             report_llm.format_window(req.start,
                                                                      req.end),
                                             system_prompt=req.system_prompt,
                                             verbose=False)
                (d / "report.md").write_text(md, encoding="utf-8")
                _emit(job, "report", "Report written.")
            except Exception as e:
                _emit(job, "warn", f"Report step failed: {e}")
                store.write_meta(run_id, report_error=str(e))

        store.write_meta(run_id, status="complete",
                         finished=datetime.now().isoformat(timespec="seconds"))
        _emit(job, "done", "Run complete.", run_id=run_id)
    except Exception as e:
        traceback.print_exc()
        store.write_meta(run_id, status="failed", error=str(e))
        _emit(job, "error", str(e))
    finally:
        job["state"] = "done"


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.get("/api/runs")
def api_runs():
    return store.list_runs()


@app.get("/api/runs/{run_id}")
def api_run(run_id: str):
    meta = store.read_meta(run_id)
    if not meta:
        raise HTTPException(404, "No such run")
    return {"meta": meta,
            "articles": store.read_articles(run_id),
            "report": store.read_report(run_id)}


class Profile(BaseModel):
    id: str | None = None
    name: str
    subject: str = ""
    terms: list[str] = []
    system_prompt: str = ""


@app.get("/api/profiles")
def api_profiles():
    return profiles.load()


@app.post("/api/profiles")
def api_profile_save(p: Profile):
    try:
        return profiles.upsert(p.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/profiles/{profile_id}")
def api_profile_delete(profile_id: str):
    if not profiles.delete(profile_id):
        raise HTTPException(404, "No such profile")
    return {"deleted": profile_id}


@app.get("/api/default-prompt")
def api_default_prompt():
    """The stock Marathi brief, so the UI can offer it as a starting point."""
    import report_llm
    return {"system_prompt": report_llm.SYSTEM_PROMPT}


@app.get("/api/runs/{run_id}/report.md")
def api_report_file(run_id: str):
    """The report as a downloadable file, named after the run."""
    p = store.RUNS / run_id / "report.md"
    if not p.exists():
        raise HTTPException(404, "No report for this run")
    return FileResponse(p, media_type="text/markdown; charset=utf-8",
                        filename=f"media-report-{run_id}.md")


@app.post("/api/run")
def api_start(req: RunRequest):
    with _lock:
        if any(j["state"] == "running" for j in JOBS.values()):
            raise HTTPException(409, "A run is already in progress")
        run_id = store.new_run_id()
        job = {"id": run_id, "state": "running", "events": queue.Queue()}
        JOBS[run_id] = job
    threading.Thread(target=_run_pipeline, args=(job, req), daemon=True).start()
    return {"run_id": run_id}


@app.get("/api/jobs/{run_id}/events")
async def api_events(run_id: str):
    job = JOBS.get(run_id)
    if not job:
        raise HTTPException(404, "No such job")

    async def stream():
        while True:
            try:
                ev = job["events"].get_nowait()
            except queue.Empty:
                if job["state"] == "done":
                    break
                await asyncio.sleep(0.4)
                continue
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            if ev["stage"] in ("done", "error"):
                break

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/archive")
def api_archive():
    """What the nightly harvest has stored, and which days are missing.

    `missing` is the number to watch: a sitemap lists about 48 hours, so a day
    with no folder is a day whose news cannot be recovered.
    """
    import archive
    return archive.status()


@app.get("/api/status")
def api_status():
    running = [j["id"] for j in JOBS.values() if j["state"] == "running"]
    return {"running": running[0] if running else None}


if __name__ == "__main__":
    import os

    import uvicorn
    store.RUNS.mkdir(parents=True, exist_ok=True)
    # 0.0.0.0 in the container, where nginx on the host is the only thing that
    # can reach the published port; 127.0.0.1 when run straight from a shell.
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"),
                port=int(os.getenv("PORT", "8000")), log_level="warning")
