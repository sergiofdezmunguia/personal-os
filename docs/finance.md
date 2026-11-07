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
Cada fichero se importa (idempotente) y se mueve a `importados/AAAA-MM/`; si se subió hace
menos de 10 minutos se importa igual pero se archiva en la siguiente sync (si iCloud aún lo
está sincronizando, deshace el movimiento). Lo que no se
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

## Open banking (Santander y efectivo de TR)

Desde el ADR 0010, `pos sync` trae a diario el saldo y los movimientos de las cuentas
autorizadas con [Enable Banking](https://enablebanking.com) (PSD2, gratis para uso personal),
sin exportar nada. También a mano: `uv run pos finance bank-sync [--days 90] [--force]`.

- Credenciales en `~/.config/personal-os/enablebanking/` (0600): `<app_id>.pem` (clave de la
  aplicación) y `sessions.json` (una sesión por banco, `santander` y `traderepublic`). Nunca
  se imprimen ni se copian al repo.
- Como mucho una consulta cada 5 h (PSD2 limita a ~4 al día por cuenta; el banco responde
  HTTP 429 si se pasa) y una ventana de 30 días.
- **Santander** da id por movimiento (`entry_reference`): lo ya importado del Excel se enlaza
  (no se duplica) y conserva su categoría. Desde entonces el Excel deja de hacer falta.
- **Trade Republic** solo da fecha e importe, sin concepto ni id, y solo los 100 últimos:
  entran con un id provisional (`prov:…`) y descripción «Trade Republic · sin concepto». Si
  ya existe un movimiento con el mismo importe a ±3 días, no se duplica. Al importar más
  tarde el CSV de TR, su id real sustituye al provisional y la descripción es la del CSV.
- El saldo tras cada movimiento se reconstruye desde el saldo contable del banco y se cuadra
  con el ya importado: si la diferencia es constante (el historial importado no empezaba en
  el saldo real) se acepta con un aviso y desde entonces manda el banco; si varía, faltan o
  sobran movimientos y no se importa nada.
- **Renovar la autorización**: caduca (Santander ~180 días, TR ~90). `pos sync` avisa 14
  días antes. `uv run pos finance bank-auth santander` (o `traderepublic`) abre el banco en el
  navegador, recoge la autorización en la dirección de retorno de la aplicación
  (`localhost:8765`) y sustituye esa sesión en `sessions.json`. Si el navegador no llega al
  PC, pega la dirección a la que te llevó el banco cuando lo pida.

### Comercio de Apple Pay

Trade Republic no manda concepto por open banking. La automatización «Transacción» de
Atajos escribe cada pago con Apple Pay en `<bandeja>/applepay/pagos.txt`
(`instante; importe €; comercio; tarjeta`). En cada `pos sync`, los movimientos «sin
concepto» con el mismo importe y fecha contable entre el día del pago y 3 días después
toman el nombre del comercio (el más cercano en fecha) y pasan por las reglas. Un pago ya
usado no se vuelve a asignar. El fichero nunca se modifica ni se borra.

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


## Analista de gastos y avisos

- `pos finance recurring [--all]`: suscripciones, cuotas y recibos detectados por patrón, con
  coste al mes, próximo cobro y subidas de precio. `pos finance week [AAAA-MM-DD]`: 7 días
  frente a una semana habitual.
- En cada sync el PC deja `panel/avisos.json`; el iPhone los muestra como notificación al
  ejecutar «Personal OS Sync» (buenos días con lo gastado ayer y la agenda, resumen del
  domingo, subidas de cuotas, cobros recurrentes nuevos, posibles duplicados, renovaciones
  anuales). Tocar el aviso abre el panel. Detalle en ADR 0011.
- Compras online sin concepto: en el cierre de mes, Claude busca el recibo en Gmail y les pone
  nombre con `name_unlabeled_transaction` (solo a movimientos sin concepto).

## Precio diario de la cartera

`pos sync` descarga el último cierre de cada fondo/ETF en cartera (justETF; Yahoo de
respaldo) como mucho cada 6 h. `pos finance prices [--force]` lo hace a mano y muestra la
cartera a precio de hoy. El panel, el widget y el aviso de buenos días usan ese valor. El PDF
mensual de TR sigue siendo la referencia de cierre. Detalle en ADR 0012.

## Cartera satélite (ideas de compra y venta)

Reglas en `[finance.satellite]` de config.toml (lista de seguimiento con símbolos de Xetra en
EUR, tope, colchón, caída mínima, stop y objetivo). `pos sync` descarga los cierres;
`pos finance ideas` y la pestaña **Ideas** del panel muestran el presupuesto disponible, las
ideas (comprar / vigilar / vender), los impuestos estimados del año y si el satélite le gana
al MSCI World. Para analizar una idea a fondo: skill `satellite-review`. Detalle en ADR 0014.

## Qué vigila `pos doctor` en la cartera

Tres áreas, sin red, sin escribir y sin gastar cupo del banco. Son avisos (`!`): no cambian el
código de salida. Los umbrales son constantes de código: `ops/doctor.py`,
`investments.LIVE_PRICE_MAX_AGE_DAYS` y `bootstrap.BANK_RENEW_DAYS`.

**`precios`**: el último cierre en EUR de cada posición abierta (lo vendido no se comprueba) y,
si el satélite está activo, de la lista de seguimiento y del índice. Una *sesión perdida* es un
día de lunes a viernes estrictamente entre el último cierre y hoy.

- Hasta 2 sesiones perdidas: bien. Un fin de semana no avisa, y tampoco Semana Santa o Navidad
  en la mayoría de los años. Un puente de 3 días da un aviso breve que se corrige solo.
- 3 o más sesiones perdidas, o ningún cierre: aviso.
- Último cierre de más de 7 días: la cartera a precio de hoy deja de usarlo. El panel vuelve a la
  valoración del PDF o, si no hay ninguna, muestra la cartera a coste. Con el satélite activo, su
  tope puede bajar.
- No sabe si la última descarga falló: solo ve la antigüedad de lo guardado.

**`operaciones`**: el sistema no puede detectar lo que no ha recibido. Dice hasta dónde conoce
las operaciones (último movimiento del CSV de TR) y qué indicios hay de que falte algo:

| Señal | Aviso |
|---|---|
| Valoración PDF con participaciones distintas de las operaciones | Inmediato (también lo da una bonificación en títulos, que no se importa como operación) |
| Cargo de open banking posterior o igual al último CSV, en categoría `inversion` o sin concepto y con el importe exacto del plan: **probable compra** | A partir de 7 días; antes, solo recuento |
| Cargo de open banking posterior o igual al último CSV, sin concepto ni categoría: **sin identificar** | **Nunca**: solo recuento |
| Último CSV de más de 40 días | Sí |

Una compra manual fuera del plan queda como «sin identificar» y no avisa por esta vía: solo se
nota por la antigüedad del CSV o por la discrepancia con la valoración. No cuentan como indicio
los abonos (las ventas quedan fuera), lo anterior al último CSV, lo que ya tiene comercio ni lo
clasificado con otra categoría.

No puede detectar compras aún no contabilizadas, en otro bróker o sin cargo en efectivo, un cargo
que se empareje con otro del mismo importe a ±3 días, ni nada si open banking no está configurado
o ha dejado de responder. «Compras» son siempre compras de inversión. Para ponerse al día,
exporta el CSV completo a la bandeja y ejecuta `uv run pos finance inbox`.

**`open banking`**: autorización caducada o a menos de 14 días, y último dato **nuevo** de más de
7 días. Una respuesta idéntica del banco no deja rastro, así que no distingue una cuenta tranquila
de una consulta fallida; para saberlo, `uv run pos sync --only finance` y mira sus errores (sin
`--force`, para respetar el cupo).
