# Embedding model microbenchmark

Compares static embedding models that `model2vec.StaticModel` can load, on a multilingual
contextual search task shaped like the backlog: 60 task-and-note documents in 20 topic
clusters (English, Russian, German, Spanish, French) and 137 queries (English, Russian, German,
Spanish, French). 80 queries target a document written in another language than the query.

Files:

- `dataset.py`: documents, queries and relevance grades. A target document has grade 3; the
  other documents of its cluster have grade 1.
- `bench.py`: loads each model, encodes documents and queries, reports MRR@10, Recall@1,
  Recall@5, nDCG@10 per language and per query kind, the margin between the best target and
  the best unrelated document, encode throughput, size on disk and in memory.
  `--variants` adds float16, int8, truncated-dimension and vocabulary-quantized variants.
- `lexical.py`: SQLite FTS5 BM25 with prefix matching, the lexical half of the backlog search,
  as a reference point.
- `results/`: the JSON and Markdown output of the last run.

Run:

```sh
uv run --with huggingface_hub python benchmarks/embeddings/bench.py
uv run --with huggingface_hub python benchmarks/embeddings/bench.py --variants \
    --models st-similarity-mrl-multilingual-v1 potion-multilingual-128M --tag variants
uv run python benchmarks/embeddings/lexical.py
```

The first run downloads about 11 GB of weights into the Hugging Face cache. Recall@1 and MRR
are the numbers that matter for the backlog: a search returns a handful of hits and the agent
reads the first ones. nDCG@10 also rewards ranking the topic neighbors next, so its ceiling is
below 1 for a model that ranks only the target well.

## Results (2026-09-20, Apple M4, model2vec 0.9.0)

Full tables: `results/models.md`, `results/variants.md`, `results/ensembles.json`,
`results/lexical.json`. 50 models loaded; `BorisTM/starse` has no `embeddings` tensor.

Best models, MRR@10 over the 137 queries; cross = query language differs from the document:

| model | dim | dtype | matrix MB | MRR | R@1 | R@5 | cross | same | ru | en |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| st-similarity-mrl-multilingual-v1 (full) | 1024 | float32 | 433.7 | 0.931 | 0.883 | 0.993 | 0.963 | 0.888 | 0.944 | 0.909 |
| bundled: the same at 512d int8 | 512 | int8 | 54.2 | 0.931 | 0.883 | 0.982 | 0.964 | 0.886 | 0.946 | 0.908 |
| the same at 256d int8 (bundle before task #99) | 256 | int8 | 27.1 | 0.908 | 0.847 | 0.950 | 0.937 | 0.868 | 0.908 | 0.896 |
| potion-multilingual-128M | 256 | float32 | 512.4 | 0.908 | 0.861 | 0.967 | 0.874 | 0.957 | 0.860 | 0.943 |
| jina-v3-matching (CISCai) | 256 | float32 | 256.0 | 0.857 | 0.774 | 0.957 | 0.861 | 0.851 | 0.824 | 0.876 |
| static-retrieval-multilingual-69m (amgix) | 384 | float32 | 276.4 | 0.848 | 0.781 | 0.929 | 0.768 | 0.959 | 0.751 | 0.930 |
| M2V_multilingual_output | 256 | float32 | 513.1 | 0.821 | 0.752 | 0.914 | 0.826 | 0.813 | 0.769 | 0.858 |
| m2v-LaBSE-distilled (hs-hf) | 384 | float32 | 769.6 | 0.815 | 0.737 | 0.914 | 0.813 | 0.816 | 0.768 | 0.845 |
| gte-multilingual-pca-256 (tboquet) | 256 | float32 | 256.0 | 0.803 | 0.730 | 0.881 | 0.722 | 0.917 | 0.792 | 0.786 |
| FTS5 BM25 prefix (lexical reference) | - | - | - | 0.675 | 0.591 | 0.774 | 0.519 | 0.895 | 0.492 | 0.859 |

The other 40 models score between 0.08 and 0.77. Plain Model2Vec distillations of e5, bge-m3,
Qwen3, gemma, granite and nomic teachers without training land at 0.33 to 0.50, below the
lexical reference. The English-only potion and static-retrieval models land at 0.38 to 0.50.
The CISCai passage+query pair scores 0.075: its two matrices do not share one space.

Quantization and dimensions of the bundled model (`results/variants.md`):

| variant | matrix MB | MRR | R@1 | R@5 | cross |
|---|---:|---:|---:|---:|---:|
| 1024d float32 | 433.7 | 0.931 | 0.883 | 0.993 | 0.963 |
| 1024d int8 | 108.4 | 0.931 | 0.883 | 0.993 | 0.963 |
| 512d int8 | 54.2 | 0.931 | 0.883 | 0.982 | 0.964 |
| 384d int8 | 40.7 | 0.919 | 0.869 | 0.971 | 0.955 |
| 256d int8 | 27.1 | 0.908 | 0.847 | 0.950 | 0.937 |
| 128d int8 | 13.6 | 0.873 | 0.810 | 0.931 | 0.922 |
| 64d int8 | 6.8 | 0.782 | 0.701 | 0.901 | 0.834 |
| 1024d, vocabulary k-means 16384 | 67.1 | 0.879 | 0.818 | 0.934 | 0.883 |
| 1024d, vocabulary k-means 4096 | 16.8 | 0.847 | 0.781 | 0.894 | 0.845 |

int8 and float16 change no ranking. Dimension truncation is free down to 512 and costs 1.2
points of MRR per step below. Vocabulary quantization is the worst trade at every size and
encodes half as fast.

Ensembles by concatenation of unit vectors (`results/ensembles.json`, int8 sizes):

| ensemble | matrix MB | MRR | R@1 | R@5 | cross | same |
|---|---:|---:|---:|---:|---:|---:|
| similarity 512d | 54.2 | 0.931 | 0.883 | 0.982 | 0.964 | 0.886 |
| similarity 512d + amgix 256d | 100.3 | 0.959 | 0.934 | 0.985 | 0.962 | 0.955 |
| similarity 256d + amgix 256d | 73.2 | 0.954 | 0.927 | 0.971 | 0.962 | 0.943 |
| similarity 512d + potion-multilingual 256d | 182.3 | 0.951 | 0.920 | 0.989 | 0.954 | 0.948 |
| similarity 512d + potion-multilingual 256d + amgix 384d | 251.4 | 0.963 | 0.942 | 0.982 | 0.960 | 0.966 |

The similarity model wins on cross-lingual queries; potion-multilingual and amgix win on
same-language queries, so a pair complements. A second model doubles the tokenizers, the load
time and the encoder code for 2 to 3 points of MRR; not adopted, kept here as the next step if
same-language precision becomes the bottleneck.

Decision: bundle `static-similarity-mrl-multilingual-v1` at 512 dimensions int8 (task #99).
