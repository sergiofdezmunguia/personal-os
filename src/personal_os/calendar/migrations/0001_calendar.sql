-- Eventos de calendario. Dueño: módulo calendar.
CREATE TABLE calendar_events (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    notes         TEXT NOT NULL DEFAULT '',
    location      TEXT NOT NULL DEFAULT '',
    starts_at     TEXT NOT NULL,                -- "YYYY-MM-DDTHH:MM" local, o "YYYY-MM-DD" si all_day
    ends_at       TEXT NOT NULL,
    all_day       INTEGER NOT NULL DEFAULT 0,
    timezone      TEXT NOT NULL,
    rrule         TEXT,
    alerts        TEXT NOT NULL DEFAULT '[]',   -- JSON: minutos antes del inicio
    status        TEXT NOT NULL CHECK (status IN ('confirmed','cancelled')),
    origin_module TEXT NOT NULL,
    origin_ref    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX calendar_events_start ON calendar_events(starts_at);
