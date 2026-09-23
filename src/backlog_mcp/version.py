"""The code that answers: the package version and the git commit of its checkout."""

from __future__ import annotations

from pathlib import Path

VERSION = "0.21.0"

SHORT = 7


def git_dir(directory: Path) -> Path | None:
    """The git directory of `directory`, or None when it holds no `.git` entry.

    A worktree keeps a `.git` file with a `gitdir:` line instead of a directory.
    """
    entry = directory / ".git"
    if entry.is_dir():
        return entry
    if not entry.is_file():
        return None
    text = entry.read_text(encoding="utf-8").strip()
    if not text.startswith("gitdir:"):
        return None
    target = Path(text.removeprefix("gitdir:").strip())
    return target if target.is_absolute() else (directory / target).resolve()


def resolve_ref(git: Path, ref: str) -> str | None:
    """The hash `ref` names: a loose ref file, else a `packed-refs` line, else None."""
    common = git
    pointer = git / "commondir"
    if pointer.is_file():
        target = Path(pointer.read_text(encoding="utf-8").strip())
        common = target if target.is_absolute() else (git / target).resolve()
    for base in (git, common):
        loose = base / ref
        if loose.is_file():
            return loose.read_text(encoding="utf-8").strip() or None
    packed = common / "packed-refs"
    if packed.is_file():
        for line in packed.read_text(encoding="utf-8").splitlines():
            if line.startswith(("#", "^")):
                continue
            parts = line.split(maxsplit=1)
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
    return None


def commit_of(start: Path) -> str | None:
    """The short HEAD commit of the checkout that holds `start`, or None outside a checkout.

    Reads `.git/HEAD` and the ref file it names. Does not run git. A checkout that cannot be
    read gives None, not an error.
    """
    try:
        for directory in (start, *start.parents):
            git = git_dir(directory)
            if git is None:
                continue
            head = (git / "HEAD").read_text(encoding="utf-8").strip()
            if head.startswith("ref:"):
                found = resolve_ref(git, head.removeprefix("ref:").strip())
            else:
                found = head or None
            return found[:SHORT] if found else None
    except OSError:
        return None
    return None


#: The commit the imported package runs from; None for an installed wheel.
COMMIT = commit_of(Path(__file__).resolve().parent)
