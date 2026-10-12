# ADR 0009 — Panel «Pulso del mes»: HTML generado en el PC, visto en PC e iPhone

**Estado:** aceptado · 2026-10-13

## Contexto
El usuario quiere ver de un vistazo cómo va de gasto este mes, en qué gasta más, dónde
centrarse para recortar, cuánto ahorra (en efectivo e invertido) y dónde está su dinero, y
poder bajar hasta comercios y movimientos. Restricciones de siempre: sin app propia, sin web
pública ni servicios 24/7, y los datos no salen de su PC ni de su iCloud.

Prueba en su iPhone (2026-10-13): la vista previa de **Archivos** muestra HTML y CSS y
despliega `<details>`, pero **no ejecuta JavaScript**; el `WebView` de **Scriptable** sí.

## Decisión
- **Cálculo en el dominio** (`finance/insights.py`, solo lectura y determinista): habitual =
  media de los 3 últimos meses cerrados; fijo (alquiler, suministros, suscripciones, seguros)
  frente a variable; el ritmo del mes se mide solo con el variable y con la curva diaria
  real de los meses habituales (no una recta); invertido = compras − ventas de `fin_trades`;
  ahorro en efectivo = ingresos − gasto − invertido. «Dónde centrarme» = los 3 mayores
  gastos variables accionables, con sus motivos (frecuencia, comercio dominante, tendencia).
- **Una página HTML autocontenida** (`ops/panel.py`): gráficos SVG generados en Python y
  cifras escritas, de modo que se lee entera sin JavaScript; el desglose categoría →
  comercio → movimientos usa `<details>`. El JavaScript solo añade tooltips. Sin recursos
  externos.
- **Dónde y cuándo**: tras cada `pos sync` (la tarea de Windows, 3 veces al día) y con
  `pos panel`, que además la abre en el navegador de Windows. Se escribe en
  `<buzón de Scriptable>/panel/index.html`; en el iPhone la abre el script «Personal OS
  Panel» (lo instala `pos bridge install`) con JavaScript, o Archivos sin él.
- Si generar el panel falla, la sync no falla: solo avisa.

## Alternativas descartadas
- **Página publicada (claude.ai u otra web)**: cómoda en cualquier dispositivo, pero saca los
  datos de su PC/iCloud.
- **Servidor local** (`pos serve`): da acciones (categorizar desde el panel), pero solo en
  el PC y encendido; el usuario quiere analizar, no editar. Se puede añadir más adelante
  sobre la misma página.
- **App de gráficos en el iPhone (Atajos, widgets)**: limitada y difícil de mantener.

## Consecuencias
- El panel solo es tan bueno como la categorización: lo sin categoría no cuenta como gasto
  (se avisa arriba).
- Los primeros días del mes la proyección es poco fiable (se avisa).
- Contiene datos financieros sin cifrar en iCloud Drive, igual que los extractos archivados.
