import asyncio
import json
import sqlite3
import threading
from collections.abc import AsyncIterator
from hashlib import sha256
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from aiohttp.test_utils import TestServer
from aiohttp_tiny_mcp import Client
from aiohttp_tiny_mcp.client.base import BaseClient, ClientError
from aiohttp_tiny_mcp.testing import connect, pick
from helpers import mutate

from backlog_mcp import embeddings
from backlog_mcp import server as operations
from backlog_mcp.backlog import Backlog
from backlog_mcp.httpd import application
from backlog_mcp.models import Status
from backlog_mcp.server import registry


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
    assert discovery["capabilities"]["extensions"][embeddings.EXTENSION]["dimensions"] == 512
    result = await embedding_client.request_method(
        "model2vec/encode",
        {"texts": ["Reset the password", "Сбросить пароль", "Bake a chocolate cake"]},
    )
    vectors = np.asarray(result["embeddings"])
    assert result["model"] == embeddings.MODEL_NAME
    assert vectors.shape == (3, 512)
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
    assert vectors.shape == (2, 512)
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
    original_indexes = embeddings.build_indexes
    called: set[str] = set()

    def encode(texts: list[str]) -> Any:
        assert threading.get_ident() != loop_thread
        called.add("encode")
        return original_encode(texts)

    def indexes(*args: Any, **kwargs: Any) -> Any:
        assert threading.get_ident() != loop_thread
        called.add("indexes")
        return original_indexes(*args, **kwargs)

    monkeypatch.setattr(embeddings.encoder, "encode", encode)
    monkeypatch.setattr(embeddings, "build_indexes", indexes)
    await embeddings.indexes(
        backlog, connection=await backlog.open(), transaction=backlog.transaction
    )
    await embeddings.encode_texts(embeddings.EncodeTexts(texts=["Hello"]))
    await embeddings.search(
        embeddings.Search(query="Hello"),
        backlog,
        connection=await backlog.open(),
        transaction=backlog.transaction,
    )
    assert called == {"encode", "indexes"}


async def test_semantic_search_scopes_ids_and_hydrates_records(backlog: Backlog) -> None:
    current, _ = await backlog.current()
    other = await backlog.create(backlog.cwd.parent / "other")
    task = await mutate(
        operations.add_task(
            await backlog.open(),
            backlog.actor,
            project_id=current.id,
            title="Сбросить пароль",
            status=Status.DONE,
            tags=["auth"],
        ),
        backlog.transaction,
    )
    await mutate(
        operations.add_task(
            await backlog.open(),
            backlog.actor,
            project_id=current.id,
            title="Bake a chocolate cake",
        ),
        backlog.transaction,
    )
    await mutate(
        operations.add_task(
            await backlog.open(), backlog.actor, project_id=other.id, title="Reset the password"
        ),
        backlog.transaction,
    )
    note = await mutate(
        operations.add_note(
            await backlog.open(),
            backlog.actor,
            project_id=other.id,
            title="Сбросить пароль",
            shared=True,
            tags=["auth"],
        ),
        backlog.transaction,
    )
    await mutate(
        operations.add_note(
            await backlog.open(),
            backlog.actor,
            project_id=other.id,
            title="Private password",
            shared=False,
        ),
        backlog.transaction,
    )
    result = await embeddings.search(
        embeddings.Search(query="Сбросить пароль", limit=1),
        backlog,
        connection=await backlog.open(),
        transaction=backlog.transaction,
    )
    assert result.tasks[0].id == task.id
    assert result.tasks[0].title == task.title
    assert result.memory[0].id == note.id
    assert result.memory[0].shared
    assert result.count == 2
    filtered = await embeddings.search(
        embeddings.Search(query="Сбросить пароль", status=[Status.TODO], tag="auth"),
        backlog,
        connection=await backlog.open(),
        transaction=backlog.transaction,
    )
    assert filtered.tasks == []
    assert [item.id for item in filtered.memory] == [note.id]
    global_result = await embeddings.search(
        embeddings.Search(query="password", all_projects=True, min_score=-1),
        backlog,
        connection=await backlog.open(),
        transaction=backlog.transaction,
    )
    # An explicit -1 includes every nonzero vector, even with lexical matches.
    assert len(global_result.tasks) == 3
    assert len(global_result.memory) == 2


@pytest.mark.parametrize("min_score", [None, 0.12, 0.15, 0.5])
async def test_search_marks_sources_and_honors_semantic_floor_after_fts(
    backlog: Backlog, monkeypatch: pytest.MonkeyPatch, min_score: float | None
) -> None:
    project, _ = await backlog.current()
    lexical = await mutate(
        operations.add_task(
            await backlog.open(), backlog.actor, project_id=project.id, title="Needle in a haystack"
        ),
        backlog.transaction,
    )
    semantic = await mutate(
        operations.add_task(
            await backlog.open(), backlog.actor, project_id=project.id, title="Related work"
        ),
        backlog.transaction,
    )
    scores: list[float] = []

    def matches(
        *args: Any,
    ) -> tuple[list[tuple[int, float]], list[tuple[int, float]], list[tuple[int, float]]]:
        scores.append(args[-1])
        return [(lexical.id, 0.8), (semantic.id, 0.3)], [], [], []

    monkeypatch.setattr(embeddings, "match_ids", matches)
    result = await embeddings.search(
        embeddings.Search(
            query="Needle", **({"min_score": min_score} if min_score is not None else {})
        ),
        backlog,
        connection=await backlog.open(),
        transaction=backlog.transaction,
    )

    assert scores == [embeddings.FTS_MIN_SCORE if min_score is None else min_score]
    assert [(item.id, item.match) for item in result.tasks] == [
        (lexical.id, "fts"),
        (semantic.id, "semantic"),
    ]


async def test_search_uses_default_semantic_floor_without_fts(
    backlog: Backlog, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ = await backlog.current()
    task = await mutate(
        operations.add_task(
            await backlog.open(), backlog.actor, project_id=project.id, title="Related work"
        ),
        backlog.transaction,
    )
    scores: list[float] = []

    def matches(
        *args: Any,
    ) -> tuple[list[tuple[int, float]], list[tuple[int, float]], list[tuple[int, float]]]:
        scores.append(args[-1])
        return [(task.id, 0.3)], [], [], []

    monkeypatch.setattr(embeddings, "match_ids", matches)
    result = await embeddings.search(
        embeddings.Search(query="Needle"),
        backlog,
        connection=await backlog.open(),
        transaction=backlog.transaction,
    )

    assert scores == [embeddings.DEFAULT_MIN_SCORE]
    assert result.tasks[0].match == "semantic"


async def test_index_retains_vectors_for_unchanged_text(
    backlog: Backlog, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ = await backlog.current()
    connection = await backlog.open()
    task = await mutate(
        operations.add_task(
            connection, backlog.actor, project_id=project.id, title="Private task text"
        ),
        backlog.transaction,
    )
    before = await embeddings.indexes(backlog, connection, backlog.transaction)
    assert before[0].ids == [task.id]
    assert before[0].vectors.shape == (1, 512)
    assert before[0].digests == {task.id: sha256(embeddings.document(task).encode()).digest()}
    original = embeddings.encoder.encode
    calls = []

    def encode(texts: list[str]) -> Any:
        calls.extend(texts)
        return original(texts)

    monkeypatch.setattr(embeddings.encoder, "encode", encode)
    indexer = embeddings.indexers[backlog.database]
    embeddings.enqueue(backlog, task)
    await indexer.refresh()
    assert calls == []
    task = await mutate(
        operations.update_task(connection, backlog.actor, task.id, title="Updated task text"),
        backlog.transaction,
    )
    assert task is not None
    embeddings.enqueue(backlog, task)
    assert (
        await embeddings.indexes(backlog, connection, backlog.transaction) is before or calls == []
    )
    await indexer.refresh()
    assert calls == [embeddings.document(task)]


async def test_results_are_read_by_id_after_ranking(
    backlog: Backlog, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ = await backlog.current()
    task = await mutate(
        operations.add_task(
            await backlog.open(), backlog.actor, project_id=project.id, title="Reset the password"
        ),
        backlog.transaction,
    )
    original = embeddings.match_ids

    def changed(*args: Any) -> Any:
        ids = original(*args)
        with sqlite3.connect(backlog.database.path) as connection:
            connection.execute("UPDATE tasks SET body = ? WHERE id = ?", ("Latest body", task.id))
        return ids

    monkeypatch.setattr(embeddings, "match_ids", changed)
    result = await embeddings.search(
        embeddings.Search(query="Reset the password"),
        backlog,
        connection=await backlog.open(),
        transaction=backlog.transaction,
    )
    assert result.tasks[0].id == task.id
    assert result.tasks[0].body == "Latest body"


async def test_concurrent_first_searches_build_once(
    backlog: Backlog, monkeypatch: pytest.MonkeyPatch
) -> None:
    await backlog.current()
    original = embeddings.build_indexes
    calls = []

    def build(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(embeddings, "build_indexes", build)
    connection = await backlog.open()
    results = await asyncio.gather(
        *(embeddings.indexes(backlog, connection, backlog.transaction) for _ in range(3))
    )
    assert len(calls) == 1
    assert results[0] is results[1] is results[2]


async def test_empty_tick_does_not_read_or_encode(
    backlog: Backlog, monkeypatch: pytest.MonkeyPatch
) -> None:
    await backlog.current()
    connection = await backlog.open()
    before = await embeddings.indexes(backlog, connection, backlog.transaction)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("an unchanged index needs no snapshot or encoding")

    monkeypatch.setattr(embeddings, "snapshot", forbidden)
    monkeypatch.setattr(embeddings, "build_indexes", forbidden)
    await embeddings.indexers[backlog.database].refresh()
    assert await embeddings.indexes(backlog, connection, backlog.transaction) is before


async def test_local_queue_and_external_refresh(
    backlog: Backlog, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ = await backlog.current()
    connection = await backlog.open()
    await embeddings.indexes(backlog, connection, backlog.transaction)
    indexer = embeddings.indexers[backlog.database]
    original_snapshot = embeddings.snapshot
    original_encode = embeddings.encoder.encode
    snapshots, texts = [], []

    async def snapshot(*args: Any, **kwargs: Any) -> Any:
        snapshots.append(1)
        return await original_snapshot(*args, **kwargs)

    def encode(items: list[str]) -> Any:
        texts.extend(items)
        return original_encode(items)

    monkeypatch.setattr(embeddings, "snapshot", snapshot)
    monkeypatch.setattr(embeddings.encoder, "encode", encode)
    task = await mutate(
        operations.add_task(connection, backlog.actor, project_id=project.id, title="Local task"),
        backlog.transaction,
    )
    embeddings.enqueue(backlog, task)
    await indexer.refresh()
    assert snapshots == []
    assert texts == [embeddings.document(task)]
    texts.clear()
    # Service writes change data_version without changing any indexed text.
    with sqlite3.connect(backlog.database.path) as external:
        external.execute("CREATE TABLE service_state (id INTEGER)")
    await indexer.refresh()
    assert len(snapshots) == 1
    assert texts == []
    # An external edit must supersede an older queued local value.
    embeddings.enqueue(backlog, task)
    with sqlite3.connect(backlog.database.path) as external:
        external.execute("UPDATE tasks SET title = 'External task' WHERE id = ?", (task.id,))
    await indexer.refresh()
    assert len(snapshots) == 2
    assert texts == ["External task\n\n"]
    assert not indexer.pending


async def test_worker_updates_after_one_second_and_stops(backlog: Backlog) -> None:
    project, _ = await backlog.current()
    connection = await backlog.open()
    before = await embeddings.indexes(backlog, connection, backlog.transaction)
    task = await operations.task_add(
        operations.AddTask(title="Queued task"), backlog, connection, backlog.transaction
    )
    assert task.id not in before[0].ids
    indexer = embeddings.indexers[backlog.database]
    async with asyncio.timeout(3):
        while (
            task.id
            not in (await embeddings.indexes(backlog, connection, backlog.transaction))[0].ids
        ):
            await asyncio.sleep(0.02)
    worker = indexer.worker
    assert worker is not None and not worker.done()
    await backlog.close()
    assert worker.done()
    assert backlog.database not in embeddings.indexers


async def test_startup_does_not_build_model_index(
    embedding_client: BaseClient, backlog: Backlog
) -> None:
    assert backlog.database not in embeddings.indexers


async def test_search_uses_old_index_during_rebuild_and_keeps_new_queue(
    backlog: Backlog, monkeypatch: pytest.MonkeyPatch
) -> None:
    await backlog.current()
    connection = await backlog.open()
    task = await operations.task_add(
        operations.AddTask(title="Reset password"), backlog, connection, backlog.transaction
    )
    old = await embeddings.indexes(backlog, connection, backlog.transaction)
    indexer = embeddings.indexers[backlog.database]
    entered, release = threading.Event(), threading.Event()
    original = embeddings.build_indexes

    def slow(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(embeddings, "build_indexes", slow)
    with sqlite3.connect(backlog.database.path) as external:
        external.execute("UPDATE tasks SET body = 'External body' WHERE id = ?", (task.id,))
    refresh = asyncio.create_task(indexer.refresh())
    try:
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(0.01)
        assert await embeddings.indexes(backlog, connection, backlog.transaction) is old
        result = await asyncio.wait_for(
            embeddings.search(
                embeddings.Search(query="Reset password"), backlog, connection, backlog.transaction
            ),
            1,
        )
        assert result.tasks[0].body == "External body"
        added = await operations.task_add(
            operations.AddTask(title="New while rebuilding"),
            backlog,
            connection,
            backlog.transaction,
        )
        assert indexer.pending
    finally:
        release.set()
        await refresh
    assert added.id not in indexer.states[backlog.principal].indexes[0].ids
    assert indexer.pending
    await indexer.refresh()
    assert added.id in indexer.states[backlog.principal].indexes[0].ids
    assert not indexer.pending


async def test_failed_commit_does_not_enqueue(
    backlog: Backlog, monkeypatch: pytest.MonkeyPatch
) -> None:
    await backlog.current()
    connection = await backlog.open()
    await embeddings.indexes(backlog, connection, backlog.transaction)

    async def fail() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(connection, "commit", fail)
    with pytest.raises(RuntimeError, match="commit failed"):
        await operations.task_add(
            operations.AddTask(title="Rolled back"), backlog, connection, backlog.transaction
        )
    assert not embeddings.indexers[backlog.database].pending
    assert await operations.read_tasks(connection, backlog.actor) == []


async def test_failed_encoding_keeps_old_index_and_retries_batch(
    backlog: Backlog, monkeypatch: pytest.MonkeyPatch
) -> None:
    await backlog.current()
    connection = await backlog.open()
    old = await embeddings.indexes(backlog, connection, backlog.transaction)
    task = await operations.task_add(
        operations.AddTask(title="Retry me"), backlog, connection, backlog.transaction
    )
    indexer = embeddings.indexers[backlog.database]
    original = embeddings.build_indexes

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("encoding failed")

    monkeypatch.setattr(embeddings, "build_indexes", fail)
    with pytest.raises(RuntimeError, match="encoding failed"):
        await indexer.refresh()
    assert await embeddings.indexes(backlog, connection, backlog.transaction) is old
    assert indexer.pending
    monkeypatch.setattr(embeddings, "build_indexes", original)
    await indexer.refresh()
    assert task.id in indexer.states[backlog.principal].indexes[0].ids
    assert not indexer.pending


async def test_writers_queue_notes_comments_and_history(
    backlog: Backlog, monkeypatch: pytest.MonkeyPatch
) -> None:
    await backlog.current()
    connection = await backlog.open()
    task = await operations.task_add(
        operations.AddTask(title="Work"), backlog, connection, backlog.transaction
    )
    await embeddings.indexes(backlog, connection, backlog.transaction)
    indexer = embeddings.indexers[backlog.database]
    texts = []
    original = embeddings.encoder.encode

    def encode(items: list[str]) -> Any:
        texts.extend(items)
        return original(items)

    monkeypatch.setattr(embeddings.encoder, "encode", encode)
    await operations.task_update(
        operations.UpdateTask(id=task.id, status=Status.DOING),
        backlog,
        connection,
        backlog.transaction,
    )
    await operations.task_claim(
        operations.ClaimTask(task=task.id, expected=None, assigned="local@here:embeddings"),
        backlog,
        connection,
        backlog.transaction,
    )
    comment = await operations.task_comment_add(
        operations.AddComment(task_id=task.id, body="Progress"),
        backlog,
        connection,
        backlog.transaction,
    )
    note = await operations.memory_save(
        operations.SaveNote(title="Note", body="Remember this"),
        backlog,
        connection,
        backlog.transaction,
    )
    await indexer.refresh()
    assert "Work\n\n" not in texts
    assert "Status changed: todo → doing" in texts
    assert "Assignment changed: null → local@here:embeddings" in texts
    assert "Progress" in texts
    assert "Note\nRemember this\n" in texts
    indexes = indexer.states[backlog.principal].indexes
    assert comment.id in indexes[2].ids
    assert note.id in indexes[1].ids
    # Delete through the writer, after the note has reached the index.
    await operations.memory_delete(
        operations.NoteId(id=note.id), None, backlog, connection, backlog.transaction
    )
    await indexer.refresh()
    assert note.id not in indexer.states[backlog.principal].indexes[1].ids


async def test_query_encoding_does_not_wait_for_index_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoder = embeddings.encoder
    await asyncio.to_thread(encoder.encode, ["Warm model"])
    assert encoder.model is not None
    original = encoder.model.encode
    entered, release = threading.Event(), threading.Event()

    def slow(texts: list[str], **kwargs: Any) -> Any:
        if texts == ["Index batch"]:
            entered.set()
            assert release.wait(5)
        return original(texts, **kwargs)

    monkeypatch.setattr(encoder.model, "encode", slow)
    background = asyncio.create_task(asyncio.to_thread(encoder.encode, ["Index batch"]))
    try:
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(0.01)
        query = await asyncio.wait_for(asyncio.to_thread(encoder.encode, ["Query"]), 1)
        assert query.shape == (1, 512)
    finally:
        release.set()
        await background
