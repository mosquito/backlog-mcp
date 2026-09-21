"""Extract legacy task updates without interpreting Markdown inside code fences."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

HEADING = re.compile(r"# UPDATE: (\d{4}-\d{2}-\d{2}(?:T| )\d{2}:\d{2}:\d{2}Z?)$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


@dataclass(frozen=True)
class LegacyEntry:
    body: str
    created_at: float


def split_history(body: str) -> tuple[str, list[LegacyEntry], bool]:
    """Split recognized update blocks; leave ambiguous histories intact and flag them.

    Dates without a zone were written in UTC. Exact original bodies are also retained
    by the migration, including separators and timestamp spelling.
    """
    lines = body.splitlines(keepends=True)
    boundaries: list[tuple[int, float]] = []
    fence = ""
    fence_size = 0
    ambiguous = False
    for index, line in enumerate(lines):
        text = line.rstrip("\r\n")
        if match := FENCE.match(text):
            marker, suffix = match.groups()
            if not fence:
                fence, fence_size = marker[0], len(marker)
            elif marker[0] == fence and len(marker) >= fence_size and not suffix.strip():
                fence = ""
            continue
        if fence or not text.startswith("# UPDATE:"):
            continue
        if index == 0 or lines[index - 1].rstrip("\r\n") != "---":
            ambiguous = True
            continue
        heading = HEADING.fullmatch(text)
        if heading is None:
            ambiguous = True
            continue
        try:
            moment = datetime.fromisoformat(heading[1].replace("Z", "+00:00"))
            timestamp = moment.replace(tzinfo=UTC).timestamp()
        except ValueError:
            ambiguous = True
            continue
        if boundaries and timestamp < boundaries[-1][1]:
            ambiguous = True
        boundaries.append((index - 1, timestamp))
    if ambiguous:
        return body, [], True
    if not boundaries:
        return body, [], False
    entries = [
        LegacyEntry(
            "".join(
                lines[
                    start + 2 : boundaries[index + 1][0]
                    if index + 1 < len(boundaries)
                    else len(lines)
                ]
            ),
            timestamp,
        )
        for index, (start, timestamp) in enumerate(boundaries)
    ]
    return "".join(lines[: boundaries[0][0]]), entries, False
