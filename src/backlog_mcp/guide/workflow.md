# Workflow

1. Call `project_board(project=<absolute local path>)`. Verify principal and roots;
   then use the returned owner/slug. Leave correct bindings unchanged.
2. Read `memory_list(project=..., full=true)` before project work.
   Search for related tasks and agreements. Verify technical facts in code.
3. For work spanning multiple steps or sessions, reuse or create a task.
   Read it with `task_get`, then claim through `task_claim` before starting.
4. Set doing. Record findings and checks with `task_comment_add`.
5. Verify the outcome, update obsolete memory, close the task, and release
   through `task_claim(expected=<your claim>, assigned=null)`.

Follow preferred_language. A brief answer needs no task. Keep tracking in backlog,
not files. On outages, report the failure, continue independent authorized work,
and keep pending updates in the conversation. Never claim an unconfirmed write.
