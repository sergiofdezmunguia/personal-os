# 0013 — Hábitos desde Apple Salud

Fecha: 2026-11-05 · Estado: aceptada

## Contexto

El panel debía mostrar hábitos (pasos, ejercicio, sueño, comida) en formato calendario. Los
datos están en Apple Salud (Fitia y Hevy escriben allí). Sin Mac ni app propia no hay API de
HealthKit desde el PC.

## Decisión

- Un atajo del iPhone («Salud del día», automatización diaria por la noche) añade una línea
  a `iCloud Drive/PersonalOS-extractos/salud/salud.txt`:
  `AAAA-MM-DD; pasos=…; ejercicio=…; sueño=…; kcal=…; peso=…` (claves opcionales).
- `habits/health.py` es un parser puro y tolerante (separador de miles, coma decimal, última
  línea del día gana). **Sin base de datos**: el fichero es la fuente y se lee al generar el
  panel. Si algún día hace falta histórico largo o cruzarlo con gastos, se añade una tabla.
- Objetivos fijos en `health.GOALS` (8.000 pasos, 30 min de ejercicio, 7 h de sueño).

## Consecuencias

- El usuario mantiene el atajo; si deja de ejecutarse, el panel muestra días vacíos.
- No sale nada de Salud fuera de su iCloud y su PC.
