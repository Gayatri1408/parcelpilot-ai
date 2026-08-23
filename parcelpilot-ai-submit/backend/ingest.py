"""
Builds two artifacts from the raw data pack:
  1. backend/parcelpilot.db   - SQLite tables: accounts, orders, tickets
  2. backend/index/*          - chunked documents + BM25 index (pickled)

Run from the backend/ folder:
    python ingest.py
"""
import json
import pickle
import re
import sqlite3
from pathlib import Path

import openpyxl
from rank_bm25 import BM25Okapi

from app.config import DATA_DIR, DOCS_DIR, INDEX_DIR, DB_PATH, DOC_REGISTRY_PATH


def build_sqlite_db():
    xlsx_path = DATA_DIR / "ParcelPilot_Assessment_Data.xlsx"
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)

    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    for sheet_name in ("accounts", "orders", "tickets"):
        ws = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))
        headers = [str(h) for h in rows[0]]
        data_rows = rows[1:]

        col_defs = ", ".join(f'"{h}" TEXT' for h in headers)
        cur.execute(f'CREATE TABLE {sheet_name} ({col_defs})')

        placeholders = ", ".join("?" for _ in headers)
        for row in data_rows:
            values = [None if v is None else str(v) for v in row]
            cur.execute(f'INSERT INTO {sheet_name} VALUES ({placeholders})', values)

    conn.commit()
    counts = {}
    for t in ("accounts", "orders", "tickets"):
        counts[t] = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    conn.close()
    print(f"Loaded accounts({counts['accounts']}), orders({counts['orders']}), "
          f"tickets({counts['tickets']}) -> {DB_PATH}")


def chunk_text(text: str, source: str, meta: dict, max_chars=700):
    """Split a document into section-aware chunks (splits on numbered headers,
    falls back to paragraph grouping so chunks stay under max_chars)."""
    # Split on top-level numbered sections like "1. Foo" or blank-line paragraphs.
    parts = re.split(r"\n(?=\d\.\s)", text.strip())
    chunks = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if len(part) <= max_chars:
            chunks.append(part)
        else:
            paras = [p.strip() for p in part.split("\n\n") if p.strip()]
            buf = ""
            for p in paras:
                if len(buf) + len(p) + 2 > max_chars and buf:
                    chunks.append(buf)
                    buf = p
                else:
                    buf = f"{buf}\n\n{p}" if buf else p
            if buf:
                chunks.append(buf)
    records = []
    for i, c in enumerate(chunks):
        records.append({
            "id": f"{source}::chunk{i}",
            "source": source,
            "text": c,
            **meta,
        })
    return records


def build_doc_index():
    registry = json.loads(DOC_REGISTRY_PATH.read_text())
    all_chunks = []
    for filename, meta in registry.items():
        path = DOCS_DIR / filename
        text = path.read_text()
        all_chunks.extend(chunk_text(text, filename, meta))

    tokenized = [c["text"].lower().split() for c in all_chunks]
    bm25 = BM25Okapi(tokenized)

    INDEX_DIR.mkdir(exist_ok=True)
    with open(INDEX_DIR / "chunks.json", "w") as f:
        json.dump(all_chunks, f, indent=2)
    with open(INDEX_DIR / "bm25.pkl", "wb") as f:
        pickle.dump(bm25, f)

    print(f"Indexed {len(all_chunks)} chunks from {len(registry)} documents -> {INDEX_DIR}")


if __name__ == "__main__":
    build_sqlite_db()
    build_doc_index()
