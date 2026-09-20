"""Ensembles of two or three static models by concatenation of their unit vectors.

The concatenation of unit vectors scores a pair by the sum of the cosine similarities of the
members. The matrix size column is the int8 size of every member at the chosen dimensions, the
cost of bundling the ensemble in the package.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from bench import HERE, MODELS, Spec, encode, evaluate, load  # noqa: E402
from dataset import DOCUMENTS, QUERIES  # noqa: E402

MEMBERS: dict[str, Spec] = {
    "similarity-512": Spec(
        "similarity-512",
        MODELS["st-similarity-mrl-multilingual-v1"].source,
        quantize_to="int8",
        dimensionality=512,
    ),
    "similarity-256": Spec(
        "similarity-256",
        MODELS["st-similarity-mrl-multilingual-v1"].source,
        quantize_to="int8",
        dimensionality=256,
    ),
    "potion-ml-256": Spec(
        "potion-ml-256", "minishlab/potion-multilingual-128M", quantize_to="int8"
    ),
    "potion-ml-128": Spec(
        "potion-ml-128",
        "minishlab/potion-multilingual-128M",
        quantize_to="int8",
        dimensionality=128,
    ),
    "amgix-384": Spec(
        "amgix-384", "amgix/static-retrieval-multilingual-69m-v1", quantize_to="int8"
    ),
    "amgix-256": Spec(
        "amgix-256",
        "amgix/static-retrieval-multilingual-69m-v1",
        quantize_to="int8",
        dimensionality=256,
    ),
    "jina-matching-256": Spec(
        "jina-matching-256", "CISCai/jina-embeddings-v3-matching-distilled", quantize_to="int8"
    ),
}
ENSEMBLES = [
    ("similarity-256",),
    ("similarity-512",),
    ("similarity-256", "potion-ml-256"),
    ("similarity-256", "potion-ml-128"),
    ("similarity-512", "potion-ml-256"),
    ("similarity-256", "amgix-384"),
    ("similarity-256", "amgix-256"),
    ("similarity-512", "amgix-256"),
    ("similarity-256", "jina-matching-256"),
    ("similarity-256", "potion-ml-256", "amgix-256"),
    ("similarity-512", "potion-ml-256", "amgix-384"),
    ("potion-ml-256", "amgix-384"),
]


def unit(values: np.ndarray) -> np.ndarray:
    return values / np.maximum(np.linalg.norm(values, axis=1, keepdims=True), 1e-12)


def main() -> None:
    texts = [doc.text for doc in DOCUMENTS]
    queries = [query.text for query in QUERIES]
    encoded: dict[str, tuple[np.ndarray, np.ndarray, float]] = {}
    for name, spec in MEMBERS.items():
        model = load(spec, spec.source)
        encoded[name] = (encode(model, texts), encode(model, queries), model.embedding.nbytes / 1e6)
    results = []
    for members in ENSEMBLES:
        docs = unit(np.concatenate([encoded[m][0] for m in members], axis=1))
        qs = unit(np.concatenate([encoded[m][1] for m in members], axis=1))
        result = evaluate(docs, qs)
        result["model"] = {
            "name": " + ".join(members),
            "dim": int(docs.shape[1]),
            "matrix_mb": round(sum(encoded[m][2] for m in members), 1),
        }
        results.append(result)
        s, m = result["summary"], result["model"]
        print(
            f"{m['name']:50s} dim={m['dim']:4d} int8 {m['matrix_mb']:6.1f}MB mrr={s['all']['mrr']:.3f} "
            f"r1={s['all']['r1']:.3f} r5={s['all']['r5']:.3f} ndcg={s['all']['ndcg']:.3f} "
            f"cross={s['cross']['mrr']:.3f} same={s['same']['mrr']:.3f} ru={s['lang:ru']['mrr']:.3f} "
            f"en={s['lang:en']['mrr']:.3f} margin={s['all']['margin']:.3f}",
            flush=True,
        )
    (HERE / "results" / "ensembles.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1)
    )


if __name__ == "__main__":
    main()
