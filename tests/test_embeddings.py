import asyncio
import json
import sqlite3
import sys
import threading
from collections.abc import AsyncIterator
from hashlib import sha256
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from aiohttp.test_utils import TestServer
from aiohttp_tiny_mcp.client import Client
from aiohttp_tiny_mcp.client_base import BaseClient, ClientError
from aiohttp_tiny_mcp.testing import connect, pick

from backlog_mcp import embeddings
from backlog_mcp.backlog import Backlog
from backlog_mcp.httpd import application
from backlog_mcp.models import Status
from backlog_mcp.server import registry


@pytest.fixture(autouse=True)
def cache_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "embeddings"
    monkeypatch.setattr(embeddings, "cache_root", lambda: path)
    return path


@pytest.fixture(params=["stdio", "http"])
async def embedding_client(
    backlog: Backlog, request: pytest.FixtureRequest
) -> AsyncIterator[BaseClient]:
    if request.param == "stdio":
        async with connect(registry) as client:
            yield client
    else:
        async with (
            TestServer(application(backlog, console=False)) as server,
            Client(str(server.make_url("/mcp")), pick(None)) as client,
        ):
            await client.initialize()
            yield client


async def test_extension_encodes_multilingual_text(embedding_client: BaseClient) -> None:
    discovery = await embedding_client.initialize()
    assert discovery["capabilities"]["extensions"][embeddings.EXTENSION]["dimensions"] == 256
    result = await embedding_client.request_method(
        "model2vec/encode",
        {"texts": ["Reset the password", "Сбросить пароль", "Bake a chocolate cake"]},
    )
    vectors = np.asarray(result["embeddings"])
    assert result["model"] == embeddings.MODEL_NAME
    assert vectors.shape == (3, 256)
    np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-6)
    assert vectors[0] @ vectors[1] > vectors[0] @ vectors[2]


def test_bundled_model_is_quantized_and_loads_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    import huggingface_hub

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("runtime must not download a model")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", forbidden)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    encoder = embeddings.Encoder()
    vectors = encoder.encode(["Hello", ""])
    assert encoder.model is not None
    assert encoder.model.embedding.dtype == np.int8
    assert vectors.shape == (2, 256)
    assert np.isfinite(vectors).all()
    assert not vectors[1].any()
    manifest = json.loads((embeddings.MODEL_PATH / "provenance.json").read_text())
    assert manifest["source"] == "sentence-transformers/static-similarity-mrl-multilingual-v1"
    for name, digest in manifest["files"].items():
        assert sha256((embeddings.MODEL_PATH / name).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("texts", [[], [" "], ["x" * 16385], ["x"] * 129])
async def test_encode_rejects_invalid_input(embedding_client: BaseClient, texts: list[str]) -> None:
    with pytest.raises(ClientError) as error:
        await embedding_client.request_method("model2vec/encode", {"texts": texts})
    assert error.value.code == -32602


async def test_missing_model_does_not_download(
    embedding_client: BaseClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(embeddings, "encoder", embeddings.Encoder(tmp_path / "missing"))
    with pytest.raises(ClientError, match="missing"):
        await embedding_client.request_method("model2vec/encode", {"texts": ["Hello"]})


async def test_inference_and_index_work_run_in_threads(
    backlog: Backlog, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop_thread = threading.get_ident()
    original_encode = embeddings.encoder.encode
    original_indexes = embeddings.cached_indexes
    called: set[str] = set()

    def encode(texts: list[str]) -> Any:
        assert threading.get_ident() != loop_thread
        called.add("encode")
        return original_encode(texts)

    def indexes(*args: Any) -> Any:
        assert threading.get_ident() != loop_thread
        called.add("indexes")
        return original_indexes(*args)

    monkeypatch.setattr(embeddings.encoder, "encode", encode)
    monkeypatch.setattr(embeddings, "cached_indexes", indexes)
    await embeddings.indexes(backlog)
    await embeddings.encode_texts(embeddings.EncodeTexts(texts=["Hello"]))
    await embeddings.search(embeddings.Search(query="Hello"), backlog)
    assert called == {"encode", "indexes"}


async def test_semantic_search_scopes_ids_and_hydrates_records(backlog: Backlog) -> None:
    current, _ = await backlog.current()
    other = await backlog.create(backlog.cwd.parent / "other")
    task = await backlog.store.add_task(
        project_id=current.id, title="Сбросить пароль", status=Status.DONE, tags=["auth"]
    )
    await backlog.store.add_task(project_id=current.id, title="Bake a chocolate cake")
    await backlog.store.add_task(project_id=other.id, title="Reset the password")
    note = await backlog.store.add_note(
        project_id=other.id, title="Сбросить пароль", shared=True, tags=["auth"]
    )
    await backlog.store.add_note(project_id=other.id, title="Private password", shared=False)
    result = await embeddings.search(embeddings.Search(query="Сбросить пароль", limit=1), backlog)
    assert result.tasks[0].id == task.id
    assert result.tasks[0].title == task.title
    assert result.notes[0].id == note.id
    assert result.notes[0].shared
    assert result.count == 2
    filtered = await embeddings.search(
        embeddings.Search(query="Сбросить пароль", status=[Status.TODO], tag="auth"), backlog
    )
    assert filtered.tasks == []
    assert [item.id for item in filtered.notes] == [note.id]
    global_result = await embeddings.search(
        embeddings.Search(query="password", all_projects=True, min_score=-1), backlog
    )
    assert len(global_result.tasks) == 2
    assert len(global_result.notes) == 2


async def test_search_marks_sources_and_raises_semantic_floor_after_fts(
    backlog: Backlog, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ = await backlog.current()
    lexical = await backlog.store.add_task(project_id=project.id, title="Needle in a haystack")
    semantic = await backlog.store.add_task(project_id=project.id, title="Related work")
    scores: list[float] = []

    def matches(*args: Any) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
        scores.append(args[-1])
        return [(lexical.id, 0.8), (semantic.id, 0.3)], []

    monkeypatch.setattr(embeddings, "match_ids", matches)
    result = await embeddings.search(embeddings.Search(query="Needle"), backlog)

    assert scores == [embeddings.FTS_MIN_SCORE]
    assert [(item.id, item.match) for item in result.tasks] == [
        (lexical.id, "fts"),
        (semantic.id, "semantic"),
    ]


async def test_search_uses_default_semantic_floor_without_fts(
    backlog: Backlog, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ = await backlog.current()
    task = await backlog.store.add_task(project_id=project.id, title="Related work")
    scores: list[float] = []

    def matches(*args: Any) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
        scores.append(args[-1])
        return [(task.id, 0.3)], []

    monkeypatch.setattr(embeddings, "match_ids", matches)
    result = await embeddings.search(embeddings.Search(query="Needle"), backlog)

    assert scores == [embeddings.DEFAULT_MIN_SCORE]
    assert result.tasks[0].match == "semantic"


async def test_index_contains_only_ids_vectors_and_digest(
    backlog: Backlog, cache_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ = await backlog.current()
    task = await backlog.store.add_task(project_id=project.id, title="Private task text")
    await embeddings.indexes(backlog)
    (path,) = cache_directory.glob("*.npz")
    with np.load(path, allow_pickle=False) as saved:
        assert set(saved.files) == {
            "signature",
            "task_ids",
            "task_vectors",
            "note_ids",
            "note_vectors",
        }
        assert saved["task_ids"].tolist() == [task.id]
        assert saved["task_vectors"].shape == (1, 256)
    original_encode = embeddings.encoder.encode
    calls: list[list[str]] = []

    def encode(texts: list[str]) -> Any:
        calls.append(texts)
        return original_encode(texts)

    monkeypatch.setattr(embeddings.encoder, "encode", encode)
    await embeddings.indexes(backlog)
    assert calls == []
    await backlog.store.update_task(task.id, title="Updated task text")
    await embeddings.indexes(backlog)
    assert len(calls) == 1
    path.write_bytes(b"broken cache")
    embeddings.states.pop(backlog.store)
    await embeddings.indexes(backlog)
    assert len(calls) == 2
    await backlog.store.delete_task(task.id)
    await embeddings.indexes(backlog)
    with np.load(path, allow_pickle=False) as saved:
        assert saved["task_ids"].tolist() == []


async def test_results_are_read_by_id_after_ranking(
    backlog: Backlog, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ = await backlog.current()
    task = await backlog.store.add_task(project_id=project.id, title="Reset the password")
    original = embeddings.match_ids

    def changed(*args: Any) -> Any:
        ids = original(*args)
        with sqlite3.connect(backlog.store.path) as connection:
            connection.execute("UPDATE tasks SET body = ? WHERE id = ?", ("Latest body", task.id))
        return ids

    monkeypatch.setattr(embeddings, "match_ids", changed)
    result = await embeddings.search(embeddings.Search(query="Reset the password"), backlog)
    assert result.tasks[0].id == task.id
    assert result.tasks[0].body == "Latest body"


async def test_concurrent_builds_of_one_index_encode_once(monkeypatch: pytest.MonkeyPatch) -> None:
    #: Two threads ask for the same index at once; the lock makes the second one a cache hit.
    original = embeddings.encoder.encode
    calls: list[int] = []

    def slow(texts: list[str]) -> Any:
        calls.append(len(texts))
        threading.Event().wait(0.05)
        return original(texts)

    monkeypatch.setattr(embeddings.encoder, "encode", slow)
    tasks = [(1, "Reset password")]
    notes = [(1, "Password policy")]
    results = await asyncio.gather(
        asyncio.to_thread(embeddings.cached_indexes, "one-db", tasks, notes),
        asyncio.to_thread(embeddings.cached_indexes, "one-db", tasks, notes),
    )
    assert calls == [2]
    assert results[0][0].ids == results[1][0].ids == [1]
    assert np.array_equal(results[0][1].vectors, results[1][1].vectors)


async def test_every_process_gets_its_own_cache_directory_and_removes_it() -> None:
    #: Two processes index the same database at once; neither sees or waits for the other.
    code = """
import tempfile
from pathlib import Path
from backlog_mcp import embeddings
root = embeddings.cache_root()
assert root.is_dir() and root.parent == Path(tempfile.gettempdir())
a, b = embeddings.cached_indexes('shared-db', [(1, 'Reset password')], [(1, 'Password policy')])
assert a.ids == [1] and b.ids == [1]
assert a.vectors.shape == b.vectors.shape == (1, 256)
assert len(list(root.glob('*.npz'))) == 1 and not list(root.glob('*.lock'))
print(root)
"""
    processes = [
        await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        for _ in range(2)
    ]
    roots: list[Path] = []
    for process in processes:
        output, error = await asyncio.wait_for(process.communicate(), timeout=30)
        assert process.returncode == 0, error.decode()
        roots.append(Path(output.decode().strip()))
    assert roots[0] != roots[1]
    #: The directory goes with the process.
    assert not roots[0].exists() and not roots[1].exists()


async def test_model_cache_is_lazy_and_invalidated_by_local_and_external_writes(
    backlog: Backlog, cache_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ = await backlog.current()
    original = embeddings.cached_indexes
    builds = 0

    def counted(*args: Any) -> Any:
        nonlocal builds
        builds += 1
        return original(*args)

    monkeypatch.setattr(embeddings, "cached_indexes", counted)
    task = await backlog.store.add_task(project_id=project.id, title="Reset the password")
    note = await backlog.store.add_note(project_id=project.id, title="Password policy")
    assert builds == 0
    assert not cache_directory.exists()
    query = embeddings.Search(query="password", min_score=-1)
    assert (await embeddings.search(query, backlog)).count == 2
    assert builds == 1
    await embeddings.search(query, backlog)
    assert builds == 1

    await backlog.store.update_task(task.id, title="Bake a cake")
    await backlog.store.update_note(note.id, body="Updated policy")
    assert builds == 1
    result = await embeddings.search(query, backlog)
    assert builds == 2
    assert result.notes[0].body == "Updated policy"
    await backlog.store.delete_task(task.id)
    await backlog.store.delete_note(note.id)
    assert builds == 2
    assert (await embeddings.search(query, backlog)).count == 0
    assert builds == 3

    task = await backlog.store.add_task(project_id=project.id, title="A new password")
    await embeddings.search(query, backlog)
    assert builds == 4
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sqlite3, sys; "
        "db = sqlite3.connect(sys.argv[1]); "
        "db.execute('UPDATE tasks SET title = ? WHERE id = ?', ('External change', sys.argv[2])); "
        "db.commit(); db.close()",
        str(backlog.store.path),
        str(task.id),
    )
    assert await asyncio.wait_for(process.wait(), timeout=10) == 0
    assert builds == 4
    result = await embeddings.search(query, backlog)
    assert builds == 5
    assert result.tasks[0].title == "External change"


async def test_startup_does_not_build_model_index(
    embedding_client: BaseClient, cache_directory: Path
) -> None:
    assert not cache_directory.exists()
