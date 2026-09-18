---
name: planning
description: Turn a request into tasks on the backlog: search for relevant knowledge first, one outcome per task, ordered, with user-provided constraints retained when unavailable elsewhere.
---

# Plan work on the backlog

Use this when a request is larger than one task: a feature, a migration, a refactor, a bug
whose cause is not known yet.

## 1. Work on the right board

Call `project_current` to see which project the tools work on. If the work belongs to another
directory tree, pass `project` to each call, or register that directory with `project_save`.
A project name is a slug, such as `backlog-mcp`, or any path inside the project.

## 2. Search for relevant context

Code changes; notes remain. Keep lasting user context unavailable elsewhere: preferences,
working agreements, people and responsibilities, business constraints, and reasons for decisions.
Exclude snapshots of implementation, architecture, configuration, bugs, and documentation.
A useful note should still help after the code is rewritten. Project scope only organizes notes.

Use `search` regularly alongside web search to recall past agreements and user preferences.
Check notes before making decisions or asking the user again. Use code, documentation, and the
web for current technical facts. Search for relevant notes instead of reading them all.
Update outdated context when the user corrects it.

Use `search` frequently: before investigating, choosing an approach, asking the user for project
facts, or creating a task or note. Search again for each new question during planning. Search
combines word prefixes and meaning across languages; try different phrasing when results are weak.

## 3. Find out what already exists

Call `search` with a short question or terms that describe the work. Refine the query or filters
if needed, and use `task_get` for relevant matches that need more detail. Avoid scanning the
whole board to find related tasks. Reserve `task_list` for a requested inventory or complete
board review. Update an existing task when it already covers part of the work.

## 4. Cut the work into tasks

One task is one outcome that one person can finish in a sitting and someone else can verify.
See `references/splitting.md` for where to cut and what to do with the parts that are only
research.

Order the tasks so that each one leaves the code working. A plan whose third step is "make it
compile again" is cut in the wrong place.

## 5. File them

Call `task_add` once per task, with a body that carries the reason and the constraints, and
tags shared with the rest of the plan so the plan can be found again later. Set the priority
of the first task or two, and leave the rest `normal`; a plan where everything is `high` says
nothing about order.

Mention earlier task ids in the body of a task that depends on them. The backlog stores no
dependency links, so the body is where a dependency lives.

## 6. Retain knowledge supplied by the user

Proactively save useful new knowledge from the user that is unavailable elsewhere: constraints,
preferences, corrections, and reasons for decisions. Save it as it arises; do not wait for a
request to remember it or for the session to end. Search first, then update the existing note
or create one short note per topic. Use `note_save` for corrections and `note_append` for related
facts. Omit guesses, duplicates, secrets, and summaries of code, documentation, or the plan.
Link relevant notes from tasks as `note #3`.

## 7. Show the plan

List what you filed, by id and title, in the order you expect to do it. Say which task you
would start with and what it needs. Ask before you start work that changes code.
