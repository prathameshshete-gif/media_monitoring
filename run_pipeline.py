"""End-to-end: discover links -> extract text -> dedupe -> rank -> LLM report.

    python3 run_pipeline.py --start 08/30/2026 --end 08/31/2026
"""

from __future__ import annotations

import argparse
from pathlib import Path

import corpus
import news_monitor as nm
import relevance

SUBJECT = "Devendra Fadnavis, Chief Minister of Maharashtra"
CORE_TERMS = ["Devendra Fadnavis", "Fadnavis", "देवेंद्र फडणवीस", "फडणवीस"]
UA = nm.UA


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="MM/DD/YYYY")
    ap.add_argument("--end", required=True, help="MM/DD/YYYY")
    ap.add_argument("--min-relevance", type=float, default=0.02,
                    help="cross-encoder cutoff, 0-1. The score distribution is "
                         "sharply bimodal, so 0.02 sits inside an empty gap; "
                         "use 0.3 for a tight editorial set (default 0.02)")
    ap.add_argument("--deep", action="store_true",
                    help="also body-match candidates (slow; see README on why "
                         "this is off by default)")
    ap.add_argument("--deep-limit", type=int, default=200)
    ap.add_argument("--delay", type=float, default=2.0)
    ap.add_argument("--report", action="store_true", help="call Gemini for the write-up")
    ap.add_argument("--out", default="articles.xlsx")
    args = ap.parse_args()

    # 1. discover
    df = nm.run(CORE_TERMS, args.start, args.end, user_agent=UA,
                deep=args.deep, deep_limit=args.deep_limit, delay=args.delay)
    if df.empty:
        print("No articles found in window.")
        return
    df.to_excel(args.out, index=False)

    # 2. full text
    fetcher = nm.Fetcher(user_agent=UA, delay=args.delay)
    docs = corpus.build_docs(df, fetcher)

    # 3. near-duplicate removal
    docs, dropped = corpus.dedupe(docs)

    # 4. relevance
    relevance.score_docs(docs, SUBJECT)
    ranked = [d for d in docs if relevance.sigmoid(d.score) >= args.min_relevance]
    print(f"Kept {len(ranked)}/{len(docs)} above relevance {args.min_relevance}.")

    text = corpus.write_corpus(ranked)

    # 5. report
    if args.report:
        import report_llm
        window = report_llm.format_window(args.start, args.end)
        report = report_llm.build_report(text, SUBJECT, window)
        Path("report_llm.md").write_text(report, encoding="utf-8")
        print("Wrote report_llm.md")
    else:
        print("Skipped the LLM report (pass --report to generate it).")


if __name__ == "__main__":
    main()
