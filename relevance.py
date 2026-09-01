"""Cross-encoder relevance scoring.

Keyword matching answers "is the name in here"; it cannot tell an article *about*
Fadnavis from one that mentions him in a list of attendees. A cross-encoder reads
the query and the article together and scores how well the document answers the
query, which is exactly that distinction.

Default model: BAAI/bge-reranker-v2-m3 -- XLM-RoBERTa-large based, trained
multilingually, and one of the few rerankers that handles Marathi and Hindi as
well as English. Runs on CPU; ~0.3-1s per article at 512 tokens.

Faster alternative (~2x, some quality loss):
    Alibaba-NLP/gte-multilingual-reranker-base   (306M vs 568M params)
English-only models such as cross-encoder/ms-marco-MiniLM-L-6-v2 are NOT usable
here -- they score Devanagari text essentially at random.
"""

from __future__ import annotations

import os

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

# Set RERANKER_MODEL to the gte alternative above on a memory-tight box:
# 306M params against 568M, roughly twice as fast, some quality loss.
DEFAULT_MODEL = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
# Peak memory scales with the batch; drop it to 4 when the host swaps.
DEFAULT_BATCH_SIZE = int(os.getenv("RERANKER_BATCH_SIZE", "8"))


class Reranker:
    def __init__(self, model_name: str = DEFAULT_MODEL, max_length: int = 512,
                 device: str | None = None):
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.max_length = max_length
        self.tok = AutoTokenizer.from_pretrained(model_name)
        self.model = AutoModelForSequenceClassification.from_pretrained(model_name)
        self.model.to(self.device).eval()

    @torch.inference_mode()
    def score(self, query: str, docs: list[str],
              batch_size: int = DEFAULT_BATCH_SIZE,
              verbose: bool = False) -> list[float]:
        """Relevance logits, one per doc. Higher is more relevant."""
        out: list[float] = []
        for i in range(0, len(docs), batch_size):
            batch = docs[i:i + batch_size]
            enc = self.tok([query] * len(batch), batch,
                           padding=True, truncation=True,
                           max_length=self.max_length, return_tensors="pt").to(self.device)
            logits = self.model(**enc).logits.view(-1).float()
            out.extend(logits.cpu().tolist())
            if verbose:
                print(f"    scored {min(i + batch_size, len(docs))}/{len(docs)}")
        return out


def score_docs(docs, query: str, reranker: Reranker | None = None,
               head_chars: int = 1800, verbose: bool = True):
    """Attach a relevance score to each Doc, in place, and sort by it.

    Only the headline plus the opening of the body is scored: news puts the
    subject up front, the model truncates at 512 tokens anyway, and feeding the
    tail wastes compute without changing the ranking.
    """
    reranker = reranker or Reranker()
    passages = [f"{d.title}\n\n{d.text[:head_chars]}" for d in docs]
    if verbose:
        print(f"Scoring {len(docs)} articles against: {query!r}")
    scores = reranker.score(query, passages, verbose=verbose)
    for d, s in zip(docs, scores):
        d.score = float(s)
    docs.sort(key=lambda d: d.score, reverse=True)
    return docs


def sigmoid(x: float) -> float:
    import math
    return 1 / (1 + math.exp(-x))
