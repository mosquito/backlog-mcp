"""Explicit FTS expressions must not expand into semantic matches."""

from typing import Any

import pytest
from helpers import mutate

from backlog_mcp import embeddings
from backlog_mcp import server as operations
from backlog_mcp.backlog import Backlog
from backlog_mcp.models import Status


async def test_literal_search_skips_model_and_applies_filters(
    backlog: Backlog, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Literal search must not load or query the embedding model")

    monkeypatch.setattr(embeddings, "indexes", forbidden)
    project, _ = await backlog.current()
    connection = await backlog.open()
    created = []
    for title, status, tags in [
        ("login token", Status.TODO, ["ui"]),
        ("login legacy token", Status.DONE, ["ui"]),
        ("authentication secret", Status.TODO, ["api"]),
        ("login token deleted", Status.DELETED, ["ui"]),
    ]:
        created.append(
            await mutate(
                operations.add_task(
                    connection,
                    backlog.actor,
                    project_id=project.id,
                    title=title,
                    status=status,
                    tags=tags,
                ),
                backlog.transaction,
            )
        )

    async def find(query: str, **args: Any) -> embeddings.Matches:
        return await embeddings.search(
            embeddings.Search(query=query, kinds=["tasks"], **args),
            backlog,
            connection,
            backlog.transaction,
        )

    exact = await find('"login token"')
    assert [item.id for item in exact.tasks] == [created[0].id]
    assert all(item.match == "fts" and item.score is None for item in exact.tasks)
    assert (await find("fts: login NOT legacy")).count == 1
    assert (await find("fts: login OR authentication")).count == 3
    assert (await find("fts: (login OR authentication) AND tok*")).count == 2
    assert (await find("fts: NEAR(login token, 0)")).count == 1
    assert (await find("fts: tags:ui", status=[Status.DONE])).count == 1
    assert (await find('"login token"', status=[Status.DELETED])).count == 1
    assert (await find("fts: login", tag="api")).count == 0
    assert (await find("fts: login", limit=1)).count == 1
    assert (await find("fts: nothing")).count == 0
    for query in ['"unterminated', "fts: (login OR)", "fts:", "fts: unknown:login"]:
        with pytest.raises(ValueError, match="FTS5"):
            await find(query)


async def test_literal_search_respects_project_and_principal(backlog: Backlog) -> None:
    project, _ = await backlog.current()
    other = await backlog.create(backlog.cwd.parent / "other")
    connection = await backlog.open()
    for owner in [project, other]:
        await mutate(
            operations.add_task(
                connection,
                backlog.actor,
                project_id=owner.id,
                title="unique literal",
            ),
            backlog.transaction,
        )
    args = embeddings.Search(query='"unique literal"', kinds=["tasks"], project=project.full_name)
    result = await embeddings.search(args, backlog, connection, backlog.transaction)
    assert result.count == 1
    assert result.tasks[0].project == project.full_name
    stranger = backlog.view(principal="stranger")
    result = await embeddings.search(
        embeddings.Search(query='"unique literal"', all_computers=True),
        stranger,
        connection,
        backlog.transaction,
    )
    assert result.count == 0


async def test_documented_literal_examples(backlog: Backlog) -> None:
    project, _ = await backlog.current()
    connection = await backlog.open()
    titles = [
        "token",
        "tokens",
        "tokenizer",
        "refresh token",
        "refresh tokens",
        "legacy token",
        "password reset",
        "token reset",
        "say hello now",
        "payload",
    ]
    for title in titles:
        await mutate(
            operations.add_task(
                connection,
                backlog.actor,
                project_id=project.id,
                title=title,
                body="token" if title == "payload" else "",
                tags=["web-ui"] if title == "payload" else [],
            ),
            backlog.transaction,
        )
    exact = {"token", "refresh token", "legacy token", "token reset", "payload"}
    prefix = exact | {"tokens", "tokenizer", "refresh tokens"}
    cases = {
        '"token"': exact,
        '"token*"': exact,
        '"token"*': prefix,
        '"refresh token"': {"refresh token"},
        '"refresh token"*': {"refresh token", "refresh tokens"},
        "fts: token*": prefix,
        "fts: token refresh": {"refresh token"},
        "fts: token AND refresh": {"refresh token"},
        "fts: token OR password": exact | {"password reset"},
        "fts: token NOT legacy": exact - {"legacy token"},
        "fts: (token OR password) AND reset": {"token reset", "password reset"},
        "fts: refresh + token": {"refresh token"},
        "fts: NEAR(refresh token, 5)": {"refresh token"},
        "fts: NEAR(refresh token)": {"refresh token"},
        "fts: ^token": {"token", "token reset", "payload"},
        "fts: title:token": exact - {"payload"},
        "fts: title:(token OR password)": (exact - {"payload"}) | {"password reset"},
        "fts: {title body}:token": exact,
        "fts: -tags:token": exact,
        'fts: tags:"web-ui"': {"payload"},
        '"say ""hello"" now"': {"say hello now"},
    }
    for query, expected in cases.items():
        assert f"`{query}`" in (operations.search.__doc__ or "")
        found = await embeddings.search(
            embeddings.Search(query=query, kinds=["tasks"]),
            backlog,
            connection,
            backlog.transaction,
        )
        assert {task.title for task in found.tasks} == expected, query
