"""MCP connections used by the command line, including a local stdio subprocess."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from base64 import b64encode
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import ClientSession, ClientTimeout
from aiohttp_tiny_mcp import AdapterSet, Client, StdioClient
from aiohttp_tiny_mcp.client_base import BaseClient, Elicitor
from aiohttp_tiny_mcp.models import CallToolResult, TextContent, TextResourceContents
from pydantic import BaseModel
from yarl import URL

from . import paths, skills
from .backlog import checkout_root
from .models import ProjectOut, Projects, Scope
from .state import State

if TYPE_CHECKING:
    from .cli import CLI


class ToolError(Exception):
    """An operation rejected by the MCP server."""

    @property
    def exit_code(self) -> int:
        return 2 if str(self).startswith(("no project", "no computer")) else 1


@dataclass(frozen=True)
class Registration:
    name: str | None = None
    description: str | None = None
    project: str | None = None
    computer: str | None = None
    root: Path | None = None


Registrar = Callable[[str, Path, list[ProjectOut]], Registration]


@dataclass
class RemoteFile:
    uri: str
    size: int


@dataclass
class RemoteSkill:
    name: str
    uri: str
    description: str
    files: list[dict[str, Any]]
    instructions: str = ""

    @property
    def references(self) -> list[RemoteFile]:
        return [
            RemoteFile(item["uri"], item["size"]) for item in self.files if item["uri"] != self.uri
        ]


class Connection:
    """Typed tool calls over one initialized connection."""

    def __init__(self, client: BaseClient, *, computer: str, timeout: float, root: Path) -> None:
        self.client = client
        self.computer = computer
        self.timeout = timeout
        self.root = root
        self.relocate: Callable[[str, Path], Awaitable[None]] | None = None
        self.state: State | None = None

    async def call[M: BaseModel](
        self, model: type[M], tool: str, *, exclude_none: bool = True, **arguments: Any
    ) -> M:
        if isinstance(self.client, Client) and arguments.get("project"):
            arguments["project"] = project_reference(arguments["project"], self.root)
        async with asyncio.timeout(self.timeout):
            result = await self.client.call_tool(
                tool,
                {
                    key: value
                    for key, value in arguments.items()
                    if value is not None or not exclude_none
                },
            )
        if not isinstance(result, CallToolResult):
            raise ToolError("the server requested unsupported input")
        text = "\n".join(item.text for item in result.content if isinstance(item, TextContent))
        if result.is_error:
            kind, separator, message = text.partition(": ")
            if separator and kind in {
                "LookupError",
                "UnknownProject",
                "ValueError",
                "PermissionError",
            }:
                text = message
            raise ToolError(text)
        value = result.structured_content
        if value is None:
            value = json.loads(text)
        return model.model_validate(value)

    async def scope(self) -> Scope:
        return await self.call(Scope, "project_current")

    async def skills(self) -> list[RemoteSkill]:
        content = await self.read(skills.LIST_URI)
        entries = json.loads(content.text)
        return [
            RemoteSkill(
                name=item["frontmatter"]["name"],
                uri=item["uri"],
                description=item["frontmatter"].get("description", ""),
                files=item["resources"],
            )
            for item in entries
        ]

    async def read(self, uri: str) -> TextResourceContents:
        async with asyncio.timeout(self.timeout):
            result = await self.client.read_resource(uri)
        if not result.contents or not isinstance(result.contents[0], TextResourceContents):
            raise ToolError(f"expected a text resource: {uri}")
        return result.contents[0]


async def stop(process: asyncio.subprocess.Process) -> None:
    """Close stdin, then terminate or kill a child that does not stop."""
    if process.stdin is not None:
        process.stdin.close()
    try:
        await asyncio.wait_for(process.wait(), 2)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 2)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()


def project_reference(reference: str, root: Path) -> str:
    if reference.startswith((".", "/", "~")) or "/" in reference:
        return str((root / Path(reference).expanduser()).resolve())
    return reference


def local_command(options: CLI, computer: str) -> list[str]:
    level = "debug" if options.log_level < logging.INFO else "warning"
    command = [sys.executable, "-m", "backlog_mcp", "--log-level", level]
    for flag, value in (
        ("--database", (options.database or paths.database_path()).expanduser().resolve()),
        ("--state", (options.state or paths.state_path()).expanduser().resolve()),
        ("--root", (options.root or Path.cwd()).expanduser().resolve()),
        ("--computer", computer),
        ("--project", options.project),
    ):
        if value is not None:
            command.extend((flag, str(value)))
    command.extend(("mcp", "--protocol", "stdio"))
    return command


def directory_project(computer: str, root: Path, projects: list[ProjectOut]) -> ProjectOut | None:
    candidates = [
        (Path(binding.root), project)
        for project in projects
        for binding in project.roots
        if binding.computer == computer and binding.root and root.is_relative_to(Path(binding.root))
    ]
    return max(candidates, key=lambda item: len(item[0].parts))[1] if candidates else None


async def bind_directory(connection: Connection, register: Registrar) -> ProjectOut:
    """Resolve a computer and directory before creating a binding through MCP."""
    root = connection.root
    original_computer = connection.computer
    if not root.is_dir():
        raise ToolError(f"no such directory: {root}")
    projects = await connection.call(Projects, "project_list", archived=True)
    project = directory_project(connection.computer, root, projects.items)
    if project is None:
        directory = checkout_root(root)
        known = await connection.call(Projects, "project_list", archived=True, all_computers=True)
        chosen = register(connection.computer, directory, known.items)
        directory = chosen.root or directory
        computer = chosen.computer or connection.computer
        if computer != connection.computer or directory != root:
            assert connection.relocate is not None
            await connection.relocate(computer, directory)
        # The edited location can already belong to a project. Check the server again.
        projects = await connection.call(Projects, "project_list", archived=True)
        project = directory_project(computer, directory, projects.items)
        if project is None:
            project = await connection.call(
                ProjectOut,
                "project_save",
                root=str(directory),
                project=chosen.project,
                name=chosen.name,
                description=chosen.description,
            )
    if isinstance(connection.client, Client):
        connection.client.base_url = str(
            URL(connection.client.base_url).update_query(project=project.slug)
        )
    if connection.computer != original_computer and connection.state is not None:
        connection.state.update(computer=connection.computer)
    return project


@asynccontextmanager
async def connect(
    options: CLI, *, on_ask: Elicitor, on_register: Registrar, scope_directory: bool = True
) -> AsyncIterator[Connection]:
    async with AsyncExitStack() as stack:
        connection = await stack.enter_async_context(transport(options, on_ask=on_ask))
        connection.state = State(options.state or paths.state_path())

        async def relocate(computer: str, root: Path) -> None:
            await stack.aclose()
            options.computer = computer
            options.root = root
            replacement = await stack.enter_async_context(transport(options, on_ask=on_ask))
            connection.client = replacement.client
            connection.computer = replacement.computer
            connection.root = replacement.root

        connection.relocate = relocate
        endpoint = URL(options.url) if options.url else None
        explicit = options.project or (endpoint.query.get("project") if endpoint else None)
        if scope_directory and not explicit:
            await bind_directory(connection, on_register)
        yield connection


@asynccontextmanager
async def transport(options: CLI, *, on_ask: Elicitor) -> AsyncIterator[Connection]:
    """Open one transport without resolving or registering a project."""
    adapter = AdapterSet.default().by_version["2026-07-28"]
    if options.timeout <= 0:
        raise ValueError("--timeout must be greater than zero")
    state = State(options.state or paths.state_path())
    root = (options.root or Path.cwd()).expanduser().resolve()
    endpoint = URL(options.url) if options.url else None
    computer = state.computer_name(
        options.computer or (endpoint.query.get("computer") if endpoint else None)
    )
    if endpoint is not None:
        if endpoint.scheme not in {"http", "https"} or not endpoint.host:
            raise ValueError("--url must be an absolute HTTP or HTTPS MCP endpoint")
        endpoint = endpoint.update_query(computer=computer)
        if options.project:
            reference = project_reference(options.project, root)
            endpoint = endpoint.update_query(project=reference)
            options.project = reference
        headers = {}
        if options.token or endpoint.query.get("token"):
            if options.user or options.password or endpoint.user or endpoint.query.get("auth"):
                raise ValueError("configure --token or Basic authentication, not both")
            if options.token:
                headers["Authorization"] = f"Bearer {options.token}"
        elif options.user:
            if ":" in options.user:
                raise ValueError("the Basic auth user must not contain a colon")
            credentials = b64encode(f"{options.user}:{options.password}".encode()).decode("ascii")
            headers["Authorization"] = f"Basic {credentials}"
        async with (
            ClientSession(
                headers=headers, timeout=ClientTimeout(total=None, sock_connect=options.timeout)
            ) as session,
            Client(str(endpoint), adapter, session=session, on_ask=on_ask) as client,
        ):
            async with asyncio.timeout(options.timeout):
                await client.initialize()
            connection = Connection(client, computer=computer, timeout=options.timeout, root=root)
            yield connection
        return
    process = await asyncio.create_subprocess_exec(
        *local_command(options, computer),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        limit=8 * 1024 * 1024,
    )
    assert process.stdout is not None and process.stdin is not None
    try:
        local = StdioClient(process.stdout, process.stdin, adapter, on_ask=on_ask)
        async with asyncio.timeout(options.timeout):
            await local.initialize()
        connection = Connection(local, computer=computer, timeout=options.timeout, root=root)
        yield connection
    finally:
        await stop(process)
