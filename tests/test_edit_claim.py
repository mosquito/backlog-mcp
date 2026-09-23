"""An edit claim protects writes and remembers the original assignee on the server."""

import pytest
from helpers import mutate
from test_task_claim import assignment, new_task

from backlog_mcp import backlog as scope
from backlog_mcp import server
from backlog_mcp.backlog import Backlog
from backlog_mcp.records import AssignmentConflict


async def test_edit_claim_restores_other_principal_and_guards_writes(backlog: Backlog) -> None:
    task = await new_task(backlog)
    project, _ = await backlog.current()
    connection = await backlog.open()
    await mutate(scope.register(connection, backlog.actor, "other", "Other"), backlog.transaction)
    await mutate(
        scope.share(connection, backlog.actor, project.id, "other", "editor"), backlog.transaction
    )
    other = backlog.view(principal="other", computer="another")
    original, token = assignment(other), assignment(backlog, "ui-edit-test")
    await mutate(
        server.claim_task(connection, other.actor, task, expected=None, assigned=original),
        backlog.transaction,
    )
    await mutate(
        server.claim_task(
            connection, backlog.actor, task, expected=original, assigned=token, temporary=True
        ),
        backlog.transaction,
    )
    for actor, expected in [(backlog.actor, None), (other.actor, token)]:
        with pytest.raises((ValueError, PermissionError)):
            await mutate(
                server.update_task(
                    connection, actor, task, body="Unsafe", expected_assigned=expected
                ),
                backlog.transaction,
            )
    with pytest.raises(ValueError, match="being edited"):
        await mutate(
            server.update_task(connection, backlog.actor, task, body="Unsafe"), backlog.transaction
        )
    with pytest.raises(ValueError, match="being edited"):
        await mutate(
            server.claim_task(
                connection,
                other.actor,
                task,
                expected=token,
                assigned=assignment(other, "edit"),
                temporary=True,
            ),
            backlog.transaction,
        )
    with pytest.raises(PermissionError):
        await mutate(
            server.claim_task(
                connection, other.actor, task, expected=token, assigned=None, restore=True
            ),
            backlog.transaction,
        )
    saved = await server.task_update(
        server.UpdateTask(id=task, body="Safe", expected_assigned=token, release_edit=True),
        backlog,
        connection,
        backlog.transaction,
    )
    assert saved.body == "Safe"
    assert saved.assigned == original
    with pytest.raises(AssignmentConflict):
        await server.task_update(
            server.UpdateTask(id=task, body="Stale", expected_assigned=token, release_edit=True),
            backlog,
            connection,
            backlog.transaction,
        )
    assert (await server.read_task(connection, backlog.actor, task)).body == "Safe"


async def test_edit_claim_survives_reopen_and_lost_claim_does_not_restore(backlog: Backlog) -> None:
    task = await new_task(backlog)
    token = assignment(backlog, "ui-edit-reload")
    connection = await backlog.open()
    await mutate(
        server.claim_task(
            connection, backlog.actor, task, expected=None, assigned=token, temporary=True
        ),
        backlog.transaction,
    )
    await backlog.close()
    connection = await backlog.open()
    restored, _ = await mutate(
        server.claim_task(
            connection, backlog.actor, task, expected=token, assigned=None, restore=True
        ),
        backlog.transaction,
    )
    assert restored.assigned is None
    await mutate(
        server.claim_task(
            connection, backlog.actor, task, expected=None, assigned=token, temporary=True
        ),
        backlog.transaction,
    )
    next_owner = assignment(backlog, "takeover")
    await mutate(
        server.claim_task(connection, backlog.actor, task, expected=token, assigned=next_owner),
        backlog.transaction,
    )
    with pytest.raises(AssignmentConflict):
        await mutate(
            server.claim_task(
                connection, backlog.actor, task, expected=token, assigned=None, restore=True
            ),
            backlog.transaction,
        )
    with pytest.raises(AssignmentConflict):
        await server.task_update(
            server.UpdateTask(
                id=task, title="Must not save", expected_assigned=token, release_edit=True
            ),
            backlog,
            connection,
            backlog.transaction,
        )
    assert (await server.read_task(connection, backlog.actor, task)).assigned == next_owner


async def test_edit_restore_failure_rolls_back_content(backlog: Backlog) -> None:
    task = await new_task(backlog)
    connection = await backlog.open()
    with pytest.raises(ValueError, match="requires expected_assigned"):
        await server.task_update(
            server.UpdateTask(id=task, body="Must roll back", release_edit=True),
            backlog,
            connection,
            backlog.transaction,
        )
    assert (await server.read_task(connection, backlog.actor, task)).body == ""
