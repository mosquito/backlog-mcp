# Workflow

1. Call `project_board(project=<absolute local path>)`. Verify principal and roots;
   then use the returned owner/slug. Leave correct bindings unchanged.
2. First call `memory_list(project=<full_name>)` for project memory.
   Keep the compact result; read relevant entries with `memory_get`.
   Use topic-specific `search` for related tasks and agreements.
   Include project-shared entries; do not load unrelated project memory. Verify technical facts in code.
3. For work spanning multiple steps or sessions, reuse or create a task.
   Read it with `task_get`, then claim through `task_claim` before starting.
4. Set doing. Use `task_comment_add` for progress, review, findings, checks, questions,
   and proposed requirement changes. The task body is the agreed definition.
   Edit it only on an explicit user request or to reflect explicitly agreed requirements
   that make it incorrect or outdated. Add a separate comment explaining every body edit.
   New findings alone do not authorize changes to the body.
5. Verify the outcome, update obsolete memory, close the task, and release
   through `task_claim(expected=<your claim>, assigned=null)`.

Follow preferred_language. A brief answer needs no task. Keep tracking in backlog,
not files. On outages, report the failure, continue independent authorized work,
and keep pending updates in the conversation. Never claim an unconfirmed write.
