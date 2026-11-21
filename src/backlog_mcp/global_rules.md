<!-- BACKLOG_START -->
## Backlog workflow

Read `backlog://guide` for the topic index and `backlog://guide/workflow` for the
workflow. Use MCP `resources/read`, or `mcp_resources_read(uri=...)` when the HTTP
connection uses `resources=tool`. Discover URIs with `resources/list` and
`resources/templates/list`, or `mcp_resources_list` in tool mode.
Read `backlog://guide/tasks` for task rules, `backlog://guide/memory` for memory,
`backlog://guide/search` for search syntax, and `backlog://guide/projects` for scope
and registration.

### 1. Check project registration first

The simplest way to find a project is to read its board by absolute local path.
At session start or when changing projects or directories, first call
`project_board(project=<absolute local project path>)`.
Compare the returned principal (`user@computer`) and project roots with the
actual local workspace. For HTTP connections, the server cwd is not the local cwd.
If the binding matches, use the returned `full_name` for subsequent calls.
No `project_list` or `project_save` call is needed for a correct binding.
If the project is not found or the binding is wrong, use `project_list`;
use `all_computers=true` to find an existing logical project on another computer.
Use `whoami` when the authenticated principal needs clarification.
If the local binding is missing or wrong, call
`project_save(project=<existing full_name>, root=<absolute local project root>)`.
Create a project only if no matching logical project exists.
Leave correct bindings unchanged. Do not access paths that belong to another computer.
Complete this check before searching context, managing tasks, or investigating code.
Use owner-qualified names (`owner/slug`) to avoid ambiguous project references.
The board provides project context, counts, ready tasks, and tasks claimed by
this principal on this computer.
Follow `preferred_language` for task titles, descriptions, and progress updates.
A null value means no language preference.
Registration and board reads do not select a project for other calls.
Pass `project=` explicitly on unscoped HTTP connections.

### 2. Search context before decisions

After the registration check, use `search` before investigating a problem,
choosing an approach, creating tasks or memory, or asking about prior agreements.
Use natural-language queries that describe the problem or question.
Search again when a new question arises. Read relevant matches with `task_get`
or `memory_get` when more detail is needed. Check memory before guessing user
preferences or project conventions.
Use targeted search for topics, filtered lists for board reviews, and `stats`
for counts. Search can match task descriptions and comments separately.
Verify current technical facts in code or documentation.
Use available code-search tools to locate implementations.

### 3. Track work through tasks

Use backlog as the persistent task record. Reuse a relevant task or create one
with `task_add` before substantive work, including investigation and verification.
Define one verifiable outcome per task. A brief answer with no separate work
does not need a new task.
Read the task before claiming it; a board result does not reserve it.
Before starting work, call `task_claim(task=<id>, expected=null,
assigned=<principal>:<marker>)` for an unassigned task.
Use the principal from `whoami` and a distinct URL-safe marker per agent run.
Both `expected` and `assigned` are required. If the claim conflicts, do not start work.
Set `doing` when work starts. Use `task_comment_add(task_id=<id>, body=...)`
for progress, review feedback, findings, hypotheses, questions, suggestions, proposed
requirement changes, decisions, checks, blockers, remaining work, and relevant commit
hashes or deployment state. Send only new update text.
Use `task_update` for task fields and status.
The task body is the agreed definition: goal, scope, requirements, constraints,
and acceptance criteria. Do not use it as a work log or for unapproved requirements.
Use `task_update(body=...)` only on an explicit user request to change the definition,
or to reflect explicitly agreed requirements that make the current body incorrect
or outdated. It replaces the entire description; it does not append history.
For every body edit, add a separate comment explaining the reason and the user request
or agreement that authorized it. New information alone does not authorize a body edit.
Read comments with `task_get` or `task_comment_list`.
If scope must be replaced, close the old task and create a successor referencing it.
Set `done` only when the outcome is verified, `blocked` with the blocker and next
step, or `cancelled` when the work is abandoned.
Status changes do not release assignments. When finished or handing off work,
release with `task_claim(task=<id>, expected=<current assignment>, assigned=null)`.
Use `task_assigned` to find existing assignments when needed.
Before ending a turn, leave the task status, assignment, and history accurate.
Keep all task tracking in backlog. Never create or maintain task lists, work
plans, progress logs, or task checklists in files, in any directory or format.
This also applies when backlog is unavailable.

Write task descriptions in Markdown for users reading them in the Web UI.
Use headings, lists, and tables when they improve clarity. Put code in
triple-backtick fenced blocks with a language label for syntax highlighting.
Use fenced blocks labelled `mermaid` when a diagram clarifies a flow or relationship.
The Web UI renders Markdown, code highlighting, and Mermaid diagrams.
Keep explanatory prose so the task also makes sense as plain text through MCP.
Use `#{ID}` for task references and `#note{ID}` for memory notes.

### 4. Keep lasting agreements in memory

Proactively save user preferences, constraints, and decisions with `memory_save`
or `memory_append`. Search first and update an existing record when appropriate.
Use `shared=true` for agreements that apply across accessible projects.
Use task comments for temporary findings, bugs, and implementation progress.
Do not save code or documentation snapshots as lasting memory.
`memory_save(body=...)` replaces the body; `memory_append` adds related context.
Keep each record within 1024 characters. Never store secrets or unconfirmed guesses.
After a user correction, save the lasting agreement that prevents recurrence.

### 5. Handle backlog outages explicitly

If backlog or a required tool is unavailable, report the failure and continue
authorized work that does not require missing context. Use an available CLI
equivalent when it accesses the same server and authenticated principal.
Keep pending task and memory updates in the conversation and save them when
access returns. Do not claim that a write succeeded unless the tool confirmed it.
<!-- BACKLOG_END -->
