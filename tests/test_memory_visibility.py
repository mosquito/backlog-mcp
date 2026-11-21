"""Personal memory stays with its author; shared memory stays in its project."""

import asyncio
import json

import pytest
from aiohttp.test_utils import TestServer
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from test_events import call, collect
from test_tenants import account

from backlog_mcp import embeddings, httpd


@pytest.mark.parametrize("adapter", AdapterSet.default().versions)
async def test_memory_sharing_and_scope(backlog, adapter):
    async with (
        TestServer(
            httpd.application(backlog, basic=["alice:password", "bob:password", "eve:password"])
        ) as server,
        account(server, "alice", adapter=adapter) as alice,
        account(server, "bob", adapter=adapter) as bob,
        account(server, "eve", adapter=adapter) as eve,
    ):
        project = (await call(alice, "project_save", root="", name="Together"))["full_name"]
        other = (await call(alice, "project_save", root="", name="Elsewhere"))["full_name"]
        await call(alice, "project_share", project=project, principal="bob", role="editor")
        await call(alice, "project_share", project=other, principal="bob", role="reader")
        personal = await call(
            alice, "memory_save", project=project, title="needle", body="Alice only"
        )
        shared = await call(alice, "memory_save", project=project, title="needle", shared=True)
        elsewhere = await call(alice, "memory_save", project=other, title="needle", shared=True)
        own = await call(bob, "memory_save", project=project, title="needle")
        assert len({personal["id"], shared["id"], own["id"]}) == 3
        for client, private in ((alice, personal), (bob, own)):
            visible = {private["id"], shared["id"]}
            listed = await call(client, "memory_list", project=project)
            assert {item["id"] for item in listed["items"]} == visible
            assert listed["count"] == 2
            assert (await call(client, "project_board", project=project))["memory"] == 2
            assert (await call(client, "stats", project=project))["memory"] == 2
            assert (await call(client, "memory_get", project=project, title="needle"))[
                "id"
            ] == private["id"]
            for query in (['"needle"'], ["needle"]):
                result = await call(
                    client, "search", project=project, query=query, kinds=["memory"], min_score=-1
                )
                assert {item["id"] for item in result["memory"]} == visible
        for name, args in (
            ("memory_get", {"id": personal["id"]}),
            ("memory_save", {"id": personal["id"], "body": "Not allowed"}),
            ("memory_sharing", {"id": personal["id"], "shared": True, "global_scope": True}),
            ("memory_append", {"id": personal["id"], "text": "Not allowed"}),
            ("memory_delete", {"id": personal["id"]}),
            ("memory_save", {"id": shared["id"], "shared": False}),
        ):
            assert (await bob.call_tool(name, args)).is_error
        resource = await bob.read_resource(f"backlog://memory/{personal['id']}")
        assert "error" in json.loads(resource.contents[0].text)
        added = await call(alice, "memory_save", project=project, title="Later private")
        indexer = embeddings.indexers[backlog.database]
        await indexer.refresh()
        bob_index = indexer.states["bob"].indexes
        assert added["id"] not in bob_index[1].ids
        await call(alice, "memory_sharing", id=personal["id"], shared=False, global_scope=True)
        await call(alice, "memory_sharing", id=shared["id"], shared=True, global_scope=True)
        assert (await call(eve, "memory_list", all_computers=True))["items"] == []
        assert (await call(eve, "search", query=['"needle"'], all_computers=True))["memory"] == []
        assert (await eve.call_tool("memory_get", {"id": shared["id"]})).is_error
        for client, visible in (
            (alice, {personal["id"], shared["id"], elsewhere["id"]}),
            (bob, {shared["id"], elsewhere["id"]}),
        ):
            listed = await call(client, "memory_list", project=other)
            assert {item["id"] for item in listed["items"]} == visible
            assert listed["items"][0]["id"] == elsewhere["id"]
            for query in (['"needle"'], ["needle"]):
                result = await call(
                    client, "search", project=other, query=query, kinds=["memory"], min_score=-1
                )
                assert {item["id"] for item in result["memory"]} == visible
        assert (await call(alice, "memory_get", project=other, title="needle"))["id"] == elsewhere[
            "id"
        ]
        await call(alice, "memory_sharing", id=added["id"], shared=False, global_scope=True)
        assert (await call(alice, "memory_get", project=other, title="Later private"))[
            "id"
        ] == added["id"]
        assert (
            await bob.call_tool("memory_get", {"project": other, "title": "Later private"})
        ).is_error
        await call(alice, "memory_save", id=shared["id"], title="Privatized")
        await call(alice, "memory_sharing", id=shared["id"], shared=False, global_scope=False)
        await indexer.refresh()
        assert (await bob.call_tool("memory_get", {"id": shared["id"]})).is_error
        assert shared["id"] not in indexer.states["bob"].indexes[1].ids


async def test_private_notifications_do_not_reach_project_members(backlog):
    async with (
        TestServer(httpd.application(backlog, basic=["alice:password", "bob:password"])) as server,
        account(server, "alice") as alice,
        account(server, "bob") as bob,
    ):
        project = (await call(alice, "project_save", root="", name="Together"))["full_name"]
        await call(alice, "project_share", project=project, principal="bob", role="editor")
        frames = asyncio.Queue()
        collecting = collect(bob.listen(resources=["backlog://memory"]), frames)
        try:
            async with asyncio.timeout(5):
                while not bob.accepted:
                    await asyncio.sleep(0.01)
            private = await call(alice, "memory_save", project=project, title="Personal secret")
            await call(alice, "memory_append", id=private["id"], text="Private text")
            async with account(server, "alice", project=project) as scoped:
                await call(scoped, "memory_delete", id=private["id"])
            shared = await call(alice, "memory_save", project=project, title="Public", shared=True)
            event = (await asyncio.wait_for(frames.get(), 5))["params"]["_meta"]["backlog/event"]
            assert event["id"] == shared["id"] and event["title"] == "Public"
            await call(alice, "memory_sharing", id=shared["id"], shared=False, global_scope=False)
            await call(alice, "memory_save", id=shared["id"], title="Now private")
            event = (await asyncio.wait_for(frames.get(), 5))["params"]["_meta"]["backlog/event"]
            assert event["action"] == "deleted" and event["title"] == "Public"
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(frames.get(), 0.1)
        finally:
            collecting.cancel()
            await asyncio.gather(collecting, return_exceptions=True)
