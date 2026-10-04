# ADR 0006 — Finanzas v1: importación de extractos y categorización determinista

**Estado:** aceptado · 2026-10-05

## Contexto
El usuario usa Santander (cuenta corriente) y Trade Republic. No hay API bancaria (PSD2
queda para más adelante) y no queremos conectores frágiles. Los datos son sensibles.

## Decisión
- Módulo `finance/` hermano de `tasks/` y `calendar/` (no se importan entre sí).
- **Importación de ficheros** descargados por el usuario. Parsers puros
  (`finance/importers/`): bytes → `ParsedStatement`. Formato detectado por contenido.
  - Santander: «Descargar movimientos» en Excel (`.xls` BIFF, leído con `xlrd`). Estructura
    documentada en `importers/santander.py`.
  - Trade Republic: exportación CSV nativa de la app (pendiente de muestra del formato).
- **Validación antes de guardar**: el saldo debe encadenar en todo el extracto; si no, se
  rechaza (fichero editado o incompleto). El saldo de cabecera que no cuadra solo avisa
  (compras con tarjeta pendientes).
- **Idempotencia**: fichero repetido (sha256 por cuenta) ⇒ no se hace nada. Cada movimiento
  tiene una huella (cuenta, fechas, importe, saldo, concepto normalizado, ordinal entre
  idénticos) ⇒ extractos solapados solo añaden lo nuevo. Si un extracto empieza después de
  lo importado y el saldo no enlaza, se avisa de un posible hueco.
- **Minimización**: del IBAN solo se guardan su sha256 y los 4 últimos; el titular nunca
  se lee; los números de tarjeta (13-19 dígitos) se enmascaran en el concepto antes de
  guardar. Importes en céntimos enteros.
- **Categorización determinista** por reglas (`contains` sin mayúsculas/acentos o `regex`,
  filtro cargo/abono, prioridad). Lo puesto a mano nunca lo pisan las reglas. Categorías
  fijas sembradas por migración, con tipo `expense | income | transfer`; los traspasos
  (entre cuentas propias, aportaciones a inversión) quedan fuera del neto.
- **Claude propone, el usuario decide**: las reglas creadas por el actor `mcp` nacen
  `proposed` y no se aplican hasta `pos finance rule approve`. Las herramientas MCP de
  finanzas son de solo lectura salvo `propose_category_rule`.

## Consecuencias
- Hay que descargar extractos a mano (`pos doctor` avisa a los 35 días).
- Cambiar la normalización del concepto cambia las huellas: requeriría migrar huellas.
- Prerrequisito cumplido: backups cifrados fuera del disco (ADR 0005).
