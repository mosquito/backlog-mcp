"""Change notifications carry the event that caused them."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from aiohttp.test_utils import TestServer
from aiohttp_tiny_mcp import Client, MethodFilter
from aiohttp_tiny_mcp.client.base import BaseClient
from aiohttp_tiny_mcp.protocol.models import CallToolResult, TextContent
from aiohttp_tiny_mcp.testing import connect, pick

from backlog_mcp.backlog import Backlog
from backlog_mcp.httpd import ENDPOINT_PATH, application
from backlog_mcp.models import EVENT_KEY
from backlog_mcp.server import (
    EVENTS,
    NOTE_CHANGED,
    PROJECT_CHANGED,
    TASK_CHANGED,
    registry,
)

UPDATED = "notifications/resources/updated"


async def call(client: BaseClient, tool_name: str, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool_name, arguments)
    assert isinstance(result, CallToolResult) and not result.is_error, result
    if result.structured_content is not None:
        value = result.structured_content
    else:
        assert isinstance(result.content[0], TextContent)
        value = json.loads(result.content[0].text)
    assert isinstance(value, dict)
    return value


def collect(
    frames: AsyncIterator[dict[str, Any]], into: asyncio.Queue[dict[str, Any]]
) -> asyncio.Task[None]:
    async def run() -> None:
        async for frame in frames:
            await into.put(frame)

    return asyncio.create_task(run())


def summary(frame: dict[str, Any]) -> tuple[str, str, str, int]:
    assert frame["method"] == UPDATED
    event = frame["params"]["_meta"][EVENT_KEY]
    return frame["params"]["uri"], event["kind"], event["action"], event["id"]


async def test_a_modern_listener_learns_what_each_write_did(backlog: Backlog) -> None:
    async with (
        connect(registry, adapter="2026-07-28") as listener,
        connect(registry, adapter="2026-07-28") as writer,
    ):
        other = await call(writer, "project_save", root="../other")
        frames: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        collecting = collect(
            listener.listen(resources=["backlog://tasks", "backlog://memory"]), frames
        )
        try:
            while not listener.accepted:
                await asyncio.sleep(0.01)
            task = await call(writer, "task_add", title="Ship it")
            await call(writer, "task_update", id=task["id"], status="done")
            await call(writer, "task_move", id=task["id"], project=other["slug"])
            await call(writer, "task_delete", id=task["id"])
            note = await call(writer, "memory_save", title="Rule", body="Always")
            await call(writer, "memory_append", id=note["id"], text="More")
            await call(writer, "memory_delete", id=note["id"])
            await call(writer, "project_save", project=other["slug"], name="Other")
            seen = [summary(await asyncio.wait_for(frames.get(), 5)) for _ in range(8)]
        finally:
            collecting.cancel()
            await asyncio.gather(collecting, return_exceptions=True)
    assert seen == [
        ("backlog://tasks", "task", "added", task["id"]),
        ("backlog://tasks", "task", "updated", task["id"]),
        ("backlog://tasks", "task", "moved", task["id"]),
        ("backlog://tasks", "task", "deleted", task["id"]),
        ("backlog://memory", "memory", "added", note["id"]),
        ("backlog://memory", "memory", "updated", note["id"]),
        ("backlog://memory", "memory", "deleted", note["id"]),
        ("backlog://tasks", "project", "updated", other["id"]),
    ]


async def test_the_event_names_the_record_and_the_writer(backlog: Backlog) -> None:
    async with (
        connect(registry, adapter="2026-07-28") as listener,
        connect(registry, adapter="2026-07-28") as writer,
    ):
        task = await call(writer, "task_add", title="Ship it", priority="high")
        frames: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        collecting = collect(listener.listen(resources=[f"backlog://tasks/{task['id']}"]), frames)
        try:
            while not listener.accepted:
                await asyncio.sleep(0.01)
            await call(writer, "task_update", id=task["id"], status="done")
            frame = await asyncio.wait_for(frames.get(), 5)
        finally:
            collecting.cancel()
            await asyncio.gather(collecting, return_exceptions=True)
    assert frame["params"]["uri"] == f"backlog://tasks/{task['id']}"
    assert frame["params"]["_meta"][EVENT_KEY] == {
        "kind": "task",
        "action": "updated",
        "id": task["id"],
        "project": task["project"],
        "title": "Ship it",
        "status": "done",
        "priority": "high",
        "computer": "here",
        "principal": backlog.store.principal,
        "project_id": (await backlog.current())[0].id,
    }


async def test_a_legacy_get_stream_carries_the_event(backlog: Backlog) -> None:
    """Revisions before 2026-07-28 subscribe by URI and read the GET stream."""
    app = application(backlog, console=False)
    async with TestServer(app) as server:
        url = str(server.make_url(ENDPOINT_PATH))
        async with (
            Client(url, pick("2025-03-26")) as listener,
            Client(url, pick("2025-03-26")) as writer,
        ):
            await listener.initialize()
            await writer.initialize()
            task = await call(writer, "task_add", title="Ship it")
            frames: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            collecting = collect(listener.listen(resources=["backlog://tasks"]), frames)
            try:
                #: The GET stream has no acknowledgement: write until the stream is open.
                frame = None
                for _ in range(100):
                    await call(writer, "task_update", id=task["id"], status="done")
                    try:
                        candidate = await asyncio.wait_for(frames.get(), 0.1)
                        # A legacy stream also carries extension events. It can attach
                        # between the resource and extension publications in SQLite.
                        if candidate["method"] == UPDATED:
                            frame = candidate
                            break
                    except TimeoutError:
                        continue
            finally:
                collecting.cancel()
                await asyncio.gather(collecting, return_exceptions=True)
    assert frame is not None
    assert summary(frame) == ("backlog://tasks", "task", "updated", task["id"])
    assert frame["params"]["_meta"][EVENT_KEY]["status"] == "done"


async def test_the_extension_declares_its_notifications(backlog: Backlog) -> None:
    async with connect(registry, adapter="2026-07-28", initialize=False) as client:
        discovery = await client.initialize()
    assert discovery["capabilities"]["extensions"][EVENTS]["notifications"] == {
        TASK_CHANGED: "project",
        NOTE_CHANGED: "project",
        PROJECT_CHANGED: None,
    }


async def wait_accepted(*clients: BaseClient) -> None:
    while not all(client.accepted for client in clients):
        await asyncio.sleep(0.01)


async def test_every_listener_hears_a_broadcast(backlog: Backlog) -> None:
    async with (
        connect(registry, adapter="2026-07-28") as one,
        connect(registry, adapter="2026-07-28") as two,
        connect(registry, adapter="2026-07-28") as writer,
    ):
        heard_one: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        heard_two: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        collecting = [
            collect(one.listen(methods=[TASK_CHANGED, NOTE_CHANGED, PROJECT_CHANGED]), heard_one),
            collect(two.listen(methods=[TASK_CHANGED]), heard_two),
        ]
        try:
            await wait_accepted(one, two)
            assert one.accepted == {"methods": [TASK_CHANGED, NOTE_CHANGED, PROJECT_CHANGED]}
            task = await call(writer, "task_add", title="Ship it", status="doing")
            note = await call(writer, "memory_save", title="Rule", body="Always")
            frames_one = [await asyncio.wait_for(heard_one.get(), 5) for _ in range(2)]
            frame_two = await asyncio.wait_for(heard_two.get(), 5)
        finally:
            for task_ in collecting:
                task_.cancel()
            await asyncio.gather(*collecting, return_exceptions=True)
    assert [frame["method"] for frame in frames_one] == [TASK_CHANGED, NOTE_CHANGED]
    assert frame_two["method"] == TASK_CHANGED
    event = {key: value for key, value in frame_two["params"].items() if key != "_meta"}
    assert event == {
        "kind": "task",
        "action": "added",
        "id": task["id"],
        "project": task["project"],
        "title": "Ship it",
        "status": "doing",
        "priority": "normal",
        "computer": "here",
        "principal": backlog.store.principal,
        "project_id": (await backlog.current())[0].id,
    }
    assert frames_one[1]["params"]["id"] == note["id"]
    assert frames_one[1]["params"]["kind"] == "memory"
    #: Nobody asked for notes on the second listener.
    assert heard_two.empty()


async def test_a_listener_picks_its_project(backlog: Backlog) -> None:
    async with (
        connect(registry, adapter="2026-07-28") as one,
        connect(registry, adapter="2026-07-28") as every,
        connect(registry, adapter="2026-07-28") as writer,
    ):
        other = await call(writer, "project_save", root="../other")
        heard_one: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        heard_every: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        collecting = [
            collect(
                one.listen(
                    methods=[MethodFilter(method=TASK_CHANGED, topics=[other["full_name"]])]
                ),
                heard_one,
            ),
            collect(every.listen(methods=[TASK_CHANGED]), heard_every),
        ]
        try:
            await wait_accepted(one, every)
            assert one.accepted == {
                "methods": [{"method": TASK_CHANGED, "topics": [other["full_name"]]}]
            }
            here = await call(writer, "task_add", title="Here")
            there = await call(writer, "task_add", title="There", project=other["slug"])
            seen_every = [
                (await asyncio.wait_for(heard_every.get(), 5))["params"]["id"] for _ in range(2)
            ]
            seen_one = (await asyncio.wait_for(heard_one.get(), 5))["params"]
        finally:
            for task_ in collecting:
                task_.cancel()
            await asyncio.gather(*collecting, return_exceptions=True)
    assert seen_every == [here["id"], there["id"]]
    assert (seen_one["id"], seen_one["project"]) == (there["id"], other["full_name"])
    assert heard_one.empty()


async def test_a_legacy_stream_hears_every_broadcast_unasked(backlog: Backlog) -> None:
    app = application(backlog, console=False)
    async with TestServer(app) as server:
        url = str(server.make_url(ENDPOINT_PATH))
        async with (
            Client(url, pick("2025-03-26")) as listener,
            Client(url, pick("2025-03-26")) as writer,
        ):
            await listener.initialize()
            await writer.initialize()
            frames: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            collecting = collect(listener.listen(), frames)
            try:
                frame = None
                for _ in range(100):
                    await call(writer, "task_add", title="Ship it")
                    try:
                        frame = await asyncio.wait_for(frames.get(), 0.1)
                        break
                    except TimeoutError:
                        continue
            finally:
                collecting.cancel()
                await asyncio.gather(collecting, return_exceptions=True)
    assert frame is not None
    assert frame["method"] == TASK_CHANGED
    assert frame["params"]["title"] == "Ship it"
