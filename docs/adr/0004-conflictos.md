# ADR 0004 — Detección y resolución de conflictos

**Estado:** aceptado · 2026-10-04

| Caso | Resolución |
|---|---|
| Completada en el iPhone | Gana el iPhone; genera la siguiente ocurrencia |
| Desmarcada en el iPhone | Se reabre; la siguiente generada (si sigue abierta) se cancela |
| Mismo campo cambiado en ambos lados | Gana el iPhone; `sync.conflict_detected` (campo, valores) |
| Campos distintos | Se fusionan |
| El bridge encuentra un estado distinto al esperado (precondición) | No aplica; devuelve el estado actual; gana el iPhone |
| Borrado en el iPhone | Se cancela esa ocurrencia/evento (`deleted_externally`); la serie sigue |
| Cancelado en el Core y editado en el iPhone | Se respeta la cancelación (decisión explícita) |
| Recordatorio/evento creado a mano en la lista/calendario Personal OS | Se adopta (`origin_module = apple`) |

Todo conflicto queda en el log de eventos; `pos log --type sync.conflict` los muestra.
