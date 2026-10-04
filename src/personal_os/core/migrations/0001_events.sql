-- Log de eventos de dominio: append-only. Es también la auditoría de cambios internos.
CREATE TABLE events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    id             TEXT NOT NULL UNIQUE,
    type           TEXT NOT NULL,
    entity_type    TEXT NOT NULL,
    entity_id      TEXT NOT NULL,
    actor          TEXT NOT NULL,          -- cli | apple | system | <modulo>
    correlation_id TEXT,                   -- agrupa los eventos de una misma operación/sync
    payload        TEXT NOT NULL,          -- JSON
    occurred_at    TEXT NOT NULL
);
CREATE INDEX events_entity ON events(entity_type, entity_id);
CREATE INDEX events_type ON events(type);
CREATE INDEX events_correlation ON events(correlation_id);
