# 0012 — Precio diario por ISIN

Fecha: 2026-11-05 · Estado: aceptada · Sustituye en parte a ADR 0008 («nunca precios de red»)

## Contexto

La cartera solo se valoraba con el PDF mensual de TR: entre PDF y PDF el panel mostraba un
valor viejo más lo comprado a coste. El usuario quiere ver la cartera al día.

## Decisión

- `adapters/quotes.py` descarga el último cierre por ISIN de **justETF** (EUR) y, si falla,
  de **Yahoo Finance** (solo si cotiza en EUR). A estas fuentes solo se envía el ISIN.
- `fin_prices` (migración 0007) guarda los cierres; `pos sync` los actualiza como mucho cada
  6 h (`bootstrap.update_prices`); `pos finance prices [--force]` a mano.
- `investments.live_portfolios`: participaciones de hoy (de las operaciones) × último cierre
  de la última semana. Si falta el precio de alguna posición, el panel sigue usando la
  valoración del PDF.
- El PDF de patrimonio sigue siendo la referencia de cierre de mes (cuadra unidades y efectivo).

## Consecuencias

- Fuentes no oficiales: pueden cambiar o cortar. El fallo de una cotización es un aviso de
  la sync, nunca un error.
- El precio es el de un mercado de referencia en EUR, no el de ejecución de TR: diferencias
  pequeñas frente al PDF son normales.
