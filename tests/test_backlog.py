"""Scope resolution: which project a call works on when nothing names one."""

from pathlib import Path

import pytest

from backlog_mcp.backlog import Backlog, UnknownProject, checkout_root, slugify
from backlog_mcp.models import Origin


def instance(tmp_path: Path, cwd: Path, project: str | None = None) -> Backlog:
    return Backlog(
        tmp_path / "backlog.sqlite3",
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
    assert project.roots[0].root == str(root)

    again, origin = await backlog.current()
    assert origin is Origin.DIRECTORY
    assert again.id == project.id
    await backlog.close()


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
    await backlog.close()


async def test_nothing_but_the_directory_chooses(tmp_path: Path) -> None:
    """Another registered project does not take over the directory scope."""
    here = tmp_path / "here"
    there = tmp_path / "there"
    here.mkdir()
    there.mkdir()
    backlog = instance(tmp_path, here)

    local, _ = await backlog.current()
    await backlog.create(there)
    chosen, origin = await backlog.current()
    assert origin is Origin.DIRECTORY
    assert chosen.id == local.id
    await backlog.close()


async def test_the_named_project_beats_everything(tmp_path: Path) -> None:
    here = tmp_path / "here"
    there = tmp_path / "there"
    here.mkdir()
    there.mkdir()
    backlog = instance(tmp_path, here)
    other = await backlog.create(there, name="Other")
    await backlog.create(here)

    backlog.named = other.slug
    chosen, origin = await backlog.current()
    assert origin is Origin.ARGUMENT
    assert chosen.id == other.id
    await backlog.close()


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
    await backlog.close()


async def test_save_registers_the_directory_a_root_or_updates_a_name(tmp_path: Path) -> None:
    root = tmp_path / "work"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    backlog = instance(tmp_path, root)

    here = await backlog.save(connection=await backlog.open(), transaction=backlog.transaction)
    assert here.roots[0].root == str(root)
    assert (
        await backlog.save(connection=await backlog.open(), transaction=backlog.transaction)
    ).id == here.id

    added = await backlog.save(
        root="../other",
        name="Other",
        description="Second board",
        connection=await backlog.open(),
        transaction=backlog.transaction,
    )
    assert added.roots[0].root == str(other)
    assert added.description == "Second board"

    renamed = await backlog.save(
        reference=here.slug,
        name="Renamed",
        archived=True,
        connection=await backlog.open(),
        transaction=backlog.transaction,
    )
    assert renamed.id == here.id
    assert renamed.name == "Renamed"
    assert renamed.archived is True
    await backlog.close()


async def test_save_binds_an_existing_project_on_another_computer(tmp_path: Path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    backlog = instance(tmp_path, root)
    project = await backlog.save(
        name="Work", connection=await backlog.open(), transaction=backlog.transaction
    )

    remote = backlog.view(computer="laptop")
    bound = await remote.save(
        reference=project.slug,
        root="/srv/work",
        connection=await remote.open(),
        transaction=remote.transaction,
    )
    contextual = await remote.save(
        reference=project.slug,
        root="",
        connection=await remote.open(),
        transaction=remote.transaction,
    )

    assert bound.id == project.id == contextual.id
    assert {(item.computer, item.root) for item in contextual.roots} == {
        ("here", str(root)),
        ("laptop", ""),
        ("laptop", "/srv/work"),
    }
    await backlog.close()
