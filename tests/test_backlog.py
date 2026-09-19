"""Scope resolution: which project a call works on when nothing names one."""

from pathlib import Path

import pytest

from backlog_mcp.backlog import Backlog, UnknownProject, checkout_root, slugify
from backlog_mcp.models import Origin
from backlog_mcp.state import State
from backlog_mcp.store import Store


def instance(tmp_path: Path, cwd: Path, project: str | None = None) -> Backlog:
    return Backlog(
        Store(tmp_path / "backlog.sqlite3"),
        State(tmp_path / "state.json"),
        cwd=cwd,
        project=project,
        computer="here",
    )


@pytest.mark.parametrize(
    ("text", "slug"),
    [("Backlog MCP", "backlog-mcp"), ("  ??  ", "project"), ("Release 1.0", "release-1-0")],
)
def test_slugify(text: str, slug: str) -> None:
    assert slugify(text) == slug


def test_checkout_root_climbs_to_the_marker(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    deep = tmp_path / "src" / "package"
    deep.mkdir(parents=True)
    assert checkout_root(deep) == tmp_path
    assert checkout_root(Path("/")) == Path("/")


async def test_the_directory_decides_and_the_project_is_made_once(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    (root / ".git").mkdir(parents=True)
    backlog = instance(tmp_path, root / "src")

    project, origin = await backlog.current()
    assert origin is Origin.CREATED
    assert project.root == str(root)
    assert backlog.state.last_project("here") == project.slug

    again, origin = await backlog.current()
    assert origin is Origin.DIRECTORY
    assert again.id == project.id
    await backlog.store.close()


async def test_a_second_directory_gets_its_own_slug(tmp_path: Path) -> None:
    first = tmp_path / "a" / "work"
    second = tmp_path / "b" / "work"
    first.mkdir(parents=True)
    second.mkdir(parents=True)

    backlog = instance(tmp_path, first)
    one, _ = await backlog.current()
    backlog.cwd = second
    two, _ = await backlog.current()
    assert (one.slug, two.slug) == ("work", "work-2")
    await backlog.store.close()


async def test_a_pin_beats_the_directory_and_a_dead_pin_is_dropped(tmp_path: Path) -> None:
    here = tmp_path / "here"
    there = tmp_path / "there"
    here.mkdir()
    there.mkdir()
    backlog = instance(tmp_path, here)

    local, _ = await backlog.current()
    other = await backlog.create(there)
    backlog.state.pin("here", other.slug)
    pinned, origin = await backlog.current()
    assert origin is Origin.PINNED
    assert pinned.id == other.id

    backlog.state.pin("here", "gone")
    fallback, origin = await backlog.current()
    assert origin is Origin.DIRECTORY
    assert fallback.id == local.id
    assert backlog.state.pinned("here") is None
    await backlog.store.close()


async def test_the_named_project_beats_everything(tmp_path: Path) -> None:
    here = tmp_path / "here"
    there = tmp_path / "there"
    here.mkdir()
    there.mkdir()
    backlog = instance(tmp_path, here)
    other = await backlog.create(there, name="Other")
    backlog.state.pin("here", (await backlog.create(here)).slug)

    backlog.named = other.slug
    chosen, origin = await backlog.current()
    assert origin is Origin.ARGUMENT
    assert chosen.id == other.id
    await backlog.store.close()


async def test_lookup_takes_a_slug_or_any_path_inside(tmp_path: Path) -> None:
    root = tmp_path / "work"
    (root / "src").mkdir(parents=True)
    backlog = instance(tmp_path, root)
    project = await backlog.create(root)

    assert (await backlog.lookup(project.slug)).id == project.id
    assert (await backlog.lookup(str(root))).id == project.id
    assert (await backlog.lookup("./src")).id == project.id
    assert (await backlog.resolve(None)).id == project.id
    with pytest.raises(UnknownProject, match="no project 'missing'"):
        await backlog.lookup("missing")
    with pytest.raises(UnknownProject):
        await backlog.lookup(str(tmp_path / "elsewhere"))
    await backlog.store.close()


async def test_save_registers_the_directory_a_root_or_updates_a_name(tmp_path: Path) -> None:
    root = tmp_path / "work"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    backlog = instance(tmp_path, root)

    here = await backlog.save()
    assert here.root == str(root)
    assert (await backlog.save()).id == here.id

    added = await backlog.save(root="../other", name="Other", description="Second board")
    assert added.root == str(other)
    assert added.description == "Second board"

    renamed = await backlog.save(reference=here.slug, name="Renamed", archived=True)
    assert renamed.id == here.id
    assert renamed.name == "Renamed"
    assert renamed.archived is True
    await backlog.store.close()
