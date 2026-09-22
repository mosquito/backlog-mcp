-- Fresh databases only. Import old installations separately.
BEGIN IMMEDIATE;

CREATE TABLE principals (
    identity TEXT PRIMARY KEY,
    handle TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL
);

CREATE TABLE projects (
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

CREATE TABLE project_members (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    principal TEXT NOT NULL REFERENCES principals(identity),
    role TEXT NOT NULL CHECK(role IN ('owner', 'editor', 'reader')),
    PRIMARY KEY(project_id, principal)
);

CREATE TABLE project_roots (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    principal TEXT NOT NULL REFERENCES principals(identity),
    computer TEXT NOT NULL,
    root TEXT NOT NULL,
    PRIMARY KEY(project_id, principal, computer, root)
);

CREATE TABLE tasks (
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

CREATE TABLE notes (
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

CREATE TABLE task_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'comment' CHECK(kind IN ('comment', 'status', 'move')),
    created_at REAL NOT NULL,
    principal TEXT NOT NULL REFERENCES principals(identity),
    computer TEXT NOT NULL
);

CREATE INDEX memberships_by_principal ON project_members(principal, project_id);
CREATE UNIQUE INDEX project_roots_unique_path
    ON project_roots(principal, computer, root) WHERE root != '';
CREATE INDEX tasks_by_project ON tasks(project_id, status, priority);
CREATE INDEX tasks_by_update ON tasks(updated_at DESC);
CREATE INDEX notes_by_update ON notes(updated_at DESC);
CREATE INDEX comments_by_task ON task_comments(task_id, created_at, id);

CREATE VIRTUAL TABLE tasks_fts USING fts5 (
    title, body, tags, content='tasks', content_rowid='id',
    tokenize="unicode61 remove_diacritics 2"
);
CREATE TRIGGER tasks_fts_insert AFTER INSERT ON tasks BEGIN
    INSERT INTO tasks_fts(rowid, title, body, tags) VALUES (new.id, new.title, new.body, new.tags);
END;
CREATE TRIGGER tasks_fts_delete AFTER DELETE ON tasks BEGIN
    INSERT INTO tasks_fts(tasks_fts, rowid, title, body, tags) VALUES ('delete', old.id, old.title, old.body, old.tags);
END;
CREATE TRIGGER tasks_fts_update AFTER UPDATE ON tasks BEGIN
    INSERT INTO tasks_fts(tasks_fts, rowid, title, body, tags) VALUES ('delete', old.id, old.title, old.body, old.tags);
    INSERT INTO tasks_fts(rowid, title, body, tags) VALUES (new.id, new.title, new.body, new.tags);
END;

CREATE VIRTUAL TABLE notes_fts USING fts5 (
    title, body, tags, content='notes', content_rowid='id',
    tokenize="unicode61 remove_diacritics 2"
);
CREATE TRIGGER notes_fts_insert AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, title, body, tags) VALUES (new.id, new.title, new.body, new.tags);
END;
CREATE TRIGGER notes_fts_delete AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, body, tags) VALUES ('delete', old.id, old.title, old.body, old.tags);
END;
CREATE TRIGGER notes_fts_update AFTER UPDATE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, body, tags) VALUES ('delete', old.id, old.title, old.body, old.tags);
    INSERT INTO notes_fts(rowid, title, body, tags) VALUES (new.id, new.title, new.body, new.tags);
END;

CREATE VIRTUAL TABLE comments_fts USING fts5 (
    body, content='task_comments', content_rowid='id',
    tokenize="unicode61 remove_diacritics 2"
);
CREATE TRIGGER comments_fts_insert AFTER INSERT ON task_comments BEGIN
    INSERT INTO comments_fts(rowid, body) VALUES (new.id, new.body);
END;
CREATE TRIGGER comments_fts_delete AFTER DELETE ON task_comments BEGIN
    INSERT INTO comments_fts(comments_fts, rowid, body) VALUES ('delete', old.id, old.body);
END;
CREATE TRIGGER comments_fts_update AFTER UPDATE ON task_comments BEGIN
    INSERT INTO comments_fts(comments_fts, rowid, body) VALUES ('delete', old.id, old.body);
    INSERT INTO comments_fts(rowid, body) VALUES (new.id, new.body);
END;

CREATE TRIGGER task_status_history AFTER UPDATE OF status ON tasks
WHEN old.status != new.status BEGIN
    INSERT INTO task_comments(task_id, body, kind, created_at, principal, computer)
    VALUES (new.id, 'Status changed: ' || old.status || ' → ' || new.status,
            'status', new.updated_at, new.updated_principal, new.updated_computer);
END;
CREATE TRIGGER task_move_history AFTER UPDATE OF project_id ON tasks
WHEN old.project_id != new.project_id BEGIN
    INSERT INTO task_comments(task_id, body, kind, created_at, principal, computer)
    VALUES (new.id, 'Moved: ' || (SELECT u.handle || '/' || p.slug FROM projects p
                JOIN principals u ON u.identity = p.owner WHERE p.id = old.project_id)
            || ' → ' || (SELECT u.handle || '/' || p.slug FROM projects p
                JOIN principals u ON u.identity = p.owner WHERE p.id = new.project_id),
            'move', new.updated_at, new.updated_principal, new.updated_computer);
END;
CREATE TRIGGER comment_updates_task AFTER INSERT ON task_comments
WHEN new.kind = 'comment' BEGIN
    UPDATE tasks SET updated_at = new.created_at,
        updated_principal = new.principal, updated_computer = new.computer
    WHERE id = new.task_id;
END;

PRAGMA application_id = 1112296275;
COMMIT;
