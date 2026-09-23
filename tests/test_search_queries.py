"""Paragraph indexing and scoped multi-formulation search contracts."""

import json
from typing import Any

import numpy as np
import pytest
from aiohttp_tiny_mcp.client.base import BaseClient

from backlog_mcp import embeddings, server
from backlog_mcp.backlog import Backlog


def test_paragraphs_keep_short_passages_and_document() -> None:
    first = "A" * 45
    second = "B" * 50
    text = f"Heading\n\n{first}\n \nShort\n\n{second}\n\nTail"
    assert embeddings.paragraphs(text) == [
        text,
        "Heading",
        first,
        "Short",
        second,
        "Tail",
    ]
    assert embeddings.paragraphs("one paragraph\n\n") == ["one paragraph\n\n"]
    assert embeddings.paragraphs("") == [""]
    repeated = first + "\n\n" + first
    assert embeddings.paragraphs(repeated) == [repeated, first]


def test_record_max_deduplicates_before_limit_and_filters_access() -> None:
    index = embeddings.VectorIndex(
        [1, 1, 2, 3], np.array([[1.0, 0.0], [0.9, 0.1], [0.8, 0.2], [0.99, 0.0]], dtype=np.float32)
    )
    hits = index.search(np.array([1.0, 0.0], dtype=np.float32), {1, 2}, 2, -1)
    assert [i for i, _ in hits] == [1, 2]
    assert hits[0][1] == 1


def test_fusion_ranks_full_candidate_lists_before_final_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vectors = np.array(
        [[1.0, 0.0, 0.0], [0.7, 0.7, 0.0], [0.5, 0.5, 0.7], [0.0, 1.0, 0.0]], dtype=np.float32
    )
    index = embeddings.VectorIndex([1, 2, 3, 4], vectors)
    empty = embeddings.VectorIndex([], np.empty((0, 3), dtype=np.float32))
    monkeypatch.setattr(
        embeddings.encoder,
        "encode",
        lambda texts: np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32),
    )
    result = embeddings.match_ids(
        (index, empty, empty, empty),
        embeddings.Search(query=["first", "second"], limit=1),
        {1, 2, 3, 4},
        set(),
        set(),
        set(),
        -1,
    )
    assert result[0] == [(2, 2 / 62)]


def test_changed_record_replaces_all_chunks_and_delete_removes_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def encode(texts: list[str]) -> Any:
        calls.extend(texts)
        return np.ones((len(texts), embeddings.DIMENSIONS), dtype=np.float32)

    monkeypatch.setattr(embeddings.encoder, "encode", encode)
    text = "A" * 50 + "\n\n" + "B" * 50
    old = embeddings.build_indexes(None, [embeddings.Change(0, 1, 1, text)], replace=True)
    assert old[0].ids == [1, 1, 1]
    calls.clear()
    same = embeddings.build_indexes(old, [embeddings.Change(0, 1, 1, text)], replace=True)
    assert calls == []
    changed = embeddings.build_indexes(
        same, [embeddings.Change(0, 1, 1, "Replacement")], replace=False
    )
    assert changed[0].ids == [1]
    assert old[0].ids == [1, 1, 1]
    removed = embeddings.build_indexes(changed, [embeddings.Change(0, 1, 1, None)], replace=False)
    assert removed[0].ids == []
    assert removed[0].vectors.shape == (0, embeddings.DIMENSIONS)
    assert removed[0].digests == {}


@pytest.mark.parametrize("count", [1, 2, 5])
async def test_queries_protocol_and_ranking(
    client: BaseClient, backlog: Backlog, count: int
) -> None:
    await backlog.current()
    await client.initialize()
    created = await client.call_tool(
        "task_add",
        {
            "title": "Reset password",
            "body": "Recover account credentials and reset the password.\n\n"
            + "Other administrative work. " * 5,
        },
    )
    query = "Reset password"
    result = await client.call_tool(
        "search", {"query": [query] * count, "kinds": ["tasks"], "limit": 1, "min_score": -1}
    )
    assert not result.is_error
    result = json.loads(result.content[0].text)
    created = json.loads(created.content[0].text)
    assert result["queries"] == [query] * count
    assert result["tasks"][0]["id"] == created["id"]
    assert result["tasks"][0]["match"] == "semantic"
    if count > 1:
        assert result["tasks"][0]["score"] == pytest.approx(count / 61)


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"query": None},
        {"query": "plain string"},
        {"query": []},
        {"query": ["a"] * 6},
        {"query": [" "]},
        {"query": ["a", ""]},
    ],
)
async def test_query_source_validation(client: BaseClient, args: dict[str, Any]) -> None:
    await client.initialize()
    result = await client.call_tool("search", args)
    assert result.is_error


async def test_mixed_formulations_keep_fts_first_and_echo_input(backlog: Backlog) -> None:
    await backlog.current()
    connection = await backlog.open()
    exact = await server.task_add(
        server.AddTask(title="Literal marker"), backlog, connection, backlog.transaction
    )
    semantic = await server.task_add(
        server.AddTask(title="Reset account password"), backlog, connection, backlog.transaction
    )
    queries = ['  "marker"  ', "Reset account password"]
    result = await embeddings.search(
        embeddings.Search(query=queries, kinds=["tasks"], min_score=-1),
        backlog,
        connection,
        backlog.transaction,
    )
    assert result.queries == queries
    assert result.tasks[0].id == exact.id
    assert result.tasks[0].match == "fts"
    assert result.tasks[0].score is None
    assert result.tasks[1].id == semantic.id
    assert result.tasks[1].score == pytest.approx(1 / 61)
    assert len({hit.id for hit in result.tasks}) == len(result.tasks)


async def test_record_heading_and_body_paragraphs_are_separate(backlog: Backlog) -> None:
    await backlog.current()
    record = await server.task_add(
        server.AddTask(title="Heading", body="Short\n\nSecond paragraph", tags=["tag"]),
        backlog,
        await backlog.open(),
        backlog.transaction,
    )
    stored = await server.read_task(await backlog.open(), backlog.actor, record.id)
    assert stored is not None
    assert embeddings.change(stored).passages == (
        embeddings.document(stored),
        "Heading\ntag",
        "Short",
        "Second paragraph",
    )


def test_changed_passage_boundaries_rebuild_identical_full_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded: list[str] = []

    def encode(texts: list[str]) -> Any:
        encoded.extend(texts)
        return np.ones((len(texts), embeddings.DIMENSIONS), dtype=np.float32)

    monkeypatch.setattr(embeddings.encoder, "encode", encode)
    old = embeddings.build_indexes(
        None,
        [embeddings.Change(0, 1, 1, "same full text", ("same full text", "first"))],
        replace=True,
    )
    encoded.clear()
    new = embeddings.build_indexes(
        old,
        [embeddings.Change(0, 1, 1, "same full text", ("same full text", "second"))],
        replace=False,
    )
    assert encoded == ["same full text", "second"]
    assert new[0].digests != old[0].digests
