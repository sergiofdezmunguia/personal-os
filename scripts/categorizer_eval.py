"""Evaluación reproducible del categorizador (ADR 0016) con datos INVENTADOS.

    uv run python scripts/categorizer_eval.py

Entrena como en producción (C y umbral de abstención con validación interna agrupada por
comercio) y mide en comercios que el modelo no ha visto (GroupKFold, 5 pliegues). Nunca toca
la base ni la configuración del usuario. Con tus datos reales, en local y solo con cifras
agregadas: `pos finance suggest --evaluate`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # para importar tests/

from personal_os.finance.categorizer import TARGET_PRECISION, evaluate_new_merchants
from tests import categorizer_fake as fake

SCENARIOS = {
    "fácil (1.500 movs., 3 % ruido, 12 % ambiguos)": dict(n=1500, seed=7),
    "difícil (600 movs., 15 % ruido, 35 % ambiguos)": dict(
        n=600, seed=11, label_noise=0.15, ambiguous=0.35
    ),
}


def main() -> int:
    print(f"Objetivo de precisión en la validación interna: {TARGET_PRECISION:.0%}\n")
    print("| Escenario | Comercios | Acierto | Cobertura | Precisión cubierta |")
    print("|---|---|---|---|---|")
    for name, kw in SCENARIOS.items():
        ev = evaluate_new_merchants(fake.generate(**kw))
        print(
            f"| {name} | {ev['merchants']} | {ev['accuracy']:.1%} | {ev['coverage']:.1%} |"
            f" {ev['precision_covered']:.1%} |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
