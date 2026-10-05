# Finanzas: uso

## Santander (cuenta)

1. Banca online → la cuenta → **Movimientos** → elige el periodo → **Descargar → Excel**.
   Se obtiene `transactions_<fecha>.xls`.
2. `uv run pos finance import ~/ruta/transactions_….xls` (prueba antes con `--dry-run`).
   Reimportar el mismo fichero o uno que solape no duplica nada.
3. Revisa lo pendiente: `uv run pos finance list --uncategorized`.

Los extractos reales **nunca** se copian al repositorio. Puedes borrarlos de Descargas tras
importarlos: los datos ya están en la base (y en los backups cifrados).

## Trade Republic

Pendiente: exportación CSV de la app (perfil → Documentos/Statements → Exportar
transacciones). Falta una muestra del formato (cabecera + filas inventadas).

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
