-- Tareas (definición/serie) y sus ocurrencias. Dueño: módulo tasks.
CREATE TABLE tasks (
    id                TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    notes             TEXT NOT NULL DEFAULT '',
    due_date          TEXT,                         -- YYYY-MM-DD local (primera ocurrencia)
    due_time          TEXT,                         -- HH:MM local o NULL
    timezone          TEXT NOT NULL,
    rrule             TEXT,                         -- subconjunto RFC 5545 o NULL
    recurrence_anchor TEXT NOT NULL DEFAULT 'schedule' CHECK (recurrence_anchor IN ('schedule','completion')),
    status            TEXT NOT NULL CHECK (status IN ('active','completed','cancelled')),
    origin_module     TEXT NOT NULL,
    origin_ref        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE task_occurrences (
    id            TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL REFERENCES tasks(id),
    seq           INTEGER NOT NULL,
    scheduled_date TEXT,                        -- fecha que dicta la recurrencia (no cambia al reprogramar)
    due_date      TEXT,
    due_time      TEXT,
    status        TEXT NOT NULL CHECK (status IN ('open','completed','skipped','cancelled')),
    completed_at  TEXT,
    completed_via TEXT,
    closed_reason TEXT,                         -- motivo de skipped/cancelled (p. ej. deleted_in_apple)
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (task_id, seq)
);
CREATE INDEX task_occurrences_open ON task_occurrences(status, due_date);
-- Como mucho una ocurrencia abierta por tarea.
CREATE UNIQUE INDEX task_one_open_occurrence ON task_occurrences(task_id) WHERE status = 'open';
