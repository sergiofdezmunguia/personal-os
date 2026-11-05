# Arquitectura

```
                      ┌──────────────────────────── PERSONAL OS (WSL) ─────────────────────────────┐
 Claude Code ──MCP──▶ │ INTERFACES  CLI `pos` · servidor MCP `pos-mcp` · skills (cierre mensual)   │
 (skills, hooks)      │ OPS         doctor · backups (age) · drill · panel «Pulso del mes»         │
                      │ BOOTSTRAP   composition root: conecta servicios, adaptadores y config      │
                      │ ADAPTERS    buzón JSON (Recordatorios) · CalDAV + iCalendar                │
                      │ SYNC        RemindersSync · CalendarSync · enlaces · operaciones · runs    │
                      │ DOMINIO     tasks · calendar · finance (extractos, reglas, cierre,         │
                      │             inversiones, patrimonio, análisis de gasto)                    │
                      │ CORE        SQLite · migraciones · eventos/auditoría · config · secretos   │
                      └────▲──────────────────────┬──────────────────────────┬─────────────────────┘
  Programador de tareas    │ `pos sync`           │ ficheros                 │ HTTPS CalDAV
  de Windows (3×/día) ─────┘                      ▼                          ▼
                     iCloud Drive (iCloud para Windows)                caldav.icloud.com
                     · Scriptable/personal-os: buzón + panel                  │
                     · PersonalOS-extractos: bandeja de extractos       iCloud Calendar
                     · PersonalOS-backups: copias cifradas (age)        → Calendario (avisos)
                                     │
        iPhone: Atajos (3×/día y al cerrar Recordatorios) → Scriptable «Personal OS Sync»
                  ⇄ EventKit ⇄ Recordatorios
                Scriptable «Personal OS Panel» → panel con JavaScript
                Archivos → subir extractos (y ver el panel sin JavaScript)
```

Flujos principales:

- **Tareas y eventos**: CLI o Claude (MCP) → dominio → `pos sync` → buzón (Recordatorios) o
  CalDAV (Calendario). Detalle abajo.
- **Finanzas**: el usuario deja extractos (Santander `.xls/.xlsx`; Trade Republic CSV y PDF de
  patrimonio) en la bandeja → `pos sync` los importa (idempotente), categoriza por reglas y
  regenera el panel. Claude propone reglas y categoriza lo suelto en el cierre mensual
  (ADR 0006–0009).
- **Operación**: `pos sync` hace un backup diario cifrado fuera del disco; `pos doctor`
  revisa todo (sync, bridge, backups, extractos, inversiones).

Pendiente: open banking (PSD2) como fuente automática de movimientos (ADR 0007).

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
