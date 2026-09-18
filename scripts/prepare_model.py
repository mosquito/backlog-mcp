"""Download the pinned multilingual model and save its 256-dimensional int8 variant."""

import json
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from shutil import copyfile

from huggingface_hub import snapshot_download
from model2vec import StaticModel

SOURCE = "sentence-transformers/static-similarity-mrl-multilingual-v1"
REVISION = "b68f4122911bcffcd6e1f695f2d99cd6788972d8"
DESTINATION = Path(__file__).resolve().parents[1] / "src/backlog_mcp/model"


def main() -> None:
    downloaded = snapshot_download(
        SOURCE,
        revision=REVISION,
        allow_patterns=["0_StaticEmbedding/*", "*.json", "README.md"],
        token=False,
    )
    model = StaticModel.from_pretrained(
        downloaded, dimensionality=256, quantize_to="int8", normalize=True
    )
    model.save_pretrained(DESTINATION, model_name="static-similarity-mrl-multilingual-v1-256-int8")
    copyfile(Path(downloaded) / "README.md", DESTINATION / "UPSTREAM.md")
    manifest = {
        "source": SOURCE,
        "revision": REVISION,
        "url": f"https://huggingface.co/{SOURCE}/tree/{REVISION}",
        "model2vec_version": version("model2vec"),
        "numpy_version": version("numpy"),
        "quantization": "int8",
        "dimensions": model.dim,
        "reduction": "Matryoshka truncation from 1024 to 256 dimensions",
        "license": "Apache-2.0",
        "files": {
            name: sha256((DESTINATION / name).read_bytes()).hexdigest()
            for name in ("model.safetensors", "tokenizer.json", "config.json")
        },
    }
    (DESTINATION / "provenance.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (DESTINATION / "README.md").write_text(
        "---\nlicense: apache-2.0\nlibrary_name: model2vec\n---\n\n"
        "# Bundled multilingual embeddings\n\n"
        f"Source: [{SOURCE}](https://huggingface.co/{SOURCE}).\n\n"
        "Converted with Model2Vec: Matryoshka truncation to 256 dimensions, int8 quantization, "
        "and normalized output. See `provenance.json` for the pinned revision and checksums, "
        "and `UPSTREAM.md` for the original model card.\n\n"
        "This model is trained for sentence similarity. Backlog search ranks title, body, "
        "and tags by cosine similarity; it is not a retrieval-specific model.\n"
    )
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
