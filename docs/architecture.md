# Arquitectura — vertical slice Tareas + Calendario con Apple

```
                ┌────────────────────────── PERSONAL OS (WSL) ──────────────────────────┐
                │  CLI (pos)                                                             │
                │    │                                                                   │
                │  DOMINIO  tasks (Task, Occurrence, recurrencias)   calendar (Event)    │
                │    │            ▲                                         ▲            │
                │  SYNC   RemindersSync (asíncrono)          CalendarSync (síncrono)     │
                │         links · operations · inbox ledger · runs · conflictos          │
                │    │                                                                   │
                │  ADAPTERS  reminders_mailbox (pos-reminders/1)   calendar_caldav+ical   │
                │  CORE      SQLite · migraciones · eventos/auditoría · config · secretos │
                └──────────────┬───────────────────────────────────────────┬────────────┘
                               │ ficheros JSON                             │ HTTPS CalDAV
                 iCloud Drive (iCloud para Windows)                 caldav.icloud.com
                               │                                           │
         iPhone: Atajos (automatización) → Scriptable                iCloud Calendar
                 "Personal OS Sync" ⇄ EventKit ⇄ Recordatorios       → Calendario (avisos)
```

## Por qué dos canales

| | Recordatorios | Calendario |
|---|---|---|
| API desde Linux | Ninguna (salieron de CalDAV en iOS 13) | CalDAV estándar |
| Canal | Buzón de ficheros + bridge en el iPhone | HTTP directo |
| Latencia | Hasta la próxima ejecución del bridge | Segundos |
| Uso | Tareas (con o sin hora) | Citas con hora concreta / días completos |

## Flujo de una tarea

1. `pos task add …` → `tasks` + `task_occurrences` + eventos `task.created`.
2. `pos sync` → `RemindersSync._push` calcula el estado deseado de cada ocurrencia, escribe
   `outbox/batch-*.json` y registra cada comando en `external_operations`.
3. En el iPhone, una automatización ejecuta *Personal OS Sync*: aplica el lote con
   precondiciones, escribe `ack-*.json` y `snapshot-*.json`, borra el lote.
4. `pos sync` → procesa ack (enlaza `identifier`) y snapshot (detecta completadas,
   ediciones, borrados, recordatorios nuevos). Completar ⇒ siguiente ocurrencia ⇒ nuevo lote.

## Modelo de sincronización

`external_links` guarda por entidad: `external_id`, `etag`, `last_pushed_state/hash`
(lo que enviamos) y `last_seen_state/hash` (lo último observado en Apple).

- Cambio en Apple = observado ≠ `last_seen`.
- Cambio local pendiente = deseado ≠ `last_pushed`.
- Ambos en el mismo campo = conflicto (gana Apple, se registra).

Idempotencia: `op_id` por comando, `batch_id` procesados en el iPhone (almacenamiento local),
`sync_inbox_processed` en el PC, `If-Match`/`If-None-Match` en CalDAV, lote ya aplicado ⇒
no-op en el bridge.

## Determinista vs. LLM

Todo el slice es determinista. Un LLM podrá vivir **por encima** de los servicios (p. ej.
convertir lenguaje natural en tareas, revisión semanal, decidir tarea vs. evento,
proponer resolución de conflictos), llamando a la CLI o a un futuro MCP como cualquier
otro cliente. Nunca dentro de `sync/`, `adapters/` ni del bridge.
