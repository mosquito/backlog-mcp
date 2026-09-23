"""Offline Model2Vec embeddings and combined FTS/semantic search."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Annotated, Literal
from weakref import WeakKeyDictionary

import aiosqlite
import numpy as np
from aiohttp_tiny_mcp import Extension
from aiohttp_tiny_mcp.protocol.core import Failure, FailureKind, Rejected
from aiohttp_tiny_mcp.protocol.models import ResultModel
from numpy.typing import NDArray
from pydantic import BaseModel, Field, StringConstraints

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
FTS_MIN_SCORE = 0.25
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
    query: Text = Field(
        description=(
            "Words or meaning to find in project names, descriptions, titles, bodies and tags. "
            "ALWAYS search when starting work, facing uncertainty, or checking past agreements."
        )
    )
    project: str | None = Field(default=None, description="Defaults to the current project.")
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
            "Minimum cosine similarity for semantic matches; FTS matches are retained. "
            "Semantic results need at least 0.25 when an FTS result exists."
        ),
    )


class TaskHit(TaskOut):
    match: Literal["fts", "semantic"] = Field(description="How this record matched the query.")
    score: float | None = Field(
        default=None, description="Cosine similarity; null for FTS-only hits."
    )


class NoteHit(NoteOut):
    match: Literal["fts", "semantic"] = Field(description="How this record matched the query.")
    score: float | None = Field(
        default=None, description="Cosine similarity; null for FTS-only hits."
    )


class CommentHit(CommentOut):
    match: Literal["fts", "semantic"]
    score: float | None = None
    snippet: str | None = None


class ProjectHit(ProjectOut):
    match: Literal["fts", "semantic"]
    score: float | None = None


class Matches(Actor):
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
        eligible = [
            (record_id, float(score))
            for record_id, score, vector in zip(ids, scores, vectors, strict=True)
            if record_id in allowed and score >= min_score and np.any(vector)
        ]
        return sorted(eligible, key=lambda item: (-item[1], item[0]))[:limit]


type Indexes = tuple[VectorIndex, VectorIndex, VectorIndex, VectorIndex]


@dataclass(frozen=True)
class Change:
    kind: int
    id: int
    project_id: int
    text: str | None


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
            )
    return Change(kind, record.id, record.project_id, None if deleted else text)


def build_indexes(previous: Indexes | None, changes: list[Change], *, replace: bool) -> Indexes:
    """Build a new snapshot in a worker thread; retain vectors for unchanged texts."""
    indexes = []
    for kind in range(4):
        old = previous[kind] if previous is not None else None
        vectors = dict(zip(old.ids, old.vectors, strict=True)) if old else {}
        digests = dict(old.digests) if old else {}
        updates = {item.id: item.text for item in changes if item.kind == kind}
        if not replace and not updates and old is not None:
            indexes.append(old)
            continue
        if replace:
            vectors = {key: value for key, value in vectors.items() if key in updates}
            digests = {key: value for key, value in digests.items() if key in updates}
        texts = {}
        for record_id, text in updates.items():
            if text is None:
                vectors.pop(record_id, None)
                digests.pop(record_id, None)
            else:
                digest = sha256(text.encode()).digest()
                if digests.get(record_id) != digest:
                    texts[record_id] = text
                    digests[record_id] = digest
        if texts:
            encoded = encoder.encode(list(texts.values()))
            vectors.update(zip(texts, encoded, strict=True))
        ids = sorted(vectors)
        matrix = (
            np.stack([vectors[key] for key in ids])
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
    task_index, note_index, comment_index, project_index = indexes
    query = encoder.encode([args.query])[0]
    return (
        task_index.search(query, task_ids, args.limit, min_score),
        note_index.search(query, note_ids, args.limit, min_score),
        comment_index.search(query, comment_ids, args.limit, min_score),
        project_index.search(query, project_ids, args.limit, min_score),
    )


async def search(
    args: Search, backlog: Backlog, connection: aiosqlite.Connection, transaction: Transaction
) -> Matches:
    """Merge FTS and semantic IDs, then fetch current records from SQLite."""
    from . import server

    reach = await backlog.reach(
        args.project,
        all_projects=args.all_projects,
        computer=args.computer,
        all_computers=args.all_computers,
    )
    project_id, computer = reach.project_id, reach.computer

    def inside(record: Task | Note | Comment) -> bool:
        """True when the record is in reach by itself, shared notes aside."""
        if project_id is not None:
            return record.project_id == project_id
        if computer is not None:
            return True
        return True

    lexical_tasks = await server.search_tasks(
        connection,
        backlog.actor,
        args.query,
        project_id=project_id,
        computer=computer,
        status=args.status,
        tag=args.tag,
        limit=args.limit,
    )
    lexical_notes = await server.search_notes(
        connection,
        backlog.actor,
        args.query,
        project_id=project_id,
        computer=computer,
        tag=args.tag,
        limit=args.limit,
    )
    lexical_comments = (
        await server.read_comments(
            connection,
            backlog.actor,
            query=args.query,
            project_id=project_id,
            computer=computer,
            status=args.status,
            tag=args.tag,
            limit=args.limit,
        )
        if "comments" in args.kinds
        else []
    )
    if "tasks" not in args.kinds:
        lexical_tasks = []
    if "memory" not in args.kinds:
        lexical_notes = []
    lexical_projects = (
        await server.search_projects(
            connection,
            backlog.actor,
            args.query,
            project_id=project_id,
            computer=computer,
            limit=args.limit,
        )
        if "projects" in args.kinds
        else []
    )
    project_fts = {item.id for item in lexical_projects}
    comment_snippets = {item.id: item.snippet for item in lexical_comments}
    task_snippets = {item.id: item.snippet for item in lexical_tasks}
    note_snippets = {item.id: item.snippet for item in lexical_notes}
    del lexical_tasks, lexical_notes
    min_score = (
        max(args.min_score, FTS_MIN_SCORE)
        if task_snippets or note_snippets or comment_snippets or project_fts
        else args.min_score
    )
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
    allowed_notes = {
        item.id for item in notes if (inside(item) or item.shared) and (not tag or tag in item.tags)
    }
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
    ranked_tasks = list(dict.fromkeys([*task_snippets, *task_scores]))[: args.limit]
    ranked_notes = list(dict.fromkeys([*note_snippets, *note_scores]))[: args.limit]
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
        data["snippet"] = task_snippets.get(record_id)
        task_hits.append(
            TaskHit(
                **data,
                match="fts" if record_id in task_snippets else "semantic",
                score=task_scores.get(record_id),
            )
        )
    for record_id in ranked_notes:
        note = await server.read_note(connection, backlog.actor, record_id)
        if note is None or not (inside(note) or note.shared):
            continue
        if tag and tag not in note.tags:
            continue
        data = asdict(note)
        data["snippet"] = note_snippets.get(record_id)
        note_hits.append(
            NoteHit(
                **data,
                match="fts" if record_id in note_snippets else "semantic",
                score=note_scores.get(record_id),
            )
        )
    comment_hits: list[CommentHit] = []
    comment_scores = dict(comment_ids)
    ranked_comments = list(dict.fromkeys([*comment_snippets, *comment_scores]))[: args.limit]
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
        data["snippet"] = comment_snippets.get(record_id) or comment.body[:200]
        comment_hits.append(
            CommentHit(
                **data,
                match="fts" if record_id in comment_snippets else "semantic",
                score=comment_scores.get(record_id),
            )
        )
    from .server import project_out

    project_hits: list[ProjectHit] = []
    project_scores = dict(project_ids)
    ranked_projects = list(
        dict.fromkeys([*(item.id for item in lexical_projects), *project_scores])
    )[: args.limit]
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
                match="fts" if record_id in project_fts else "semantic",
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
