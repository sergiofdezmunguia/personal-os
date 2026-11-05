---
name: satellite-review
description: Revisión de la cartera satélite del Personal OS — presupuesto, ideas de compra/venta por reglas, comprobación de fundamentales y noticias de cada idea, tamaño de la posición, checklist antes de operar y diario de operaciones. Úsala cuando el usuario pida "revisa las ideas", "¿compro X?", "ideas de inversión", "satélite", "he comprado/vendido X" o similar.
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
2. **Para cada idea `comprar` (y las `vigilar` con caída más fuerte)**, antes de opinar:
   - Fundamentales y valoración: skill `us-stock-analysis` (crecimiento, márgenes, deuda,
     PER frente a su media). ¿La caída es de mercado o de la empresa?
   - Noticias de los últimos días: skill `market-news-analyst` o WebSearch. Separa ruido de
     cambios de tesis (resultados, regulación, guía rebajada).
   - Tamaño: skill `position-sizer` con el stop de la idea; nunca por encima de
     `budget.per_trade_cents` ni del disponible.
   - Checklist: skill `pre-trade-discipline-gate` (plan, tamaño, no venganza tras pérdidas,
     circuito de pérdidas con `drawdown-circuit-breaker`).
   - Fiscalidad: avisos de la regla de los dos meses que vengan en la idea.
3. **Conclusión por idea**: «encaja / esperar / descartar», con 2–3 razones y el plan
   (importe, precio de entrada orientativo, stop, objetivo, cuándo revisar).
4. **Si el usuario opera**: registra la tesis con `trader-memory-core` (entrada, stop,
   objetivo, motivo). La operación real llegará sola con el CSV de TR y el panel la contará.
5. **Ideas `vender`**: recuerda el plan de salida que se fijó; si en noviembre/diciembre hay
   `harvest`, explica la compensación de pérdidas con ganancias del año.
6. **Cada mes**: `versus_benchmark` y `weekly-performance-digest`. Si tras 6–12 meses el
   satélite no le gana al MSCI World, dilo claro: lo sensato sería volver al índice.

## No hagas

- No digas «compra» o «vende» como orden; son ideas con riesgo de pérdida.
- No cambies las reglas de `[finance.satellite]` sin que el usuario lo pida.
- No uses el colchón de emergencia ni el dinero del plan periódico.
