# Tasks

## Task body and comments

`task.body` is the agreed task definition: goal, scope, requirements, constraints,
and acceptance criteria. It is not a work log.

Use `task_comment_add` for progress, review feedback, findings, research results,
hypotheses, questions, suggestions, blockers, and verification. Proposed requirement
changes also belong in comments until the user explicitly agrees to them.
New information alone does not authorize a body edit.

Use `task_update(body=...)` only when the user explicitly requests a definition change,
or explicitly agrees to requirements that make the current body incorrect or outdated.
The supplied body replaces the entire description; an empty string clears it.
For every body edit, add a separate comment explaining the reason and the user request
or agreement that authorized it. Do not put review feedback, progress, or unapproved
requirements into body edits.

For example, a review that finds a missing requirement produces a comment proposing
that requirement. Update the body only after the user agrees, then comment on why it changed.

## Status and relationships

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
