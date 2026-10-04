# Protocolo `pos-reminders/1` — buzón Personal OS ⇄ iPhone

Contrato entre el **Apple adapter** (`src/personal_os/adapters/apple/reminders_mailbox.py`)
y el **bridge** que corre en el iPhone (`bridge/scriptable/personal-os-sync.js`).
Cualquier ejecutor futuro (Atajos puro, Mac, app propia) debe cumplir este mismo contrato.

## Transporte

Una carpeta en iCloud Drive (dentro de la carpeta de Scriptable):

```
personal-os/
├── outbox/   batch-<batch_id>.json      escribe: Personal OS   lee: bridge
└── inbox/    ack-<batch_id>.json        escribe: bridge        lee: Personal OS
              snapshot-<run_id>.json     escribe: bridge        lee: Personal OS
```

- Escrituras atómicas: se escribe `*.tmp` y se renombra. Los lectores ignoran todo lo que no
  case exactamente con `^(batch|ack|snapshot)-[A-Za-z0-9_]+\.json$` (p. ej. copias de
  conflicto de iCloud como `batch-x 2.json`).
- **El PC nunca borra** (iCloud para Windows no permite borrar ficheros de iCloud Drive):
  registra en SQLite qué `ack`/`snapshot` ya procesó y los ignora si reaparecen.
- **La limpieza la hace el bridge**: borra cada `batch` tras procesarlo, conserva los 3
  snapshots más recientes y borra los `ack` con más de 14 días. Si un ack se pierde, el
  snapshot permite re-enlazar por marcador.
- El bridge guarda en su almacenamiento **local** (no iCloud) los `batch_id` ya
  procesados, para no reaplicar un lote si iCloud propaga los borrados fuera de orden.
- Los lotes se procesan en orden lexicográfico de nombre (los `batch_id` son ULID ⇒ orden
  temporal).

## Representación de un recordatorio (`state`)

```json
{ "title": "Sacar reciclaje",
  "notes": "texto libre, SIN el marcador",
  "due": { "date": "2026-10-06", "time": "20:00" },   // o null; time null = sin hora
  "is_completed": false }
```

Fechas y horas en hora local del dispositivo (que debe coincidir con `timezone` del Personal OS;
el snapshot informa de `device_timezone`).

## Marcador

El bridge añade al final de las notas una línea `[pos:<id>]` (p. ej. `[pos:occ_01J…]`).
Sirve para re-enlazar si el `identifier` de EventKit cambia. En `state.notes` nunca aparece.

## Lote (`outbox/batch-<batch_id>.json`)

```json
{ "protocol": "pos-reminders/1",
  "batch_id": "bat_01J…",
  "created_at": "2026-10-04T10:00:00Z",
  "list_name": "Personal OS",
  "commands": [
    { "op_id": "op_01J…", "type": "upsert", "marker": "occ_01J…",
      "external_id": null,               // o el identifier conocido
      "expected": null,                  // o el último `state` observado (precondición)
      "fields": { …state… } },
    { "op_id": "op_01J…", "type": "delete", "marker": "occ_01J…",
      "external_id": "…", "expected": { …state… } }
  ] }
```

Semántica en el bridge:

1. Localizar el recordatorio en la lista: por `external_id`; si no, por marcador.
2. `upsert`:
   - no existe y `expected == null` → **crear** (`created`);
   - no existe y `expected != null` → `not_found` (se borró en el iPhone; decide el Core);
   - existe y su estado == `fields` → `applied` (no-op, idempotente);
   - existe, `expected != null` y estado ≠ `expected` → `conflict` (no se toca);
   - en otro caso → actualizar (`applied`).
3. `delete`: no existe → `not_found`; estado ≠ `expected` (si se da) → `conflict`; si no → `deleted`.

## Ack (`inbox/ack-<batch_id>.json`)

```json
{ "protocol": "pos-reminders/1", "batch_id": "bat_…", "run_id": "run_…",
  "processed_at": "…Z",
  "results": [ { "op_id": "op_…", "status": "created|applied|deleted|conflict|not_found|error",
                 "marker": "occ_…", "external_id": "…" | null,
                 "state": { …state actual… } | null, "error": null | "mensaje" } ] }
```

## Snapshot (`inbox/snapshot-<run_id>.json`)

Estado de la lista tras aplicar los lotes: recordatorios incompletos + completados en los
últimos `window_days` días.

```json
{ "protocol": "pos-reminders/1", "run_id": "run_…", "taken_at": "…Z",
  "list_name": "Personal OS", "list_found": true, "device_timezone": "Europe/Madrid",
  "window_days": 90, "applied_batches": ["bat_…"],
  "reminders": [ { "external_id": "…", "marker": "occ_…" | null,
                   "state": { … }, "completion_date": "…Z" | null,
                   "creation_date": "…Z" | null } ] }
```
