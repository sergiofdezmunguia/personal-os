# 0011 — Analista de gastos y avisos en el iPhone

Fecha: 2026-11-05 · Estado: aceptada

## Contexto

Con open banking y el registro de Apple Pay los movimientos llegan solos. Faltaba que el
sistema **avise** sin tener que abrir el panel: lo gastado ayer, la semana, las subidas de
cuotas y los cobros raros. Restricciones: 0 € (sin servidor ni APNs), datos financieros solo
en el PC y en iCloud del usuario, nunca un LLM en la sync.

## Decisión

- `finance/analyst.py` (solo lectura, determinista): cobros **recurrentes** detectados por
  patrón (mensual: ≤ 1 cobro por mes natural, 25–35 días entre cobros; anual: 350–380 días),
  **subidas** solo sobre importes antes estables (±3 %), **posibles duplicados** (mismo
  comercio, importe y día) y resúmenes de un periodo (`spend`, `week`). Agrupa por
  `series_key` (sin números, meses ni «recibo/cuota», que cambian cada mes) y separa por importe
  cuando un comercio tiene varias cuotas.
- `ops/notices.py` escribe `<buzón>/panel/avisos.json` en cada `pos panel`/`pos sync`: avisos
  con id estable y ventana `[deliver_after, expires]` (buenos días 07:00–12:30, semana el
  domingo 19:00 → lunes 11:00, alertas 3 días).
- En el iPhone, `Personal OS Avisos.js` (Scriptable) los muestra como notificación local, una
  vez cada uno, solo dentro de su ventana. Lo llama `Personal OS Sync` al terminar (las
  automatizaciones ya existentes, 3 veces al día): no hay que crear otra automatización. Al de
  buenos días le añade la agenda de hoy leída del Calendario y Recordatorios del iPhone.
- `pos finance recurring|week`, MCP `finance_recurring` y `name_unlabeled_transaction` (pone
  el comercio, p. ej. desde un recibo de Gmail, **solo** a movimientos sin concepto).

## Consecuencias

- Sin PC encendido no hay avisos nuevos (igual que el panel). Los caducados no se muestran.
- La hora real de cada aviso depende de cuándo corren las automatizaciones del iPhone.
- Las heurísticas pueden fallar con cobros irregulares; los umbrales están en `analyst.py`.
