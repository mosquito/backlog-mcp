# backlog-mcp

A project backlog for the terminal, and the same backlog over MCP.

Tasks belong to projects, and a project is a directory tree. The working directory therefore
decides which board a command works on, so there is nothing to pass and nothing to configure.
Notes preserve lasting context from the user: preferences, working agreements, people,
and business constraints unavailable from other sources.
`backlog` is the command for people; `backlog mcp` serves the identical data to an agent, on
stdio or over HTTP.

## Install

```bash
uv tool install backlog-mcp
```

Register the server with the MCP clients on this machine:

```bash
backlog install            # claude and codex
backlog install --claude   # one of them only
```

Installation auto-approves this server's tools in both clients:

- Claude: adds `mcp__backlog__*` to `permissions.allow` in `~/.claude/settings.json`.
- Codex: sets `mcp_servers.backlog.default_tools_approval_mode = "approve"` in
  `~/.codex/config.toml`, including existing per-tool approval overrides for this server.
- Codex: also sets the user-wide `sandbox_mode = "danger-full-access"`, disabling its sandbox.

`--name` changes the server name and its approval rule. Reinstalling updates these settings;
`--force` also replaces an existing server registration. Unrelated configuration values remain intact.
Codex configuration is read and written with `rtoml`. The clients' own CLIs still register the
server when available; permission settings are applied after registration in either case.
Server confirmation for deletion from another project still applies.

Configuration references: [Claude permissions](https://code.claude.com/docs/en/permissions#mcp)
and [Codex configuration](https://learn.chatgpt.com/docs/config-file/config-reference).

## Use it from the terminal

```bash
cd ~/dev/my-project        # the project is registered on first use
backlog add Write the release notes -t docs
backlog create             # the same, in $EDITOR
backlog list
backlog search notes       # tasks and notes at once
backlog show 1
backlog start 1            # doing
backlog block 1            # blocked
backlog close 1            # done
backlog reopen 1           # todo again
backlog stats              # open tasks, closed tasks, and notes
```

Every command prints a table or a panel, in colour when the terminal takes it:

```
$ backlog search notes
1 task match(es) for 'notes' in my-project
╭────┬────────┬────────┬─────────────────────────┬──────────────────────────────┬──────┬─────╮
│ id │ status │ pri    │ title                   │ match                        │ tags │ age │
├────┼────────┼────────┼─────────────────────────┼──────────────────────────────┼──────┼─────┤
│  1 │ todo   │ normal │ Write the release notes │ Mention the HTTP [notes]     │ docs │  2m │
╰────┴────────┴────────┴─────────────────────────┴──────────────────────────────┴──────┴─────╯
```

The title column always holds the title; `match` holds the excerpt of the description that the
search hit, and it appears only when there is one. A second table follows when a note matched.

`stats` reports `open` (todo, doing, blocked), `closed` (done, cancelled), and `notes`,
plus the task total, status and priority breakdowns, and tags. Note counts include shared notes.

### Search

For agents, `search` is the default way to find project information. Use targeted queries,
inspect returned matches, and fetch individual records only when more detail is needed.
Avoid full listings and reading every task or note to answer a question. Refine queries or
filters when results are weak. Reserve listing tools for requested inventories or complete
reviews, with filters and pagination; use `stats` for counts.

`backlog search` and the MCP `search` tool combine SQLite FTS5 prefix matching with
multilingual semantic search over titles, bodies and tags. FTS matches come first in
BM25 order. Additional semantic matches follow in cosine similarity order. Each ID
appears once per record kind. The limit applies separately to tasks and notes after merging.

Project scope, task status and tag filters apply to both searches. Shared notes remain
visible across projects. `min_score` (`--min-score` in the terminal) defaults to `0.15`
and filters semantic matches only; it never removes FTS matches. When FTS finds anything,
additional semantic matches need a score of at least `0.25`. MCP results include `match`
(`fts` or `semantic`) and a cosine `score` for semantic candidates (`null` for FTS-only
matches). FTS excerpts remain in `snippet`. Semantic matches can use different words or
languages from the query.

The bundled Model2Vec model is
[`sentence-transformers/static-similarity-mrl-multilingual-v1`](https://huggingface.co/sentence-transformers/static-similarity-mrl-multilingual-v1),
truncated to 256 dimensions with MRL and quantized to int8. It is already a static model;
no transformer distillation is required. Inference uses `asyncio.to_thread()` and runs
offline. The source model was trained for similarity, so retrieval quality can vary.

Model files live in `src/backlog_mcp/model/`; weights use Git LFS. Run `git lfs pull`
after cloning. To reproduce the conversion from the pinned Hugging Face revision, run
`uv run python scripts/prepare_model.py`. The directory includes provenance, model license
(Apache-2.0), and the Model2Vec library citation (MIT).

Each running process keeps its embedding cache in a private temporary directory
(`backlog-mcp-embeddings-*` under the system temporary directory), created on the first search
and removed when the process exits. It holds IDs, vectors and a content digest; SQLite remains
the source of record text. SQLite change counters invalidate the cache after local writes or
commits from other processes; the next search rebuilds it and reads results from SQLite by ID.
Nothing is shared between processes, so there is no lock file.

The `backlog-mcp/model2vec` extension also exposes `model2vec/encode` for normalized text
embeddings. It uses the same local model. Ordinary `search` works on every supported
MCP revision.

### Projects

A directory becomes a project the first time a command runs in it. The root is the nearest
ancestor holding `.git`, `.hg`, `.svn` or `.jj`, else the directory itself.

```bash
backlog current            # which project, and why that one
backlog projects           # every registered project
backlog move 1 other-project
```

`--project` goes before the subcommand and applies to every command:

```bash
backlog --project other-project list
backlog projects delete other-project  # every task and note on that board goes with it
```

### Computers

A project is a directory tree on one computer, and every project remembers its computer. One
database can therefore hold the boards of several machines: a synced SQLite file, or one HTTP
server every machine talks to. The name comes from `computer = "..."` in the config file or
`--computer`; else it is the short host name, remembered in the state file on first use, so a
renamed host keeps its projects.

```bash
backlog current                    # names this computer and lists the others
backlog projects                   # the projects of this computer
backlog projects --all-computers   # every project, with a column for the other computers
backlog list --computer laptop     # open tasks of every project on the laptop
backlog search deploy --all-computers
backlog move 3 laptop-app          # a task moves between computers like between projects
```

Listings stay on this computer unless `--computer` or `--all-computers` widens them, and shared
notes are visible from every computer. Over MCP the same selectors are the `computer` and
`all_computers` arguments, and every project, task and note names its computer. A project on
another computer says `here: false`; the server's instructions tell the agent that its root is a
path on that computer, so it may read and change the tasks but must not touch the files.

Over HTTP one server answers for every computer. A client connects as one of them with query
parameters on the endpoint URL:

```
https://backlog.example.com/mcp?computer=laptop&project=app
```

`computer` names the computer the client runs on, and `project` fixes its current project, since
the server cannot see the client's working directory. A client that cannot send an
`Authorization` header passes the credentials in the URL instead, as `auth=<base64 of
user:password>`; the header wins when both are present. Without `project`, `project_current`
reports `origin: none` and every tool that needs a project takes `project=<slug>`: nothing is
pinned or remembered, so one client never redirects another. `project_save` with an explicit `root`
checks that the directory exists on this computer; for another computer it takes the path as
given. Every client shares the credentials and may name any computer: the server tells the
computers apart, it does not keep them from each other.

### Notes

Code changes; notes remain. Notes preserve lasting user context unavailable elsewhere:
preferences, working agreements, people and responsibilities, business constraints, and reasons
for decisions. They concern the context around the work, not the project's implementation.
Do not save snapshots of code, architecture, configuration, bugs, or documentation: they become
stale as the project changes. A useful note should still help after the code is rewritten.
Project scope organizes notes; it does not define their subject.

Use `search` as a regular information source alongside web search. Check notes to recall past
agreements and preferences before making decisions or asking the user again. Use code,
documentation, and the web for current technical facts. Search for relevant notes instead of
reading them all; update outdated context when the user corrects it.

Save proactively whenever the user provides useful new knowledge unavailable elsewhere.
Do not wait for an explicit request to remember it or for the session to end. Capture preferences,
constraints, corrections, and reasons for decisions as they arise. Search first, then update an
existing note or create one short note per topic. Omit guesses and duplicates.

A note body holds at most 1,024 characters, including appended paragraphs.
A note has a Markdown body, tags and a stable id, so a task body can say `see note #3`. One title per project: saving the same title again replaces the
body. `--shared` makes a note visible from every project. Notes are in the search index with the
tasks. Never write a secret into a note, only where the secret lives.

```bash
# Save this only if the user supplied it and it is unavailable elsewhere.
backlog note add release coordination -b "Ask Anna before scheduling a customer rollout." -t people
backlog note append 1 "Anna prefers one day of advance notice."
backlog note list                      # this project plus every shared note
backlog note show 1
backlog note edit 1                    # the same form, in $EDITOR
backlog search release coordination    # relevant tasks and past agreements
backlog note add communication --shared # visible from every project
```

## Serve it to an agent

```bash
backlog mcp                                        # stdio, what a client starts itself
backlog mcp --protocol=http --http-port=3009       # endpoint /mcp, console /
backlog-mcp --protocol=http --http-user=me --http-password=secret
```

`backlog-mcp` is `backlog mcp` without the word. A config file holds these options in its
`[mcp]` table, and the environment as `BACKLOG_MCP_HTTP_PORT` and friends; `--config FILE` is
accepted before and after `mcp`. Over HTTP the
endpoint lives at `/mcp` and the bundled console at `/`. A user and a password turn Basic auth
on; without them every request is served, so bind to `127.0.0.1`. The endpoint refuses a browser
`Origin` other than its own, against DNS rebinding; behind a TLS-terminating reverse proxy the
scheme it sees differs from the public one, so pass `--http-trust-proxy` and let the proxy
validate origins. One HTTP server can answer for several computers; see [Computers](#computers).

The agent gets 17 tools (`project_current`, `project_list`, `project_save`, `project_delete`,
`task_add`, `task_get`, `task_list`, `task_update`, `task_move`, `task_delete`, `note_save`,
`note_append`, `note_get`, `note_list`, `note_delete`, `search`, `stats`), the same
boards, and the same database. `search` is the one search: it answers with the tasks and
the notes that match, so a new kind of record later is one more list in the result, not a new
tool. The resources `backlog://project`, `backlog://projects`, `backlog://tasks`,
`backlog://tasks/{id}`, `backlog://notes` and `backlog://notes/{id}` read the same data without a
tool call, and a write publishes `notifications/resources/updated` for the ones it touched.

`task_delete` and `note_delete` delete immediately within the current project. Deleting
a record owned by another project requires confirmation, including shared notes. MCP clients
without confirmation support cannot delete across projects. Terminal commands follow the same
rule; `--yes` confirms deletion from another project. `project_delete` removes a project with
every task and note on its board and frees its directories, only with `confirm=true`; without it
the tool reports what the project holds. `backlog projects delete <slug>` does the same and asks
first unless `--yes` is given. Archive with `project_save` when the history should stay.

The server's instructions and bundled skills tell agents to search for relevant project knowledge,
read only what they need, and retain user-provided facts unavailable elsewhere, without secrets.

### Skills

Two written skills ship with the server: `triage` and `planning`. A skill is text — the
instructions a client loads before it works, and the reference files those instructions cite.
Nothing is executed: the client reads the instructions and the tools above do the work.

A client that speaks the `io.modelcontextprotocol/skills` extension of MCP `2026-07-28` finds
them with `skills/list` and `skills/get`, then reads each file with `resources/read`. The same
data is also readable as plain resources on that revision:

| URI | What |
| --- | --- |
| `backlog://skills` | Every skill: the URI of its instructions, its frontmatter, and its files with SHA-256 digests and sizes |
| `backlog://skills/<name>` | One skill, described the same way |
| `skill://<name>/SKILL.md` | The instructions, frontmatter first |
| `skill://<name>/references/<file>` | One text file the instructions cite |

On older revisions, read
`mcp-extensions://io.modelcontextprotocol/skills/skills/list` with `resources/read`.
Its entries include namespaced file URIs. For details, read
`mcp-extensions://io.modelcontextprotocol/skills/skills/get?params=<encoded-json>`;
the JSON object is `{"uri": "<skill-uri>"}`, percent-encoded as one query value.
On older revisions, select these URIs under **Resources** and **Resource templates**.
The console shows native **Extensions** and **Skills** only on MCP `2026-07-28`.

Every registered extension method gets a compatibility resource automatically.
File templates belong to the extension.
The registry installs the extension after all declarations. Restart the server
and reload the viewer after editing these registrations, including with an editable install.

The server registers these methods through `aiohttp-tiny-mcp`'s native `Extension` API
(version 0.3.1 or newer). HTTP and stdio use the standard protocol adapters. Skill files remain
text-only and are read from disk on each request.

The terminal reads them too:

```bash
backlog skills          # name, description, how many files
backlog skills triage   # the instructions in full
```

## Where the data lives

| What | Path | Override |
| --- | --- | --- |
| Projects, tasks and notes | `$XDG_DATA_HOME/backlog-mcp/backlog.sqlite3` | `--database`, `BACKLOG_MCP_DATABASE` |
| The computer name | `$XDG_STATE_HOME/backlog-mcp/state.json` | `--state`, `BACKLOG_MCP_STATE` |
| Defaults | `/etc/backlog-mcp/config.toml`, `$XDG_CONFIG_HOME/backlog-mcp/config.toml`, `./.backlog-mcp.toml` | `--config` |

Every global option, and every option of `backlog mcp`, can come from the command line, from
`BACKLOG_MCP_*` in the environment, or from a TOML config file, in that order. The server options
form the `[mcp]` table of the file. The options of the other commands are command-line only.

`--generate-config` writes the effective options as a config file. Options given before the flag
are written active, the rest as commented-out defaults; on `backlog mcp` the flag sees the server
options typed before it. A password given before the flag lands in the file, so keep that file
private.

```bash
backlog --generate-config -                        # print the template
backlog mcp --protocol=http --http-port=3009 --generate-config ~/.config/backlog-mcp/config.toml
```

```toml
computer = "laptop"

[mcp]
protocol = "http"
http_listen = "127.0.0.1"
http_port = 3009
```

## The library

The CLI and the MCP server are one object. `Backlog` answers which project a call works on:

<!-- name: test_readme_library -->
```python
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from backlog_mcp import Backlog, Priority, State, Status, Store


async def main(directory: Path) -> None:
    backlog = Backlog(
        Store(directory / "backlog.sqlite3"),
        State(directory / "state.json"),
        cwd=directory,
    )
    await backlog.store.open()

    project, origin = await backlog.current()
    assert origin.value == "created"      # the directory was not registered yet
    assert project.roots[0].root == str(directory)

    task = await backlog.store.add_task(
        project_id=project.id,
        title="Write the release notes",
        body="Mention the HTTP transport.",
        priority=Priority.HIGH,
        tags=["docs"],
    )
    assert task.status is Status.TODO

    found = await backlog.store.search("release", project_id=project.id)
    assert [item.id for item in found] == [task.id]

    saved = await backlog.store.add_note(
        project_id=project.id, title="Release coordination", body="Contact Anna first", tags=["people"]
    )
    noted = await backlog.store.append_note(saved.id, "Give one day of notice")
    assert noted is not None and noted.body.endswith("Give one day of notice")
    assert [item.id for item in await backlog.store.search_notes("notice", project_id=project.id)] == [
        saved.id
    ]

    closed = await backlog.store.update_task(task.id, status=Status.DONE)
    assert closed is not None
    assert closed.closed_at is not None    # closing stamps the time

    await backlog.store.close()


with TemporaryDirectory() as name:
    asyncio.run(main(Path(name).resolve()))
```

## Development

```bash
make sync      # install every group
make           # lint, types, tests
make coverage
```

## License

MIT
