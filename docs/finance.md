# Finanzas: uso

## Cada mes (flujo normal)

1. El día 3 te salta el recordatorio «Subir extractos del mes».
2. Descarga los extractos (ver abajo) y guárdalos desde el iPhone en
   **Archivos → iCloud Drive → PersonalOS-extractos** (o cópialos ahí desde el PC).
3. En el PC, pide a Claude **«cierre de mes»** (skill `monthly-close`). Claude:
   importa la bandeja, categoriza lo pendiente (propone reglas para lo que se repite,
   categoriza directamente los comercios sueltos y te pregunta los dudosos), comprueba
   intereses y saveback, y te resume el mes frente al anterior.
4. Revisa y aprueba: `pos finance rule approve --all` y, si quieres,
   `pos finance list --by-claude -m AAAA-MM` (lo que categorizó Claude, marcado con `~`).

Sin Claude también funciona: `pos sync` (o `pos finance inbox`) importa la bandeja y
`pos finance close [AAAA-MM]` da el estado del cierre.

### Bandeja de extractos

`[finance].inbox_dir` en `config.toml` (la ruta real, `/mnt/c/Users/<usuario>/iCloudDrive/…`).
Cada `.xls`/`.pdf` se importa (idempotente) y se mueve a `importados/AAAA-MM/`. Lo que no se
reconoce o no cuadra se queda en la bandeja y `pos sync`/`pos doctor` avisan; nunca se borra
nada. Los extractos archivados son datos bancarios sin cifrar en iCloud Drive: bórralos
cuando quieras (los datos ya están en la base y en los backups cifrados).

### Cierre mensual (`pos finance close`)

- **Extractos**: por cuenta, si hay movimientos hasta fin de mes o se importó después.
- **Pendientes**: sin categoría y categorizados por Claude.
- **Comparación**: gasto por categoría principal frente al mes anterior.
- **Rendimiento del efectivo** (`[finance.cash_yield.<entidad>]`): intereses esperados
  (tipo anual sobre el saldo de cada día, brutos) frente a los abonados a primeros del mes
  siguiente, y saveback esperado (% de lo pagado con tarjeta, con tope opcional) frente al
  abonado. Es una **estimación**: retenciones fiscales, cambios de tipo, topes o comercios
  excluidos explican diferencias. Si aún no se ha abonado, sale como «pendiente».

```toml
[finance.cash_yield.trade_republic]
interest_rate = 2.5   # % anual
saveback_rate = 1.0   # % de lo pagado con tarjeta
# saveback_cap = 15   # € al mes
```

`pos doctor` avisa si hay ficheros sin importar en la bandeja y, pasado el día 7, si el mes
anterior sigue con movimientos sin categoría.

## Santander (cuenta)

1. Banca online → la cuenta → **Movimientos** → elige el periodo → **Descargar → Excel**.
   Según la versión de la web se obtiene `transactions_<fecha>.xls` o `export_excel.xlsx`;
   los dos sirven. Entre formatos, alguna compra con tarjeta cambia de fecha de operación:
   se reconoce como el mismo movimiento (misma fecha valor, importe, saldo y concepto).
2. `uv run pos finance import ~/ruta/transactions_….xls` (prueba antes con `--dry-run`).
   Reimportar el mismo fichero o uno que solape no duplica nada.
3. Revisa lo pendiente: `uv run pos finance list --uncategorized`.

Los extractos reales **nunca** se copian al repositorio. Puedes borrarlos de Descargas tras
importarlos: los datos ya están en la base (y en los backups cifrados).

## Trade Republic (efectivo e inversiones)

Desde la v3 (ADR 0008) TR se importa con dos ficheros de la app, que se dejan en la bandeja:

1. **CSV «Exportación de transacción»**: todo el historial (efectivo y operaciones). Expórtalo
   **completo**, no un rango de fechas: el saldo se reconstruye desde la apertura y una
   exportación parcial se rechaza. Cada exportación nueva solo añade lo que falte.
2. **PDF «Extracto del patrimonio neto»** a fin de mes: posiciones valoradas y efectivo. Es
   la única fuente del valor de mercado (no se consultan precios en internet).

Al importar el CSV, los movimientos que ya venían del PDF «Extracto de cuenta» se enlazan
(no se duplican) y conservan su categoría. A partir de ahí ese PDF se rechaza: ya no hace
falta. Las compras del plan van a `inversion` (traspaso, fuera del neto) por las reglas de
siempre.

## Inversiones y patrimonio

```bash
uv run pos finance portfolio            # valor (última valoración) vs invertido, por posición
uv run pos finance contributions -n 12  # plan deducido, aportado por mes y si se cumplió
uv run pos finance networth -n 12       # efectivo de todas las cuentas + inversiones, por mes
```

- **Invertido** = coste por precio medio (con comisiones); una venta reduce el coste en
  proporción y materializa resultado.
- **Plan**: se deduce de los 3 últimos meses completos (importe y ejecuciones por ISIN). Un
  mes cumple si cada ETF recibe al menos el 90 % de lo habitual.
- **Patrimonio neto**: las inversiones van a valor de mercado si hay valoración de la última
  semana del mes; si no, a coste (marcado con `·`). `pos doctor` avisa si la última
  valoración tiene más de 40 días.

## Categorizar

```bash
uv run pos finance categories                          # slugs disponibles
uv run pos finance rule preview "mercadona"            # qué casaría
uv run pos finance rule add "mercadona" supermercado --debit
uv run pos finance categorize <txn> restaurantes       # a mano (gana a las reglas y a Claude)
uv run pos finance list --by-claude                    # lo que categorizó Claude (`~`)
uv run pos finance categorize <txn> --clear
uv run pos finance rule list --status proposed         # propuestas de Claude
uv run pos finance rule approve --all | <id>…          # o reject <id>
uv run pos finance rule apply --recategorize           # tras cambiar prioridades
```

Subcategorías (un nivel): `pos finance categories` las muestra sangradas;
`pos finance category add <slug> "<Nombre>" --parent transporte` crea una propia;
`pos finance rule edit <id> --category vuelos` cambia el destino de una regla y recoloca
sus movimientos. El resumen agrupa por categoría principal con desglose, y filtrar por una
principal incluye sus subcategorías.

Las reglas comparan sin mayúsculas ni acentos. Menor prioridad = se evalúa antes; gana la
primera que casa.

## Consultar

```bash
uv run pos finance summary [2026-09]       # ingresos, gastos, neto, por categoría
uv run pos finance list -m 2026-09 -c supermercado
uv run pos finance list -s "uber"
uv run pos finance accounts | imports
```

`traspaso` e `inversion` son de tipo *transfer*: no cuentan en el neto.

Desde Claude (MCP): `finance_summary`, `finance_month_close`, `investments_overview`, `list_transactions`,
`list_category_rules`, `propose_category_rule` (queda pendiente de tu aprobación) y
`categorize_transactions` (comercios sueltos; queda marcado como de Claude y nunca cambia
lo que tú pusiste a mano).

## Panel «Pulso del mes»

`uv run pos panel` lo genera y lo abre en el navegador. También se regenera solo tras cada
`pos sync` (3 veces al día con la tarea de Windows). Responde a:

- **¿Cómo voy este mes?** Gasto variable hasta hoy frente a lo que sueles llevar a estas
  alturas (media de los 3 últimos meses cerrados), fijos pagados, previsión de gasto y de
  ahorro a fin de mes.
- **¿Dónde se está yendo?** Cada categoría variable este mes frente a lo habitual a este día.
- **¿En qué gasto más?** Ranking con su peso sobre el gasto y la nómina; toca una categoría
  para ver sus comercios y un comercio para ver sus movimientos.
- **¿Dónde está mi dinero?** Reparto de un mes tipo (fijos, variable, inversión, ahorro en
  efectivo) y patrimonio por tipo.
- **¿Dónde debería centrarme?** Los 3 mayores gastos variables y cuánto ahorrarías
  recortando un 20 %.
- **Mes a mes**: ingresos frente a gasto (12 meses) y patrimonio neto.

En el iPhone: Scriptable → **Personal OS Panel** (ver `docs/setup/iphone.md`). Definiciones
en ADR 0009.
