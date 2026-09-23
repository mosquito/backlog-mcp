# Memory

Read project memory with `memory_list(full=true)` before work or saving an entry.
Keep it small. Store lasting agreements, preferences and invariants stated by the user.
Update a matching entry by id instead of adding a near-duplicate.

A correction replaces the body through `memory_save` with the current truth.
Do not append historical corrections. A supplied body replaces the whole body,
at most 1024 characters. Before closing a task, update memory it made obsolete.

Put task status, progress, findings and verification in tasks and comments.
Do not store secrets, guesses, code snapshots or documentation summaries.
Use shared=true for agreements across accessible projects; membership still applies.
Delete memory only when the user requests it.
