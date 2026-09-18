"""Text skills through native extension methods and backwards-compatible resource reads."""

import json
from collections.abc import AsyncIterator
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from aiohttp.test_utils import TestServer
from aiohttp_tiny_mcp.client import Client
from aiohttp_tiny_mcp.client_base import BaseClient, ClientError
from aiohttp_tiny_mcp.core import FailureKind, Rejected
from aiohttp_tiny_mcp.models import TextResourceContents
from aiohttp_tiny_mcp.testing import connect, pick

from backlog_mcp import skills
from backlog_mcp.backlog import Backlog
from backlog_mcp.httpd import ENDPOINT_PATH, application
from backlog_mcp.server import registry
from backlog_mcp.skills import Library

VERSION = "2026-07-28"

SAMPLE = """\
---
name: sample
description: A skill written by the tests.
metadata:
  kind: ignored
---

# Sample

Call `task_add` and stop.
"""


def written(root: Path, name: str, text: str = SAMPLE) -> Path:
    """One skill on disk, so a test can hold a library of its own."""
    folder = root / name
    (folder / skills.REFERENCES).mkdir(parents=True)
    (folder / skills.MANIFEST).write_text(text, encoding="utf-8")
    (folder / skills.REFERENCES / "notes.md").write_text("Keep it short.\n", encoding="utf-8")
    return folder


@pytest.fixture(params=["stdio", "http"])
async def skill_client(
    backlog: Backlog, request: pytest.FixtureRequest
) -> AsyncIterator[BaseClient]:
    """Both transports use the default adapters and the same registered extension."""
    match request.param:
        case "stdio":
            async with connect(registry, adapter=VERSION) as client:
                yield client
        case "http":
            async with (
                TestServer(application(backlog, console=False)) as server,
                Client(str(server.make_url(ENDPOINT_PATH)), pick(VERSION)) as client,
            ):
                await client.initialize()
                yield client


async def read(client: BaseClient, uri: str) -> TextResourceContents:
    if client.adapter.version != VERSION and uri.startswith(("skill://", "backlog://skills")):
        uri = f"mcp-extensions://{skills.EXTENSION}/" + uri.split("://", 1)[1]
    result = await client.read_resource(uri)
    assert len(result.contents) == 1
    block = result.contents[0]
    assert isinstance(block, TextResourceContents)
    return block


def test_frontmatter_reads_scalars_and_ignores_nesting() -> None:
    assert skills.frontmatter(SAMPLE) == {
        "name": "sample",
        "description": "A skill written by the tests.",
        "metadata": "",
    }
    assert skills.frontmatter('---\nname: "quoted"\n---\n') == {"name": "quoted"}
    assert skills.frontmatter("# No frontmatter\n") == {}
    assert skills.frontmatter("") == {}


def test_body_is_everything_below_the_frontmatter() -> None:
    assert skills.body(SAMPLE).startswith("# Sample")
    assert skills.body("plain text\n") == "plain text"
    # An unterminated block leaves no instructions to follow.
    assert skills.body("---\nname: broken\n") == ""


def test_bundled_skills_describe_themselves() -> None:
    assert skills.library.names() == ["planning", "triage"]
    for skill in skills.library.skills():
        assert skill.frontmatter["name"] == skill.name
        assert skill.description
        assert skill.uri == f"{skills.SCHEME}{skill.name}/{skills.MANIFEST}"
        assert skill.instructions
        assert not skill.instructions.startswith("---")
        assert skill.references


def test_entry_carries_a_digest_and_a_size_for_every_file() -> None:
    skill = skills.library.skill("triage")
    assert skill is not None
    entry = skill.entry()
    assert entry["uri"] == skill.uri
    assert entry["frontmatter"]["name"] == "triage"
    assert [item["uri"] for item in entry["resources"]] == [item.uri for item in skill.files]
    for item, described in zip(skill.files, entry["resources"], strict=True):
        data = item.path.read_bytes()
        assert described["digest"] == f"sha256:{sha256(data).hexdigest()}"
        assert described["size"] == len(data)


def test_a_library_of_ones_own(tmp_path: Path) -> None:
    written(tmp_path, "sample")
    library = Library(tmp_path)
    skill = library.skill("sample")
    assert skill is not None
    assert library.names() == ["sample"]
    assert [item.uri for item in skill.files] == [
        "skill://sample/SKILL.md",
        "skill://sample/references/notes.md",
    ]
    assert skill.manifest.contents().mime_type == skills.MIME


def test_only_text_files_are_served(tmp_path: Path) -> None:
    folder = written(tmp_path, "sample")
    (folder / skills.REFERENCES / "run.sh").write_text("#!/bin/sh\nrm -rf /\n", encoding="utf-8")
    library = Library(tmp_path)
    assert [item.path.name for item in library.references("sample")] == ["notes.md"]
    assert library.file("sample", "references/run.sh") is None


@pytest.mark.parametrize(
    ("name", "relative"),
    [
        ("..", skills.MANIFEST),
        (".", skills.MANIFEST),
        (".ssh", skills.MANIFEST),
        ("/etc", skills.MANIFEST),
        ("sample", "references/../../SKILL.md"),
        ("sample", "../sample/SKILL.md"),
        ("sample", "references/.."),
        ("sample", "SKILL.md/../../SKILL.md"),
        ("sample", "notes.md"),
        ("sample", "references/deeper/notes.md"),
        ("sample", ""),
        ("missing", skills.MANIFEST),
    ],
)
def test_a_path_that_names_nothing_served_is_refused(
    tmp_path: Path, name: str, relative: str
) -> None:
    written(tmp_path, "sample")
    assert Library(tmp_path).file(name, relative) is None


def test_an_empty_root_holds_no_skills(tmp_path: Path) -> None:
    library = Library(tmp_path / "nowhere")
    assert library.names() == []
    assert library.skills() == []
    assert library.skill("sample") is None
    assert library.references("sample") == ()


def test_named_accepts_a_manifest_uri_only() -> None:
    assert skills.named("skill://triage/SKILL.md") == "triage"
    for wrong in [
        "backlog://triage/SKILL.md",
        "skill://triage/references/conventions.md",
        "skill://triage",
        "skill://../SKILL.md",
        "skill:///SKILL.md",
    ]:
        with pytest.raises(Rejected) as raised:
            skills.named(wrong)
        assert raised.value.failure.kind is FailureKind.INVALID_PARAMS


async def test_discover_reports_the_skills_extension(skill_client: BaseClient) -> None:
    discovery = await skill_client.initialize()
    assert discovery["capabilities"]["extensions"][skills.EXTENSION] == {}
    assert "resources" in discovery["capabilities"]


async def test_skills_list_names_every_skill_with_its_files(skill_client: BaseClient) -> None:
    result = await skill_client.request_method("skills/list")
    assert result["resultType"] == "complete"
    assert result["cacheScope"] == "public"
    assert result["ttlMs"] == 0
    assert [item["frontmatter"]["name"] for item in result["skills"]] == ["planning", "triage"]
    for item in result["skills"]:
        assert item["uri"].startswith(skills.SCHEME)
        for file in item["resources"]:
            assert file["digest"].startswith("sha256:")
            assert file["size"] > 0


async def test_skills_get_returns_one_skill(skill_client: BaseClient) -> None:
    result = await skill_client.request_method("skills/get", {"uri": "skill://triage/SKILL.md"})
    assert result["resultType"] == "complete"
    assert result["cacheScope"] == "public"
    assert result["ttlMs"] == 0
    skill = result["skill"]
    assert skill["uri"] == "skill://triage/SKILL.md"
    assert skill["frontmatter"]["name"] == "triage"
    assert [item["uri"] for item in skill["resources"]] == [
        "skill://triage/SKILL.md",
        "skill://triage/references/conventions.md",
    ]
    listing = await skill_client.request_method("skills/list")
    assert skill in listing["skills"]


@pytest.mark.parametrize(
    ("method", "params", "code", "message"),
    [
        ("skills/get", {}, -32602, "uri"),
        ("skills/get", {"uri": 42}, -32602, "uri"),
        ("skills/get", {"uri": "skill://../SKILL.md"}, -32602, "not a skill uri"),
        ("skills/get", {"uri": "skill://missing/SKILL.md"}, -32602, "no skill"),
        (
            "skills/get",
            {"uri": "skill://triage/references/conventions.md"},
            -32602,
            "not a skill uri",
        ),
        ("skills/list", {"cursor": "unknown"}, -32602, "invalid skill cursor"),
        ("skills/nothing", {}, -32601, "skills/nothing"),
    ],
)
async def test_extension_errors_are_protocol_errors(
    skill_client: BaseClient, method: str, params: dict[str, Any], code: int, message: str
) -> None:
    with pytest.raises(ClientError) as raised:
        await skill_client.request_method(method, params)
    assert raised.value.code == code
    assert message in str(raised.value)


async def test_extension_methods_require_the_new_revision(client: BaseClient) -> None:
    discovery = await client.initialize()
    if client.adapter.version == VERSION:
        assert skills.EXTENSION in discovery["capabilities"]["extensions"]
        return
    assert skills.EXTENSION not in discovery["capabilities"].get("extensions", {})
    for method, params in [("skills/list", {}), ("skills/get", {"uri": "skill://triage/SKILL.md"})]:
        with pytest.raises(ClientError) as raised:
            await client.request_method(method, params)
        assert raised.value.code == -32601
    manifest = await read(client, f"mcp-extensions://{skills.EXTENSION}/manifest.json")
    assert json.loads(manifest.text)["methods"] == ["skills/get", "skills/list"]


async def test_the_files_of_a_skill_are_plain_resource_reads(client: BaseClient) -> None:
    instructions = await read(client, "skill://triage/SKILL.md")
    cited = await read(client, "skill://triage/references/conventions.md")
    listing = await read(client, skills.LIST_URI)
    entry = await read(client, f"{skills.LIST_URI}/triage")
    assert instructions.mime_type == skills.MIME
    assert instructions.text.startswith("---")
    assert "task_update" in instructions.text
    assert cited.text
    assert [item["frontmatter"]["name"] for item in json.loads(listing.text)] == [
        "planning",
        "triage",
    ]
    assert json.loads(entry.text)["uri"] == "skill://triage/SKILL.md"


async def test_reading_a_skill_that_is_not_there(client: BaseClient) -> None:
    for uri in (
        "skill://missing/SKILL.md",
        "skill://triage/references/missing.md",
        f"{skills.LIST_URI}/missing",
    ):
        assert "error" in json.loads((await read(client, uri)).text)


async def test_the_served_library_is_the_one_on_disk(
    skill_client: BaseClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "library"
    written(root, "sample")
    monkeypatch.setattr(skills, "library", Library(root))
    listing = await skill_client.request_method("skills/list")
    one = await skill_client.request_method("skills/get", {"uri": "skill://sample/SKILL.md"})
    assert [item["frontmatter"]["name"] for item in listing["skills"]] == ["sample"]
    assert one["skill"]["frontmatter"]["description"] == "A skill written by the tests."
    (root / "sample" / skills.MANIFEST).write_text(
        SAMPLE + "\nNew instructions.\n", encoding="utf-8"
    )
    updated = await skill_client.request_method("skills/get", {"uri": "skill://sample/SKILL.md"})
    content = await read(skill_client, "skill://sample/SKILL.md")
    manifest = updated["skill"]["resources"][0]
    assert manifest["digest"] != one["skill"]["resources"][0]["digest"]
    assert manifest["digest"] == f"sha256:{sha256(content.text.encode()).hexdigest()}"


async def test_legacy_method_resources_return_current_namespaced_skills(client: BaseClient) -> None:
    if client.adapter.version == VERSION:
        return
    prefix = f"mcp-extensions://{skills.EXTENSION}/"
    listing = json.loads((await read(client, prefix + "skills/list")).text)
    assert [item["frontmatter"]["name"] for item in listing["skills"]] == ["planning", "triage"]
    entry = listing["skills"][0]
    assert entry["uri"] == prefix + "planning/SKILL.md"
    params = quote(json.dumps({"uri": entry["uri"]}), safe="")
    detail = json.loads((await read(client, prefix + "skills/get?params=" + params)).text)
    assert detail["skill"] == entry
    for file in entry["resources"]:
        assert file["uri"].startswith(prefix)
        contents = await read(client, file["uri"])
        assert contents.uri == file["uri"]
        assert "sha256:" + sha256(contents.text.encode()).hexdigest() == file["digest"]
