import json
import pickle

from app.config import INDEX_DIR

_chunks = json.loads((INDEX_DIR / "chunks.json").read_text())
with open(INDEX_DIR / "bm25.pkl", "rb") as f:
    _bm25 = pickle.load(f)


def search_documents(query: str, top_k: int = 5, account_id: str | None = None) -> list[dict]:
    """
    Search policies, SOPs, product docs, and customer agreements.

    Ranking favors account-specific customer agreements when an account_id is
    given, then filters out DEPRECATED sources by default (they are only
    surfaced if nothing else matches, and are always clearly labeled).
    """
    tokens = query.lower().split()
    scores = _bm25.get_scores(tokens)
    scored = list(zip(scores, _chunks))
    scored.sort(key=lambda x: x[0], reverse=True)

    results = []
    for score, chunk in scored:
        if score <= 0:
            continue
        # Boost chunks belonging to the caller's own account agreement.
        boost = 0.0
        if account_id and chunk.get("account_id") == account_id:
            boost = 5.0
        results.append((score + boost, chunk))

    results.sort(key=lambda x: x[0], reverse=True)

    current = [r for r in results if r[1]["status"] != "DEPRECATED"]
    deprecated = [r for r in results if r[1]["status"] == "DEPRECATED"]

    picked = current[:top_k]
    # Only include a deprecated doc if we're clearly short on current material,
    # and always flag it so the agent cannot pass it off as authoritative.
    if len(picked) < top_k and deprecated:
        picked.extend(deprecated[: top_k - len(picked)])

    out = []
    for score, chunk in picked:
        out.append({
            "source": chunk["source"],
            "title": chunk["title"],
            "doc_type": chunk["doc_type"],
            "status": chunk["status"],
            "authority_rank": chunk["authority_rank"],
            "account_id": chunk.get("account_id"),
            "effective_date": chunk.get("effective_date"),
            "text": chunk["text"],
            "relevance_score": round(float(score), 3),
        })
    return out
