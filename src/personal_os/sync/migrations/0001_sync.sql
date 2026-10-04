-- Estado de sincronización con proveedores externos. Dueño: módulo sync.

-- Relación entidad interna <-> objeto externo.
CREATE TABLE external_links (
    provider         TEXT NOT NULL,            -- apple_reminders | apple_calendar
    entity_type      TEXT NOT NULL,            -- task_occurrence | calendar_event
    entity_id        TEXT NOT NULL,
    external_id      TEXT,                     -- identifier EventKit / href CalDAV
    etag             TEXT,
    last_pushed_hash TEXT,                     -- hash del estado que enviamos
    last_pushed_state TEXT,                    -- JSON del estado que enviamos (detección de conflictos por campo)
    pushed_at        TEXT,
    last_seen_hash   TEXT,                     -- hash del último estado observado en el proveedor
    last_seen_state  TEXT,                     -- JSON del último estado observado
    last_seen_marker TEXT,                     -- marcador observado (Recordatorios)
    sync_state       TEXT NOT NULL CHECK (sync_state IN ('pending_push','pushed','synced','conflict','orphaned','error')),
    pending_op_id    TEXT,
    last_error       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (provider, entity_type, entity_id)
);
CREATE UNIQUE INDEX external_links_external ON external_links(provider, external_id) WHERE external_id IS NOT NULL;

-- Registro de toda operación enviada a un proveedor (qué ha creado/modificado el Personal OS).
CREATE TABLE external_operations (
    op_id        TEXT PRIMARY KEY,
    provider     TEXT NOT NULL,
    operation    TEXT NOT NULL,                -- upsert | delete | put | ...
    entity_type  TEXT NOT NULL,
    entity_id    TEXT NOT NULL,
    external_id  TEXT,
    batch_id     TEXT,
    request      TEXT NOT NULL,                -- JSON
    status       TEXT NOT NULL,                -- pending | sent | created | applied | deleted | conflict | not_found | error | superseded
    result       TEXT,                         -- JSON
    correlation_id TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX external_operations_entity ON external_operations(entity_type, entity_id);
CREATE INDEX external_operations_batch ON external_operations(batch_id);

-- Ejecuciones de sincronización.
CREATE TABLE sync_runs (
    id          TEXT PRIMARY KEY,
    provider    TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,                 -- running | ok | error
    stats       TEXT NOT NULL DEFAULT '{}',
    error       TEXT
);

-- Valores de cursor por proveedor (p. ej. último snapshot procesado).
CREATE TABLE sync_cursors (
    provider TEXT NOT NULL,
    key      TEXT NOT NULL,
    value    TEXT NOT NULL,
    PRIMARY KEY (provider, key)
);

-- Ficheros/objetos de entrada ya procesados (el buzón de iCloud no se puede borrar desde Windows).
CREATE TABLE sync_inbox_processed (
    provider     TEXT NOT NULL,
    kind         TEXT NOT NULL,                -- ack | snapshot
    item_id      TEXT NOT NULL,                -- batch_id | run_id
    processed_at TEXT NOT NULL,
    PRIMARY KEY (provider, kind, item_id)
);
