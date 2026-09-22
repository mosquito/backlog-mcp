"""Offline Model2Vec embeddings and combined FTS/semantic search."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from functools import cache
from hashlib import sha256
from pathlib import Path
from threading import Lock, RLock
from typing import TYPE_CHECKING, Annotated, Literal
from weakref import WeakKeyDictionary
from zipfile import BadZipFile

import numpy as np
from aiohttp_tiny_mcp import Extension
from aiohttp_tiny_mcp.protocol.core import Failure, FailureKind, Rejected
from aiohttp_tiny_mcp.protocol.models import ResultModel
from numpy.typing import NDArray
from pydantic import BaseModel, Field, StringConstraints

from .backlog import Backlog
from .models import Actor, CommentOut, NoteOut, ProjectOut, Status, TaskOut
from .store import Comment, Note, Project, Store, Task

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
    """Load once from a local directory; serialize inference outside the event loop."""

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
    computer: str | None = Field(
        default=None,
        description="Computer of the project or computer covered, absent when every computer.",
    )


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


@cache
def model_digest(path: Path) -> str:
    return sha256((path / "model.safetensors").read_bytes()).hexdigest()


@cache
def cache_directory() -> tempfile.TemporaryDirectory[str]:
    """One private directory per running process, created on first use and removed at exit.

    Nothing is shared between processes, so no file lock; `INDEXING` serializes the threads of
    this one. The cache holds the object, so its finalizer runs at exit and not before.
    """
    return tempfile.TemporaryDirectory(prefix="backlog-mcp-embeddings-")


def cache_root() -> Path:
    return Path(cache_directory().name)


#: Two stores on one database share a cache file; the second thread finds what the first wrote.
INDEXING = RLock()


def cached_indexes(
    database: str,
    tasks: list[tuple[int, str]],
    notes: list[tuple[int, str]],
    comments: list[tuple[int, str]],
    projects: list[tuple[int, str]],
) -> tuple[VectorIndex, VectorIndex, VectorIndex, VectorIndex]:
    """Load the ID/vector snapshot of this process, or build it and write it for the next call."""
    digest = model_digest(encoder.path)
    identity = sha256(f"{database}:{digest}".encode()).hexdigest()
    signature = sha256(
        json.dumps([tasks, notes, comments, projects], ensure_ascii=False).encode()
    ).hexdigest()
    directory = cache_root()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{identity}.npz"
    with INDEXING:
        return load_or_build(path, signature, tasks, notes, comments, projects)


def load_or_build(
    path: Path,
    signature: str,
    tasks: list[tuple[int, str]],
    notes: list[tuple[int, str]],
    comments: list[tuple[int, str]],
    projects: list[tuple[int, str]],
) -> tuple[VectorIndex, VectorIndex, VectorIndex, VectorIndex]:
    try:
        with np.load(path, allow_pickle=False) as saved:
            if str(saved["signature"].item()) == signature:
                task_index = VectorIndex(saved["task_ids"].tolist(), saved["task_vectors"])
                note_index = VectorIndex(saved["note_ids"].tolist(), saved["note_vectors"])
                comment_index = VectorIndex(saved["comment_ids"].tolist(), saved["comment_vectors"])
                project_index = VectorIndex(saved["project_ids"].tolist(), saved["project_vectors"])
                if all(
                    index.vectors.shape == (len(index.ids), DIMENSIONS)
                    and np.isfinite(index.vectors).all()
                    for index in (task_index, note_index, comment_index, project_index)
                ):
                    return task_index, note_index, comment_index, project_index
    except OSError, ValueError, KeyError, EOFError, BadZipFile:
        pass
    texts = (
        [text for _, text in tasks]
        + [text for _, text in notes]
        + [text for _, text in comments]
        + [text for _, text in projects]
    )
    vectors = encoder.encode(texts) if texts else np.empty((0, DIMENSIONS), dtype=np.float32)
    task_index = VectorIndex([item[0] for item in tasks], vectors[: len(tasks)])
    note_index = VectorIndex(
        [item[0] for item in notes], vectors[len(tasks) : len(tasks) + len(notes)]
    )
    comment_index = VectorIndex(
        [item[0] for item in comments],
        vectors[len(tasks) + len(notes) : len(tasks) + len(notes) + len(comments)],
    )
    project_index = VectorIndex(
        [item[0] for item in projects], vectors[len(tasks) + len(notes) + len(comments) :]
    )
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".npz", delete=False) as stream:
            temporary = Path(stream.name)
            np.savez_compressed(
                stream,
                signature=np.array(signature),
                task_ids=np.asarray(task_index.ids, dtype=np.int64),
                task_vectors=task_index.vectors,
                note_ids=np.asarray(note_index.ids, dtype=np.int64),
                note_vectors=note_index.vectors,
                comment_ids=np.asarray(comment_index.ids, dtype=np.int64),
                comment_vectors=comment_index.vectors,
                project_ids=np.asarray(project_index.ids, dtype=np.int64),
                project_vectors=project_index.vectors,
            )
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return task_index, note_index, comment_index, project_index


def database_key(backlog: Backlog) -> str:
    if backlog.store.in_memory:
        return f"memory:{os.getpid()}:{id(backlog.store.database)}:{backlog.store.principal}"
    return f"{backlog.store.path.resolve()}:{backlog.store.principal}"


async def snapshot(backlog: Backlog) -> tuple[list[Task], list[Note], list[Comment], list[Project]]:
    tasks = await backlog.store.tasks(status=list(Status), limit=-1)
    notes = await backlog.store.notes(limit=-1)
    comments = await backlog.store.comments(status=list(Status), limit=-1)
    projects = await backlog.store.projects()
    return tasks, notes, comments, projects


@dataclass
class IndexState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    revision: tuple[object, int, int] | None = None
    indexes: tuple[VectorIndex, VectorIndex, VectorIndex, VectorIndex] | None = None


states: WeakKeyDictionary[Store, IndexState] = WeakKeyDictionary()


async def indexes(backlog: Backlog) -> tuple[VectorIndex, VectorIndex, VectorIndex, VectorIndex]:
    """Build lazily; local writes and external commits invalidate the cached snapshot."""
    state = states.setdefault(backlog.store, IndexState())
    async with state.lock:
        connection = await backlog.store.open()
        version = await backlog.store.data_version()
        revision = (connection, version, connection.total_changes)
        if state.indexes is None or state.revision != revision:
            tasks, notes, comments, projects = await snapshot(backlog)
            state.indexes = await asyncio.to_thread(
                cached_indexes,
                database_key(backlog),
                [(item.id, document(item)) for item in tasks],
                [(item.id, document(item)) for item in notes],
                [(item.id, item.body) for item in comments],
                [
                    (item.id, "\n".join((item.full_name, item.name, item.description)))
                    for item in projects
                ],
            )
            state.revision = revision
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


async def search(args: Search, backlog: Backlog) -> Matches:
    """Merge FTS and semantic IDs, then fetch current records from SQLite."""
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

    lexical_tasks = await backlog.store.search(
        args.query,
        project_id=project_id,
        computer=computer,
        status=args.status,
        tag=args.tag,
        limit=args.limit,
    )
    lexical_notes = await backlog.store.search_notes(
        args.query, project_id=project_id, computer=computer, tag=args.tag, limit=args.limit
    )
    lexical_comments = (
        await backlog.store.comments(
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
        await backlog.store.search_projects(
            args.query, project_id=project_id, computer=computer, limit=args.limit
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
    vectors = await indexes(backlog)
    tasks = await backlog.store.tasks(
        project_id=project_id, computer=computer, status=args.status, tag=args.tag, limit=-1
    )
    notes = await backlog.store.notes(
        project_id=project_id, computer=computer, tag=args.tag, limit=-1
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
        await backlog.store.comments(
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
    projects = await backlog.store.projects(computer=computer) if "projects" in args.kinds else []
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
        task = await backlog.store.task(record_id)
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
        note = await backlog.store.note(record_id)
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
            for item in await backlog.store.comments(
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
        project = await backlog.store.project(record_id)
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
                    project, computer=backlog.computer, principal=backlog.store.principal
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
        computer=reach.scope_computer,
        principal=backlog.store.principal,
    )


__all__ = ["DIMENSIONS", "EXTENSION", "MODEL_NAME", "MODEL_PATH", "Encoder", "extension"]
