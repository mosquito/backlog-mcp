"""Offline Model2Vec embeddings and text, vector, or mixed search."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections import deque
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Annotated, Literal, Self
from weakref import WeakKeyDictionary

import aiosqlite
import numpy as np
from aiohttp_tiny_mcp import Extension
from aiohttp_tiny_mcp.protocol.core import Failure, FailureKind, Rejected
from aiohttp_tiny_mcp.protocol.models import ResultModel
from numpy.typing import NDArray
from pydantic import BaseModel, Field, StringConstraints, model_validator

from . import backlog as scope
from .backlog import Backlog
from .database import Database
from .models import Actor, CommentOut, NoteOut, ProjectOut, Status, TaskOut
from .queries import QUERIES
from .records import Comment, Note, Project, Task
from .transaction import Transaction

if TYPE_CHECKING:
    from model2vec import StaticModel

EXTENSION = "backlog-mcp/model2vec"
MODEL_NAME = "static-similarity-mrl-multilingual-v1-512-int8"
MODEL_PATH = Path(__file__).with_name("model")
DIMENSIONS = 512
DEFAULT_MIN_SCORE = 0.15
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16384)]


class ModelUnavailable(RuntimeError):
    """The bundled model cannot be loaded from disk."""


class Encoder:
    """Load once from a local directory; run inference outside the event loop."""

    def __init__(self, path: Path = MODEL_PATH) -> None:
        self.path = path.resolve()
        self.model: StaticModel | None = None
        self.lock = Lock()

    def encode(self, texts: list[str]) -> NDArray[np.float32]:
        with self.lock:
            if self.model is None:
                for name in ("model.safetensors", "config.json", "tokenizer.json"):
                    if not (self.path / name).is_file():
                        raise ModelUnavailable(f"Bundled model file is missing: {name}")
                from model2vec import StaticModel

                try:
                    self.model = StaticModel.from_pretrained(
                        self.path, force_download=False, normalize=True
                    )
                except OSError, ValueError:
                    raise ModelUnavailable("The bundled Model2Vec model cannot be loaded") from None
                if self.model.dim != DIMENSIONS:
                    self.model = None
                    raise ModelUnavailable(f"The bundled model must have {DIMENSIONS} dimensions")
        values = np.asarray(
            self.model.encode(texts, max_length=None, use_multiprocessing=False),
            dtype=np.float32,
        )
        norms = np.linalg.norm(values, axis=1, keepdims=True)
        normalized = np.zeros_like(values)
        np.divide(values, norms, out=normalized, where=norms > 0)
        return normalized


encoder = Encoder()
extension = Extension(
    EXTENSION,
    capabilities={"model": MODEL_NAME, "dimensions": DIMENSIONS, "quantization": "int8"},
)


class EncodeTexts(BaseModel):
    texts: list[Text] = Field(min_length=1, max_length=128)


class Encoded(ResultModel):
    model: str = MODEL_NAME
    dimensions: int = DIMENSIONS
    embeddings: list[list[float]]


class Search(BaseModel):
    kinds: list[Literal["tasks", "comments", "memory", "projects"]] = Field(
        default=["tasks", "comments", "memory", "projects"],
        min_length=1,
        description="Content to search: projects, tasks, comments, or memory.",
    )
    query: list[Annotated[str, StringConstraints(min_length=1, max_length=16384)]] = Field(
        min_length=1,
        max_length=5,
        description=(
            "1–5 formulations of what to find. "
            "2–5 short rephrasings of the same intent, e.g. in both languages; improves recall. "
            "Quoted expressions search text; unquoted text searches vectors. "
            "Mixed queries use both. See backlog://guide/search for syntax and examples."
        ),
    )

    @model_validator(mode="after")
    def nonblank_formulations(self) -> Self:
        if any(not text.strip() for text in self.query):
            raise ValueError("Query formulations must not be blank")
        return self

    project: str | None = Field(
        default=None, description="Defaults to connection project scope; required if unscoped."
    )
    all_projects: bool = Field(default=False, description="Every project of this computer.")
    computer: str | None = Field(
        default=None, description="Every project of the named computer instead."
    )
    all_computers: bool = Field(default=False, description="Every project of every computer.")
    status: list[Status] | None = Field(default=None, description="Task statuses only.")
    tag: str | None = None
    limit: int = Field(default=20, ge=1, le=200, description="Maximum matches per content kind.")
    min_score: float = Field(
        default=DEFAULT_MIN_SCORE,
        ge=-1,
        le=1,
        allow_inf_nan=False,
        description=(
            "Minimum cosine similarity for vector search, default 0.15. "
            "Does not filter full-text matches."
        ),
    )


class TaskHit(TaskOut):
    match: Literal["fts", "semantic"] = Field(description="How this record matched the query.")
    score: float | None = Field(
        default=None,
        description=(
            "Cosine similarity for one formulation, RRF sum for multiple; null for FTS-only hits."
        ),
    )


class NoteHit(NoteOut):
    match: Literal["fts", "semantic"] = Field(description="How this record matched the query.")
    score: float | None = Field(
        default=None,
        description=(
            "Cosine similarity for one formulation, RRF sum for multiple; null for FTS-only hits."
        ),
    )


class CommentHit(CommentOut):
    match: Literal["fts", "semantic"]
    score: float | None = None
    snippet: str | None = None


class ProjectHit(ProjectOut):
    match: Literal["fts", "semantic"]
    score: float | None = None


class Matches(Actor):
    queries: list[str] = Field(default_factory=list)
    model: str = MODEL_NAME
    tasks: list[TaskHit]
    projects: list[ProjectHit]
    memory: list[NoteHit]
    comments: list[CommentHit]
    count: int
    project: str | None


async def encode(texts: list[str]) -> NDArray[np.float32]:
    try:
        return await asyncio.to_thread(encoder.encode, texts)
    except ModelUnavailable as error:
        raise Rejected(Failure(FailureKind.INTERNAL, str(error))) from error


@extension.method("model2vec/encode")
async def encode_texts(args: EncodeTexts) -> Encoded:
    """Create normalized 512-dimensional embeddings locally, in input order."""
    vectors = await encode(args.texts)
    return Encoded(embeddings=vectors.tolist())


def document(record: Task | Note) -> str:
    return "\n".join((record.title, record.body, " ".join(record.tags)))


@dataclass(slots=True)
class VectorIndex:
    """Only record IDs and vectors; record contents stay in SQLite."""

    ids: list[int]
    vectors: NDArray[np.float32]
    digests: dict[int, bytes] = field(default_factory=dict)

    def search(
        self, query: NDArray[np.float32], allowed: set[int], limit: int, min_score: float
    ) -> list[tuple[int, float]]:
        if not np.any(query):
            return []
        positions = [index for index, record_id in enumerate(self.ids) if record_id in allowed]
        vectors = self.vectors[positions]
        ids = [self.ids[index] for index in positions]
        scores = np.clip(vectors @ query, -1, 1)
        best: dict[int, float] = {}
        for record_id, score, vector in zip(ids, scores, vectors, strict=True):
            if score >= min_score and np.any(vector):
                best[record_id] = max(best.get(record_id, -1.0), float(score))
        return sorted(best.items(), key=lambda item: (-item[1], item[0]))[:limit]

    def blocks(self) -> dict[int, NDArray[np.float32]]:
        """Return contiguous vector blocks; IDs are sorted when a snapshot is built."""
        if not self.ids:
            return {}
        starts = [
            i for i, record_id in enumerate(self.ids) if not i or self.ids[i - 1] != record_id
        ]
        ends = starts[1:] + [len(self.ids)]
        return {
            self.ids[start]: self.vectors[start:end]
            for start, end in zip(starts, ends, strict=True)
        }


def paragraphs(text: str) -> list[str]:
    """Keep the full document and each distinct paragraph, including short ones."""
    pieces = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    return [text] + list(dict.fromkeys(part for part in pieces if part != text.strip()))


type Indexes = tuple[VectorIndex, VectorIndex, VectorIndex, VectorIndex]


@dataclass(frozen=True)
class Change:
    kind: int
    id: int
    project_id: int
    text: str | None
    passages: tuple[str, ...] = ()
    private_to: str | None = None


def record_passages(record: Task | Note | Comment | Project) -> tuple[str, ...]:
    """Keep the heading separate from body paragraphs and the full document."""
    if isinstance(record, Comment):
        return tuple(paragraphs(record.body))
    if isinstance(record, Project):
        heading = "\n".join((record.full_name, record.name))
        body = record.description
        full = "\n".join((heading, body))
    else:
        heading = "\n".join((record.title, " ".join(record.tags))).strip()
        body = record.body
        full = document(record)
    body_parts = paragraphs(body)
    passages = body_parts[1:] or [body.strip()]
    return tuple(
        dict.fromkeys(
            [full, *[part for part in [heading, *passages] if part and part != full.strip()]]
        )
    )


def change(record: Task | Note | Comment | Project, *, deleted: bool = False) -> Change:
    match record:
        case Task():
            kind, text = 0, document(record)
        case Note():
            kind, text = 1, document(record)
        case Comment():
            kind, text = 2, record.body
        case Project():
            return Change(
                3,
                record.id,
                record.id,
                None
                if deleted or record.archived
                else "\n".join((record.full_name, record.name, record.description)),
                record_passages(record),
            )
    return Change(
        kind,
        record.id,
        record.project_id,
        None if deleted else text,
        record_passages(record),
        record.created_by.principal
        if isinstance(record, Note) and not record.shared and record.created_by
        else None,
    )


def build_indexes(previous: Indexes | None, changes: list[Change], *, replace: bool) -> Indexes:
    """Build a new snapshot in a worker thread; retain vectors for unchanged texts."""
    indexes = []
    for kind in range(4):
        old = previous[kind] if previous is not None else None
        vectors = old.blocks() if old else {}
        digests = dict(old.digests) if old else {}
        updates = {item.id: item for item in changes if item.kind == kind}
        if not replace and not updates and old is not None:
            indexes.append(old)
            continue
        if replace:
            vectors = {key: value for key, value in vectors.items() if key in updates}
            digests = {key: value for key, value in digests.items() if key in updates}
        texts = {}
        for record_id, item in updates.items():
            text = item.text
            if text is None:
                vectors.pop(record_id, None)
                digests.pop(record_id, None)
            else:
                passages = item.passages or tuple(paragraphs(text))
                digest = sha256(json.dumps(passages).encode()).digest()
                if digests.get(record_id) != digest:
                    texts[record_id] = passages
                    digests[record_id] = digest
        if texts:
            encoded = encoder.encode([text for chunks in texts.values() for text in chunks])
            offset = 0
            for record_id, chunks in texts.items():
                vectors[record_id] = encoded[offset : offset + len(chunks)]
                offset += len(chunks)
        records = sorted(vectors)
        ids = [record_id for record_id in records for _ in vectors[record_id]]
        matrix = (
            np.concatenate([vectors[key] for key in records])
            if ids
            else np.empty((0, DIMENSIONS), dtype=np.float32)
        )
        indexes.append(VectorIndex(ids, matrix, digests))
    return indexes[0], indexes[1], indexes[2], indexes[3]


async def snapshot(
    backlog: Backlog, connection: aiosqlite.Connection
) -> tuple[list[Task], list[Note], list[Comment], list[Project]]:
    from . import server

    tasks = await server.read_tasks(connection, backlog.actor, status=list(Status), limit=-1)
    notes = await server.read_notes(connection, backlog.actor, limit=-1)
    comments = await server.read_comments(connection, backlog.actor, status=list(Status), limit=-1)
    projects = await scope.read_projects(connection, backlog.actor)
    return tasks, notes, comments, projects


@dataclass
class IndexState:
    backlog: Backlog
    indexes: Indexes | None = None


@dataclass
class Indexer:
    connection: aiosqlite.Connection
    transaction: Transaction
    pending: deque[Change | None] = field(default_factory=deque)
    states: dict[str, IndexState] = field(default_factory=dict)
    version: int | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    worker: asyncio.Task[None] | None = None

    async def refresh(self) -> None:
        async with self.lock:
            batch: list[Change | None] = []
            try:
                async with self.transaction.lock:
                    version = await data_version(self.connection)
                    states = list(self.states.values())
                    if (
                        version == self.version
                        and not self.pending
                        and all(state.indexes is not None for state in states)
                    ):
                        return
                    batch = list(self.pending)
                    self.pending.clear()
                    replace = version != self.version or None in batch
                    snapshots: list[tuple[IndexState, list[Change], bool]] = []
                    while True:
                        snapshots.clear()
                        for state in states:
                            full = replace or state.indexes is None
                            if full:
                                records = await snapshot(state.backlog, self.connection)
                                updates = [change(item) for group in records for item in group]
                            else:
                                updates = []
                                allowed = {
                                    project_id: bool(
                                        await QUERIES.membership(
                                            self.connection,
                                            project_id=project_id,
                                            principal=state.backlog.principal,
                                        )
                                    )
                                    for project_id in {
                                        item.project_id for item in batch if item is not None
                                    }
                                }
                                for item in batch:
                                    if item is not None:
                                        updates.append(
                                            item
                                            if allowed[item.project_id]
                                            and item.private_to in (None, state.backlog.principal)
                                            else Change(item.kind, item.id, item.project_id, None)
                                        )
                            snapshots.append((state, updates, full))
                        after = await data_version(self.connection)
                        if after == version:
                            break
                        # An external commit raced the reads. Take a fresh snapshot.
                        version, replace = after, True
                built = [
                    (
                        state,
                        await asyncio.to_thread(
                            build_indexes, state.indexes, updates, replace=full
                        ),
                    )
                    for state, updates, full in snapshots
                ]
                # Publish complete snapshots together. Writes during encoding stay queued.
                for state, indexes in built:
                    state.indexes = indexes
                self.version = version
            except BaseException:
                self.pending.extendleft(reversed(batch))
                raise

    async def run(self) -> None:
        while True:
            await asyncio.sleep(1)
            try:
                await self.refresh()
            except Exception:
                logging.getLogger(__name__).exception("Failed to update embeddings; will retry")


indexers: WeakKeyDictionary[Database, Indexer] = WeakKeyDictionary()


def enqueue(
    backlog: Backlog, *records: Task | Note | Comment | Project, deleted: bool = False
) -> None:
    """Queue committed records. Before the first search, the initial snapshot covers writes."""
    if indexer := indexers.get(backlog.database):
        indexer.pending.extend(change(record, deleted=deleted) for record in records)


def invalidate(backlog: Backlog) -> None:
    """Access changes or project deletion require a new scoped snapshot."""
    if indexer := indexers.get(backlog.database):
        indexer.pending.append(None)


async def stop(database: Database) -> None:
    if (indexer := indexers.pop(database, None)) and indexer.worker:
        indexer.worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await indexer.worker


async def data_version(connection: aiosqlite.Connection) -> int:
    # Cache invalidation is an inline PRAGMA exception; data queries belong in queries.sql.
    async with connection.execute("PRAGMA data_version") as cursor:
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def indexes(
    backlog: Backlog, connection: aiosqlite.Connection, transaction: Transaction
) -> Indexes:
    """Wait for the first index only; later searches use the last completed snapshot."""
    indexer = indexers.setdefault(backlog.database, Indexer(connection, transaction))
    state = indexer.states.setdefault(backlog.principal, IndexState(backlog))
    if state.indexes is None:
        await indexer.refresh()
    if indexer.worker is None:
        indexer.worker = asyncio.create_task(indexer.run(), name="embeddings-indexer")
    assert state.indexes is not None
    return state.indexes


def match_ids(
    indexes: tuple[VectorIndex, VectorIndex, VectorIndex, VectorIndex],
    args: Search,
    task_ids: set[int],
    note_ids: set[int],
    comment_ids: set[int],
    project_ids: set[int],
    min_score: float,
) -> tuple[
    list[tuple[int, float]],
    list[tuple[int, float]],
    list[tuple[int, float]],
    list[tuple[int, float]],
]:
    """Rank allowed IDs entirely in a worker thread."""
    formulations = args.query
    encoded = encoder.encode(formulations)

    def rank(index: VectorIndex, allowed: set[int]) -> list[tuple[int, float]]:
        if len(encoded) == 1:
            return index.search(encoded[0], allowed, args.limit, min_score)
        rankings = [index.search(vector, allowed, len(allowed), min_score) for vector in encoded]
        return reciprocal_rank_fusion(rankings, args.limit)

    return (
        rank(indexes[0], task_ids),
        rank(indexes[1], note_ids),
        rank(indexes[2], comment_ids),
        rank(indexes[3], project_ids),
    )


def reciprocal_rank_fusion(
    rankings: list[list[tuple[int, float]]], limit: int
) -> list[tuple[int, float]]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for position, (record_id, _) in enumerate(ranking, 1):
            scores[record_id] = scores.get(record_id, 0.0) + 1.0 / (60 + position)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]


def split_query(query: str) -> tuple[str | None, str | None]:
    """Select text, vector, or mixed search without passing prose to the text parser."""
    if '"' not in query:
        return None, query
    phrases = list(re.finditer(r'"(?:[^"]|"")*"\*?', query))
    remainder = re.sub(r'"(?:[^"]|"")*"\*?', " ", query)
    if '"' in remainder:
        raise ValueError("Invalid FTS5 expression: unterminated quote")
    # Operators and column filters belong to a text expression, not semantic prose.
    syntax = re.sub(r"-?(?:[\w]+|\{[\w\s]+\})\s*:", " ", remainder)
    syntax = re.sub(r",\s*\d+\s*(?=\))", "", syntax)
    syntax = re.sub(r"\b(?:AND|OR|NOT|NEAR)\b|[()^+*\s]", "", syntax)
    if not syntax:
        return query, None
    return " ".join(match.group() for match in phrases), query.replace('"', "")


async def search(
    args: Search, backlog: Backlog, connection: aiosqlite.Connection, transaction: Transaction
) -> Matches:
    """Preserve exact text matches before fusing only semantic formulations."""
    modes = [split_query(text.strip()) for text in args.query]
    results: list[Matches] = []
    for text_query, _ in modes:
        if text_query is not None:
            results.append(
                await search_mode(
                    args.model_copy(update={"query": [text_query]}),
                    backlog,
                    connection,
                    transaction,
                )
            )
    vector_queries = [text for _, text in modes if text is not None]
    if vector_queries:
        semantic = await search_mode(
            args.model_copy(update={"query": vector_queries}), backlog, connection, transaction
        )
        if len(args.query) > 1 and len(vector_queries) == 1:

            def rank_scores[T: (TaskHit, NoteHit, CommentHit, ProjectHit)](items: list[T]) -> None:
                for position, item in enumerate(items, 1):
                    item.score = 1.0 / (60 + position)

            rank_scores(semantic.tasks)
            rank_scores(semantic.memory)
            rank_scores(semantic.comments)
            rank_scores(semantic.projects)
        results.append(semantic)
    result = merge_matches(results, args.limit)
    result.queries = args.query
    return result


def merge_matches(results: list[Matches], limit: int) -> Matches:
    result = results[0].model_copy(deep=True)
    for name in ("tasks", "memory", "comments", "projects"):
        items = {item.id: item for item in getattr(result, name)}
        for other in results[1:]:
            for item in getattr(other, name):
                items.setdefault(item.id, item)
        setattr(result, name, list(items.values())[:limit])
    result.count = sum(
        len(getattr(result, name)) for name in ("tasks", "memory", "comments", "projects")
    )
    return result


async def search_mode(
    args: Search, backlog: Backlog, connection: aiosqlite.Connection, transaction: Transaction
) -> Matches:
    """Choose text or vector search, then fetch accessible records."""
    from . import server

    query = args.query[0]
    reach = await backlog.reach(
        args.project,
        all_projects=args.all_projects,
        computer=args.computer,
        all_computers=args.all_computers,
    )
    project_id, computer = reach.project_id, reach.computer

    def inside(record: Task | Note | Comment) -> bool:
        """True when the record is in the selected project scope."""
        if project_id is not None:
            return record.project_id == project_id or (
                isinstance(record, Note) and record.global_scope
            )
        if computer is not None:
            return True
        return True

    if '"' in query:
        try:
            lexical_tasks = (
                await server.search_tasks(
                    connection,
                    backlog.actor,
                    query,
                    project_id=project_id,
                    computer=computer,
                    status=args.status,
                    tag=args.tag,
                    limit=args.limit,
                    raw=True,
                )
                if "tasks" in args.kinds
                else []
            )
            lexical_notes = (
                await server.search_notes(
                    connection,
                    backlog.actor,
                    query,
                    project_id=project_id,
                    computer=computer,
                    tag=args.tag,
                    limit=args.limit,
                    raw=True,
                )
                if "memory" in args.kinds
                else []
            )
            lexical_comments = (
                await server.read_comments(
                    connection,
                    backlog.actor,
                    query=query,
                    project_id=project_id,
                    computer=computer,
                    status=args.status,
                    tag=args.tag,
                    limit=args.limit,
                    raw=True,
                )
                if "comments" in args.kinds
                else []
            )
            lexical_projects = (
                await server.search_projects(
                    connection,
                    backlog.actor,
                    query,
                    project_id=project_id,
                    computer=computer,
                    limit=args.limit,
                    raw=True,
                )
                if "projects" in args.kinds
                else []
            )
        except aiosqlite.OperationalError as error:
            if any(token in str(error) for token in ("fts5:", "unterminated", "no such column")):
                raise ValueError(f"Invalid FTS5 expression: {error}") from error
            raise
        exact_tasks = [TaskHit(**asdict(item), match="fts") for item in lexical_tasks]
        exact_notes = [NoteHit(**asdict(item), match="fts") for item in lexical_notes]
        exact_comments = [CommentHit(**asdict(item), match="fts") for item in lexical_comments]
        exact_projects = [
            ProjectHit(
                **server.project_out(
                    item, computer=backlog.computer, principal=backlog.principal
                ).model_dump(),
                match="fts",
            )
            for item in lexical_projects
        ]
        return Matches(
            tasks=exact_tasks,
            memory=exact_notes,
            comments=exact_comments,
            projects=exact_projects,
            count=len(exact_tasks) + len(exact_notes) + len(exact_comments) + len(exact_projects),
            project=reach.slug,
            computer=backlog.computer,
            principal=backlog.principal,
        )
    min_score = args.min_score
    vectors = await indexes(backlog, connection, transaction)
    tasks = await server.read_tasks(
        connection,
        backlog.actor,
        project_id=project_id,
        computer=computer,
        status=args.status,
        tag=args.tag,
        limit=-1,
    )
    notes = await server.read_notes(
        connection, backlog.actor, project_id=project_id, computer=computer, tag=args.tag, limit=-1
    )
    tag = args.tag.strip().lower() if args.tag else None
    allowed_tasks = {
        item.id
        for item in tasks
        if inside(item)
        and (not args.status or item.status in args.status)
        and (item.status != Status.DELETED or (args.status and Status.DELETED in args.status))
        and (not tag or tag in item.tags)
    }
    allowed_notes = {item.id for item in notes if inside(item) and (not tag or tag in item.tags)}
    comments = (
        await server.read_comments(
            connection,
            backlog.actor,
            project_id=project_id,
            computer=computer,
            status=args.status,
            tag=args.tag,
            limit=-1,
        )
        if "comments" in args.kinds
        else []
    )
    allowed_comments = {item.id for item in comments}
    if "tasks" not in args.kinds:
        allowed_tasks = set()
    if "memory" not in args.kinds:
        allowed_notes = set()
    del tasks, notes, comments
    projects = (
        await scope.read_projects(connection, backlog.actor, computer=computer)
        if "projects" in args.kinds
        else []
    )
    allowed_projects = {item.id for item in projects if project_id is None or item.id == project_id}
    task_ids, note_ids, comment_ids, project_ids = await asyncio.to_thread(
        match_ids,
        vectors,
        args,
        allowed_tasks,
        allowed_notes,
        allowed_comments,
        allowed_projects,
        min_score,
    )
    task_hits: list[TaskHit] = []
    note_hits: list[NoteHit] = []
    tag = args.tag.strip().lower() if args.tag else None
    task_scores = dict(task_ids)
    note_scores = dict(note_ids)
    ranked_tasks = list(task_scores)[: args.limit]
    ranked_notes = list(note_scores)[: args.limit]
    for record_id in ranked_tasks:
        task = await server.read_task(connection, backlog.actor, record_id)
        if (
            task is None
            or not inside(task)
            or (args.status and task.status not in args.status)
            or (
                task.status == Status.DELETED
                and (not args.status or Status.DELETED not in args.status)
            )
            or (tag and tag not in task.tags)
        ):
            continue
        data = asdict(task)
        data["snippet"] = None
        task_hits.append(
            TaskHit(
                **data,
                match="semantic",
                score=task_scores.get(record_id),
            )
        )
    for record_id in ranked_notes:
        note = await server.read_note(connection, backlog.actor, record_id)
        if note is None or not inside(note):
            continue
        if tag and tag not in note.tags:
            continue
        data = asdict(note)
        data["snippet"] = None
        note_hits.append(
            NoteHit(
                **data,
                match="semantic",
                score=note_scores.get(record_id),
            )
        )
    comment_hits: list[CommentHit] = []
    comment_scores = dict(comment_ids)
    ranked_comments = list(comment_scores)[: args.limit]
    current_comments = (
        {
            item.id: item
            for item in await server.read_comments(
                connection,
                backlog.actor,
                project_id=project_id,
                computer=computer,
                status=args.status,
                tag=args.tag,
                limit=-1,
            )
        }
        if ranked_comments
        else {}
    )
    for record_id in ranked_comments:
        comment = current_comments.get(record_id)
        if comment is None:
            continue
        data = asdict(comment)
        data["snippet"] = comment.body[:200]
        comment_hits.append(
            CommentHit(
                **data,
                match="semantic",
                score=comment_scores.get(record_id),
            )
        )
    from .server import project_out

    project_hits: list[ProjectHit] = []
    project_scores = dict(project_ids)
    ranked_projects = list(project_scores)[: args.limit]
    for record_id in ranked_projects:
        project = await scope.read_project(connection, backlog.actor, record_id)
        if (
            project is None
            or project.archived
            or (project_id is not None and project.id != project_id)
        ):
            continue
        if computer is not None and not any(root.computer == computer for root in project.roots):
            continue
        project_hits.append(
            ProjectHit(
                **project_out(
                    project, computer=backlog.computer, principal=backlog.principal
                ).model_dump(),
                match="semantic",
                score=project_scores.get(record_id),
            )
        )
    return Matches(
        projects=project_hits,
        comments=comment_hits,
        tasks=task_hits,
        memory=note_hits,
        count=len(task_hits) + len(note_hits) + len(comment_hits) + len(project_hits),
        project=reach.slug,
        computer=backlog.computer,
        principal=backlog.principal,
    )


__all__ = ["DIMENSIONS", "EXTENSION", "MODEL_NAME", "MODEL_PATH", "Encoder", "extension"]
