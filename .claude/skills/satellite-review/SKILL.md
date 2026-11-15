---
name: satellite-review
description: Revisión de la cartera satélite del Personal OS — presupuesto, ideas de compra/venta por reglas, lectura de mercado (miedo/euforia, tendencia, rangos de bajada y subida, tramos de entrada), comprobación de fundamentales y noticias de cada idea, tamaño de la posición, checklist antes de operar y diario de operaciones. Úsala cuando el usuario pida "revisa las ideas", "¿compro X?", "ideas de inversión", "satélite", "he comprado/vendido X" o similar.
---

# Revisión del satélite

Objetivo: decidir con disciplina si alguna idea del satélite merece una orden **manual** en
Trade Republic, y dejarla registrada para medir si el satélite le gana al índice (ADR 0014).
Nunca ejecutas órdenes ni prometes rentabilidad: das análisis y el usuario decide.

## Privacidad

Importes del usuario solo en la conversación; nunca en ficheros del repo, commits ni docs.

## Pasos

1. **Estado**: herramienta MCP `satellite_overview` (o `uv run pos finance ideas`).
   - Si `budget.available_cents` es 0, dilo primero y por qué (`why_zero`). Sin presupuesto no
     se compra: como mucho se prepara la lista para cuando lo haya.
2. **Mercado** (ADR 0015): `market.regime` (miedo / neutral / euforia / desconocido) con sus
   indicadores. Explícalo en una frase y, si es `desconocido`, di qué falta (`missing`).
   Recuerda lo que dice `market.evidence`: con `fear_edge` false, el miedo **no** dio mejores
   entradas en la prueba histórica y esperar a que llegue rindió menos que comprar cada mes.
   No lo vendas como ventaja ni como motivo para comprar más.
   - Para un análisis más fino de amplitud: skill `market-breadth-analyzer`.
3. **Para cada idea `comprar` (y las `vigilar` con caída más fuerte)**, antes de opinar:
   - Contexto de la idea (`context`) y vista del valor (`views[isin]`): tendencia corto/medio/
     largo, rango a 1, 3 y 12 meses. Dilo como probabilidad («en 1 de cada 10 casos parecidos
     cayó más de X %»), nunca como predicción, y añade la `note` de calibración si la hay (a 12
     meses los rangos se quedaron cortos en la prueba).
   - Tramos (`tranches`): reparten el **mismo** importe; ninguno por debajo del stop. Explica
     que reducen el riesgo de equivocarse de día pero no mejoraron el resultado medio.
   - Fundamentales y valoración: skill `us-stock-analysis` (crecimiento, márgenes, deuda,
     PER frente a su media). ¿La caída es de mercado o de la empresa?
   - Noticias de los últimos días: skill `market-news-analyst` o WebSearch. Separa ruido de
     cambios de tesis (resultados, regulación, guía rebajada).
   - Tamaño: skill `position-sizer` con el stop de la idea; nunca por encima de
     `budget.per_trade_cents` ni del disponible.
   - Checklist: skill `pre-trade-discipline-gate` (plan, tamaño, no venganza tras pérdidas,
     circuito de pérdidas con `drawdown-circuit-breaker`).
   - Fiscalidad: avisos de la regla de los dos meses que vengan en la idea.
4. **Conclusión por idea**: «encaja / esperar / descartar», con 2–3 razones y el plan
   (importe, precio de entrada orientativo, stop, objetivo, cuándo revisar).
5. **Si el usuario opera**: registra la tesis con `trader-memory-core` (entrada, stop,
   objetivo, motivo). La operación real llegará sola con el CSV de TR y el panel la contará.
6. **Ideas `vender`**: recuerda el plan de salida que se fijó; si en noviembre/diciembre hay
   `harvest`, explica la compensación de pérdidas con ganancias del año.
7. **Cada mes**: `versus_benchmark` y `weekly-performance-digest`. Si tras 6–12 meses el
   satélite no le gana al MSCI World, dilo claro: lo sensato sería volver al índice.

## No hagas

- No digas «compra» o «vende» como orden; son ideas con riesgo de pérdida.
- No cambies las reglas de `[finance.satellite]` sin que el usuario lo pida.
- No uses el colchón de emergencia ni el dinero del plan periódico.
- No digas «el mercado va a bajar/subir»: los rangos son frecuencias del pasado del valor.
- No conviertas «hay miedo» en «compra más»: el importe lo fija la regla, no el régimen.
