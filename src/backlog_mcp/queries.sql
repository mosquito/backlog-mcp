-- name: register_principal(identity, handle, name)!
INSERT INTO principals(identity, handle, name) VALUES (:identity, :handle, :name)
ON CONFLICT(identity) DO UPDATE SET name = excluded.name WHERE name != excluded.name;

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
