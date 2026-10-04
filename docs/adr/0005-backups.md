# ADR 0005 — Backups de SQLite verificados y restauración probada

**Estado:** aceptado · 2026-10-04 · ampliado 2026-10-05 (copia externa cifrada)

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

## Copia externa cifrada (2026-10-05)
- Cada backup local se sube cifrado con **age** (`pos-….db.gz.age`, X25519) a
  `[backup].offsite_dir`, junto a su manifiesto sin cifrar (solo sha256, filas por tabla y
  migraciones). Escritura atómica y relectura byte a byte.
- Destino elegido: **iCloud Drive** (`C:\Users\PULSE\iCloudDrive\PersonalOS-backups`), viable
  tras corregir el perfil de Windows (ver `docs/investigations/2026-10-04-icloud-drive-backups.md`).
  Se descartaron OneDrive (no activo; mismo mecanismo de Windows), disco externo (depende de
  estar conectado) y rclone + B2 (dependencia y cuenta nuevas sin necesidad).
- La clave pública vive en la config (no es secreta): subir no requiere la privada.
  La privada es un fichero de identidad age estándar (`backup-identity.txt`, 0600, fuera del
  repo) con copia en el gestor de contraseñas del usuario. Se descifra sin el Personal OS con
  el binario `age`.
- `pos sync` sube el último backup local si falta fuera (idempotente: un fallo se reintenta en
  la siguiente sync, sin interrumpirla) y aplica la misma retención en el destino.
- `pos backup fetch` descifra una copia externa al directorio local para `drill`/`restore`.
  `restore` nunca lee directamente un `.age`.
- `pos doctor` comprueba destino accesible, ruta sin enlaces simbólicos, clave presente,
  0600 y correspondiente al destinatario, antigüedad y que la última copia se descifra y
  verifica.

## Consecuencias
- **Perder la clave privada = perder las copias externas.** La copia local sigue sin cifrar
  (mismo disco y usuario que la base de datos, que tampoco lo está).
- El cifrado con age cumple el requisito previo a Finanzas con datos reales.
- Las rutas de iCloud deben ser las reales (`/mnt/c/Users/PULSE/...`), nunca las del enlace.
