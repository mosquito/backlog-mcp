ALTER TABLE tasks ADD COLUMN assigned TEXT;
CREATE INDEX tasks_by_assigned ON tasks(assigned COLLATE NOCASE);
