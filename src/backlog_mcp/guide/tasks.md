# Tasks

Use todo for planned work, doing for active work, done for verified outcomes,
cancelled for abandoned work, and blocked for external blockers explained in a comment.
Use `task_link(task, relation, target)` for relationships:
- part_of: task belongs to a parent; inverse contains.
- depends_on: task waits for a prerequisite; inverse blocks.
- caused_by: task results from another task; inverse causes.
- related_to: symmetric association.

Dependencies belong in links, not blocked status or prose. Board ready tasks are
unassigned open tasks whose direct dependencies are closed.

Assignments are advisory. Any editor can take over through CAS:
`task_claim(task, expected=<observed value>, assigned=<principal>:<marker>)`.
Both nullable values are required. On conflict, reread before retrying.
Closing does not release assignment. Use assigned=null to release.
Temporary UI edit claims protect writes until restored; see task_claim parameters.
