# 0014 — Cartera satélite por reglas

Fecha: 2026-11-05 · Estado: aceptada

## Contexto

El usuario quiere aprovechar caídas fuertes (p. ej. tecnológicas) para ganar algo extra, sin
tocar su inversión indexada. La mayoría de quienes operan a corto plazo rinden menos que el
índice, así que el diseño prioriza la disciplina y la medición frente a «acertar».

## Decisión

- **Núcleo / satélite**: lo que está en el plan periódico es núcleo y no se toca. El satélite
  tiene un tope (`share` % del patrimonio) y solo usa efectivo que sobre tras
  `emergency_months` meses de gasto habitual y el plan del mes.
- **Reglas fijas** en `[finance.satellite]` de config.toml: caída `dip` % desde el máximo de
  52 semanas; «tendencia sana» (precio sobre la media de 200 sesiones o esta subiendo) para
  proponer importe; si no, «vigilar». Stop `stop` %, objetivo vuelta a máximos (≤
  `take_profit` %), `max_trade` % del tope por operación. Posiciones testimoniales (< 50 €)
  no generan ventas.
- **Datos**: cierres diarios de Yahoo por símbolo **en EUR** (Xetra), `fin_market_prices`
  (migración 0008), 5 años la primera vez y el último mes después, dentro de `pos sync`.
- **Fiscalidad España**: estimación de la base del ahorro del año (19–30 %), aviso de la regla
  de los dos meses al recomprar tras vender con pérdidas y, en noviembre y diciembre,
  pérdidas latentes que compensarían ganancias. Todo se declara.
- **Medición**: cada compra/venta del satélite se replica virtualmente en el índice de
  referencia (`benchmark`); el panel enseña si le gana.
- **Ejecución**: nunca automática. Ideas en el panel (pestaña Ideas), avisos semanales,
  `pos finance ideas`, MCP `satellite_overview` y la skill `satellite-review`, que usa las
  skills de tradermonty/claude-trading-skills instaladas a nivel de usuario.

## Consecuencias

- Las reglas técnicas no ven fundamentales ni noticias: eso se revisa en sesión
  (`satellite-review`) antes de operar.
- Fuentes de precios no oficiales; si fallan, no hay ideas (aviso en la sync).
