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
   Se obtiene `transactions_<fecha>.xls`.
2. `uv run pos finance import ~/ruta/transactions_….xls` (prueba antes con `--dry-run`).
   Reimportar el mismo fichero o uno que solape no duplica nada.
3. Revisa lo pendiente: `uv run pos finance list --uncategorized`.

Los extractos reales **nunca** se copian al repositorio. Puedes borrarlos de Descargas tras
importarlos: los datos ya están en la base (y en los backups cifrados).

## Trade Republic (cuenta de efectivo)

1. App o web → Perfil → **Documentos** → «Extracto de cuenta» (PDF) con el periodo deseado.
2. `uv run pos finance import "~/ruta/Extracto de cuenta.pdf"`.

El PDF no indica de forma fiable si un importe es entrada o salida: el signo se deduce del
saldo, y el extracto se rechaza si algún movimiento no cuadra o si el saldo final no coincide
con el resumen. Las compras de los planes de inversión aparecen como `Operar Savings plan`
(categoría `inversion`, fuera del neto). Las posiciones y valoración de la cartera no están
en este extracto (quedan para el módulo de inversiones).

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

Desde Claude (MCP): `finance_summary`, `finance_month_close`, `list_transactions`,
`list_category_rules`, `propose_category_rule` (queda pendiente de tu aprobación) y
`categorize_transactions` (comercios sueltos; queda marcado como de Claude y nunca cambia
lo que tú pusiste a mano).
