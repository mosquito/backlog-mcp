"""Microbenchmark of Model2Vec static models on multilingual contextual search.

Usage:
    uv run python benchmarks/embeddings/bench.py [--models NAME ...] [--variants] [--out DIR]

Each model is loaded with `model2vec.StaticModel`, encodes the documents and the queries of
`dataset.py`, and is scored with MRR@10, Recall@1, Recall@5 and nDCG@10. Encoding throughput,
weight size on disk and matrix size in memory are recorded as well. `--variants` adds int8,
float16 and truncated-dimension variants of the named models.
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from huggingface_hub import snapshot_download
from model2vec import StaticModel

sys.path.insert(0, str(Path(__file__).parent))
from dataset import DOCUMENTS, QUERIES, Query  # noqa: E402

HERE = Path(__file__).parent
BUNDLED = HERE.parents[1] / "src/backlog_mcp/model"
PATTERNS = [
    "model.safetensors",
    "tokenizer.json",
    "config.json",
    "modules.json",
    "README.md",
    "0_StaticEmbedding/*",
    "config_sentence_transformers.json",
]
K = 10


@dataclass(frozen=True, slots=True)
class Spec:
    """One benchmarked configuration: a model plus optional query-side model and variants."""

    name: str
    source: str
    query_source: str | None = None
    quantize_to: str | None = None
    dimensionality: int | None = None
    vocabulary_quantization: int | None = None


# Multilingual candidates and English-only references, by upstream teacher.
MODELS: dict[str, Spec] = {
    spec.name: spec
    for spec in [
        Spec("bundled", str(BUNDLED)),
        Spec(
            "st-similarity-mrl-multilingual-v1",
            "sentence-transformers/static-similarity-mrl-multilingual-v1",
        ),
        Spec("st-retrieval-mrl-en-v1", "sentence-transformers/static-retrieval-mrl-en-v1"),
        Spec("potion-multilingual-128M", "minishlab/potion-multilingual-128M"),
        Spec("M2V_multilingual_output", "minishlab/M2V_multilingual_output"),
        Spec("potion-base-32M", "minishlab/potion-base-32M"),
        Spec("potion-base-8M", "minishlab/potion-base-8M"),
        Spec("potion-base-4M", "minishlab/potion-base-4M"),
        Spec("potion-base-2M", "minishlab/potion-base-2M"),
        Spec("potion-retrieval-32M", "minishlab/potion-retrieval-32M"),
        Spec("potion-code-16M-v2", "minishlab/potion-code-16M-v2"),
        Spec("jina-v3-separation (CISCai)", "CISCai/jina-embeddings-v3-separation-distilled"),
        Spec("jina-v3-matching (CISCai)", "CISCai/jina-embeddings-v3-matching-distilled"),
        Spec(
            "jina-v3-passage+query (CISCai)",
            "CISCai/jina-embeddings-v3-passage-distilled",
            query_source="CISCai/jina-embeddings-v3-query-distilled",
        ),
        Spec("jina-v3-distilled (hs-hf)", "hs-hf/jina-embeddings-v3-distilled"),
        Spec(
            "static-retrieval-multilingual-69m (amgix)",
            "amgix/static-retrieval-multilingual-69m-v1",
        ),
        Spec("multilingual-e5-large (nexus666)", "nexus666/multilingual-e5-large-m2v"),
        Spec(
            "multilingual-e5-large-instruct (jester6136)",
            "jester6136/multilingual-e5-large-instruct-m2v",
        ),
        Spec("multilingual-e5-small-16m (cnmoro)", "cnmoro/multilingual-e5-small-distilled-16m"),
        Spec(
            "multilingual-e5-small-256d (jalbarracin)",
            "jalbarracin/multilingual-e5-small-distilled-256d",
        ),
        Spec("m2v-256-multilingual-e5-small (Jarbas)", "Jarbas/m2v-256-multilingual-e5-small"),
        Spec("m2v-256-multilingual-e5-base (Jarbas)", "Jarbas/m2v-256-multilingual-e5-base"),
        Spec("m2v-256-multilingual-e5-large (Jarbas)", "Jarbas/m2v-256-multilingual-e5-large"),
        Spec(
            "m2v-256-multilingual-e5-large-instruct (Jarbas)",
            "Jarbas/m2v-256-multilingual-e5-large-instruct",
        ),
        Spec("e5-m2v-1024 (beeking)", "beeking/e5-m2v-1024"),
        Spec("multilingual-e5 (Remasalmania)", "Remasalmania/multilingual-e5"),
        Spec("m2v-bge-m3-1024d (tss-deposium)", "tss-deposium/m2v-bge-m3-1024d"),
        Spec("bge-m3-m2v-256 (FlukeTJ)", "FlukeTJ/bge-m3-m2v-distilled-256"),
        Spec("m2v-256-bge-m3 (Jarbas)", "Jarbas/m2v-256-bge-m3"),
        Spec("m2v-LaBSE-distilled (hs-hf)", "hs-hf/m2v-LaBSE-distilled"),
        Spec("m2v-256-LaBSE (Jarbas)", "Jarbas/m2v-256-LaBSE"),
        Spec("m2v-labse-ru-turbo (amekhrishvili)", "amekhrishvili/m2v-labse-ru-turbo"),
        Spec(
            "m2v-256-distiluse-multilingual (Jarbas)",
            "Jarbas/m2v-256-distiluse-base-multilingual-cased-v2",
        ),
        Spec(
            "m2v-256-paraphrase-multilingual-MiniLM (Jarbas)",
            "Jarbas/m2v-256-paraphrase-multilingual-MiniLM-L12-v2",
        ),
        Spec(
            "m2v-256-paraphrase-multilingual-mpnet (Jarbas)",
            "Jarbas/m2v-256-paraphrase-multilingual-mpnet-base-v2",
        ),
        Spec(
            "granite-97m-multilingual-r2 (Nourh7)", "Nourh7/granite-embedding-97m-multilingual-r2"
        ),
        Spec(
            "m2v-256-granite-278m-multilingual (Jarbas)",
            "Jarbas/m2v-256-granite-embedding-278m-multilingual",
        ),
        Spec("F2LLM-v2-330M (giangndm)", "giangndm/F2LLM-v2-330M-model2vec"),
        Spec("embeddinggemma (karthikrajgopal)", "karthikrajgopal/distilled-embeddinggemma"),
        Spec(
            "embeddinggemma-european (flipbitsnotburgers)",
            "flipbitsnotburgers/m2v-embeddinggemma-european",
        ),
        Spec("nomic-v2-moe-hq (cnmoro)", "cnmoro/nomic-embed-text-v2-moe-distilled-high-quality"),
        Spec("nomic-v2-moe (Abdelkareem)", "Abdelkareem/nomic-embed-text-v2-moe_distilled"),
        Spec(
            "gte-multilingual-768 (JanSchachtschabel)", "JanSchachtschabel/m2v-gte-multilingual-768"
        ),
        Spec("gte-multilingual-pca-256 (tboquet)", "tboquet/gte-multilingual-base-pca-256"),
        Spec(
            "arctic-embed-l-v2-256 (FlukeTJ)",
            "FlukeTJ/snowflake-arctic-embed-l-v2.0-m2v-distilled-256",
        ),
        Spec("Qwen3-Embedding-0.6B (Pringled)", "Pringled/m2v-Qwen3-Embedding-0.6B"),
        Spec(
            "qwen3-embedding-0.6b-1024d (tss-deposium)",
            "tss-deposium/m2v-qwen3-embedding-0.6b-1024d",
        ),
        Spec("qwen25-deposium-1024d (tss-deposium)", "tss-deposium/qwen25-deposium-1024d"),
        Spec("octen-8b-static-512 (jeadie)", "jeadie/octen-embedding-8b-static-512"),
        Spec("harrier-m2v (rahulydc)", "rahulydc/harrier-m2v"),
        Spec("starse (BorisTM)", "BorisTM/starse"),
    ]
}


def resolve(source: str) -> Path:
    path = Path(source)
    if path.is_dir():
        return path
    return Path(snapshot_download(source, allow_patterns=PATTERNS, token=False))


def load(spec: Spec, source: str) -> StaticModel:
    path = resolve(source)
    return StaticModel.from_pretrained(
        path,
        normalize=True,
        quantize_to=spec.quantize_to,
        dimensionality=spec.dimensionality,
        vocabulary_quantization=spec.vocabulary_quantization,
        force_download=False,
    )


def weight_bytes(source: str) -> int:
    path = resolve(source)
    return sum(
        file.stat().st_size
        for file in path.rglob("*")
        if file.is_file() and file.suffix in {".safetensors", ".npy", ".bin"}
    )


def normalize(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.float32)
    norms = np.linalg.norm(values, axis=1, keepdims=True)
    out = np.zeros_like(values)
    np.divide(values, norms, out=out, where=norms > 0)
    return out


def encode(model: StaticModel, texts: list[str]) -> np.ndarray:
    return normalize(model.encode(texts, max_length=None, use_multiprocessing=False))


def dcg(gains: list[int]) -> float:
    return sum(gain / math.log2(rank + 2) for rank, gain in enumerate(gains))


def score_query(query: Query, ranking: list[str]) -> dict[str, float]:
    top = ranking[:K]
    targets = set(query.targets)
    first = next((rank for rank, doc_id in enumerate(top) if doc_id in targets), None)
    ideal = sorted(query.grades.values(), reverse=True)[:K]
    return {
        "mrr": 0.0 if first is None else 1.0 / (first + 1),
        "r1": float(bool(top) and top[0] in targets),
        "r5": len(targets & set(top[:5])) / len(targets),
        "ndcg": dcg([query.grades.get(doc_id, 0) for doc_id in top]) / dcg(ideal),
    }


def evaluate(doc_vectors: np.ndarray, query_vectors: np.ndarray) -> dict[str, Any]:
    doc_ids = [doc.id for doc in DOCUMENTS]
    doc_lang = {doc.id: doc.lang for doc in DOCUMENTS}
    scores = query_vectors @ doc_vectors.T
    per_query: list[dict[str, Any]] = []
    for query, row in zip(QUERIES, scores, strict=True):
        order = np.argsort(-row, kind="stable")
        ranking = [doc_ids[index] for index in order]
        metrics = score_query(query, ranking)
        cross = any(doc_lang[target] != query.lang for target in query.targets)
        per_query.append(
            {
                **metrics,
                "id": query.id,
                "lang": query.lang,
                "kind": query.kind,
                "cross": cross,
                "top1": ranking[0],
                "score_top1": float(row[order[0]]),
                "score_target": float(max(row[doc_ids.index(t)] for t in query.targets)),
            }
        )
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in per_query:
        groups["all"].append(item)
        groups[f"lang:{item['lang']}"].append(item)
        groups["cross" if item["cross"] else "same"].append(item)
        groups[f"kind:{item['kind']}"].append(item)
    summary = {
        group: {
            metric: round(float(np.mean([item[metric] for item in items])), 4)
            for metric in ("mrr", "r1", "r5", "ndcg")
        }
        | {"n": len(items)}
        for group, items in groups.items()
    }
    # Separation: mean margin between the best target and the best non-relevant document.
    margins = []
    for query, row in zip(QUERIES, scores, strict=True):
        relevant = {doc_ids.index(doc_id) for doc_id in query.grades}
        best_target = max(row[doc_ids.index(t)] for t in query.targets)
        best_other = max(value for index, value in enumerate(row) if index not in relevant)
        margins.append(float(best_target - best_other))
    summary["all"]["margin"] = round(float(np.mean(margins)), 4)
    return {"summary": summary, "per_query": per_query}


def run(spec: Spec, repeats: int = 5) -> dict[str, Any]:
    started = time.perf_counter()
    model = load(spec, spec.source)
    query_model = load(spec, spec.query_source) if spec.query_source else model
    load_seconds = time.perf_counter() - started
    texts = [doc.text for doc in DOCUMENTS]
    queries = [query.text for query in QUERIES]
    encode(model, texts[:4])  # warm up
    started = time.perf_counter()
    for _ in range(repeats):
        doc_vectors = encode(model, texts)
    doc_seconds = (time.perf_counter() - started) / repeats
    started = time.perf_counter()
    for _ in range(repeats):
        query_vectors = encode(query_model, queries)
    query_seconds = (time.perf_counter() - started) / repeats
    started = time.perf_counter()
    for query in queries[:20]:
        encode(query_model, [query])
    single_ms = (time.perf_counter() - started) / 20 * 1000
    result = evaluate(doc_vectors, query_vectors)
    embedding = model.embedding
    result["model"] = {
        "name": spec.name,
        "source": spec.source,
        "query_source": spec.query_source,
        "quantize_to": spec.quantize_to,
        "dimensionality": spec.dimensionality,
        "vocabulary_quantization": spec.vocabulary_quantization,
        "dim": int(model.dim),
        "vocab": int(embedding.shape[0]),
        "dtype": str(embedding.dtype),
        "matrix_mb": round(embedding.nbytes / 1e6, 1),
        "weights_mb": round(weight_bytes(spec.source) / 1e6, 1),
        "load_s": round(load_seconds, 2),
        "docs_per_s": round(len(texts) / doc_seconds, 1),
        "queries_per_s": round(len(queries) / query_seconds, 1),
        "single_query_ms": round(single_ms, 3),
    }
    del model, query_model
    gc.collect()
    return result


def variants(spec: Spec, vocabulary: bool) -> list[Spec]:
    """Quantization and dimensionality variants of one model.

    Vocabulary quantization runs k-means over the whole vocabulary; it takes minutes per model
    and needs scikit-learn, so it is opt-in.
    """
    out = [spec]
    for dtype in ("float16", "int8"):
        out.append(
            Spec(f"{spec.name} [{dtype}]", spec.source, spec.query_source, quantize_to=dtype)
        )
    base = load(spec, spec.source)
    for dim in (512, 384, 256, 128, 64):
        if dim < base.dim:
            out.append(
                Spec(f"{spec.name} [{dim}d]", spec.source, spec.query_source, dimensionality=dim)
            )
            out.append(
                Spec(
                    f"{spec.name} [{dim}d int8]",
                    spec.source,
                    spec.query_source,
                    quantize_to="int8",
                    dimensionality=dim,
                )
            )
    if vocabulary:
        for centroids in (4096, 16384):
            if centroids < base.embedding.shape[0]:
                out.append(
                    Spec(
                        f"{spec.name} [vq{centroids}]",
                        spec.source,
                        spec.query_source,
                        vocabulary_quantization=centroids,
                    )
                )
    return out


def markdown(results: list[dict[str, Any]]) -> str:
    rows = sorted(results, key=lambda item: -item["summary"]["all"]["mrr"])
    lines = [
        "| model | dim | vocab | dtype | matrix MB | MRR@10 | R@1 | R@5 | nDCG@10 | cross MRR | same MRR | ru MRR | en MRR | margin | docs/s | q ms |",
        "|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in rows:
        m, s = item["model"], item["summary"]
        lines.append(
            f"| {m['name']} | {m['dim']} | {m['vocab']} | {m['dtype']} | {m['matrix_mb']} | "
            f"{s['all']['mrr']:.3f} | {s['all']['r1']:.3f} | {s['all']['r5']:.3f} | {s['all']['ndcg']:.3f} | "
            f"{s['cross']['mrr']:.3f} | {s['same']['mrr']:.3f} | {s['lang:ru']['mrr']:.3f} | {s['lang:en']['mrr']:.3f} | "
            f"{s['all']['margin']:.3f} | {m['docs_per_s']:.0f} | {m['single_query_ms']:.2f} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="*", default=None, help="Names from MODELS; default: all")
    parser.add_argument(
        "--variants", action="store_true", help="Add quantization and dimension variants"
    )
    parser.add_argument(
        "--vocabulary-quantization",
        action="store_true",
        help="Add k-means vocabulary quantization variants (slow, needs scikit-learn)",
    )
    parser.add_argument("--out", type=Path, default=HERE / "results")
    parser.add_argument("--tag", default="models", help="File stem for the results")
    args = parser.parse_args()
    names = args.models or list(MODELS)
    specs: list[Spec] = []
    for name in names:
        spec = MODELS[name]
        specs.extend(variants(spec, args.vocabulary_quantization) if args.variants else [spec])
    args.out.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for spec in specs:
        try:
            result = run(spec)
        except Exception as error:  # noqa: BLE001
            print(f"FAIL {spec.name}: {error!r}", flush=True)
            continue
        results.append(result)
        m, s = result["model"], result["summary"]["all"]
        print(
            f"{spec.name:55s} dim={m['dim']:5d} {m['dtype']:7s} {m['weights_mb']:7.1f}MB "
            f"ndcg={s['ndcg']:.3f} mrr={s['mrr']:.3f} r1={s['r1']:.3f} r5={s['r5']:.3f} "
            f"margin={s['margin']:.3f} docs/s={m['docs_per_s']:.0f}",
            flush=True,
        )
        (args.out / f"{args.tag}.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=1)
        )
        (args.out / f"{args.tag}.md").write_text(markdown(results))


if __name__ == "__main__":
    main()
