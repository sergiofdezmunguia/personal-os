# ADR 0008 — Finanzas v3: inversiones y patrimonio neto (Trade Republic)

**Estado:** aceptado · 2026-10-13

## Contexto
El usuario invierte con planes semanales en ETF de Trade Republic (y alguna compra suelta).
Quiere ver lo aportado por mes y si se cumple el plan, la rentabilidad de la cartera y su
patrimonio neto total (Santander + efectivo de TR + inversiones) mes a mes. El PDF «Extracto
de cuenta» de TR (ADR 0006) no trae posiciones ni valoración.

TR ofrece dos ficheros que sí lo cubren:
- **CSV «Exportación de transacción»**: todo el historial de la cuenta desde la apertura,
  una fila por movimiento con `transaction_id`, tipo (tarjeta, transferencia, compra,
  venta, dividendo, intereses, saveback…), ISIN, participaciones, precio, comisión e
  impuestos. Verificado con datos reales: la suma de `amount + fee + tax` desde el principio
  coincide al céntimo con el efectivo del PDF de patrimonio, y 330 de los 331 movimientos ya
  importados del PDF de cuenta casan en fecha e importe (el otro, con un día de diferencia).
- **PDF «Extracto del patrimonio neto»**: posiciones a una fecha (unidades, precio, valor,
  ISIN), efectivo y total.

## Decisión
- **Dentro del módulo `finance`** (`finance/investments.py`), no un módulo hermano: el
  patrimonio neto cruza efectivo e inversiones y los módulos hermanos no se importan entre sí.
- **El CSV sustituye al PDF de cuenta como fuente del efectivo de TR.** Sus movimientos
  pasan por `FinanceService.import_statement` con `external_id`; los que ya existían (sin
  id, del PDF) se **enlazan** —mismo importe y fecha a ±3 días— y conservan descripción,
  categoría y saldo. Antes de enlazar se contrasta el saldo al cierre de los días comunes
  (≥ 80 % deben coincidir): una exportación parcial (saldo desde cero incorrecto) se
  rechaza. Una vez hay movimientos con id, el PDF de cuenta de TR se rechaza (duplicaría).
  Las descripciones llevan el prefijo del PDF («Transacción con tarjeta: …», «Operar: …»)
  para que las reglas existentes sigan funcionando. Nunca se leen las columnas de
  contraparte (nombre, IBAN).
- **Operaciones** (`fin_trades`, migración 0005): compras, ventas y dividendos con
  participaciones y precios como texto decimal exacto; deduplicadas por `transaction_id`.
  Las compras del plan se reconocen por «Savings plan execution».
- **Valor de mercado solo desde la valoración que sube el usuario** (`fin_valuations`). Sin
  precios de internet: nada de dependencias externas ni red en el sync. Al importar se
  contrastan participaciones y efectivo con lo importado (avisos, no errores). Entre
  valoraciones, las inversiones cuentan **a coste**, y se dice.
- **Coste por precio medio ponderado**; las ventas materializan resultado.
- **Plan deducido, no configurado**: por ISIN, importe y ejecuciones/mes más repetidos y
  mediana de lo invertido al mes en los últimos 3 meses completos. Un mes cumple si cada
  ISIN recibe ≥ 90 % de lo habitual; solo se evalúan esos meses (antes el plan pudo ser otro).
- **Patrimonio neto a fin de mes**: saldo de cada cuenta + inversiones (a mercado si hay
  valoración de la última semana del mes, más lo comprado después a coste; si no, a coste).
  Los meses sin datos de alguna cuenta se marcan y la CLI los omite.
- Interfaces: `pos finance portfolio | contributions | networth`, MCP `investments_overview`
  (solo lectura) y aviso del doctor si la última valoración tiene más de 40 días.

## Alternativas descartadas
- **Precios por ISIN de una fuente pública**: automático, pero frágil (APIs que cambian,
  límites) y mete red en el sync. Se puede añadir más adelante como fuente opcional.
- **Seguir con el PDF de cuenta + CSV solo para inversiones**: dos fuentes del mismo efectivo
  y ningún id estable; el CSV es más fiable.
- **Configurar el plan a mano**: se desactualiza; deducirlo de los datos no.

## Consecuencias
- El historial anterior al primer PDF importado entra de golpe (cientos de movimientos de
  meses ya pasados): las reglas activas categorizan la mayoría; el resto queda sin categoría
  y se puede categorizar en una pasada (Claude) o dejar, porque solo afecta a esos resúmenes.
- Hay que exportar el CSV completo (no un rango de fechas) y subir el PDF de patrimonio a
  fin de mes para que el patrimonio neto vaya a valor de mercado.
- Solo EUR: otra divisa en el CSV se rechaza.
