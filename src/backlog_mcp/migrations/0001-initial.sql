-- Initial schema. Existing 0.2 databases retain their data.

CREATE TABLE IF NOT EXISTS principals (
    identity TEXT PRIMARY KEY,
    handle TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL REFERENCES principals(identity),
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    preferred_language TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    created_principal TEXT NOT NULL REFERENCES principals(identity),
    created_computer TEXT NOT NULL,
    updated_principal TEXT NOT NULL REFERENCES principals(identity),
    updated_computer TEXT NOT NULL,
    UNIQUE(owner, slug)
);

CREATE TABLE IF NOT EXISTS project_members (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    principal TEXT NOT NULL REFERENCES principals(identity),
    role TEXT NOT NULL CHECK(role IN ('owner', 'editor', 'reader')),
    PRIMARY KEY(project_id, principal)
);

CREATE TABLE IF NOT EXISTS project_roots (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    principal TEXT NOT NULL REFERENCES principals(identity),
    computer TEXT NOT NULL,
    root TEXT NOT NULL,
    PRIMARY KEY(project_id, principal, computer, root)
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'todo',
    priority INTEGER NOT NULL DEFAULT 2,
    tags TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    closed_at REAL,
    created_principal TEXT NOT NULL REFERENCES principals(identity),
    created_computer TEXT NOT NULL,
    updated_principal TEXT NOT NULL REFERENCES principals(identity),
    updated_computer TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',
    shared INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    created_principal TEXT NOT NULL REFERENCES principals(identity),
    created_computer TEXT NOT NULL,
    updated_principal TEXT NOT NULL REFERENCES principals(identity),
    updated_computer TEXT NOT NULL,
    UNIQUE(project_id, title)
);

CREATE TABLE IF NOT EXISTS task_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'comment' CHECK(kind IN ('comment', 'status', 'move')),
    created_at REAL NOT NULL,
    principal TEXT NOT NULL REFERENCES principals(identity),
    computer TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_links (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    relation TEXT NOT NULL CHECK(relation IN (
        'part_of', 'contains', 'depends_on', 'blocks', 'caused_by', 'causes', 'related_to'
    )),
    reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    created_principal TEXT NOT NULL REFERENCES principals(identity),
    created_computer TEXT NOT NULL,
    updated_principal TEXT NOT NULL REFERENCES principals(identity),
    updated_computer TEXT NOT NULL,
    PRIMARY KEY(task_id, relation, target_id),
    CHECK(task_id != target_id)
);
CREATE INDEX IF NOT EXISTS links_by_target ON task_links(target_id);

CREATE INDEX IF NOT EXISTS memberships_by_principal ON project_members(principal, project_id);
CREATE UNIQUE INDEX IF NOT EXISTS project_roots_unique_path
    ON project_roots(principal, computer, root) WHERE root != '';
CREATE INDEX IF NOT EXISTS tasks_by_project ON tasks(project_id, status, priority);
CREATE INDEX IF NOT EXISTS tasks_by_update ON tasks(updated_at DESC);
CREATE INDEX IF NOT EXISTS notes_by_update ON notes(updated_at DESC);
CREATE INDEX IF NOT EXISTS comments_by_task ON task_comments(task_id, created_at, id);

CREATE VIRTUAL TABLE IF NOT EXISTS projects_fts USING fts5 (
    slug, name, description, content='projects', content_rowid='id',
    tokenize="unicode61 remove_diacritics 2"
);
CREATE TRIGGER IF NOT EXISTS projects_fts_insert AFTER INSERT ON projects BEGIN
    INSERT INTO projects_fts(rowid, slug, name, description) VALUES (new.id, new.slug, new.name, new.description);
END;
CREATE TRIGGER IF NOT EXISTS projects_fts_delete AFTER DELETE ON projects BEGIN
    INSERT INTO projects_fts(projects_fts, rowid, slug, name, description)
    VALUES ('delete', old.id, old.slug, old.name, old.description);
END;
CREATE TRIGGER IF NOT EXISTS projects_fts_update AFTER UPDATE ON projects BEGIN
    INSERT INTO projects_fts(projects_fts, rowid, slug, name, description)
    VALUES ('delete', old.id, old.slug, old.name, old.description);
    INSERT INTO projects_fts(rowid, slug, name, description) VALUES (new.id, new.slug, new.name, new.description);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts USING fts5 (
    title, body, tags, content='tasks', content_rowid='id',
    tokenize="unicode61 remove_diacritics 2"
);
CREATE TRIGGER IF NOT EXISTS tasks_fts_insert AFTER INSERT ON tasks BEGIN
    INSERT INTO tasks_fts(rowid, title, body, tags) VALUES (new.id, new.title, new.body, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS tasks_fts_delete AFTER DELETE ON tasks BEGIN
    INSERT INTO tasks_fts(tasks_fts, rowid, title, body, tags) VALUES ('delete', old.id, old.title, old.body, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS tasks_fts_update AFTER UPDATE ON tasks BEGIN
    INSERT INTO tasks_fts(tasks_fts, rowid, title, body, tags) VALUES ('delete', old.id, old.title, old.body, old.tags);
    INSERT INTO tasks_fts(rowid, title, body, tags) VALUES (new.id, new.title, new.body, new.tags);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5 (
    title, body, tags, content='notes', content_rowid='id',
    tokenize="unicode61 remove_diacritics 2"
);
CREATE TRIGGER IF NOT EXISTS notes_fts_insert AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, title, body, tags) VALUES (new.id, new.title, new.body, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS notes_fts_delete AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, body, tags) VALUES ('delete', old.id, old.title, old.body, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS notes_fts_update AFTER UPDATE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, body, tags) VALUES ('delete', old.id, old.title, old.body, old.tags);
    INSERT INTO notes_fts(rowid, title, body, tags) VALUES (new.id, new.title, new.body, new.tags);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS comments_fts USING fts5 (
    body, content='task_comments', content_rowid='id',
    tokenize="unicode61 remove_diacritics 2"
);
CREATE TRIGGER IF NOT EXISTS comments_fts_insert AFTER INSERT ON task_comments BEGIN
    INSERT INTO comments_fts(rowid, body) VALUES (new.id, new.body);
END;
CREATE TRIGGER IF NOT EXISTS comments_fts_delete AFTER DELETE ON task_comments BEGIN
    INSERT INTO comments_fts(comments_fts, rowid, body) VALUES ('delete', old.id, old.body);
END;
CREATE TRIGGER IF NOT EXISTS comments_fts_update AFTER UPDATE ON task_comments BEGIN
    INSERT INTO comments_fts(comments_fts, rowid, body) VALUES ('delete', old.id, old.body);
    INSERT INTO comments_fts(rowid, body) VALUES (new.id, new.body);
END;

CREATE TRIGGER IF NOT EXISTS task_status_history AFTER UPDATE OF status ON tasks
WHEN old.status != new.status BEGIN
    INSERT INTO task_comments(task_id, body, kind, created_at, principal, computer)
    VALUES (new.id, 'Status changed: ' || old.status || ' → ' || new.status,
            'status', new.updated_at, new.updated_principal, new.updated_computer);
END;
CREATE TRIGGER IF NOT EXISTS task_move_history AFTER UPDATE OF project_id ON tasks
WHEN old.project_id != new.project_id BEGIN
    INSERT INTO task_comments(task_id, body, kind, created_at, principal, computer)
    VALUES (new.id, 'Moved: ' || (SELECT u.handle || '/' || p.slug FROM projects p
                JOIN principals u ON u.identity = p.owner WHERE p.id = old.project_id)
            || ' → ' || (SELECT u.handle || '/' || p.slug FROM projects p
                JOIN principals u ON u.identity = p.owner WHERE p.id = new.project_id),
            'move', new.updated_at, new.updated_principal, new.updated_computer);
END;
CREATE TRIGGER IF NOT EXISTS comment_updates_task AFTER INSERT ON task_comments
WHEN new.kind = 'comment' BEGIN
    UPDATE tasks SET updated_at = new.created_at,
        updated_principal = new.principal, updated_computer = new.computer
    WHERE id = new.task_id;
END;

PRAGMA application_id = 1112296275;
