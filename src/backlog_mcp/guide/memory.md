# Memory

Read all `global_memory` entries returned by `project_board` before work. They are
mandatory context, returned in full regardless of list pagination or search relevance.
Then call `memory_list(project=<full_name>)` for project memory.
Keep the default compact result; use `memory_get` to read relevant entries in full.
Include your personal entries and entries shared with this project.
Search other projects only when the task needs that context.
Keep it small. Store lasting agreements, preferences and invariants stated by the user.
Update a matching entry by id instead of adding a near-duplicate.

A correction replaces the body through `memory_save` with the current truth.
Do not append historical corrections. A supplied body replaces the whole body,
at most 1024 characters. Before closing a task, update memory it made obsolete.

Put task status, progress, findings and verification in tasks and comments.
Do not store secrets, guesses, code snapshots or documentation summaries.
Use shared=true to share an entry with members of this project.
Personal entries (shared=false) are visible only to the entry author.
If the project, audience or global scope is unclear, ask the user before saving or
changing sharing. Do not guess or use defaults to resolve uncertainty.
Choose by audience: `my dev server` is personal; `team dev servers catalog` is shared
because the team needs it. `global_scope=true` makes an entry available in every project
to the same audience; source-project membership is still required.
Use `memory_sharing(id, shared, global_scope)` to change visibility.
Shared=false does not select a project. Existing entries remain shared within their project.
Delete memory only when the user requests it.
