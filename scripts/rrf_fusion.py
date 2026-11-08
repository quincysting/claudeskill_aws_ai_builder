"""Reciprocal Rank Fusion (RRF) for hybrid search.

Run the keyword (BM25) and vector (kNN) searches separately, then fuse them by
rank: score(doc) = sum over result lists of 1 / (k + rank), with ranks starting
at 1. Ranks are fused, not raw scores, so BM25 scores and vector similarities
never need to share a scale. A single bool/should query that mixes match and
knn can't do this: it returns raw hits with no fused score.

Standard library only, Python 3.9+. The tests are in test_rrf_fusion.py.

In an OpenSearch client, over-fetch each list before fusing:
    n = max(top_k * 2, 10)
    rrf([bm25(text, n), knn(vector, n)], top_k=top_k)
Key on your own chunk id, not on _id: Classic vector collections in OpenSearch
Serverless don't keep a client _id, so a re-seed can index a chunk twice.
"""
import math


def rrf(ranked_lists, k=60, top_k=3, key="chunk_id"):
    """Fuse ranked lists of dicts and return the best top_k (all of them if top_k is None).

    - `key` names the id field. The first copy of a doc seen is returned, with "score" set.
    - A doc counts once per list, at its first (best) rank. Later copies in the same list
      are dropped and don't push other docs down.
    - Scores are summed with math.fsum, so equal ranks give equal scores in any list order.
      Ties keep first-seen order, so docs from earlier lists win a tie.
    """
    if k < 0:
        raise ValueError(f"k must be 0 or more, got {k}")
    if top_k is not None and top_k < 0:
        raise ValueError(f"top_k must be 0 or more (or None for all), got {top_k}")
    docs, parts = {}, {}
    for ranked in ranked_lists:
        seen = set()
        for doc in ranked:
            doc_id = doc[key]
            if doc_id in seen:
                continue
            seen.add(doc_id)
            docs.setdefault(doc_id, doc)
            parts.setdefault(doc_id, []).append(1.0 / (k + len(seen)))  # rank = distinct position, from 1
    fused = [{**docs[doc_id], "score": math.fsum(p)} for doc_id, p in parts.items()]
    fused.sort(key=lambda d: d["score"], reverse=True)  # stable: ties stay in first-seen order
    return fused[:top_k]


if __name__ == "__main__":
    out = rrf([[{"chunk_id": "a"}, {"chunk_id": "b"}], [{"chunk_id": "b"}, {"chunk_id": "c"}]])
    assert [d["chunk_id"] for d in out] == ["b", "a", "c"]  # found by both lists, so first
    print("rrf OK")
