-- name: register_principal(identity, handle, name)!
INSERT INTO principals(identity, handle, name) VALUES (:identity, :handle, :name)
ON CONFLICT(identity) DO UPDATE SET name = excluded.name WHERE name != excluded.name;

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
