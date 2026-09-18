# Conventions for tasks on this backlog

## Titles

One line, imperative, and specific enough to act on without the body: "Retry failed uploads
three times", not "Uploads". Name the thing the way the code names it. Keep it under about
70 characters, because listings show titles in a column.

## Bodies

The body holds what the title cannot: the reason, the constraint, the failing input, the
decision that was already made. Paste the error text if there is one. A body of three useful
lines beats a body of three paragraphs.

Write the body for whoever picks the task up cold, in a month.

## Statuses

- `todo`: agreed, not started.
- `doing`: being worked on now. Keep one per person.
- `blocked`: cannot proceed. The body must say on what.
- `done`: finished. The closing time is stamped for you.
- `cancelled`: decided against. The body must say why.

`todo`, `doing` and `blocked` are the open statuses, and are what listings show by default.

## Priorities

- `urgent`: work stops or users are affected until this is done.
- `high`: the next thing to pick up once the urgent tasks are clear.
- `normal`: agreed work with no date on it. Most tasks are normal.
- `low`: worth doing if it is ever convenient. A parking place, not a promise.

## Tags

Lowercase, single words, and reused rather than invented: `bug`, `docs`, `tests`, `perf`,
`refactor`, plus one for the area of the code (`cli`, `http`, `store`). Two or three tags are
enough. Call `stats` to see which tags a project already uses before you add a new one.

## One task, one outcome

If a task needs the word "and", it is two tasks. If it cannot be finished in a sitting, split
it, and keep a parent task only when it carries a decision the parts do not.
