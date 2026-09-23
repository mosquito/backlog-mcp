"""Explicit FTS expressions must not expand into semantic matches."""

from typing import Any

import pytest
from helpers import mutate

from backlog_mcp import embeddings, guidance
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
            embeddings.Search(query=[query], kinds=["tasks"], **args),
            backlog,
            connection,
            backlog.transaction,
        )

    exact = await find('"login token"')
    assert [item.id for item in exact.tasks] == [created[0].id]
    assert all(item.match == "fts" and item.score is None for item in exact.tasks)
    assert (await find('"login" NOT "legacy"')).count == 1
    assert (await find('"login" OR "authentication"')).count == 3
    assert (await find('("login" OR "authentication") AND "tok"*')).count == 2
    assert (await find('NEAR("login" "token", 0)')).count == 1
    assert (await find('tags:"ui"', status=[Status.DONE])).count == 1
    assert (await find('"login token"', status=[Status.DELETED])).count == 1
    assert (await find('"login"', tag="api")).count == 0
    assert (await find('"login"', limit=1)).count == 1
    assert (await find('"nothing"')).count == 0
    for query in ['"unterminated', '("login" OR)', '"', 'unknown:"login"']:
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
    args = embeddings.Search(query=['"unique literal"'], kinds=["tasks"], project=project.full_name)
    result = await embeddings.search(args, backlog, connection, backlog.transaction)
    assert result.count == 1
    assert result.tasks[0].project == project.full_name
    stranger = backlog.view(principal="stranger")
    result = await embeddings.search(
        embeddings.Search(query=['"unique literal"'], all_computers=True),
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
        '"refresh token"': {"refresh token"},
        '"refresh token"*': {"refresh token", "refresh tokens"},
        '"token"*': prefix,
        '"token" "refresh"': {"refresh token"},
        '"token" AND "refresh"': {"refresh token"},
        '"token" OR "password"': exact | {"password reset"},
        '"token" NOT "legacy"': exact - {"legacy token"},
        '("token" OR "password") AND "reset"': {"token reset", "password reset"},
        '"refresh" + "token"': {"refresh token"},
        'NEAR("refresh" "token", 5)': {"refresh token"},
        'NEAR("refresh" "token")': {"refresh token"},
        '^"token"': {"token", "token reset", "payload"},
        'title:"token"': exact - {"payload"},
        'title:("token" OR "password")': (exact - {"payload"}) | {"password reset"},
        '{title body}:"token"': exact,
        '-tags:"token"': exact,
        'tags:"web-ui"': {"payload"},
        '"say ""hello"" now"': {"say hello now"},
    }
    for query, expected in cases.items():
        assert f"`{query}`" in guidance.read("search")
        found = await embeddings.search(
            embeddings.Search(query=[query], kinds=["tasks"]),
            backlog,
            connection,
            backlog.transaction,
        )
        assert {task.title for task in found.tasks} == expected, query


@pytest.mark.parametrize(
    ("query", "text", "vector"),
    [
        ("token", None, "token"),
        ('"token"', '"token"', None),
        ('"token" "production"', '"token" "production"', None),
        (
            '"token" восстановить "production"',
            '"token" "production"',
            "token восстановить production",
        ),
        ('restore "refresh token"', '"refresh token"', "restore refresh token"),
        ('"token" 2026', '"token"', "token 2026"),
        ('"token"* restore', '"token"*', "token* restore"),
        ('"token" OR "production"', '"token" OR "production"', None),
        ('NEAR("token" "production", 5)', 'NEAR("token" "production", 5)', None),
        ('title:"token"', 'title:"token"', None),
    ],
)
def test_query_modes(query: str, text: str | None, vector: str | None) -> None:
    assert embeddings.split_query(query) == (text, vector)


async def test_mixed_search_routes_queries_and_merges_without_duplicates(
    backlog: Backlog, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ = await backlog.current()
    connection = await backlog.open()
    tasks = []
    for title in ["token production", "recover credentials", "token staging"]:
        tasks.append(
            await mutate(
                operations.add_task(
                    connection, backlog.actor, project_id=project.id, title=title, tags=["ui"]
                ),
                backlog.transaction,
            )
        )
    encoded = []
    original = embeddings.encoder.encode

    def capture(texts: list[str]) -> Any:
        encoded.extend(texts)
        return original(texts)

    monkeypatch.setattr(embeddings.encoder, "encode", capture)
    args = embeddings.Search(
        query=['"token" восстановить "production"'], kinds=["tasks"], min_score=-1, tag="ui"
    )
    result = await embeddings.search(args, backlog, connection, backlog.transaction)
    assert encoded[-1] == "token восстановить production"
    assert result.count == 3
    assert result.tasks[0].id == tasks[0].id
    assert result.tasks[0].match == "fts"
    assert all(item.match == "semantic" for item in result.tasks[1:])
    assert len({item.id for item in result.tasks}) == 3
    limited = await embeddings.search(
        args.model_copy(update={"limit": 1}), backlog, connection, backlog.transaction
    )
    assert limited.count == 1 and limited.tasks[0].id == tasks[0].id
    strict = await embeddings.search(
        args.model_copy(update={"min_score": 1}), backlog, connection, backlog.transaction
    )
    assert strict.count == 1 and strict.tasks[0].match == "fts"
    excluded = await embeddings.search(
        args.model_copy(update={"tag": "other"}), backlog, connection, backlog.transaction
    )
    assert excluded.count == 0
