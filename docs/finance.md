# Finanzas: uso

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
uv run pos finance categorize <txn> restaurantes       # a mano (gana a las reglas)
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

Desde Claude (MCP): `finance_summary`, `list_transactions`, `list_category_rules` y
`propose_category_rule` (queda pendiente de tu aprobación).
