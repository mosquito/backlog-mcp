"""Text skills: instructions a client loads, and the files those instructions refer to.

A skill is a directory under `skills/`: `<name>/SKILL.md` holds YAML frontmatter with `name`
and `description`, then the instructions, and `<name>/references/` holds the text files the
instructions cite. Nothing here is executed; the client reads the text and the tools of this
server do the work.

The methods `skills/list` and `skills/get` of the `io.modelcontextprotocol/skills` extension are
answered on revision 2026-07-28 only, because that is the revision the extension targets.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from aiohttp_tiny_mcp import Extension
from aiohttp_tiny_mcp.core import Failure, FailureKind, Rejected
from aiohttp_tiny_mcp.models import (
    CacheableResult,
    ListParams,
    ReadResourceParams,
    TextResourceContents,
)

#: The extension these two methods belong to, as the specification names it.
EXTENSION = "io.modelcontextprotocol/skills"
SCHEME = "skill://"
MANIFEST = "SKILL.md"
REFERENCES = "references"
MIME = "text/markdown"
#: Text only: instructions and the files they cite, never anything a client could execute.
SUFFIXES = frozenset({".md", ".txt"})
#: One directory name or one file name, and never `.` or `..`, because these arrive over the wire.
SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")

#: Resource reads remain available to clients of every supported revision.
LIST_URI = "backlog://skills"
ENTRY_TEMPLATE = f"{LIST_URI}/{{name}}"

ROOT = Path(__file__).parent / "skills"


def uri_of(name: str, path: str) -> str:
    """The resource URI of one file of one skill."""
    return f"{SCHEME}{name}/{path}"


def frontmatter(text: str) -> dict[str, str]:
    """The leading `---` block of a SKILL.md, as flat `key: value` pairs.

    Text skills need scalars only, so a nested block is read as far as its key and no further.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, separator, value = line.partition(":")
        if separator and key == key.strip():
            fields[key] = value.strip().strip("\"'")
    return fields


def body(text: str) -> str:
    """The instructions of a SKILL.md: everything below the frontmatter block."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text.strip()
    for index, line in enumerate(lines[1:], start=2):
        if line.strip() == "---":
            return "\n".join(lines[index:]).strip()
    return ""


@dataclass(frozen=True, slots=True)
class File:
    """One text file of a skill: where it is, what it hashes to, and how large it is."""

    uri: str
    path: Path
    digest: str
    size: int

    @classmethod
    def of(cls, name: str, path: Path, relative: str) -> File:
        data = path.read_bytes()
        return cls(
            uri=uri_of(name, relative),
            path=path,
            digest=f"sha256:{sha256(data).hexdigest()}",
            size=len(data),
        )

    def digests(self) -> dict[str, Any]:
        """One entry of the manifest the extension asks for."""
        return {"uri": self.uri, "digest": self.digest, "size": self.size}

    def contents(self) -> TextResourceContents:
        return TextResourceContents(
            uri=self.uri, text=self.path.read_text(encoding="utf-8"), mime_type=MIME
        )


@dataclass(frozen=True, slots=True)
class Skill:
    """One skill: the instructions, their frontmatter, and the files the instructions cite."""

    name: str
    frontmatter: Mapping[str, str]
    manifest: File
    references: tuple[File, ...] = ()

    @property
    def uri(self) -> str:
        return self.manifest.uri

    @property
    def files(self) -> tuple[File, ...]:
        return (self.manifest, *self.references)

    @property
    def description(self) -> str:
        return self.frontmatter.get("description", "")

    @property
    def instructions(self) -> str:
        """What the client is meant to follow: the SKILL.md without its frontmatter."""
        return body(self.manifest.path.read_text(encoding="utf-8"))

    def entry(self) -> dict[str, Any]:
        """The skill as the extension describes it: the URI, the frontmatter, the file list."""
        return {
            "uri": self.uri,
            "frontmatter": dict(self.frontmatter),
            "resources": [item.digests() for item in self.files],
        }


class Library:
    """The skills under one directory, read from disk on every call so edits show up at once."""

    def __init__(self, root: Path = ROOT) -> None:
        self.root = root

    def names(self) -> list[str]:
        """Every skill name in the library, in alphabetical order."""
        if not self.root.is_dir():
            return []
        return sorted(
            item.name
            for item in self.root.iterdir()
            if SAFE.match(item.name) and (item / MANIFEST).is_file()
        )

    def skills(self) -> list[Skill]:
        return [found for name in self.names() if (found := self.skill(name)) is not None]

    def skill(self, name: str) -> Skill | None:
        """One skill by name, or `None` when the library holds no such skill."""
        manifest = self.file(name, MANIFEST)
        if manifest is None:
            return None
        return Skill(
            name=name,
            frontmatter=frontmatter(manifest.path.read_text(encoding="utf-8")),
            manifest=manifest,
            references=self.references(name),
        )

    def references(self, name: str) -> tuple[File, ...]:
        """The text files the instructions cite, from the `references` directory of the skill."""
        folder = self.root / name / REFERENCES
        if not folder.is_dir():
            return ()
        return tuple(
            File.of(name, item, f"{REFERENCES}/{item.name}")
            for item in sorted(folder.iterdir())
            if item.is_file() and item.suffix in SUFFIXES and SAFE.match(item.name)
        )

    def file(self, name: str, relative: str) -> File | None:
        """One file of one skill, or `None` when the path names nothing this library serves."""
        if not SAFE.match(name):
            return None
        match relative.split("/"):
            case [manifest] if manifest == MANIFEST:
                path = self.root / name / MANIFEST
            case [folder, item] if folder == REFERENCES and SAFE.match(item):
                path = self.root / name / REFERENCES / item
            case _:
                return None
        if not path.is_file() or path.suffix not in SUFFIXES:
            return None
        return File.of(name, path, relative)


#: The skills this server serves. Tests point it at a directory of their own.
library = Library()


def named(uri: str) -> str:
    """The skill name in a `skill://<name>/SKILL.md` URI, as `skills/get` asks for one."""
    name, _, rest = uri.removeprefix(SCHEME).partition("/")
    if not uri.startswith(SCHEME) or rest != MANIFEST or not SAFE.match(name):
        raise Rejected(Failure(FailureKind.INVALID_PARAMS, f"not a skill uri: {uri!r}"))
    return name


class ListResult(CacheableResult):
    skills: list[dict[str, Any]]


class GetResult(CacheableResult):
    skill: dict[str, Any]


extension = Extension(EXTENSION)


@extension.method("skills/list")
async def list_skills(args: ListParams) -> ListResult:
    """Every text skill with its frontmatter and file manifest."""
    if args.cursor is not None:
        raise Rejected(Failure(FailureKind.INVALID_PARAMS, "invalid skill cursor"))
    return ListResult(
        skills=[item.entry() for item in library.skills()], ttl_ms=0, cache_scope="public"
    )


@extension.method("skills/get")
async def get_skill(args: ReadResourceParams) -> GetResult:
    """One text skill identified by the URI of its instructions."""
    name = named(args.uri)
    found = library.skill(name)
    if found is None:
        raise Rejected(Failure(FailureKind.RESOURCE_NOT_FOUND, f"no skill {name!r}: {args.uri}"))
    return GetResult(skill=found.entry(), ttl_ms=0, cache_scope="public")


__all__ = [
    "body",
    "ENTRY_TEMPLATE",
    "EXTENSION",
    "LIST_URI",
    "MANIFEST",
    "MIME",
    "ROOT",
    "SCHEME",
    "File",
    "Library",
    "Skill",
    "extension",
    "frontmatter",
    "library",
    "named",
    "uri_of",
]
