-- name: register_principal(identity, handle, name)!
INSERT INTO principals(identity, handle, name) VALUES (:identity, :handle, :name)
ON CONFLICT(identity) DO UPDATE SET name = excluded.name WHERE name != excluded.name;

-- name: claim_task(task_id, expected, assigned, now, principal, computer)!
UPDATE tasks SET assigned = :assigned, updated_at = :now,
    updated_principal = :principal, updated_computer = :computer
WHERE id = :task_id AND assigned IS :expected;

-- name: assigned_tasks(principal, prefix, pattern, limit, offset)
-- LIKE uses tasks_by_assigned; substr retains case-sensitive principal and marker matching.
SELECT t.*, u.handle || '/' || p.slug AS project
FROM tasks t JOIN projects p ON p.id = t.project_id
JOIN principals u ON u.identity = p.owner
WHERE t.assigned LIKE :pattern ESCAPE '\'
AND substr(t.assigned, 1, length(:prefix)) = :prefix
AND EXISTS (SELECT 1 FROM project_members m
            WHERE m.project_id = t.project_id AND m.principal = :principal)
ORDER BY t.assigned COLLATE NOCASE, t.id LIMIT :limit OFFSET :offset;

-- name: count_assigned(principal, prefix, pattern)^
SELECT COUNT(*) FROM tasks t
WHERE t.assigned LIKE :pattern ESCAPE '\'
AND substr(t.assigned, 1, length(:prefix)) = :prefix
AND EXISTS (SELECT 1 FROM project_members m
            WHERE m.project_id = t.project_id AND m.principal = :principal);

-- name: task_links(task_id, principal)
SELECT t.id AS task, u.handle || '/' || p.slug AS project,
       l.relation, t.title, t.status, l.reason
FROM task_links l JOIN tasks t ON t.id = l.target_id
JOIN projects p ON p.id = t.project_id JOIN principals u ON u.identity = p.owner
JOIN tasks source ON source.id = l.task_id
WHERE l.task_id = :task_id
AND EXISTS (SELECT 1 FROM project_members m
            WHERE m.project_id = p.id AND m.principal = :principal)
AND EXISTS (SELECT 1 FROM project_members m
            WHERE m.project_id = source.project_id AND m.principal = :principal)
ORDER BY l.relation, l.target_id;

-- name: link_pair(task_id, target_id, relation, inverse)
SELECT * FROM task_links
WHERE (task_id = :task_id AND target_id = :target_id AND relation = :relation)
   OR (task_id = :target_id AND target_id = :task_id AND relation = :inverse);

-- name: link_cycle(source, target, relation)^
WITH RECURSIVE reachable(id) AS (
    SELECT :target
    UNION
    SELECT l.target_id FROM task_links l JOIN reachable r ON l.task_id = r.id
    WHERE l.relation = :relation
)
SELECT id FROM reachable WHERE id = :source LIMIT 1;

-- name: link_tasks(task_id, target_id, relation, inverse, reason, now, principal, computer)!
INSERT INTO task_links (
    task_id, target_id, relation, reason, created_at, updated_at,
    created_principal, created_computer, updated_principal, updated_computer
) VALUES
    (:task_id, :target_id, :relation, :reason, :now, :now,
     :principal, :computer, :principal, :computer),
    (:target_id, :task_id, :inverse, :reason, :now, :now,
     :principal, :computer, :principal, :computer)
ON CONFLICT(task_id, relation, target_id) DO UPDATE SET
    reason = excluded.reason, updated_at = excluded.updated_at,
    updated_principal = excluded.updated_principal, updated_computer = excluded.updated_computer;

-- name: unlink_tasks(task_id, target_id, relation, inverse)!
DELETE FROM task_links
WHERE (task_id = :task_id AND target_id = :target_id AND relation = :relation)
   OR (task_id = :target_id AND target_id = :task_id AND relation = :inverse);

-- name: touch_link_tasks(task_id, target_id, now, principal, computer)!
UPDATE tasks SET updated_at = :now, updated_principal = :principal, updated_computer = :computer
WHERE id IN (:task_id, :target_id);

-- name: membership(project_id, principal)^
SELECT role FROM project_members WHERE project_id = :project_id AND principal = :principal;

-- name: members(project_id)
SELECT m.principal, p.name, p.handle, m.role FROM project_members m
JOIN principals p ON p.identity = m.principal
WHERE m.project_id = :project_id ORDER BY m.role, m.principal;

-- name: save_member(project_id, principal, role)!
INSERT INTO project_members(project_id, principal, role) VALUES (:project_id, :principal, :role)
ON CONFLICT(project_id, principal) DO UPDATE SET role = excluded.role;

-- name: principal(identity)^
SELECT identity, name FROM principals WHERE identity = :identity;

-- name: add_project(slug, name, description, preferred_language, created_at, updated_at, owner, created_principal, created_computer, updated_principal, updated_computer)<!
INSERT INTO projects (
    slug, name, description, preferred_language, created_at, updated_at, owner,
    created_principal, created_computer, updated_principal, updated_computer
) VALUES (
    :slug, :name, :description, :preferred_language, :created_at, :updated_at, :owner,
    :created_principal, :created_computer, :updated_principal, :updated_computer
);

-- name: add_task(project_id, title, body, status, priority, tags, created_at, updated_at, closed_at, created_principal, created_computer, updated_principal, updated_computer)<!
INSERT INTO tasks (
    project_id, title, body, status, priority, tags, created_at, updated_at, closed_at,
    created_principal, created_computer, updated_principal, updated_computer
) VALUES (
    :project_id, :title, :body, :status, :priority, :tags, :created_at, :updated_at,
    :closed_at, :created_principal, :created_computer, :updated_principal, :updated_computer
);

-- name: add_note(project_id, title, body, tags, shared, created_at, updated_at, created_principal, created_computer, updated_principal, updated_computer)<!
INSERT INTO notes (
    project_id, title, body, tags, shared, created_at, updated_at, created_principal,
    created_computer, updated_principal, updated_computer
) VALUES (
    :project_id, :title, :body, :tags, :shared, :created_at, :updated_at,
    :created_principal, :created_computer, :updated_principal, :updated_computer
);

-- name: add_comment(task_id, body, created_at, principal, computer)<!
INSERT INTO task_comments (
    task_id, body, created_at, principal, computer
) VALUES (
    :task_id, :body, :created_at, :principal, :computer
);

-- name: project_roots(ids, principal)
SELECT project_id, computer, root FROM project_roots
WHERE project_id IN (SELECT value FROM json_each(:ids)) AND principal = :principal
ORDER BY computer, root;

-- name: project(project_id, principal)^
SELECT p.*, (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id) AS total_tasks,
(SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id AND t.status IN ('todo', 'doing',
    'blocked')) AS open_tasks FROM projects p WHERE p.id = :project_id AND EXISTS (SELECT 1 FROM
    project_members m WHERE m.project_id = p.id AND m.principal = :principal);

-- name: project_by_slug(slug, handle, principal)
SELECT p.*, (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id) AS total_tasks,
(SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id AND t.status IN ('todo', 'doing',
    'blocked')) AS open_tasks FROM projects p JOIN principals u ON u.identity = p.owner
WHERE p.slug = :slug AND (:handle IS NULL OR u.handle = :handle) AND EXISTS (SELECT 1 FROM
    project_members m WHERE m.project_id = p.id AND m.principal = :principal);

-- name: project_by_root(computer, root, principal)^
SELECT p.*, (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id) AS total_tasks,
(SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id AND t.status IN ('todo', 'doing',
    'blocked')) AS open_tasks FROM projects p JOIN project_roots r ON r.project_id = p.id
WHERE r.computer = :computer AND r.root = :root AND r.principal = :principal AND EXISTS (SELECT 1
    FROM project_members m WHERE m.project_id = p.id AND m.principal = :principal);

-- name: projects_for_paths(computer, paths, principal)
SELECT p.*, (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id) AS total_tasks,
(SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id AND t.status IN ('todo', 'doing',
    'blocked')) AS open_tasks FROM projects p JOIN project_roots r ON r.project_id = p.id
WHERE r.computer = :computer AND r.root != '' AND r.root IN (SELECT value FROM json_each(:paths))
AND r.principal = :principal AND EXISTS (SELECT 1 FROM project_members m WHERE m.project_id = p.id
    AND m.principal = :principal);

-- name: projects(principal, archived, computer)
SELECT p.*, (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id) AS total_tasks,
(SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id AND t.status IN ('todo', 'doing',
    'blocked')) AS open_tasks FROM projects p WHERE EXISTS (SELECT 1 FROM project_members m WHERE
    m.project_id = p.id AND m.principal = :principal) AND (:archived OR p.archived = 0)
AND (:computer IS NULL OR EXISTS (SELECT 1 FROM project_roots r
    WHERE r.project_id = p.id AND r.principal = :principal AND r.computer = :computer)) ORDER BY
    p.archived, p.slug;

-- name: search_projects(match, principal, computer, project_id, limit)
SELECT p.*, (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id) AS total_tasks,
(SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id AND t.status IN ('todo', 'doing',
    'blocked')) AS open_tasks FROM projects p JOIN projects_fts ON projects_fts.rowid = p.id
WHERE projects_fts MATCH :match AND EXISTS (SELECT 1 FROM project_members m WHERE m.project_id =
    p.id AND m.principal = :principal) AND (p.archived = 0)
AND (:computer IS NULL OR EXISTS (SELECT 1 FROM project_roots r
    WHERE r.project_id = p.id AND r.principal = :principal AND r.computer = :computer))
AND (:project_id IS NULL OR p.id = :project_id)
ORDER BY bm25(projects_fts), p.id LIMIT :limit;

-- name: computers(principal)
SELECT DISTINCT r.computer FROM project_roots r JOIN projects p ON p.id = r.project_id
WHERE r.principal = :principal AND EXISTS (SELECT 1 FROM project_members m WHERE m.project_id = p.id
    AND m.principal = :principal) ORDER BY r.computer;

-- name: owned_slugs(principal)
SELECT slug FROM projects WHERE owner = :principal;

-- name: add_root(project_id, principal, computer, root)!
INSERT INTO project_roots (project_id, principal, computer, root) VALUES (:project_id, :principal,
    :computer, :root);

-- name: existing_root(project_id, principal, computer, root)^
SELECT project_id FROM project_roots WHERE project_id = :project_id AND principal = :principal AND
    computer = :computer AND root = :root;

-- name: project_contents(project_id)^
SELECT (SELECT COUNT(*) FROM tasks WHERE project_id = :project_id) AS tasks,
(SELECT COUNT(*) FROM notes WHERE project_id = :project_id) AS notes,
(SELECT COUNT(*) FROM notes WHERE project_id = :project_id AND shared = 1) AS shared_notes;

-- name: delete_project_tasks(project_id)!
DELETE FROM tasks WHERE project_id = :project_id;

-- name: delete_project_notes(project_id)!
DELETE FROM notes WHERE project_id = :project_id;

-- name: delete_project_project_roots(project_id)!
DELETE FROM project_roots WHERE project_id = :project_id;

-- name: delete_project(project_id)!
DELETE FROM projects WHERE id = :project_id;

-- name: update_project(changes, project_id)!
UPDATE projects SET name = CASE WHEN json_type(:changes, '$.name') IS NULL THEN name ELSE
    json_extract(:changes, '$.name') END,
    description = CASE WHEN json_type(:changes, '$.description') IS NULL THEN description ELSE
    json_extract(:changes, '$.description') END,
    archived = CASE WHEN json_type(:changes, '$.archived') IS NULL THEN archived ELSE
    json_extract(:changes, '$.archived') END,
    slug = CASE WHEN json_type(:changes, '$.slug') IS NULL THEN slug ELSE json_extract(:changes,
    '$.slug') END,
    preferred_language = CASE WHEN json_type(:changes, '$.preferred_language') IS NULL THEN
    preferred_language ELSE json_extract(:changes, '$.preferred_language') END,
    updated_principal = CASE WHEN json_type(:changes, '$.updated_principal') IS NULL THEN
    updated_principal ELSE json_extract(:changes, '$.updated_principal') END,
    updated_computer = CASE WHEN json_type(:changes, '$.updated_computer') IS NULL THEN
    updated_computer ELSE json_extract(:changes, '$.updated_computer') END,
    updated_at = CASE WHEN json_type(:changes, '$.updated_at') IS NULL THEN updated_at ELSE
    json_extract(:changes, '$.updated_at') END
WHERE id = :project_id;

-- name: update_task(changes, task_id)!
UPDATE tasks SET title = CASE WHEN json_type(:changes, '$.title') IS NULL THEN title ELSE
    json_extract(:changes, '$.title') END,
    body = CASE WHEN json_type(:changes, '$.body') IS NULL THEN body ELSE json_extract(:changes,
    '$.body') END,
    status = CASE WHEN json_type(:changes, '$.status') IS NULL THEN status ELSE
    json_extract(:changes, '$.status') END,
    closed_at = CASE WHEN json_type(:changes, '$.closed_at') IS NULL THEN closed_at ELSE
    json_extract(:changes, '$.closed_at') END,
    priority = CASE WHEN json_type(:changes, '$.priority') IS NULL THEN priority ELSE
    json_extract(:changes, '$.priority') END,
    tags = CASE WHEN json_type(:changes, '$.tags') IS NULL THEN tags ELSE json_extract(:changes,
    '$.tags') END,
    project_id = CASE WHEN json_type(:changes, '$.project_id') IS NULL THEN project_id ELSE
    json_extract(:changes, '$.project_id') END,
    updated_principal = CASE WHEN json_type(:changes, '$.updated_principal') IS NULL THEN
    updated_principal ELSE json_extract(:changes, '$.updated_principal') END,
    updated_computer = CASE WHEN json_type(:changes, '$.updated_computer') IS NULL THEN
    updated_computer ELSE json_extract(:changes, '$.updated_computer') END,
    updated_at = CASE WHEN json_type(:changes, '$.updated_at') IS NULL THEN updated_at ELSE
    json_extract(:changes, '$.updated_at') END
WHERE id = :task_id;

-- name: update_note(changes, note_id)!
UPDATE notes SET title = CASE WHEN json_type(:changes, '$.title') IS NULL THEN title ELSE
    json_extract(:changes, '$.title') END,
    body = CASE WHEN json_type(:changes, '$.body') IS NULL THEN body ELSE json_extract(:changes,
    '$.body') END,
    tags = CASE WHEN json_type(:changes, '$.tags') IS NULL THEN tags ELSE json_extract(:changes,
    '$.tags') END,
    shared = CASE WHEN json_type(:changes, '$.shared') IS NULL THEN shared ELSE
    json_extract(:changes, '$.shared') END,
    project_id = CASE WHEN json_type(:changes, '$.project_id') IS NULL THEN project_id ELSE
    json_extract(:changes, '$.project_id') END,
    updated_principal = CASE WHEN json_type(:changes, '$.updated_principal') IS NULL THEN
    updated_principal ELSE json_extract(:changes, '$.updated_principal') END,
    updated_computer = CASE WHEN json_type(:changes, '$.updated_computer') IS NULL THEN
    updated_computer ELSE json_extract(:changes, '$.updated_computer') END,
    updated_at = CASE WHEN json_type(:changes, '$.updated_at') IS NULL THEN updated_at ELSE
    json_extract(:changes, '$.updated_at') END
WHERE id = :note_id;

-- name: task(task_id, principal)^
SELECT t.*, (SELECT handle FROM principals WHERE identity = p.owner) || '/' || p.slug AS project
    FROM tasks t JOIN projects p ON p.id = t.project_id WHERE t.id = :task_id AND EXISTS (SELECT 1
    FROM project_members m WHERE m.project_id = p.id AND m.principal = :principal);

-- name: comment(comment_id, principal)^
SELECT c.*, t.assigned AS task_assigned, t.project_id, (SELECT handle FROM principals WHERE identity
    = p.owner) || '/' || p.slug AS project FROM task_comments c
JOIN tasks t ON t.id = c.task_id JOIN projects p ON p.id = t.project_id WHERE c.id = :comment_id AND
    EXISTS (SELECT 1 FROM project_members m WHERE m.project_id = p.id AND m.principal = :principal);

-- name: count_comments(task_id)^
SELECT COUNT(*) FROM task_comments WHERE task_id = :task_id;

-- name: comments(principal, project_id, computer, statuses, tag, priority, task_id, exclude_deleted, limit)
SELECT c.*, t.assigned AS task_assigned, t.project_id, (SELECT handle FROM principals WHERE identity
    = p.owner) || '/' || p.slug AS project FROM task_comments c
JOIN tasks t ON t.id = c.task_id JOIN projects p ON p.id = t.project_id WHERE EXISTS (SELECT 1 FROM
    project_members m WHERE m.project_id = p.id AND m.principal = :principal)
AND (:project_id IS NULL OR t.project_id = :project_id)
AND (:computer IS NULL OR EXISTS (SELECT 1 FROM project_roots r
    WHERE r.project_id = p.id AND r.principal = :principal AND r.computer = :computer))
AND (:statuses IS NULL OR t.status IN (SELECT value FROM json_each(:statuses)))
AND (:tag IS NULL OR (',' || t.tags || ',') LIKE :tag)
AND (:priority IS NULL OR t.priority = :priority)
AND (:task_id IS NULL OR c.task_id = :task_id)
AND (NOT :exclude_deleted OR t.status != 'deleted') ORDER BY c.created_at, c.id LIMIT :limit;

-- name: search_comments(match, principal, project_id, computer, statuses, tag, priority, task_id, exclude_deleted, limit)
SELECT c.*, t.assigned AS task_assigned, t.project_id, (SELECT handle FROM principals WHERE identity
    = p.owner) || '/' || p.slug AS project, snippet(comments_fts, 0, '[', ']', '…', 16) AS snippet
    FROM task_comments c
JOIN tasks t ON t.id = c.task_id JOIN projects p ON p.id = t.project_id
JOIN comments_fts ON c.id = comments_fts.rowid
WHERE comments_fts MATCH :match AND EXISTS (SELECT 1 FROM project_members m WHERE m.project_id =
    p.id AND m.principal = :principal)
AND (:project_id IS NULL OR t.project_id = :project_id)
AND (:computer IS NULL OR EXISTS (SELECT 1 FROM project_roots r
    WHERE r.project_id = p.id AND r.principal = :principal AND r.computer = :computer))
AND (:statuses IS NULL OR t.status IN (SELECT value FROM json_each(:statuses)))
AND (:tag IS NULL OR (',' || t.tags || ',') LIKE :tag)
AND (:priority IS NULL OR t.priority = :priority)
AND (:task_id IS NULL OR c.task_id = :task_id)
AND (NOT :exclude_deleted OR t.status != 'deleted')
ORDER BY bm25(comments_fts), c.created_at, c.id LIMIT :limit;

-- name: tasks(principal, project_id, computer, statuses, tag, priority, ordering, limit, offset)
SELECT t.*, (SELECT handle FROM principals WHERE identity = p.owner) || '/' || p.slug AS project
    FROM tasks t JOIN projects p ON p.id = t.project_id WHERE EXISTS (SELECT 1 FROM project_members
    m WHERE m.project_id = p.id AND m.principal = :principal)
AND (:project_id IS NULL OR t.project_id = :project_id)
AND (:computer IS NULL OR EXISTS (SELECT 1 FROM project_roots r
    WHERE r.project_id = p.id AND r.principal = :principal AND r.computer = :computer))
AND (:statuses IS NULL OR t.status IN (SELECT value FROM json_each(:statuses)))
AND (:tag IS NULL OR (',' || t.tags || ',') LIKE :tag)
AND (:priority IS NULL OR t.priority = :priority)
ORDER BY CASE WHEN :ordering = 'backlog' THEN t.priority END,
CASE WHEN :ordering = 'backlog' THEN t.created_at END,
CASE WHEN :ordering = 'backlog' THEN t.id END,
CASE WHEN :ordering = 'recent' THEN t.updated_at END DESC,
CASE WHEN :ordering = 'recent' THEN t.id END DESC LIMIT :limit OFFSET :offset;

-- name: count_tasks(principal, project_id, computer, statuses, tag, priority)^
SELECT COUNT(*) AS found FROM tasks t JOIN projects p ON p.id = t.project_id WHERE EXISTS (SELECT 1
    FROM project_members m WHERE m.project_id = p.id AND m.principal = :principal)
AND (:project_id IS NULL OR t.project_id = :project_id)
AND (:computer IS NULL OR EXISTS (SELECT 1 FROM project_roots r
    WHERE r.project_id = p.id AND r.principal = :principal AND r.computer = :computer))
AND (:statuses IS NULL OR t.status IN (SELECT value FROM json_each(:statuses)))
AND (:tag IS NULL OR (',' || t.tags || ',') LIKE :tag)
AND (:priority IS NULL OR t.priority = :priority);

-- name: search_tasks(match, principal, project_id, computer, statuses, tag, priority, exclude_deleted, limit)
SELECT t.*, (SELECT handle FROM principals WHERE identity = p.owner) || '/' || p.slug AS project,
    snippet(tasks_fts, 1, '[', ']', '…', 10) AS snippet FROM tasks t JOIN projects p ON p.id =
    t.project_id
JOIN tasks_fts ON tasks_fts.rowid = t.id
WHERE tasks_fts MATCH :match AND EXISTS (SELECT 1 FROM project_members m WHERE m.project_id = p.id
    AND m.principal = :principal)
AND (:project_id IS NULL OR t.project_id = :project_id)
AND (:computer IS NULL OR EXISTS (SELECT 1 FROM project_roots r
    WHERE r.project_id = p.id AND r.principal = :principal AND r.computer = :computer))
AND (:statuses IS NULL OR t.status IN (SELECT value FROM json_each(:statuses)))
AND (:tag IS NULL OR (',' || t.tags || ',') LIKE :tag)
AND (:priority IS NULL OR t.priority = :priority)
AND (NOT :exclude_deleted OR t.status != 'deleted')
ORDER BY bm25(tasks_fts), t.priority, t.id DESC LIMIT :limit;

-- name: tags(principal, project_id, computer, statuses, tag, priority)
SELECT t.tags FROM tasks t JOIN projects p ON p.id = t.project_id
WHERE EXISTS (SELECT 1 FROM project_members m WHERE m.project_id = p.id AND m.principal =
    :principal)
AND (:project_id IS NULL OR t.project_id = :project_id)
AND (:computer IS NULL OR EXISTS (SELECT 1 FROM project_roots r
    WHERE r.project_id = p.id AND r.principal = :principal AND r.computer = :computer))
AND (:statuses IS NULL OR t.status IN (SELECT value FROM json_each(:statuses)))
AND (:tag IS NULL OR (',' || t.tags || ',') LIKE :tag)
AND (:priority IS NULL OR t.priority = :priority) AND t.status != 'deleted' AND t.tags != '';

-- name: group_tasks(column, principal, project_id, computer, statuses, tag, priority)
SELECT CASE :column WHEN 'status' THEN t.status WHEN 'priority' THEN t.priority END AS key,
COUNT(*) AS found FROM tasks t JOIN projects p ON p.id = t.project_id WHERE EXISTS (SELECT 1 FROM
    project_members m WHERE m.project_id = p.id AND m.principal = :principal)
AND (:project_id IS NULL OR t.project_id = :project_id)
AND (:computer IS NULL OR EXISTS (SELECT 1 FROM project_roots r
    WHERE r.project_id = p.id AND r.principal = :principal AND r.computer = :computer))
AND (:statuses IS NULL OR t.status IN (SELECT value FROM json_each(:statuses)))
AND (:tag IS NULL OR (',' || t.tags || ',') LIKE :tag)
AND (:priority IS NULL OR t.priority = :priority) GROUP BY key;

-- name: note(note_id, principal)^
SELECT n.*, (SELECT handle FROM principals WHERE identity = p.owner) || '/' || p.slug AS project
    FROM notes n JOIN projects p ON p.id = n.project_id WHERE n.id = :note_id AND EXISTS (SELECT 1
    FROM project_members m WHERE m.project_id = p.id AND m.principal = :principal);

-- name: note_by_title(project_id, title, principal)^
SELECT n.*, (SELECT handle FROM principals WHERE identity = p.owner) || '/' || p.slug AS project
    FROM notes n JOIN projects p ON p.id = n.project_id WHERE n.project_id = :project_id AND n.title
    = :title AND EXISTS (SELECT 1 FROM project_members m WHERE m.project_id = p.id AND m.principal =
    :principal);

-- name: append_note(text, suffix, now, principal, computer, note_id)!
UPDATE notes SET body = CASE WHEN body = '' THEN :text ELSE body || :suffix END,
updated_at = :now, updated_principal = :principal, updated_computer = :computer WHERE id = :note_id;

-- name: delete_note(note_id)!
DELETE FROM notes WHERE id = :note_id;

-- name: notes(principal, project_id, computer, shared, tag, limit, offset)
SELECT n.*, (SELECT handle FROM principals WHERE identity = p.owner) || '/' || p.slug AS project
    FROM notes n JOIN projects p ON p.id = n.project_id WHERE EXISTS (SELECT 1 FROM project_members
    m WHERE m.project_id = p.id AND m.principal = :principal)
AND (:project_id IS NULL OR n.project_id = :project_id OR n.shared = 1)
AND (:computer IS NULL OR n.shared = 1 OR EXISTS (SELECT 1 FROM project_roots r
    WHERE r.project_id = p.id AND r.principal = :principal AND r.computer = :computer))
AND (:shared IS NULL OR n.shared = :shared)
AND (:tag IS NULL OR (',' || n.tags || ',') LIKE :tag) ORDER BY n.updated_at DESC, n.id DESC LIMIT
    :limit OFFSET :offset;

-- name: count_notes(principal, project_id, computer, shared, tag)^
SELECT COUNT(*) AS found FROM notes n JOIN projects p ON p.id = n.project_id WHERE EXISTS (SELECT 1
    FROM project_members m WHERE m.project_id = p.id AND m.principal = :principal)
AND (:project_id IS NULL OR n.project_id = :project_id OR n.shared = 1)
AND (:computer IS NULL OR n.shared = 1 OR EXISTS (SELECT 1 FROM project_roots r
    WHERE r.project_id = p.id AND r.principal = :principal AND r.computer = :computer))
AND (:shared IS NULL OR n.shared = :shared)
AND (:tag IS NULL OR (',' || n.tags || ',') LIKE :tag);

-- name: search_notes(match, principal, project_id, computer, shared, tag, limit)
SELECT n.*, (SELECT handle FROM principals WHERE identity = p.owner) || '/' || p.slug AS project,
    snippet(notes_fts, 1, '[', ']', '…', 10) AS snippet FROM notes n JOIN projects p ON p.id =
    n.project_id
JOIN notes_fts ON notes_fts.rowid = n.id
WHERE notes_fts MATCH :match AND EXISTS (SELECT 1 FROM project_members m WHERE m.project_id = p.id
    AND m.principal = :principal)
AND (:project_id IS NULL OR n.project_id = :project_id OR n.shared = 1)
AND (:computer IS NULL OR n.shared = 1 OR EXISTS (SELECT 1 FROM project_roots r
    WHERE r.project_id = p.id AND r.principal = :principal AND r.computer = :computer))
AND (:shared IS NULL OR n.shared = :shared)
AND (:tag IS NULL OR (',' || n.tags || ',') LIKE :tag)
ORDER BY bm25(notes_fts), n.updated_at DESC LIMIT :limit;

-- name: linked_tasks(task_id, project_id)
SELECT DISTINCT t.*, (SELECT handle FROM principals WHERE identity = p.owner) || '/' || p.slug AS
    project FROM tasks t JOIN projects p ON p.id = t.project_id
JOIN task_links l ON l.task_id = t.id JOIN tasks target ON target.id = l.target_id
WHERE target.id = :task_id OR target.project_id = :project_id;

-- name: notification_recipients(project_id)
SELECT principal FROM project_members WHERE project_id = :project_id;

-- name: board_summary(project_id, principal, prefix)^
WITH candidates AS MATERIALIZED (
    SELECT t.id, t.title, t.status, t.priority, t.created_at, t.assigned,
           t.status IN ('todo', 'doing', 'blocked') AS is_open,
           CASE WHEN t.status IN ('todo', 'doing', 'blocked') THEN EXISTS (
               SELECT 1 FROM task_links l JOIN tasks d ON d.id = l.target_id
               WHERE l.task_id = t.id AND l.relation = 'depends_on'
               AND (d.status NOT IN ('done', 'cancelled', 'deleted')
                    OR NOT EXISTS (SELECT 1 FROM project_members m
                                   WHERE m.project_id = d.project_id
                                   AND m.principal = :principal))
           ) ELSE 0 END AS dependency_blocked
    FROM tasks t
    WHERE t.project_id = :project_id
    AND EXISTS (SELECT 1 FROM project_members m
                WHERE m.project_id = t.project_id AND m.principal = :principal)
), classified AS MATERIALIZED (
    SELECT *,
           assigned IS NULL AND status IN ('todo', 'doing')
               AND NOT dependency_blocked AS is_ready,
           is_open AND substr(assigned, 1, length(:prefix)) = :prefix AS is_mine
    FROM candidates
)
SELECT COUNT(*) AS total,
       COUNT(*) FILTER (WHERE is_open) AS open,
       COUNT(*) FILTER (WHERE is_open AND assigned IS NOT NULL) AS claimed,
       COUNT(*) FILTER (WHERE is_open AND assigned IS NULL) AS unclaimed,
       COUNT(*) FILTER (WHERE is_open AND (status = 'blocked' OR dependency_blocked)) AS blocked,
       COUNT(*) FILTER (WHERE status = 'done') AS done,
       COUNT(*) FILTER (WHERE status = 'cancelled') AS cancelled,
       COUNT(*) FILTER (WHERE status = 'deleted') AS deleted,
       COUNT(*) FILTER (WHERE is_ready) AS ready_count,
       COUNT(*) FILTER (WHERE is_mine) AS claimed_count,
       (SELECT json_group_array(json_object('id', id, 'title', title))
        FROM (SELECT id, title FROM classified WHERE is_ready
              ORDER BY priority, created_at, id LIMIT 10)) AS ready_items,
       (SELECT json_group_array(json_object('id', id, 'title', title))
        FROM (SELECT id, title FROM classified WHERE is_mine
              ORDER BY priority, created_at, id LIMIT 10)) AS claimed_items
FROM classified;
