# Runbook: backups y restauración

Toda la información del Personal OS vive en un único fichero SQLite
(`~/.local/share/personal-os/pos.db`). Apple es un espejo: **no** es un backup.

## Qué se hace solo

- `pos sync` crea un backup si el último tiene más de 24 h (`[backup].auto_daily`) y aplica la
  retención: el más reciente de cada uno de los últimos 14 días, 8 semanas y 12 meses.
- Cada backup se **verifica al crearse** (descompresión, sha256, `integrity_check`, filas).
- `pos doctor` avisa si el último backup tiene más de 48 h, si no se verifica o si está en
  el mismo disco que la base de datos.

## Comandos

```bash
uv run pos backup create [--label antes-de-migrar]   # manual; con etiqueta no se poda nunca
uv run pos backup list
uv run pos backup verify [latest|all|<nombre>]
uv run pos backup drill [<nombre>]                    # simulacro: no toca la base real
uv run pos backup restore <latest|nombre> --yes       # restauración real
uv run pos backup prune
uv run pos doctor
```

## Formato

`pos-<UTC>[-etiqueta].db.gz` + `pos-<UTC>[-etiqueta].json` (manifiesto `pos-backup/1`:
sha256 de la base sin comprimir, filas por tabla, migraciones). Es un SQLite estándar
comprimido: se puede abrir sin el Personal OS (`gunzip -k … && sqlite3 pos.db`).

## Procedimiento de restauración

1. **Diagnóstico**: `uv run pos doctor`. Si la base está dañada, lo indicará.
2. **Elegir backup**: `uv run pos backup list` (y `verify <nombre>` si hay dudas).
3. **Simulacro** (recomendado): `uv run pos backup drill <nombre>`.
4. **Restaurar**: `uv run pos backup restore <nombre> --yes`
   - Verifica el backup antes de tocar nada; si no es válido, no restaura.
   - Guarda el estado actual como `…-pre-restore.db.gz` (se conservan los 5 últimos). Si la
     base actual está dañada, se guarda tal cual como `…-damaged.db`.
   - Bloquea la sincronización, elimina `-wal`/`-shm` obsoletos, sustituye atómicamente y
     aplica las migraciones pendientes (un backup antiguo se pone al día).
5. **Reconciliar**: `uv run pos sync`.
   - Lo que se hizo en el iPhone después del backup (completar, editar) se detecta y aplica.
   - Lo creado en el Personal OS después del backup se pierde (está en el `pre-restore`).
   - Recordatorios/eventos creados por el Personal OS tras el backup aparecerán como
     "nuevos" en la lista/calendario y se **adoptarán**; revisa duplicados con `pos task list`.
6. **Comprobar**: `uv run pos doctor`.

### Deshacer una restauración

`uv run pos backup restore <…-pre-restore.db.gz> --yes`

## Simulacro periódico

Una vez al mes (o antes de cambios grandes): `uv run pos backup drill`. Se ejecutó con éxito
sobre la base real el 2026-10-04, junto con una restauración real en el sitio (filas
idénticas antes y después).

## Pendiente (decisión del usuario)

- **Copia fuera de este disco**: configurar `[backup].dir` en una carpeta sincronizada
  (p. ej. OneDrive). Ojo: iCloud Drive desde Windows no permite borrar, y la poda fallaría.
- **Cifrado**: hoy los backups no están cifrados. Antes de guardar datos financieros reales,
  y sobre todo si van a la nube, se añadirá cifrado (p. ej. `age`). Ver ADR 0005.
