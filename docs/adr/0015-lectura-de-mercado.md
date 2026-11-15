# 0015 — Lectura de mercado para el satélite

Fecha: 2026-11-15 · Estado: aceptada (pendiente de probar en la instalación real) · Amplía ADR 0014 (no cambia sus reglas de dinero)

## Contexto

Las ideas del satélite (ADR 0014) solo miran la caída desde el máximo de 52 semanas, la media
de 200 sesiones y el RSI14, y aplican stop y objetivo fijos. El usuario quiere que «entienda
cuándo es miedo y cuándo es euforia, que vea tendencias, que estime cuánto pueden bajar y
subir las cosas y que diga momentos donde se podría entrar».

El riesgo es vender como conocimiento lo que es ruido. Por eso: indicadores gratuitos y
explicables, umbrales fijados **a priori** (convenciones conocidas, no ajustados a los datos),
estimaciones siempre como rangos con probabilidad, y una prueba histórica sin fuga de
información cuyo resultado, sea el que sea, se enseña tal cual en el panel.

## Decisión

### 1. Régimen de mercado (miedo · neutral · euforia · desconocido)

Seis indicadores, cada uno vota miedo (−1), nada (0) o euforia (+1):

| Indicador | Miedo | Euforia |
|---|---|---|
| VIX (volatilidad implícita del S&P 500) | ≥ 25 | ≤ 14 |
| Estructura VIX / VIX3M | ≥ 1,00 (los asustados pagan más por el corto plazo) | ≤ 0,85 |
| S&P 500 frente a su media de 200 sesiones | ≤ −5 % | ≥ +10 % |
| Caída del S&P 500 desde su máximo de 52 semanas | ≥ 10 % | — |
| Amplitud: % de valores del S&P 500 sobre su media de 200 (media de 8 sesiones, CSV público de TraderMonty) | ≤ 35 % | ≥ 75 % |
| Volatilidad realizada del S&P 500 (20 sesiones, anualizada) | ≥ 25 % | ≤ 10 % |

- Neto = votos de euforia − votos de miedo. **Miedo** si neto ≤ −2, **euforia** si ≥ +2, si no
  neutral. Hace falta un mínimo de 3 indicadores con dato de los últimos 7 días; si no,
  **desconocido** y se avisa en la sync. Una fuente caída nunca rompe la sync.
- Fuentes: Yahoo (`^VIX`, `^VIX3M`, `^GSPC`) y `market_breadth_data.csv` de
  tradermonty.github.io. **Solo se envían símbolos públicos**, nada del usuario.
- Se guardan en `fin_market_prices` (sin migración nueva): los índices con su divisa (USD) y la
  amplitud como símbolo `BREADTH200.SP500`, divisa `RATIO`. `satellite.closes()` sigue leyendo
  solo EUR, así que no se mezclan con la lista de seguimiento.
- Mercado de referencia: EE. UU. (≈ 70 % del MSCI World y el único con todos estos datos
  gratis y largos).

### 2. Tendencia por valor

- Corto (≈ 1 mes): precio frente a la media de 20 y su pendiente en 10 sesiones.
- Medio (≈ 3 meses): media de 50, pendiente en 20 sesiones.
- Largo (≈ 1 año): media de 200, pendiente en 20 sesiones.
- Alcista si precio > media y la media sube; bajista si precio < media y la media baja; si no,
  lateral. Estructura: las dos últimas mitades de 60 sesiones con máximos y mínimos crecientes,
  decrecientes o mixtos. Resumen en una frase («corrección dentro de una tendencia alcista»).

### 3. Rangos estimados de bajada y subida (1, 3 y 12 meses)

Historia propia del valor (hasta 30 años; la primera descarga pasa a ser «máximo» y los valores
ya guardados se completan una vez, apuntado en `sync_cursors`):

- Para cada día pasado t: rendimiento final a h sesiones, peor caída y mayor subida dentro de
  la ventana, en logaritmos, **divididos por la volatilidad de entonces**; se reescalan con la
  volatilidad de hoy. Volatilidad = mezcla de la de 63 sesiones y la de 5 años con peso
  `exp(−h/126)` (la volatilidad vuelve a su media: pesa mucho a 1 mes y poco a 12 meses).
- Se publican cuantiles 10, 50 y 90 del final, el cuantil 10 de la peor caída y el 90 de la
  mayor subida, con el número de casos. Texto: «en 1 de cada 10 casos parecidos llegó a caer
  más de X %». Nunca una predicción puntual.
- Mínimo de casos: 2 años de ventanas para 1 y 3 meses, 3 años para 12 meses; si no, «sin
  historia suficiente».

### 4. Zonas de entrada

- Solo en ideas «comprar» («vigilar» dice esperar). Hasta tres tramos iguales del importe que
  ya propone la regla (nunca más dinero), elegidos entre: el precio de hoy, el mínimo de las
  últimas 120 sesiones y los niveles de la caída «1 de cada 10» a 1 y a 3 meses, separados al
  menos un 3 %. Se enseñan solo si salen dos o más. Para los valores de la lista que aún no han
  caído lo bastante se enseña el precio en el que la regla `dip` se activaría.
- Ningún tramo por debajo del stop: si se llega ahí, el plan es salir, no promediar.
- Euforia: nunca tramo «a precio de hoy» (prudencia); si no queda otro nivel, se dice que es
  mejor esperar. Esta variante no está en la prueba histórica (que usa la neutral). Miedo con
  tendencia sana: se señala como contexto favorable **solo en la medida en que la prueba
  histórica lo respalde** (ver 5).
- No cambian: núcleo intocable, colchón, tope, importe por operación, stop y objetivo.

### 5. Validación

`finance/market_backtest.py` (puro) + `scripts/market_backtest.py` (descarga pública a una
carpeta de caché, nunca a la base de datos):

- Universo: 9 índices y 20 valores grandes, **incluidos perdedores** (Intel, GE, Citi, Pfizer,
  IBM, Boeing, AT&T…), desde 2000. Señal al cierre del día t con datos hasta t; entrada al
  cierre de t+1.
- Compara rendimientos a 3 y 12 meses de: cualquier día, miedo, miedo + tendencia sana,
  euforia, la regla actual (caída ≥ `dip` + tendencia sana) y la regla actual con miedo.
  Episodios separados h sesiones para no contar diez veces la misma caída; intervalos de
  confianza al 95 % con *bootstrap* por años (las caídas llegan a la vez a todos los valores).
- Frente a DCA: aportación mensual fija invertida al momento frente a guardarla hasta la
  siguiente señal (el efectivo parado no rinde).
- Calibración de los rangos: fechas de evaluación cada h sesiones, cuantiles calculados solo
  con ventanas ya cerradas en esa fecha; se cuenta cuántas veces el resultado cae fuera de los
  cuantiles 10 y 90 (lo ideal: 10 %).
- Las cifras resultantes se copian en `market.EVIDENCE` (con fecha) y el panel las enseña. Si
  no hay mejora clara, el panel lo dice y no lo presenta como ventaja.

### 6. Dónde se ve

Pestaña Ideas (tarjeta de régimen, tendencia, rangos y tramos por idea), `pos finance ideas`,
MCP `satellite_overview` (campos `market`, `views`) y la skill `satellite-review`. Castellano
llano y el aviso de siempre: ideas por reglas, no recomendaciones.

## Resultado de la prueba (2026-11-15)

29 series (9 índices y 20 valores), 2000-01 → 2026-11, cierres sin dividendos. Régimen: 1.394
sesiones de miedo, 3.374 neutrales y 1.990 de euforia (la amplitud solo existe desde 2016 y el
VIX3M desde 2006; antes el régimen usa los indicadores que hay, mínimo 3).

| Entrada (frente a cualquier día) | 3 meses (pp, IC 95 %) | 12 meses (pp, IC 95 %) |
|---|---|---|
| Miedo | +0,5 [−2,9, +4,5] | −4,1 [−11,7, +3,0] |
| Miedo + tendencia sana | +0,9 [−1,8, +3,9] | −2,4 [−10,0, +4,9] |
| Euforia | −0,1 [−1,2, +0,8] | +2,0 [−2,8, +6,8] |
| Regla actual (caída ≥ 15 % + tendencia sana) | +0,7 [−1,8, +3,2] | −0,2 [−8,1, +7,2] |
| Regla actual + miedo | +1,4 [−2,4, +5,3] | −1,1 [−11,4, +8,8] |

- **Ninguna condición mejora de forma significativa** a entrar cualquier día. La euforia
  tampoco fue peor.
- **Frente a DCA**: guardar la aportación hasta «miedo + tendencia sana» acaba con 0,909 veces
  el valor del DCA (IC 0,868–0,940; gana en 2 de 29 series); «miedo» 0,942; la regla actual
  0,965 (gana en 6 de 29).
- **Tramos frente a todo de golpe** (12 meses, 357 casos): −0,6 pp [−2,0, +0,8]; mejor en el
  44 % de los casos.
- **Calibración** (lo ideal, 10 %): a 1 mes, final bajo el cuantil 10 el 10,1 %, peor caída más
  honda que la estimada el 10,4 %, por encima del 90 el 8,3 %; a 3 meses 11,1 / 10,9 / 7,2 %; a
  12 meses 12,7 / **15,0** / 4,6 %: a un año las caídas se quedan cortas y las subidas largas
  (la historia anterior a 2000 fue más alcista). Los intervalos de Wilson (p. ej. 9,8–11,1 % a
  1 mes) no corrigen la correlación entre valores: son optimistas. Lo mismo el IC del DCA
  (bootstrap por valores, que se mueven juntos). Todos los valores siguen cotizando hoy (no hay
  Lehman ni Enron): queda algo de sesgo de supervivencia, que favorece a comprar caídas.
  Tampoco se sabe si la amplitud de TraderMonty usa los componentes actuales del S&P 500.

Conclusión: el régimen, la tendencia y los rangos **describen** bien el riesgo (1 y 3 meses),
pero **no dan una ventaja para elegir el momento**. El panel lo dice así (`market.fear_edge()`
es falso), el contexto de cada idea lo repite y el importe sigue siendo el de la regla. En
euforia se mantiene la prudencia que pidió el usuario (primer tramo bajo el precio de hoy),
avisando de que es cautela y no ventaja.

## Consecuencias

- Más descarga en la sync: 3 símbolos + 1 CSV (≈ 600 kB) cada 6 h como mucho, y una descarga
  larga por valor la primera vez. Si pasa tiempo sin sync, se pide lo justo para cubrir el hueco
  (1 mes, 3 meses, 1 año o todo). Los fallos de amplitud o de régimen son avisos, no errores.
- El régimen es de EE. UU.; un valor europeo puede ir por libre.
- Los umbrales son convenciones, no óptimos; cambiar uno exige repetir la prueba histórica.
- Los rangos asumen que el pasado del valor es representativo: no ven quiebras ni cambios de
  negocio. La revisión de fundamentales y noticias sigue en `satellite-review`.
