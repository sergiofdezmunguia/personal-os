# Runbook: backups y restauración

Toda la información del Personal OS vive en un único fichero SQLite
(`~/.local/share/personal-os/pos.db`). Apple es un espejo: **no** es un backup.

## Qué se hace solo

- `pos sync` crea un backup si el último tiene más de 24 h (`[backup].auto_daily`) y aplica la
  retención: el más reciente de cada uno de los últimos 14 días, 8 semanas y 12 meses.
- Cada backup se **verifica al crearse** (descompresión, sha256, `integrity_check`, filas).
- Si hay copia externa configurada, `pos sync` sube además el último backup **cifrado con age**
  a iCloud Drive (`[backup].offsite_dir`) y aplica allí la misma retención. Si falla (iCloud
  cerrado, carpeta no accesible) avisa y lo reintenta en la siguiente sync.
- `pos doctor` avisa si el último backup tiene más de 48 h, si no se verifica, si está en
  el mismo disco sin copia externa, y comprueba la copia externa y la clave.

## Comandos

```bash
uv run pos backup create [--label antes-de-migrar]   # manual; con etiqueta no se poda nunca
uv run pos backup list
uv run pos backup verify [latest|all|<nombre>]
uv run pos backup drill [<nombre>]                    # simulacro: no toca la base real
uv run pos backup restore <latest|nombre> --yes       # restauración real
uv run pos backup prune                               # local y externa
uv run pos backup offsite                             # sube ya el último si falta fuera
uv run pos backup list --offsite
uv run pos backup verify [latest|all] --offsite       # descifra y verifica
uv run pos backup fetch [latest|<nombre>]             # descifra una copia externa a local
uv run pos backup keygen                              # solo la primera vez
uv run pos doctor
```

## Copia externa cifrada

- Destino: `C:\Users\PULSE\iCloudDrive\PersonalOS-backups` (en WSL,
  `/mnt/c/Users/PULSE/iCloudDrive/PersonalOS-backups`). **Usa la ruta real**, nunca
  `C:\Users\sergio\…` (es un enlace y Windows rechaza los borrados por esa ruta).
- Clave privada: `~/.config/personal-os/backup-identity.txt` (0600). **Debe haber una copia en
  el gestor de contraseñas**: sin ella las copias externas no se pueden descifrar.
- Configuración desde cero: `pos backup keygen` → guardar la clave en el gestor → añadir
  `offsite_dir` y `offsite_recipient` en `[backup]` → `pos backup offsite` → `pos doctor`.
- Descifrar sin el Personal OS:
  `age -d -i backup-identity.txt pos-….db.gz.age | gunzip > pos.db`.

### Recuperación tras perder este PC (o la base y los backups locales)

1. Instalar el Personal OS y restaurar `~/.config/personal-os/` (config) y la clave privada
   desde el gestor de contraseñas (`chmod 600`).
2. `uv run pos backup list --offsite` y `uv run pos backup fetch latest`.
3. `uv run pos backup drill <nombre>` y `uv run pos backup restore <nombre> --yes`.
4. `uv run pos sync` y `uv run pos doctor`.

## Formato

`pos-<UTC>[-etiqueta].db.gz` + `pos-<UTC>[-etiqueta].json` (manifiesto `pos-backup/1`:
sha256 de la base sin comprimir, filas por tabla, migraciones). Es un SQLite estándar
comprimido: se puede abrir sin el Personal OS (`gunzip -k … && sqlite3 pos.db`).
La copia externa es el mismo fichero cifrado con age: `pos-<UTC>[-etiqueta].db.gz.age`, con
el manifiesto `.json` al lado (sin cifrar; no contiene datos personales).

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
