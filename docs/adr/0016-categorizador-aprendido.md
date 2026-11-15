# 0016 — Categorizador aprendido: propuestas con abstención

Fecha: 2026-11-15 · Estado: propuesta

## Contexto

Las reglas resuelven lo conocido («MERCADONA → supermercado») y nada de lo nuevo; lo que no
casa con ninguna lo categoriza a mano el usuario o Claude en el cierre mensual. El proyecto
aparte `expense-categorizer` midió, con una evaluación honesta sobre los movimientos reales del
usuario (en local; en ese repo solo hay código, datos sintéticos y cifras agregadas), cuánto se
puede automatizar con un modelo pequeño y explicable:

- TF-IDF de caracteres y palabras del concepto + importe, signo, día y tipo de cuenta, con
  regresión logística, con `C` elegida por validación interna **agrupada por comercio**: en
  comercios que el modelo no había visto acertó el **70 %** y, con un umbral de abstención
  elegido también sin mirar la prueba, se atrevió con el **41 %** de los movimientos con un
  **98 %** de precisión.
- Embeddings multilingües (e5-small, MiniLM) y un LLM local en zero-shot no aportaron (42 %,
  37 % y 30 % de acierto) y cuestan cientos de MB y mucha latencia.

Los movimientos sin categoría que llegan al cierre son justo los de comercios que las reglas no
conocen, así que la cifra que importa es la de comercios nuevos.

## Decisión

- **Modelo** (`finance/categorizer.py`, puro, determinista, sin torch ni LLM): la misma receta.
  `C ∈ {1, 10, 100, 1000}` y el umbral se eligen con `GroupKFold(3)` por clave de comercio
  (dos primeras palabras significativas del concepto) sobre lo ya categorizado: el umbral es
  la confianza más baja con la que lo propuesto en esa validación acierta al menos el 90 %
  (`TARGET_PRECISION`, el objetivo con el que se obtuvo el 98 % real). Las categorías con menos
  de 5 ejemplos se juntan en una pseudo-clase que nunca se propone. Por simplicidad, el umbral
  sale de las predicciones fuera de pliegue de la `C` elegida (no de una segunda validación
  anidada): algo optimista, y por eso la evaluación (abajo) sí entrena como en producción.
- **Entrena con la propia base**: todo lo categorizado (a mano, reglas o Claude). En memoria,
  unos segundos, cada vez que se pide. **No se guarda ningún modelo ni vector**; si algún día
  hiciera falta cachearlo, iría al directorio de datos del usuario, nunca al repo.
- **Propuestas, no categorías** (`finance/suggestions.py`, tabla
  `fin_category_suggestions`, migración `finance/0009`): una fila por movimiento sin
  categoría con `category`, `confidence` y `status` (`pending` · `accepted` · `rejected` ·
  `superseded`). Proponer **no toca `fin_transactions`**: el resumen del mes no cambia hasta
  aprobar. Es el análogo de las reglas `proposed`.
- **Solo el usuario revisa** (actor `cli`). Aprobar = `FinanceService.set_category` como el
  usuario (`category_source = 'manual'`, `categorized_by = 'cli'`) y solo si el movimiento
  sigue sin categoría; si entretanto lo categorizó él, una regla o Claude, la propuesta queda
  `superseded` y no se toca nada. Rechazar es definitivo para ese movimiento.
- **Fuera de la sync**: se ejecuta solo bajo demanda (`pos finance suggest`, MCP
  `model_category_suggestions` con `refresh`). La sync sigue siendo determinista y sin modelos.
- Interfaces: `pos finance suggest [-m AAAA-MM] [--evaluate]`,
  `pos finance suggestion list|approve|reject`, el cierre (`pos finance close` y
  `finance_month_close`) cuenta las propuestas pendientes, y la skill `monthly-close` las pide
  antes de categorizar para que Claude no repita trabajo.
- Dependencia nueva: `scikit-learn` (trae numpy y scipy), solo en el venv.

## Evaluación reproducible (datos inventados)

`uv run python scripts/categorizer_eval.py` (generador `tests/categorizer_fake.py`; GroupKFold
de 5 pliegues por comercio; cada pliegue entrena como en producción, con su propia `C` y su
propio umbral):

| Escenario | Comercios | Acierto | Cobertura | Precisión cubierta |
|---|---|---|---|---|
| fácil (1.500 movs., 3 % ruido, 12 % ambiguos) | 638 | 88,2 % | 97,9 % | 89,5 % |
| difícil (600 movs., 15 % ruido, 35 % ambiguos) | 353 | 68,3 % | 7,7 % | 89,1 % |

El escenario fácil queda fijado en `test_reproducible_evaluation_on_new_merchants`. Los datos
sintéticos comparten trozos de nombre entre comercios de la misma categoría, justo lo que
aprovecha TF-IDF, y por eso son más fáciles que los reales: las cifras que justifican la
decisión son las reales de arriba. Con los datos del usuario, en local y solo con cifras
agregadas: `pos finance suggest --evaluate`.

## Consecuencias

- Menos trabajo manual en el cierre en lo que el modelo tiene claro; lo dudoso sigue igual
  (reglas propuestas, Claude, preguntas al usuario).
- El modelo imita lo ya categorizado, errores incluidos: si una regla está mal, el modelo
  aprende ese error. Lo categorizado por Claude (`mcp`) también se usa para entrenar.
- Una propuesta aprobada se ve igual que una categoría manual del usuario (lo es: la aprobó él);
  su origen queda en `fin_category_suggestions` (`accepted`) y en `events`.
- Si no hay al menos 30 movimientos categorizados, 2 categorías con 5 ejemplos y 3 comercios,
  no propone nada y lo dice.
- La precisión real con comercios nuevos se debe vigilar: `suggestion list --status rejected`
  frente a `accepted` da la tasa de acierto de lo propuesto.
