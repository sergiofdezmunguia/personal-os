# ADR 0010 — Open banking con Enable Banking

**Estado:** aceptado · 2026-11-02

## Contexto
Hasta ahora los movimientos llegaban con extractos que el usuario exportaba a mano (Excel de
Santander, CSV y PDF de Trade Republic), así que el panel siempre iba días o semanas por
detrás. El usuario quiere ver sus finanzas al día sin exportar nada y sin pagar servidor ni
API. El ADR 0007 aparcó PSD2 como fuente futura: GoCardless (Nordigen) ya no admite altas;
Enable Banking es gratis para uso personal y cubre Santander España y Trade Republic.

Prueba real (2026-11-02): Santander da `entry_reference` y concepto, sin saldo por
movimiento; TR da solo fecha e importe con signo (6 decimales), sin id ni concepto, y como
mucho los 100 últimos. Ambos devuelven el IBAN, que casa con las cuentas ya importadas, y un
saldo contable (`CLBD` / `OTHR`).

## Decisión
- **Capas**: `adapters/enablebanking.py` solo hace HTTP (JWT RS256 firmado con la clave de la
  aplicación); `finance/importers/enablebanking.py` es un parser puro como los de ficheros;
  `bootstrap.bank_sync` orquesta y entra en `pos sync` después de la bandeja.
- **Cuenta destino**: la única ya existente de esa entidad; no se consulta `/details` (ahorra
  cupo). Sin cuenta previa, error: primero se importa un extracto.
- **Cupo PSD2**: como mucho una consulta cada 5 h (la sync corre 3 veces al día) y ventana
  de 30 días. Un fallo (caducada, 429, red) va al informe y nunca para la sync.
- **Saldo**: se reconstruye hacia atrás desde el saldo contable. Se contrasta con el saldo
  diario ya importado: diferencia constante ⇒ se acepta con aviso (el historial importado
  partía de otro saldo) y manda el banco; diferencia variable ⇒ se rechaza todo.
- **Sin id del banco** ⇒ id provisional `prov:<hash(fecha, importe, ordinal)>`. Un movimiento
  provisional no se duplica si ya hay uno de cualquier fuente con el mismo importe a ±3 días
  (primero el mismo día). Un id real posterior (CSV de TR) adopta al provisional.
- Solo movimientos contabilizados (`BOOK`).

## Consecuencias
- Santander deja de necesitar el Excel. TR sigue necesitando el CSV para conceptos,
  operaciones de inversión e ids definitivos; el open banking da el día a día y el saldo.
- Los movimientos de TR llegan «sin concepto» hasta que se crucen con los pagos de Apple Pay
  (`applepay/pagos.txt`, siguiente paso) o llegue el CSV.
- La autorización caduca (Santander ~180 días, TR ~90): la sync avisa 14 días antes.
- Riesgo aceptado: dos cargos del mismo importe a pocos días, uno ya importado y otro no,
  podrían emparejarse mal. El cuadre de saldos detecta los casos que descuadran.
