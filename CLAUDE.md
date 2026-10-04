# Personal OS — guía para Claude Code

Personal OS es la **capa de inteligencia, automatización y coordinación**. Las apps nativas de
Apple (Recordatorios, Calendario) son la **interfaz**. Los datos importantes pertenecen al
Personal OS (SQLite local); Apple es un espejo sincronizado y sustituible.

## Comandos

```bash
uv run pytest -q                      # tests (unit + e2e con el bridge JS real vía Node)
POS_LIVE=1 uv run pytest -m live -v   # integración real con iCloud (usa config/secretos locales)
uv run ruff format src tests && uv run ruff check src tests
uv run lint-imports                   # fronteras arquitectónicas (deben pasar SIEMPRE)
uv run pos --help                     # CLI
```

## Flujo de trabajo

- Ramas: `main` (solo vía PR desde `dev`), `dev` (integración), `feat/…`/`fix/…` desde `dev`.
- Antes de proponer un PR: `./scripts/check.sh` (lo mismo que ejecuta la CI).
- **Nunca commitear, pushear ni abrir PRs sin autorización explícita del usuario.**
- Detalle en `docs/development.md`.

## Arquitectura (no mezclar responsabilidades)

```
cli/ mcp_server/   interfaces (CLI `pos`, servidor MCP `pos-mcp`); sin lógica de negocio
ops/          operación: `doctor` (diagnóstico, solo lectura) y `drill` (simulacro de restore)
bootstrap.py  composition root: conecta todo (solo lo importan las interfaces)
adapters/     comunicación con proveedores (apple/: buzón Recordatorios, CalDAV, iCalendar)
sync/         ports (interfaces + DTOs), motores de sync, enlaces, auditoría de operaciones
tasks/ calendar/   dominio; módulos hermanos independientes
core/         db+migraciones, backups, ids, reloj, eventos, config, secretos, rrule
bridge/scriptable/ ejecutor en el iPhone (JS); docs/protocol-reminders-v1.md es el contrato
```

Reglas impuestas por `lint-imports`:
- Capas: `(cli | mcp_server) → ops → bootstrap → adapters → sync → (tasks | calendar) → core`.
- `tasks` y `calendar` no se importan entre sí.
- `adapters` no importa el dominio: solo `sync.ports` y `core`.

## Invariantes

- **Toda mutación pasa por un servicio de dominio** (`TaskService`, `CalendarService`) con un
  `ChangeContext` (actor `cli` | `apple` | `system`) y deja eventos en `events`.
- **Todo lo enviado a Apple queda en `external_operations`** (qué, cuándo, resultado).
- **Sync 100 % determinista. Nunca un LLM dentro de sync/, adapters/ ni del bridge.**
- Conflictos: gana el iPhone en ediciones humanas; cancelación explícita en el Core se respeta.
  Siempre se registra `sync.conflict_detected`.
- El Personal OS es dueño de la recurrencia de tareas: a Recordatorios solo va la ocurrencia
  abierta (como recordatorio no repetitivo). Eventos de calendario usan RRULE nativa.
- Fechas "de pared" (YYYY-MM-DD / HH:MM) en la zona configurada (`Atlantic/Canary`).
  Instantes en UTC ISO con `Z`. Nunca `datetime.now()` directo: usar `Clock`.
- Ids con prefijo + ULID (`tsk_`, `occ_`, `evt_`, `op_`, `bat_`). El UID iCalendar de
  nuestros eventos es `<evt_id>@personal-os`; el marcador de Recordatorios es `[pos:<occ_id>]`.
- **El PC nunca borra en el buzón de iCloud Drive**: se registran procesados en
  `sync_inbox_processed`; la limpieza la hace el bridge. (Borrar en iCloud Drive sí funciona
  desde que se corrigió el perfil de Windows, y los backups externos lo necesitan; las rutas
  deben ser las reales, `/mnt/c/Users/PULSE/…`.)

## MCP y hooks

- `.mcp.json` registra `personal-os` (`uv run pos-mcp`). Herramientas: `create_task`,
  `list_tasks`, `complete_task`, `create_calendar_event`, `list_calendar_events`,
  `sync_apple`. Actor de auditoría: `mcp`. Nueva herramienta ⇒ solo si aporta algo que las
  existentes no cubren; siempre sobre servicios de dominio y con test en `test_mcp_server.py`.
- `.claude/hooks/` (configurados en `.claude/settings.json`, probados en `test_claude_hooks.py`):
  `guard.py` (bloquea secretos, push a main, force-push, commits en main),
  `format_python.py` (ruff tras cada edición), `check_on_stop.py` (`scripts/check.sh` al
  terminar si hay cambios sin verificar).

## Operación

- `uv run pos doctor` antes de investigar cualquier problema: dice qué falla y cómo arreglarlo.
- Backups: `pos sync` hace uno diario y sube una copia cifrada (age) a iCloud Drive;
  `pos backup create|list|verify|drill|restore|prune|offsite|fetch|keygen`.
  Nunca leer ni mostrar `backup-identity.txt` (clave privada).
  Antes de una migración o cambio arriesgado: `pos backup create --label antes-de-…`.
- Procedimiento completo: `docs/runbooks/backup-restore.md`.
- Nunca restaurar sin `pos backup drill` previo ni sin que el usuario lo pida.

## Secretos y datos

- Config: `~/.config/personal-os/config.toml` · Secretos: `~/.config/personal-os/secrets.toml`
  (0600, `pos secrets set …`) · Datos: `~/.local/share/personal-os/pos.db`.
- Nunca leer, imprimir ni copiar secretos. Nunca escribir datos personales en el repo.
- Los tests aíslan `POS_CONFIG_DIR`/`POS_DATA_DIR` (conftest autouse).

## Cambios habituales

- **Nueva tabla/columna**: nueva migración `NNNN_nombre.sql` en el módulo dueño; nunca editar
  una migración ya aplicada en la máquina del usuario.
- **Cambio del bridge**: editar `bridge/scriptable/personal-os-sync.js`, subir `BRIDGE_VERSION`,
  añadir test e2e, `uv run pos bridge install`. Cambios incompatibles ⇒ `pos-reminders/2`.
- **Nuevo módulo** (finanzas, comidas…): paquete hermano de `tasks/` con `migrations/`,
  `models.py`, `service.py`; registrar migraciones en `cli/wiring.py`; añadirlo a los
  contratos de `pyproject.toml`. Para crear tareas/eventos usa los servicios con
  `origin_module` propio, nunca SQL directo.

## Decisiones

Ver `docs/adr/`. Arquitectura general en `docs/architecture.md`. Pasos manuales del
iPhone/Windows en `docs/setup/iphone.md`.
