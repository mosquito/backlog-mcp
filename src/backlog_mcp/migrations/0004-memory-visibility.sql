-- Existing entries keep their project access.
CREATE TABLE notes_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',
    shared INTEGER NOT NULL DEFAULT 0 CHECK(shared IN (0, 1)),
    global_scope INTEGER NOT NULL DEFAULT 0 CHECK(global_scope IN (0, 1)),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    created_principal TEXT NOT NULL REFERENCES principals(identity),
    created_computer TEXT NOT NULL,
    updated_principal TEXT NOT NULL REFERENCES principals(identity),
    updated_computer TEXT NOT NULL
);
INSERT INTO notes_new SELECT id, project_id, title, body, tags, 1, 0, created_at, updated_at,
    created_principal, created_computer, updated_principal, updated_computer FROM notes;
INSERT INTO sqlite_sequence(name, seq)
SELECT 'notes_new', seq FROM sqlite_sequence WHERE name = 'notes'
    AND NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'notes_new');
UPDATE sqlite_sequence SET seq = MAX(seq, COALESCE(
    (SELECT seq FROM sqlite_sequence WHERE name = 'notes'), 0)) WHERE name = 'notes_new';
DROP TABLE notes;
ALTER TABLE notes_new RENAME TO notes;
CREATE INDEX notes_by_update ON notes(updated_at DESC);
CREATE UNIQUE INDEX shared_note_titles ON notes(project_id, title) WHERE shared = 1;
CREATE UNIQUE INDEX personal_note_titles ON notes(project_id, created_principal, title)
    WHERE shared = 0;
CREATE TRIGGER notes_fts_insert AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, title, body, tags) VALUES (new.id, new.title, new.body, new.tags);
END;
CREATE TRIGGER notes_fts_delete AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, body, tags)
    VALUES ('delete', old.id, old.title, old.body, old.tags);
END;
CREATE TRIGGER notes_fts_update AFTER UPDATE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, body, tags)
    VALUES ('delete', old.id, old.title, old.body, old.tags);
    INSERT INTO notes_fts(rowid, title, body, tags) VALUES (new.id, new.title, new.body, new.tags);
END;
INSERT INTO notes_fts(notes_fts) VALUES ('rebuild');
