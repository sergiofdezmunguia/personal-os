# ADR 0007 — Finanzas v2: bandeja de extractos y cierre mensual asistido

**Estado:** aceptado · 2026-10-06

## Contexto
Con la v1 el usuario descarga extractos e importa a mano. Quiere mandarlos una vez al mes y
que Claude gestione la categorización, comprobar que Trade Republic paga lo prometido
(intereses sobre el efectivo y saveback) y, más adelante, conectar el banco. Sin servicios
24/7, sin app propia y sin Telegram; el iPhone solo tiene iCloud Drive y Atajos.

## Decisión
- **Bandeja en iCloud Drive** (`[finance].inbox_dir`): el iPhone guarda ahí los ficheros con
  «Guardar en Archivos». `finance/inbox.py` importa (mismo camino idempotente que la CLI) y
  mueve a `importados/AAAA-MM/`; lo que falla se queda con el motivo. Se ejecuta en
  `pos sync` (determinista, sin LLM) y con `pos finance inbox`. Nunca borra.
- **Cierre mensual** (`finance/close.py`, solo lectura): cobertura por cuenta, pendientes,
  comparación con el mes anterior y contraste del rendimiento del efectivo. Lo abonado por
  el mes M se busca en M+1 (intereses en los primeros 10 días). Tipos configurables, porque
  cambian (los intereses de TR siguen al BCE) y el banco aplica retenciones y topes que el
  extracto no detalla: el resultado es una estimación, no una alarma.
- **Claude categoriza lo suelto**: `categorize_transactions` (MCP) pone la categoría como
  manual con `categorized_by = 'mcp'` (migración 0003). Lo recurrente sigue yendo por reglas
  propuestas que aprueba el usuario. Claude nunca cambia lo que el usuario puso a mano, y lo
  que categoriza se revisa con `pos finance list --by-claude`.
- **Skill de Claude Code `monthly-close`** con el procedimiento (privacidad, cuándo regla y
  cuándo categoría directa, cuándo preguntar). Recordatorio mensual (tarea recurrente el
  día 3) para subir los extractos.
- `intereses` y `bonificaciones` pasan a ser categorías sembradas (migración 0004).

## Alternativas descartadas (por ahora)
- **Open banking (PSD2) vía agregador**: automatizaría la descarga, pero exige alta en un
  proveedor, consentimiento renovable cada 180 días y credenciales en el PC. Encaja como
  otra fuente de `ParsedStatement` cuando se aborde; la categorización y el cierre no cambian.
- Leer los extractos desde el correo: el usuario no los recibe por correo.

## Consecuencias
- Los extractos archivados quedan sin cifrar en iCloud Drive hasta que el usuario los borre.
- La cobertura «completa» se da por buena si se importó después de fin de mes, aunque el
  último movimiento sea anterior (meses tranquilos).
