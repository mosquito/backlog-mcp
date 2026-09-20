"""Lexical reference point for the embedding benchmark: SQLite FTS5 with BM25 ranking.

Prefix matching on every token mirrors the FTS half of the backlog search. The result shows how
much the semantic half adds, in particular on cross-lingual queries where lexical search finds
nothing.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from bench import HERE, evaluate  # noqa: E402
from dataset import DOCUMENTS, QUERIES  # noqa: E402

TOKEN = re.compile(r"\w+", re.UNICODE)


def match_expression(query: str) -> str:
    tokens = [f'"{token}"*' for token in TOKEN.findall(query.lower())]
    return " OR ".join(tokens)


def main() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE VIRTUAL TABLE docs USING fts5(id UNINDEXED, text, tokenize='unicode61 remove_diacritics 2')"
    )
    connection.executemany(
        "INSERT INTO docs(id, text) VALUES (?, ?)", [(doc.id, doc.text) for doc in DOCUMENTS]
    )
    ids = [doc.id for doc in DOCUMENTS]
    scores = np.zeros((len(QUERIES), len(DOCUMENTS)), dtype=np.float32)
    for row, query in enumerate(QUERIES):
        hits = connection.execute(
            "SELECT id, -bm25(docs) FROM docs WHERE docs MATCH ? ORDER BY bm25(docs)",
            (match_expression(query.text),),
        ).fetchall()
        for doc_id, score in hits:
            scores[row, ids.index(doc_id)] = score
    # `evaluate` expects unit vectors; feed the score matrix through an identity document basis.
    result = evaluate(np.eye(len(DOCUMENTS), dtype=np.float32), scores)
    result["model"] = {
        "name": "FTS5 BM25 prefix (lexical reference)",
        "dim": 0,
        "vocab": 0,
        "dtype": "-",
        "weights_mb": 0.0,
        "docs_per_s": 0.0,
        "single_query_ms": 0.0,
    }
    out = HERE / "results" / "lexical.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps([result], ensure_ascii=False, indent=1))
    s = result["summary"]
    print(
        json.dumps(
            {group: s[group] for group in ("all", "cross", "same", "lang:ru", "lang:en")}, indent=1
        )
    )


if __name__ == "__main__":
    main()
