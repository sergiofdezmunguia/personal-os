# ADR 0005 — Backups de SQLite verificados y restauración probada

**Estado:** aceptado · 2026-10-04

## Contexto
Toda la información vive en un fichero SQLite local. No hay servidores ni procesos 24/7.
Apple contiene solo una parte (lo abierto/reciente) y no sirve como backup.

## Decisión
- API de backup online de SQLite (consistente con WAL) → `.db.gz` + manifiesto con sha256,
  filas por tabla y migraciones. Verificación inmediata tras escribir.
- Backup automático diario disparado por `pos sync` (no hay cron fiable en WSL).
- Retención abuelo-padre-hijo (14 días / 8 semanas / 12 meses); etiquetados manuales nunca
  se podan; `pre-restore` conserva los 5 últimos.
- Restauración segura: verificar → copia pre-restore (o copia bruta si la base está dañada)
  → eliminar WAL/SHM → reemplazo atómico → migrar hacia delante.
- `pos backup drill` restaura en un directorio temporal y lee con los servicios de dominio.
- `pos doctor` vigila antigüedad, verificabilidad y ubicación de los backups.
- El módulo vive en `core` (solo depende de db/reloj) para que `bootstrap` pueda llamarlo.

## Consecuencias / pendiente
- Por defecto los backups quedan en el mismo disco: `doctor` lo avisa hasta configurar un
  destino externo.
- Sin cifrado todavía. **Requisito antes de Finanzas con datos reales**: cifrado de backups
  (p. ej. `age`, clave fuera del repo) si el destino es la nube.
