# ADR 0001 — Monolito modular en Python + SQLite

**Estado:** aceptado · 2026-10-04

## Contexto
Sistema personal, un usuario, mantenido durante años, que empezará con tareas/calendario y
crecerá (finanzas, comidas, inversiones…). No se quiere nada desplegado 24/7 todavía.

## Decisión
- Un único paquete Python (`uv`), módulos con fronteras impuestas por `import-linter`.
- SQLite local (WAL) con migraciones SQL por módulo (`<modulo>/migrations/NNNN_*.sql`).
- Sin Postgres, Docker, workers, colas ni MCP en este slice.

## Consecuencias
- Cero operación; backup = copiar un fichero.
- Las interfaces (servicios, ports, eventos) permiten migrar a Postgres o extraer servicios
  sin tocar el dominio si algún día hace falta.
