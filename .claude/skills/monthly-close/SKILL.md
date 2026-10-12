---
name: monthly-close
description: Cierre mensual de finanzas del Personal OS — importa los extractos de la bandeja, categoriza lo pendiente (reglas propuestas para lo recurrente, categoría directa para comercios sueltos), comprueba intereses y saveback y resume el mes frente al anterior. Úsala cuando el usuario pida "cierre de mes", "cierra septiembre", "he subido los extractos" o similar.
---

# Cierre mensual

Objetivo: que el mes quede **completo** (extractos de todas las cuentas hasta fin de mes),
**categorizado** (0 sin categoría) y **explicado** (qué ha cambiado y si el banco ha pagado
lo que toca). Mes por defecto: el anterior al actual.

## Privacidad (obligatorio)

- Nunca muestres IBAN, números de tarjeta, teléfonos ni nombres de personas de los conceptos.
- Los importes del resumen sí se pueden dar al usuario (son suyos y los pide), pero no los copies
  a ficheros del repo, commits ni docs. Nunca abras los ficheros de la bandeja para leerlos.

## Pasos

1. **Importar**: `uv run pos finance inbox`. Si algo falla, di qué fichero y por qué (formato no
   reconocido, saldo que no cuadra, iCloud aún descargando) y sigue con lo demás.
2. **Estado**: herramienta MCP `finance_month_close` (o `uv run pos finance close AAAA-MM`).
   - Si `statements_complete` es falso: di qué cuenta falta y cómo descargar su extracto
     (ver `docs/finance.md`) y para aquí hasta que lo suba; lo demás sería provisional.
3. **Categorizar lo pendiente** (`list_transactions` con `uncategorized=true` y `month`):
   - Mira antes `list_category_rules` y `finance_summary.categories`.
   - **Comercio que se repite** (o que claramente se repetirá: suscripciones, gimnasio,
     supermercado): `propose_category_rule` (queda propuesta; el usuario la aprueba).
   - **Comercio suelto** que reconoces con seguridad: `categorize_transactions` en lote.
   - **Dudoso** (nombre opaco, siglas, persona): no inventes. Agrúpalos y pregunta qué son en
     un solo mensaje. Con la respuesta, regla si se repite o categoría directa si no.
   - Transferencias con personas y Bizum → `gastos-compartidos` (ya hay reglas). Traspasos
     entre sus cuentas → `traspaso`. Planes de inversión → `inversion`.
   - Si falta una categoría que tendría sentido, propónla; solo el usuario las crea
     (`pos finance category add …`).
4. **Rendimiento del efectivo** (`cash_yield` del paso 2): intereses y saveback esperados vs
   abonados. Son estimaciones: el banco puede aplicar retención fiscal, topes o exclusiones.
   Señala solo desviaciones grandes o cambios respecto a meses anteriores; si `received` es
   null, aún no se ha abonado (llega el mes siguiente).
5. **Resumen para el usuario** (breve, en español):
   - Ingresos, gastos y neto, frente al mes anterior.
   - Las 3–5 categorías que más se han movido y por qué (si se ve en los movimientos).
   - Intereses y saveback.
   - Lo que queda pendiente del usuario: reglas propuestas (`pos finance rule approve --all`),
     preguntas sobre comercios, extractos que faltan.
   - Cuántos movimientos categorizaste tú (`pos finance list --by-claude -m AAAA-MM` para
     revisarlos).

## No hagas

- No apruebes reglas ni crees categorías (solo el usuario).
- No cambies categorías que el usuario puso a mano (la herramienta lo impide; no lo rodees).
- No borres ni muevas ficheros de la bandeja a mano: lo hace `pos finance inbox`.
