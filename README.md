# Personal OS

Capa personal de inteligencia, automatización y coordinación. Las apps nativas de Apple
(Recordatorios, Calendario) son la interfaz; los datos viven aquí.

**Estado:** vertical slice 1 — Tareas ⇄ Apple Recordatorios y Eventos ⇄ iCloud Calendar.

## Uso rápido

```bash
uv sync
uv run pos init                                   # config + base de datos locales

uv run pos task add "Sacar reciclaje" -d lunes -t 20:00 --every weekly --on MO
uv run pos task add "Llamar al fontanero" -d mañana
uv run pos event add "Dentista" -s "2026-10-06 18:00" -m 45 -a 60 -a 15
uv run pos sync                                   # envía/recibe (Recordatorios + Calendario)

uv run pos task list | pos task show <id> | pos task done <id> | pos task skip <id>
uv run pos event list | pos event edit <id> … | pos event cancel <id>
uv run pos sync status                            # ejecuciones, buzón, enlaces con problemas
uv run pos log                                    # auditoría de eventos de dominio
uv run pos ops                                    # qué se ha enviado a Apple y con qué resultado
uv run pos doctor                                 # diagnóstico completo con pistas de arreglo
uv run pos backup list | create | drill | restore # ver docs/runbooks/backup-restore.md
```

Los ids aceptan prefijo único (`pos task done tsk_01M43`).

### Con Claude (MCP)

Al abrir Claude Code en este repo se ofrece el servidor MCP `personal-os` (`.mcp.json`).
Puedes pedir, por ejemplo: *"recuérdame sacar el reciclaje todos los lunes a las 20:00"* o
*"pon el dentista el martes a las 18:00 con aviso una hora antes"*. Claude usa las mismas
reglas y servicios que la CLI; lo que crea queda auditado con actor `mcp`.

## Documentación

- `CLAUDE.md` — reglas del proyecto (arquitectura, invariantes, comandos).
- `docs/architecture.md` — cómo encaja todo.
- `docs/adr/` — decisiones (SQLite, bridge Apple, recurrencias, conflictos).
- `docs/protocol-reminders-v1.md` — contrato PC ⇄ iPhone.
- `docs/setup/iphone.md` — pasos manuales en iPhone y Windows.
- `docs/runbooks/backup-restore.md` — backups y restauración.

## Tests

```bash
uv run pytest -q                       # unitarios + e2e (bridge JS real vía Node)
POS_LIVE=1 uv run pytest -m live -v    # contra iCloud real
```
