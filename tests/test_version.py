import tomllib
from importlib.metadata import version
from pathlib import Path

from backlog_mcp.version import COMMIT, VERSION, commit_of

HASH = "0123456789abcdef0123456789abcdef01234567"
OTHER = "fedcba9876543210fedcba9876543210fedcba98"


def checkout(root: Path, head: str) -> Path:
    git = root / ".git"
    git.mkdir(parents=True)
    (git / "HEAD").write_text(head + "\n")
    return git


def test_a_branch_with_a_loose_ref(tmp_path: Path) -> None:
    git = checkout(tmp_path, "ref: refs/heads/master")
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "refs" / "heads" / "master").write_text(HASH + "\n")
    #: Any directory inside the checkout finds it.
    assert commit_of(tmp_path / "src" / "pkg") == HASH[:7]


def test_a_branch_with_a_packed_ref(tmp_path: Path) -> None:
    git = checkout(tmp_path, "ref: refs/heads/master")
    (git / "packed-refs").write_text(
        f"# pack-refs with: peeled fully-peeled sorted\n{OTHER} refs/tags/v1\n"
        f"^{OTHER}\n{HASH} refs/heads/master\n"
    )
    assert commit_of(tmp_path) == HASH[:7]


def test_a_detached_head(tmp_path: Path) -> None:
    checkout(tmp_path, HASH)
    assert commit_of(tmp_path) == HASH[:7]


def test_a_worktree_reads_the_common_refs(tmp_path: Path) -> None:
    main = checkout(tmp_path / "main", "ref: refs/heads/master")
    (main / "refs" / "heads").mkdir(parents=True)
    (main / "refs" / "heads" / "feature").write_text(HASH + "\n")
    private = main / "worktrees" / "feature"
    private.mkdir(parents=True)
    (private / "HEAD").write_text("ref: refs/heads/feature\n")
    (private / "commondir").write_text("../..\n")
    tree = tmp_path / "feature"
    tree.mkdir()
    (tree / ".git").write_text(f"gitdir: {private}\n")
    assert commit_of(tree) == HASH[:7]


def test_no_checkout_and_broken_refs_give_none(tmp_path: Path) -> None:
    assert commit_of(tmp_path) is None
    checkout(tmp_path / "dangling", "ref: refs/heads/missing")
    assert commit_of(tmp_path / "dangling") is None
    (tmp_path / "odd").mkdir()
    (tmp_path / "odd" / ".git").write_text("not a gitdir line\n")
    assert commit_of(tmp_path / "odd") is None


def test_the_package_knows_its_own_checkout() -> None:
    assert VERSION
    assert COMMIT is not None and len(COMMIT) == 7


def test_package_metadata_matches_runtime_version() -> None:
    project = Path(__file__).resolve().parents[1] / "pyproject.toml"
    metadata = tomllib.loads(project.read_text())
    assert metadata["project"]["version"] == VERSION
    assert version("backlog-mcp") == VERSION
    assert "+" not in VERSION
