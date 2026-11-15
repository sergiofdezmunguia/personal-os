# 0016 — Diario del satélite y avisos de cambio de régimen

Fecha: 2026-11-15 · Estado: aceptada · Amplía ADR 0011 (avisos), 0014 (satélite) y 0015 (lectura de mercado)

## Contexto

La prueba histórica del ADR 0015 dice que la lectura de mercado describe bien el riesgo pero no
da ventaja para elegir el momento. Esa prueba es **dentro de muestra** (umbrales conocidos,
datos pasados). Falta saber cómo le va a lo que el satélite dice **de verdad**, día a día, sin
poder retocarlo después. Y el usuario quiere enterarse en el iPhone cuando el mercado cambia de
humor, sin ruido: pocos avisos, texto llano y recordando que es contexto, no una señal.

## Decisión

### 1. Diario (`fin_satellite_journal` + `fin_satellite_journal_ideas`, migración finance 0009)

- `bootstrap.satellite_journal`, paso final de `pos sync` (tras descargar precios): si el
  satélite está activo, hay cierres y hoy (fecha de pared) no está apuntado, guarda lo que dice
  `satellite.overview`. **Una vez al día**: la clave es el día; las siguientes syncs no tocan
  nada. `pos panel` no escribe. Un fallo es un error del informe, nunca rompe la sync.
- Se guarda: régimen, sesión de sus datos e indicadores (clave, valor, voto, texto); índice de
  referencia; por idea el ISIN, símbolo, nombre, tipo (comprar/vigilar/vender), posición en el
  panel y si estaba en el top, precio y su sesión, stop, objetivo, tendencia, rangos estimados
  (cuantiles en %) y tramos (precio y etiqueta).
- **No se guarda**: ningún importe (ni el propuesto ni el de los tramos), ni el motivo de las
  ventas («ganas un 23 %»), ni los avisos de cada idea (ventas con pérdidas, posición, colchón).
  Solo datos de mercado y la decisión. Que una idea sea «vigilar» por falta de presupuesto sí
  queda (es la decisión), sin cifras.

### 2. Evaluación fuera de muestra (`finance/journal.py`, `evaluate`)

- Para cada valor y plazo (21, 63 y 252 sesiones): **casos independientes**, uno cada `h`
  sesiones como mínimo (una idea que sale 20 días seguidos es un caso, no 20), y solo los ya
  cumplidos. Rendimiento del valor menos el del índice de referencia entre las mismas sesiones,
  en puntos (cierres sin dividendos). Las ventas se leen al revés: si el valor lo hace peor que
  el índice, vender acertó.
- Intervalo de confianza al 95 % con *bootstrap* por meses (las ideas de un mismo mes van
  juntas); sin al menos 3 meses distintos, sin intervalo.
- Calibración de los rangos con las ideas que los tenían: cuántas veces la peor caída superó la
  «1 de cada 10», el final quedó bajo el cuantil 10 o sobre el 90, cada una con intervalo de
  Wilson.
- Sesgo conocido: las acciones van sin dividendos y el índice por defecto (`EUNL.DE`) es de
  acumulación, con ellos. La comparación sale algo en contra de las ideas (1–2 puntos al año);
  el texto lo dice.
- Mínimo `MIN_CASES = 10` casos para enseñar un resultado. Mientras tanto se dice cuántos días
  hay apuntados y hacia cuándo se cumplen los primeros casos a 1 mes (o, si ya se cumplen,
  cuántos hay de los 10).
- Se ve en `pos finance journal [--days N]` (últimos días + evaluación) y en una tarjeta
  plegada de la pestaña Ideas («¿Acierta el satélite?») **solo cuando hay algo que enseñar**.

### 3. Aviso de cambio de régimen (`ops/notices.py`, `regime`)

- Se calcula del propio diario (el estado persistido es el diario: nada nuevo que mantener).
  El primer régimen conocido es el punto de partida, sin aviso.
- Un régimen nuevo cuenta si se ve **dos sesiones de mercado seguidas** (`CONFIRM_SESSIONS`).
  La misma sesión apuntada en fin de semana no suma; un «desconocido» o un hueco de más de 5
  días en el diario rompen la racha (sin cambiar el régimen confirmado). A cambio, el aviso
  llega un día más tarde.
- Tras un cambio, el siguiente necesita al menos 5 sesiones (`MIN_SESSIONS_BETWEEN`): un
  indicador que baila en el umbral no genera avisos de ida y vuelta.
- Avisa al pasar a miedo, a euforia o de vuelta a neutral. Id estable
  `regimen:<régimen>:<sesión>` y ventana de 3 días como las alertas: el iPhone lo enseña una vez.
  Tipo `mercado` (no entra en las alertas de gasto de la pestaña Hoy).
- Texto: «🧭 Mercado con miedo · El mercado pasa a miedo: VIX en 31, S&P 500 a −12 % de su
  máximo de 52 semanas. Es contexto, no una señal: en la prueba histórica entrar con miedo no
  dio ventaja. Tus reglas no cambian.» (euforia: «Es contexto, no una señal con ventaja»;
  neutral: «Se acaba el miedo: el mercado vuelve a neutral…»). Si un día `market.fear_edge()`
  fuera cierto, el aviso de miedo diría «la prueba histórica sugiere algo de ventaja, sin
  garantías».

## Consecuencias

- La evaluación tardará: con pocos valores en seguimiento, 10 casos a 1 mes son unos meses de
  diario; a 12 meses, años. Es lo honesto; mientras tanto la tarjeta no aparece.
- El diario guarda lo que dijo el satélite **con las reglas de ese día**; si se cambian umbrales
  o la lista, la evaluación mezcla versiones (se puede filtrar por fecha).
- Si el PC no sincroniza un día, ese día no se apunta (no se rellena hacia atrás: sería dentro
  de muestra). Si la primera sync del día va con precios viejos, se apunta la sesión que había.
- Los intervalos con pocos casos son anchos y el *bootstrap* por meses no corrige que los valores
  se muevan juntos: el texto lo dice («orientación, no prueba»).
