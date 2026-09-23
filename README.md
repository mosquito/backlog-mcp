# backlog-mcp

A project backlog for the terminal, and the same backlog over MCP.

Tasks belong to projects, and each project can have directory bindings on several computers.
After registration, the computer and working directory select the board for each command.
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

CLI data operations always use MCP. With no endpoint configured, each command starts
a local stdio server and stops it when the command finishes. The local server uses
the configured database, computer, and working directory.

To use a running HTTP server:

```bash
backlog --url https://backlog.example/mcp --project alice/my-project task list
```

Set `url` in the global section of the TOML configuration, or set `BACKLOG_MCP_URL`,
to use that endpoint for later commands. `--user` and `--password` supply HTTP Basic
authentication; their environment variables are `BACKLOG_MCP_USER` and
`BACKLOG_MCP_PASSWORD`. `--timeout` sets the request timeout in seconds (default: 120).
For JWT authentication, use `--token` or `BACKLOG_MCP_TOKEN` instead of Basic credentials.
The CLI sends `Authorization: Bearer <token>`. An endpoint URL with `?token=<JWT>`
also works. Basic credentials and JWT tokens are mutually exclusive.
An unreachable configured server produces an error; it never redirects writes to
the local database. `--url ''` explicitly selects the local stdio server.

Over HTTP, `--computer` overrides the URL's `computer`; otherwise the CLI uses the
URL's computer or the local computer name. `--project` overrides the URL's `project`.
Without either project setting, scoped commands select the deepest registered root
containing the local working directory on that computer. If none exists, they ask
for registration details before creating a project or task. This also applies to
local stdio connections. Broad listings and `skills` do not register a checkout.
The server's working directory never determines the CLI's local checkout.

```bash
cd ~/dev/my-project        # first use prompts for project registration
backlog task add Write the release notes -t docs
backlog task create             # the same, in $EDITOR
backlog task list
backlog search notes       # projects, tasks, comments, and memory
backlog task show 1
backlog task start 1            # doing
backlog task block 1            # blocked
backlog task close 1            # done
backlog task reopen 1           # todo again
backlog stats              # open tasks, closed tasks, and notes
```

Commands are grouped by domain. Global connection and project options come before the group.
Command aliases are shown in `--help`.

| Group | Commands | Alias |
| --- | --- | --- |
| `task` | `add`, `create`, `list`, `show`, `edit`, `link`, `unlink`, `comment`, `comments`, `start`, `block`, `close`, `cancel`, `reopen`, `move`, `delete` | `ta` |
| `memory` | `add`, `append`, `list`, `show`, `edit`, `delete` | `mm` |
| `projects` | list by default; `update`, `delete` | `pr` |

`whoami`, `search`, `stats`, `board`, `init`, `watch`, `skills`, `install`, and `mcp` remain top-level commands.
For example, `backlog ta sh 1` shows a task and
`backlog mm na Convention -b "Use English"` saves a memory entry.

`backlog task edit <id>` edits the task fields and description. Routine progress belongs
in a separate comment:

```bash
backlog task comment 1 "Implemented the parser; checking error handling."
backlog task comments 1
backlog search parser --kind comments
backlog search parser --kind tasks --kind comments
```

`backlog task show <id>` displays the description and all comments as separate Markdown
blocks. Dates and entry types appear outside the comment text. `backlog task comments <id>`
shows the complete history without the task description.

Over MCP, `task_comment_add(task_id, body)` adds a comment. `task_update(body=...)`
replaces the description; an empty string clears it. `task_get` returns all comments
in a separate `comments` list. `task_comment_list(task_id)` returns the complete history
on its own. Entries sort by creation time, then id. Status transitions
and project moves create history entries without changing the description.

Comments and automatic history entries record the writer's `principal` as `user@computer`.
Projects, tasks and notes return `created_by` and `updated_by` as `user@computer` strings.

Task links express dependencies, decomposition, and provenance:

```bash
backlog task link 42 depends_on 38 --reason "Needs the new schema"
backlog task show 38
backlog task unlink 38 blocks 42
```

Over MCP, use `task_link(task, relation, target, reason=None)` and
`task_unlink(task, relation, target)`. `relation` is an enum:

| Relation | Inverse | Meaning |
| --- | --- | --- |
| `part_of` | `contains` | The source task is part of the target task. |
| `depends_on` | `blocks` | The source needs the target's result. |
| `caused_by` | `causes` | The source task arose from the target task. |
| `related_to` | `related_to` | The tasks are related without a dependency. |

Both directions are stored and changed in one transaction. Repeating a call from either
direction does not create duplicates. Omit `reason` or pass null to preserve the explanation;
an empty string clears it on both records. Different relation types between the same tasks
are independent. Self-links and cycles in `part_of` or `depends_on` are rejected.

`task_get` and the task resource return direct links in `links`, with `task`, `project`,
`relation`, `title`, `status`, and `reason`. Relations are relative to the requested task.
Titles and statuses reflect the current target task. Links are not expanded recursively.
There is no separate `task_links` tool. The CLI shows links in the task panel.

Link changes require editor access to both tasks, including across projects. Reads omit
links to inaccessible tasks. Closing, moving, or soft-deleting a task preserves its links.
Links do not change task statuses. Physical project deletion removes links to its tasks.

Task assignment coordinates agents that share a principal or work on different computers.
Use `task_claim(task, expected, assigned)` before starting work. Both `expected` and `assigned`
are required nullable arguments. The server compares the current assignment with `expected`
and replaces it atomically. A mismatch returns an error with the current value, even if it
already equals `assigned`. Only one concurrent claim with `expected=null` succeeds.

An assignment has the form `username@computer:marker`. Copy the principal from `whoami` and
append a marker with 1–64 ASCII letters, digits, underscores or hyphens. Use a distinct marker
for each agent run and keep it across reconnects. Both strings are validated. A non-null
`assigned` must belong to the requesting principal, including its computer. Editor access is
required. An editor can take over another assignment by supplying its exact current value.

```bash
backlog task claim 42 --expected null --assigned 'alice@laptop:agent-k7Q2m'
backlog task claim 42 --expected 'alice@laptop:agent-k7Q2m' --assigned null
```

`task_get`, task lists, search results and task resources expose `assigned`. Assignment changes
create a history comment and notify subscribers. Status changes are independent: closing a
task preserves its assignment. Release it explicitly with `assigned=null`. There is no timeout
or MCP session dependency. This coordinates cooperating agents; it does not lock their files.

Comment responses include `task_assigned`, the task's current assignment. This applies to
adding comments, reading history and searching comments, so agents can detect reassignment
in the response to a progress update.

Use `task_assigned(prefix=null)` to list your assignments across accessible projects, including
closed tasks. Omit `prefix` for the same result. Pass `username@computer:` to select another
principal, or append a marker prefix to narrow the list. Prefixes are literal and case-sensitive;
`%` and `_` are not wildcards. An empty prefix selects all accessible assignments. `limit` and
`offset` paginate the results; `count` is the total. `whoami.assigned_tasks` counts your assignments
with the same access rules. In the CLI, use `backlog task assigned [--prefix PREFIX]`.

Search accepts `kinds=["tasks", "comments", "memory", "projects"]` by default. Select any subset to
search task descriptions, comment text, memory, or project names and descriptions. Each kind has its own result list;
comment results include their id, task_id, project, snippet, and match score.

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

`stats` reports `open` (todo, doing, blocked), `closed` (done, cancelled), and `memory`,
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
appears once per record kind. The limit applies separately to each selected content kind after merging.

Project membership and scope filter candidates before ranking and limits in both searches.
Project results include their owner-qualified `full_name`; archived projects are excluded.
Status filters apply to tasks and their comments. Tags apply to tasks, comments, and memory.
Shared memory remains visible across projects only when the caller can access its source project.
Use `backlog search orchestration --kind projects --all-computers` to find accessible projects
without selecting a current project. `min_score` (`--min-score` in the terminal) defaults to `0.15`
and filters semantic matches only; it never removes FTS matches. When FTS finds anything,
additional semantic matches need a score of at least `0.25`. MCP results include `match`
(`fts` or `semantic`) and a cosine `score` for semantic candidates (`null` for FTS-only
matches). FTS excerpts remain in `snippet`. Semantic matches can use different words or
languages from the query.

The bundled Model2Vec model is
[`sentence-transformers/static-similarity-mrl-multilingual-v1`](https://huggingface.co/sentence-transformers/static-similarity-mrl-multilingual-v1),
truncated to 512 dimensions with MRL and quantized to int8. It is already a static model;
no transformer distillation is required. Inference uses `asyncio.to_thread()` and runs
offline. The source model was trained for similarity, so retrieval quality can vary.

Model files live in `src/backlog_mcp/model/`; weights use Git LFS. Run `git lfs pull`
after cloning. To reproduce the conversion from the pinned Hugging Face revision, run
`uv run python scripts/prepare_model.py`. The directory includes provenance, model license
(Apache-2.0), and the Model2Vec library citation (MIT).

Each running process keeps its embedding index in memory, built on the first search.
Writers queue changed records after commit. A background worker applies the queue once per
second; searches use the last completed index while it updates. If SQLite `data_version`
changes, the worker discards the old queue and rereads the database. Unchanged text retains
its vector. New writes during a rebuild stay queued for the next pass. Search results are
filtered by current access and read from SQLite by ID. Closing the database stops the worker.

The `backlog-mcp/model2vec` extension also exposes `model2vec/encode` for normalized text
embeddings. It uses the same local model. Ordinary `search` works on every supported
MCP revision.

### Projects

On first use, the CLI asks for registration fields: computer, directory, project
name, and optional description. Each prompt accepts Enter to keep its default.
The directory defaults to the nearest ancestor holding `.git`, `.hg`, `.svn` or
`.jj`, else the working directory itself. The project name defaults to the selected
directory's name. `--computer` and `--root` set the initial values.

After you change the computer or directory, the CLI checks that location for an
existing binding. If one exists, it offers to use that project. The pending command
runs in the selected location. A changed computer name becomes the local default
after successful registration; an explicit flag, configuration value, or URL computer
still overrides it. Later commands resolve the directory from their own working
directory or `--root`.

If the name or slug matches an existing project, including one on another computer,
the CLI asks whether to bind this directory to that project. It preserves that
project's description, tasks, and notes. Otherwise it asks to confirm a new project.
Cancellation or end of input leaves the project and requested task uncreated.
Later commands reuse the binding without prompting.

Without a terminal, a missing binding is an error. Register explicitly before running
scripts. Naming a project creates it; `--existing` explicitly reuses a known project:

```bash
backlog init                         # interactive registration
backlog init "My project" --description "Release coordination"
backlog init --existing my-project   # add this computer and directory to the same board
```

An existing name makes `init NAME` fail with instructions to use `--existing`.
It does not silently create a second board with a suffixed slug.

Each project has an optional `preferred_language`, which defaults to `null`.
Use this language for project work, including task titles, descriptions, and progress
updates. `null` means no language preference. The value is text, such as `ru` or `en`.
Changing it does not translate existing tasks.

Set or clear the preference from the CLI:

```bash
backlog projects update my-project --preferred-language ru
backlog projects update my-project --clear-preferred-language
```

The MCP tool `project_save` accepts `preferred_language` when creating or updating a
project. Omit the field to keep its current value; send `null` to clear it.
`project_board` and `project_list` return the preference. The CLI shows it in
`backlog board` and `backlog projects`.

```bash
backlog board            # project context, counts and ready and claimed tasks
backlog projects           # every registered project
backlog task move 1 other-project
```

`--project` goes before the subcommand and applies to every command:

```bash
backlog --project other-project task list
backlog projects delete other-project  # every task and note on that board goes with it
```

### Computers

A logical project can bind directories on several computers. One database can hold
all these boards, accessed through a shared HTTP server or a local stdio server.
The computer name comes from `computer = "..."` in the config file or
`--computer`; otherwise it is the short host name. Set `computer` in the config to keep a
stable name when the host is renamed.

```bash
backlog board                    # project context and registered roots
backlog projects                   # the projects of this computer
backlog projects --all-computers   # every project, with a column for the other computers
backlog task list --computer laptop     # open tasks of every project on the laptop
backlog search deploy --all-computers
backlog task move 3 laptop-app          # a task moves between computers like between projects
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
user:password>` for Basic or `token=<JWT>` for JWT. The header wins when both are present,
including when the header is invalid. Without a connection project, remote clients pass
`project=<slug>` to project-scoped tools. Nothing is pinned or remembered between clients.
`project_board(project=...)` always requires an explicit project and never creates one.
It returns the project description, language, role and roots; task counts; accessible memory
count; and two sections, `ready` and `claimed`. Each section has a full `count` and
up to ten `items`, each containing only `id` and `title`, sorted by priority, creation time and id.
`ready` contains unassigned `todo/doing` tasks whose direct `depends_on` targets are closed
(`done/cancelled/deleted`). Inaccessible dependencies also prevent readiness.
`claimed` contains open tasks assigned to the current `username@computer:`, across all markers.
The `tasks` counters cover the whole project: total, open, claimed, unclaimed, blocked,
done, cancelled and deleted. Blocked counts each open task once if its status is blocked
or a dependency prevents readiness. It can overlap claimed. The total includes deleted tasks.
The memory count includes accessible shared memory. Read a task and use `task_claim`
before starting it: the board is a snapshot, not a reservation.
`backlog board [project]` shows the same summary; omitting the project uses the CLI checkout
or `--project` selection. `project_save` with an explicit `root`
checks that the directory exists on this computer; for another computer it takes the path as
given. A user may name any computer within their own data. Computer selection never
switches the authenticated user or exposes another user's projects.

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
backlog memory add release coordination -b "Ask Anna before scheduling a customer rollout." -t people
backlog memory append 1 "Anna prefers one day of advance notice."
backlog memory list                      # this project plus every shared note
backlog memory show 1
backlog memory edit 1                    # the same form, in $EDITOR
backlog search release coordination    # relevant tasks and past agreements
backlog memory add communication --shared # visible from every project
```

## Serve it to an agent

```bash
backlog mcp                                        # stdio, what a client starts itself
backlog mcp --protocol=http --http-port=3009       # endpoint /mcp, console /
backlog-mcp --protocol=http --auth-basic=alice:secret --auth-basic=bob:another-secret
```

`backlog-mcp` is `backlog mcp` without the word. A config file holds these options in its
`[mcp]` table and nested groups. Environment variables use `BACKLOG_MCP_HTTP_PORT` and
related names. `--config FILE` is accepted before and after `mcp`. Over HTTP the endpoint
lives at `/mcp` and the bundled console at `/console`. Configure Basic credentials or a JWT
verification key to protect MCP requests. The console HTML, JS, and CSS are public;
they contain no backlog data. Without authentication, bind to `127.0.0.1`.
The endpoint refuses a browser
`Origin` other than its own, against DNS rebinding; behind a TLS-terminating reverse proxy the
scheme it sees differs from the public one, so pass `--http-trust-proxy` and let the proxy
validate origins. One HTTP server can answer for several computers; see [Computers](#computers).

Server options use the same group structure in each configuration source:

| CLI flag | TOML table and key | Environment variable |
| --- | --- | --- |
| `--http-port` | `[mcp.http] port` | `BACKLOG_MCP_HTTP_PORT` |
| `--auth-basic` | `[mcp.auth] basic` | `BACKLOG_MCP_AUTH_BASIC` |
| `--auth-jwt-public-key` | `[mcp.auth.jwt] public_key` | `BACKLOG_MCP_AUTH_JWT_PUBLIC_KEY` |

Move flat HTTP settings to `[mcp.http]`, Basic accounts to `[mcp.auth]`, and JWT
settings to `[mcp.auth.jwt]`. Replace `--basic` with `--auth-basic` and
`--http-jwt-*` with `--auth-jwt-*` in existing launch commands.

HTTP authentication uses the
[aiohttp-tiny-mcp policies](https://github.com/mosquito/aiohttp-tiny-mcp/blob/master/docs/guide/auth.md).
The dependency `aiohttp-tiny-mcp[jwt]~=0.5.0` supplies Basic authentication and
asynchronous JWT verifiers. Backlog adds header/query credential handling and isolates
stored data and notifications by authenticated identity.
Basic and JWT policies accept the existing header and query credentials. Basic rejection
responses include `charset="UTF-8"`. These policies do not publish OAuth metadata.
Each HTTP application has its own authentication policy and session storage; stdio stays
independent. Each authenticated identity sees only its own projects, tasks, comments,
notes, search results and notifications. Shared notes and `all_computers` remain inside
that user's data. Record IDs are local to each user's database.

`--auth-basic` accepts several `username:password` values or repeated flags. Passwords may
contain colons; usernames must be unique. The `[mcp.auth]` key `basic` is an array:

```toml
[mcp]
protocol = "http"

[mcp.auth]
basic = ["alice:secret", "bob:another-secret"]
```

Use `[mcp.http]` for HTTP settings and `[mcp.auth]` for credentials.
`BACKLOG_MCP_AUTH_BASIC` accepts a shell-quoted list, such as
`alice:secret 'bob:password with spaces'`. CLI values extend the configured account list.

All principals use one SQLite file. It holds projects, memberships, tasks, comments,
notes, MCP sessions and notification events. New projects belong to their creator.
Account order does not grant ownership. A Basic username and a verified JWT `sub` with the
same value identify one account and share all project data. Principal output uses `user@computer`; stored account identities remain usernames.
Local stdio and HTTP without authorizers use `--default-user`, falling back to `$USER`.
The option defaults to `None`. HTTP rejects an explicit default user combined with any Basic
or JWT authorizer; the `$USER` fallback is resolved only when no authorizer applies.

Use `project_share(project="example", principal="bob", role="reader")` to give a configured
Basic user read access. Use `role="editor"` for task, comment, note and project edits.
Only the owner can share or delete a project. `project_members(project="example")`
lists its members. JWT principals register on first authenticated use; pass their exact
identity from `whoami` when sharing. Basic users register at server startup.

Project roots belong to a principal and computer. Use `project_list(all_computers=true)`
to discover shared projects, then register your own root with `project_save`.
Projects retain a short `slug` and expose `full_name` in `{owner}/{slug}` form: `alice/foo`, `bob/foo`.
Use that name for tools, CLI `--project`, and notification filters. Short slugs work only
when one accessible project matches. `project="#<id>"` also selects a stable ID.
Owner names use percent encoding for special characters, independent of authentication type.
Prefix relative filesystem paths with `./` to distinguish them from project names.

Lists, direct IDs, resources, FTS5 and vector search only expose accessible projects.
Search filters candidates before ranking and limits. A shared note remains limited to
members of its source project. Notifications go to project members; sessions stay
principal-scoped. Existing streams and stored event replay have no revocation machinery.

The server applies numbered SQL migrations at startup. Legacy 0.1 databases require a separate import.
Import the old installation later with a separate ad hoc script. Back up the SQLite
file with SQLite's backup API when the server is running.

### Browser interface

Open `/` or `/ui/` on the HTTP server. The root redirects to the tracker.
The diagnostic MCP console is at `/console`; `--http-no-console` disables it.
The MCP URL remains `/mcp`, including its existing query parameters.

The tracker supports projects, a kanban board, task details, Markdown editing,
comments, task relations, and search with Cmd+K or Ctrl+K. Drag a task between
columns to change its status. Filters are saved in the page URL. The board loads
up to 500 open tasks and the 50 most recently changed completed tasks. Use Refresh
to reload it; changes from other clients do not appear automatically.

Sign in with Basic credentials or a Bearer token supported by the server.
Credentials stay in `sessionStorage` unless you select Remember me, which uses
`localStorage`. Sign out clears both stores. Requests send credentials only to
the configured MCP endpoint. Public UI assets live at `/ui/static/`; the MCP
subapplication applies authentication independently.

Drafts persist in `localStorage`, scoped by user and task. Before saving edits,
the browser reads the task again and compares its timestamp and editable fields.
If the task changed, the editor displays the base, current, and draft values.
You can keep your changes or merge them manually. This check is not atomic:
another client can write between the comparison and the update. The current API
does not offer server-side compare-and-swap.

The UI uses native JavaScript modules without a build step or npm installation.
Marked and DOMPurify are vendored with their licenses; Markdown is sanitized
before display. Run the client tests with `node --test tests/webui_client.mjs`
using Node.js 22 or newer.

## JWT authentication

Configure one or both verification methods in `[mcp.auth.jwt]`. `public_key` accepts a PEM
public key or its file path. `pre_shared_key` accepts an HS256 shared secret of at least
32 bytes. Basic, JWT-HMAC, and public-key JWT are independent server policies;
enable any combination by supplying their credentials or keys. Each request must
pass one policy. Each JWT verifier accepts only its configured key family.

```toml
[mcp]
protocol = "http"

[mcp.auth.jwt]
public_key = "/etc/backlog/jwt-public.pem"
issuer = "https://identity.example"
audience = "backlog"
```

To also accept Basic and HS256 on the same endpoint, configure `[mcp.auth].basic` and
`[mcp.auth.jwt].pre_shared_key` alongside `public_key`.
Both JWT policies use the configured issuer and audience checks. The CLI still
sends one authentication scheme per request. Rejected requests advertise the
configured schemes in `WWW-Authenticate`.

The corresponding flags are `--auth-jwt-public-key`, `--auth-jwt-pre-shared-key`,
`--auth-jwt-issuer`, and `--auth-jwt-audience`. Environment variables use the
`BACKLOG_MCP_` prefix, for example `BACKLOG_MCP_AUTH_JWT_PRE_SHARED_KEY`.
`issuer` and `audience` checks are optional; omit their settings to disable those checks.

The configured key determines the accepted algorithms:

| Key | Algorithms |
| --- | --- |
| Pre-shared key | HS256 |
| RSA public key, at least 2048 bits | RS256/384/512, PS256/384/512 |
| EC public key, P-256 / P-384 / P-521 | ES256 / ES384 / ES512, respectively |
| Ed25519 or Ed448 public key | EdDSA |

Token headers cannot enable another algorithm family. Every JWT must have a valid `exp`.
The server also checks `nbf` and `iat` when present. Invalid keys stop startup;
invalid, missing, or expired credentials receive HTTP 401, including on existing sessions.

Session ownership uses the verified `iss` and `sub`. Renewed tokens with the same identity
can reuse a session; another identity receives HTTP 404. Tokens without a non-empty `sub`
are rejected; `client_id` and `azp` do not identify user accounts. Account data is selected by
`sub`, matching Basic usernames, after the configured verifier accepts the token.
The verifier maps `scope` (a space-separated string) or `scp` (a list) to the principal.
Scope claims do not grant access to another user's projects or computers.

```bash
backlog --url https://backlog.example/mcp --token "$JWT" list
backlog --url "https://backlog.example/mcp?token=$JWT" list
```

The `Authorization` header takes precedence over query credentials. Prefer the header
when available: URLs can appear in browser history and proxy logs. JWT authenticates
access to the whole backlog; token claims do not restrict projects or computers.
The server verifies externally issued tokens; it does not issue or refresh them.
To use the browser console, open `/`, set its **Endpoint** field to `/mcp?token=<JWT>`,
and click **Connect**. Add `&computer=laptop&project=app` to select a scope.
The console preserves the query string on MCP requests and remembers the endpoint
in browser local storage. Its static files load without credentials; MCP requests
require valid credentials, including after a session has been established.

The agent gets tools for identity (`whoami`), project access (`project_share`, `project_members`),
projects (`project_board`, `project_list`, `project_save`, `project_delete`), and content:
`task_add`, `task_get`, `task_list`, `task_update`, `task_comment_add`, `task_comment_list`,
`task_move`, `task_delete`, `task_link`, `task_unlink`, `memory_save`,
`memory_append`, `memory_get`, `memory_list`, `memory_delete`, `search`, `stats`. They use the same
boards, and the same database. `search` is the one search: it answers with the tasks, comments, and
notes that match, so a new kind of record later is one more list in the result, not a new
tool. The resources `backlog://project`, `backlog://projects`, `backlog://tasks`,
`backlog://tasks/{id}`, `backlog://memory` and `backlog://memory/{id}` read the same data without a
tool call, and a write publishes `notifications/resources/updated` for the ones it touched. Each
such notification carries `_meta["backlog/event"]`, what the write did: `kind` (task, note or
project), `action` (added, updated, moved or deleted), `id`, `project`, `title`, for tasks `status`
and `priority`, and the writer's `principal` (`user@computer`). A subscriber learns what happened without reading
the resource again.

`task_delete` and `memory_delete` delete immediately within the current project. Deleting
a record owned by another project asks the user to confirm, including shared notes; a client
that cannot ask is refused. Terminal commands follow the same rule; `--yes` confirms deletion
from another project. `project_delete` removes a project with every task and note on its board
and frees its directories. It is destructive and irreversible, so the server tells agents to
call it only when the user asked for exactly that, and it always asks the user to confirm.
Nothing stands in for that answer: a client without elicitation is refused. `backlog projects
delete <slug>` does the same and asks first unless `--yes` is given. Archive with
`project_save` when the history should stay.

The MCP initialize response includes the tracker and memory workflow in `instructions`: check
project registration, search context, track work in tasks, save lasting agreements, and report
outages. It uses `memory_*` tools and `task_comment_add` for progress history. The client decides
whether to include these instructions in the model context.

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

### Live updates

Every write announces itself. A client that subscribes to a resource by URI gets
`notifications/resources/updated` with the event in `_meta["backlog/event"]`, as above. A client
that wants the events without subscribing to resources asks for the notifications of the
`backlog-mcp/events` extension: `notifications/backlog/task` and `notifications/backlog/memory`,
multicast by project slug, and `notifications/backlog/project`, sent to every listener. On
2026-07-28 the client names them in `subscriptions/listen`, with `{"method": ..., "topics":
["<slug>"]}` to keep one project; a `GET` stream of an older revision gets every event of every
project. The params of each notification are the event: `kind`, `action`, `id`, `project`,
`title`, for tasks `status` and `priority`, and `principal` (`user@computer`).

```
backlog watch https://host/mcp?computer=laptop -u user --password secret
backlog --project backlog-mcp watch http://127.0.0.1:3009/mcp
backlog watch              # use the configured endpoint
```

`backlog watch` prints one line per event until interrupted, `-n 5` stops after five. Agents
do not see these notifications: Claude Code and Codex do not forward them into the
conversation, so an agent still calls `task_list` or `search`. The notifications serve this
command, the console, dashboards and bots.

Use a shared HTTP server to watch changes from other clients. Separate local
stdio servers do not share event subscriptions. The request timeout does not
limit the duration of a watch subscription.

## Where the data lives

| What | Path | Override |
| --- | --- | --- |
| Projects, tasks, comments, and notes | `$XDG_DATA_HOME/backlog-mcp/backlog.sqlite3` | `--database`, `BACKLOG_MCP_DATABASE` |
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

[mcp.http]
listen = "127.0.0.1"
port = 3009
```

## The library

`Backlog` holds project scope and the database lifetime. Tools receive the shared
`aiosqlite.Connection` and `Transaction` through dependency injection. A transaction
uses `async with transaction:` and commits on success or rolls back on failure.
The same instance serializes writers on that connection. Transactions must not nest.
SQL remains in `queries.sql`; there is no `Store` interface.

Python applications can call tools with these dependencies explicitly:

<!-- name: test_readme_library -->
```python
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from backlog_mcp import Backlog, Priority, Status, use
from backlog_mcp import server


async def main(directory: Path) -> None:
    backlog = Backlog(
        directory / "backlog.sqlite3",
        cwd=directory,
    )
    use(backlog)
    connection = await backlog.open()
    try:
        project, origin = await backlog.current()
        assert origin.value == "created"
        assert project.roots[0].root == str(directory)

        task = await server.task_add(
            server.AddTask(
                project=project.full_name,
                title="Write the release notes",
                priority=Priority.HIGH,
                tags=["docs"],
            ),
            backlog,
            connection,
            backlog.transaction,
        )
        assert task.status is Status.TODO

        closed = await server.task_update(
            server.UpdateTask(id=task.id, status=Status.DONE),
            backlog,
            connection,
            backlog.transaction,
        )
        assert closed.closed_at is not None
    finally:
        await backlog.close()


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

## Database migrations

At startup, the migrator uses the existing aiosqlite connection to apply `src/backlog_mcp/migrations/*.sql` in numeric
order. Filenames use four digits and a topic, for example `0001-initial.sql`.
The `migrations` table records each applied filename, its SHA-256, and the application time.
Pending migrations are the set difference of `(path, SHA-256)` pairs, sorted by path.
Applied files are not run again. A changed checksum or missing applied file stops startup.
Add a new numbered file for each subsequent schema change.

INFO logs report the pending count, including `0 migrations needed`, and each file being
applied. Success is logged only after commit. Failures and transaction rollback are logged.

`0001-initial.sql` creates the complete schema with `IF NOT EXISTS`. It also registers an
existing 0.2 database without replacing its records. No manual task-links upgrade is needed.
The SQL files contain no transaction commands: the migrator applies pending statements and
records their filenames and checksums in one transaction. An error rolls back the whole pending batch.
The SQLite writer lock serializes concurrent startup attempts. Migration uses the same connection as the store, including for in-memory databases.

## Upgrade from 0.1 to 0.2

Version 0.2 uses a different schema from 0.1. Import 0.1 data separately; subsequent schema
changes use the automatic migrations described above.
Tool names changed from `note_*` to `memory_*`. Memory resources use `backlog://memory`,
and memory notifications use `notifications/backlog/memory`. Search results and statistics
use the `memory` field. Task CLI actions now live under `backlog task`; notes use `backlog memory`.
Old tool and command names are not aliases.

Import a single-user 0.1 installation from the 0.2 checkout:

```bash
uv run python scripts/migrate_db_from_0.1.py old.sqlite3 new.sqlite3 --user alice --computer old-host
```

The destination must not exist. The script opens the source read-only and publishes a new
file only after SQLite, foreign-key, and FTS integrity checks pass. Stop the old server before
importing the final copy so later writes cannot be missed.

All imported projects belong to `--user` (alias `--default-user`), defaulting to `$USER`.
For `--user alice`, project names become `alice/<project>`.
IDs, timestamps, tags, statuses, memory, and computer/root bindings are preserved.
Existing comments retain their IDs. Legacy `---` / `# UPDATE:` blocks become separate comments
with their original text and UTC timestamps. Code-fenced examples stay in descriptions.
Malformed or ambiguous histories remain intact, and the script reports their task IDs.
Historical actors use the specified owner and `--computer`
(default: `unknown`); this is import attribution, not recovered authorship.
Sessions and authentication state are not imported. Configure the same Basic username on the
new server and reconnect clients. Keep the source file until you have checked the imported data.

`backlog whoami` (alias `wi`) reports `principal` as `user@computer`, `auth_type` (`basic`, `jwt`,
or `none`), project-owner `handle`, and `principal` (`user@computer`). The MCP tool is also named `whoami`. It does not require a current
project or register a directory. Local stdio uses `--default-user`, defaulting to `$USER`,
and reports the same principal as a Basic account with that username. Set it with `backlog --default-user alice whoami`
or `backlog mcp --default-user alice`; `backlog-mcp --default-user alice` also works.
An explicit server option overrides the global option. HTTP reports the verified Basic or JWT
principal. HTTP without any Basic or JWT authorizer uses the same `--default-user` / `$USER`
principal as stdio. All unauthenticated clients share that identity. It has ordinary project
membership permissions,
not administrator access to other principals’ private projects. The computer part records the client
context and does not create a separate account. Use the returned principal when sharing projects.

For example, Basic and JWT requests for the same user both return `principal: "mosquito@localhost"` on that computer.
Only `auth_type` differs. Stdio and unauthenticated HTTP return `auth_type: "none"`.
Use `project_share(project="mosquito/example", principal="alice")` to grant access by username.

Tool and resource output uses `user@computer` for principals and `user@computer:path` for project
roots. Bindings without a filesystem path are omitted. There are no separate computer fields
in output. Owners and members with no computer context use `user@unknown`. Separator characters
in usernames and computer names are percent-encoded. `project_share` accepts the displayed
principal; its computer does not change the target account. Plain usernames without `@` also work.
`created_at` and `updated_at` are rounded to integer Unix seconds in JSON output. Storage keeps
full timestamp precision for ordering.
