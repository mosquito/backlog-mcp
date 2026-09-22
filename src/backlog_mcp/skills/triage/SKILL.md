---
name: triage
description: Triage a backlog: search first for relevant notes and tasks, always consult notes when uncertain what to do, always work through tasks, keep status and priority true, close what is done, and retain user-provided knowledge unavailable elsewhere.
---

# Triage a backlog

Triage keeps a board short and true. Do it when you start work on a project, when a session
ends, and whenever the board disagrees with the code.

## 1. See where you are

Call `project_current` first. It reports which project the tools work on and why that one.
Call `stats` next, for the counts by status and priority and the tags in use.

A large `todo` count with an empty `doing` count means nobody is working. Several `doing`
tasks at once mean the board tracks intentions, not work.

## 2. Search for what is already known and consult notes

Code changes; notes remain. Keep lasting user context unavailable elsewhere: preferences,
working agreements, people and responsibilities, business constraints, and reasons for decisions.
Exclude snapshots of implementation, architecture, configuration, bugs, and documentation.
A useful note should still help after the code is rewritten. Project scope only organizes notes.

Use `search` as the primary retrieval tool alongside web search to recall past agreements and
user preferences. ALWAYS consult notes (`memory_get`, `memory_list`, `search`) whenever you do not
know what to do, what conventions or architecture to follow, or what the user prefers. Check notes
before making decisions, assuming answers, or asking the user again. Use code, documentation, and the
web for current technical facts. Search for relevant notes instead of reading them all.
Update outdated context when the user corrects it.

Start each session with `search` for the current task or question. Search frequently throughout
work: before investigating, choosing an approach, asking the user for project facts, or creating
a task or note. Search again whenever a new question arises. Inspect returned matches and use
`memory_get` or `task_get` only when more detail is needed. Verify facts that may have changed.
Avoid listing and reading all notes or tasks to find information. Search combines word prefixes
and meaning across languages; rephrase queries or adjust filters when needed.

## 3. Always work through tasks: review and track work

Never perform untracked work. Use `search` to find tasks related to the request. Check whether each relevant task still
matches the code. Use `task_list` only when the user requests a complete board review or an
inventory; apply filters and paginate through that scope. Use `stats` for counts.

- Done in the code but open on the board: set `status="done"` with `task_update`.
- No longer wanted: set `status="cancelled"`. Do not delete it; a cancelled task records the decision.
- Blocked on something outside the task: set `status="blocked"` and explain the blocker with `task_comment_add`.
- Being worked on now: set `status="doing"`, one task at a time.

Use `task_comment_add(task_id, body)` for progress and decisions. Status transitions
create separate history entries. `task_update(body=...)` replaces the task description;
use it only when the task definition changes. Follow the project's `preferred_language`.

## 4. File what is missing

Call `search` before you add anything, because a duplicate is worse than a missing
task. It answers with the tasks and the notes that match.
Search matches titles, bodies and tags by word prefix and meaning. Use specific terms or
a short natural-language question.

Add what the search did not find with `task_add`. Follow `references/conventions.md` for the
title, the body, the priority and the tags.

## 5. Set priority against the other tasks

Priority is a ranking, not a feeling. At most a few tasks can be `urgent`; if everything is
urgent the ranking carries no information. Read `references/conventions.md` for what each
level means here.

## 6. Retain knowledge supplied by the user

Save proactively as soon as the user reveals useful new knowledge unavailable elsewhere.
Do not wait for a request to remember it or for the session to end. Capture preferences,
constraints, corrections, and reasons for decisions that a later session would otherwise need
to ask about again. Search first, then update the existing note or create one short note per topic.
Use `memory_save` for corrections and `memory_append` for related facts. Omit guesses, duplicates,
and summaries of code, documentation, or the session.

Link notes from tasks by stable id, such as `note #3`. Never store passwords, tokens, or keys.

## 7. Report

Say what changed: how many tasks you closed, what you added, and what is blocked and on what.
Name tasks by id and title, so the user can follow up on one of them.

Prefer closing tasks with status `done` or `cancelled` during triage. `task_delete` marks
a task's status as `deleted` without removing the record. Closing or cancelling keeps the
history easily visible.
