CREATE TABLE task_edit_claims (
    task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    assigned TEXT NOT NULL,
    previous TEXT
);
