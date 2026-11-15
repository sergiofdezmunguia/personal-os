"""Categorizador de movimientos aprendido de lo ya categorizado (ADR 0016). Puro y determinista.

Lleva a Personal OS lo medido en el proyecto `expense-categorizer`: TF-IDF de caracteres y
palabras del concepto + importe, signo, día de la semana y tipo de cuenta, con regresión
logística. Dos cosas se eligen **sin mirar lo que se va a categorizar**, con validación cruzada
agrupada por comercio (ningún comercio de validación está en su entrenamiento, que es justo el
caso de lo que las reglas no cubren):

- la regularización `C` (la que más acierta en comercios nuevos; empate → la más pequeña);
- el **umbral de abstención**: la confianza más baja con la que, en esa validación, lo propuesto
  acierta al menos `target_precision`. Por debajo, el modelo no propone nada.

Con los datos reales de Sergio (fuera del repo), esta receta acertó el 70 % en comercios nuevos y
se atrevió con el 41 % con un 98 % de precisión. Embeddings y LLM no aportaron: no hay torch.

No guarda nada: se entrena en memoria cada vez (unos segundos) y no deja vectores ni modelos con
datos personales en disco. No conoce la base de datos: recibe y devuelve dataclasses.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np
from scipy import sparse
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold
from sklearn.preprocessing import OneHotEncoder
from threadpoolctl import threadpool_limits

TARGET_PRECISION = 0.90  # objetivo en la validación interna (en la prueba real dio un 98 %)
C_GRID = (1.0, 10.0, 100.0, 1000.0)
FOLDS = 3
MIN_PER_CLASS = 5  # menos ejemplos: ni se aprende ni se valida; el modelo se abstiene
MIN_EXAMPLES = 30
RARE = "__rara__"  # pseudo-clase de las categorías con pocos ejemplos: nunca se propone

# Palabras de relleno de los conceptos bancarios (las mismas que en expense-categorizer).
_NOISE = re.compile(
    r"\b(compra|pago|tarjeta|tarj|card|apple ?pay|contactless|movil|en|de|del|la|el|los|las|"
    r"s\.?l\.?u?|s\.?a\.?|sl|sa|es|esp|spain|madrid|barcelona|tenerife|www|com|online)\b"
)


class NotEnoughData(ValueError):
    """Hay muy pocos movimientos categorizados (o muy pocas categorías) para aprender."""


@dataclass(frozen=True)
class Example:
    description: str
    amount_cents: int
    booking_date: str  # YYYY-MM-DD
    account_kind: str = "bank"
    category: str | None = None  # la etiqueta (solo en entrenamiento)


@dataclass(frozen=True)
class Prediction:
    category: str | None  # None ⇒ se abstiene (poca confianza o categoría rara)
    confidence: float
    best_guess: str  # la más probable, aunque no se proponga


@dataclass(frozen=True)
class TrainingReport:
    """Lo que se sabe del modelo antes de usarlo. Las cifras `cv_*` son de la validación
    agrupada por comercio con la `C` elegida: estiman cómo le irá con comercios nuevos."""

    examples: int
    merchants: int
    categories: int  # categorías que puede proponer (con ≥ MIN_PER_CLASS ejemplos)
    rare_examples: int  # ejemplos de categorías con menos (no se proponen nunca)
    C: float
    threshold: float  # > 1 ⇒ nunca alcanzó el objetivo: no propondrá nada
    target_precision: float
    cv_accuracy: float
    cv_coverage: float
    cv_precision: float | None  # None si no cubre nada


# --------------------------------------------------------------------------- texto


def fold_text(text: str) -> str:
    """Minúsculas, sin tildes y sin signos: «Café Ñoño, S.L.» → «cafe nono s l»."""
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def clean(text: str) -> str:
    """Concepto sin cifras ni palabras de relleno: lo que ve el modelo."""
    s = re.sub(r"\d+", " ", fold_text(text))
    s = _NOISE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def merchant_key(text: str) -> str:
    """Dos primeras palabras significativas. Aproximación para agrupar la validación por
    comercio, no una verdad sobre el comercio."""
    words = [w for w in clean(text).split() if len(w) > 1]
    return " ".join(words[:2]) or "(vacio)"


# --------------------------------------------------------------------------- rasgos


class _Features:
    """TF-IDF (caracteres 2-5 y palabras 1-2) + signo y log del importe + día y tipo de cuenta."""

    def fit(self, items: Sequence[Example]) -> _Features:
        texts = _texts(items)
        self.chars = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), sublinear_tf=True)
        self.words = TfidfVectorizer(
            analyzer="word", ngram_range=(1, 2), sublinear_tf=True, token_pattern=r"\S+"
        )
        self.chars.fit(texts)
        self.words.fit(texts)
        self.onehot = OneHotEncoder(handle_unknown="ignore").fit(_categorical(items))
        return self

    def transform(self, items: Sequence[Example]) -> sparse.csr_matrix:
        texts = _texts(items)
        amounts = np.array([[x.amount_cents / 100] for x in items], dtype=float)
        numeric = np.hstack([(amounts > 0).astype(float), np.log1p(np.abs(amounts))])
        return sparse.hstack(
            [
                self.chars.transform(texts),
                self.words.transform(texts),
                sparse.csr_matrix(numeric),
                self.onehot.transform(_categorical(items)),
            ],
            format="csr",
        )


def _texts(items: Sequence[Example]) -> list[str]:
    return [clean(x.description) or "vacio" for x in items]


def _categorical(items: Sequence[Example]) -> list[list[str]]:
    return [[str(date.fromisoformat(x.booking_date).weekday()), x.account_kind] for x in items]


def _fit(items: Sequence[Example], labels: np.ndarray, C: float):
    features = _Features().fit(items)
    clf = LogisticRegression(C=C, max_iter=3000, class_weight="balanced")
    clf.fit(features.transform(items), labels)
    return features, clf


def pick_threshold(conf: np.ndarray, correct: np.ndarray, target: float) -> float:
    """Confianza más baja cuya precisión acumulada (de más a menos confianza) alcanza el
    objetivo. 1,01 si nunca lo alcanza: así no se propone nada."""
    if not len(conf):
        return 1.01
    order = np.argsort(-conf, kind="stable")
    hits = np.cumsum(correct[order]) / np.arange(1, len(order) + 1)
    ok = np.where(hits >= target)[0]
    return float(conf[order][ok.max()]) if len(ok) else 1.01


# --------------------------------------------------------------------------- modelo


class Categorizer:
    """`Categorizer.train(ejemplos)` → `propose(movimientos)`. Ver el docstring del módulo."""

    def __init__(self, features, clf, report: TrainingReport) -> None:
        self._features = features
        self._clf = clf
        self.report = report

    @classmethod
    def train(
        cls,
        examples: Sequence[Example],
        *,
        target_precision: float = TARGET_PRECISION,
        grid: Sequence[float] = C_GRID,
        folds: int = FOLDS,
    ) -> Categorizer:
        items = [x for x in examples if x.category]
        if len(items) < MIN_EXAMPLES:
            raise NotEnoughData(
                f"Hacen falta al menos {MIN_EXAMPLES} movimientos categorizados (hay {len(items)})"
            )
        counts = Counter(x.category for x in items)
        labels = np.array(
            [x.category if counts[x.category] >= MIN_PER_CLASS else RARE for x in items]
        )
        real = {c for c in labels if c != RARE}
        groups = np.array([merchant_key(x.description) for x in items])
        if len(real) < 2 or len(set(groups)) < folds:
            raise NotEnoughData(
                "Hacen falta al menos 2 categorías con "
                f"{MIN_PER_CLASS} ejemplos y {folds} comercios distintos"
            )
        with threadpool_limits(1):  # en WSL, todos los hilos de BLAS lo hacen 40 veces más lento
            C, oof = _choose_c(items, labels, groups, grid, folds)
            pred = np.array([p[0] for p in oof])
            conf = np.array([p[1] for p in oof])
            proposable = pred != RARE
            correct = pred == labels
            threshold = pick_threshold(conf[proposable], correct[proposable], target_precision)
            covered = proposable & (conf >= threshold)
            features, clf = _fit(items, labels, C)
        report = TrainingReport(
            examples=len(items),
            merchants=len(set(groups)),
            categories=len(real),
            rare_examples=int((labels == RARE).sum()),
            C=C,
            threshold=threshold,
            target_precision=target_precision,
            cv_accuracy=float(correct.mean()),
            cv_coverage=float(covered.mean()),
            cv_precision=float(correct[covered].mean()) if covered.any() else None,
        )
        return cls(features, clf, report)

    def predict(self, items: Sequence[Example]) -> list[Prediction]:
        if not items:
            return []
        with threadpool_limits(1):
            proba = self._clf.predict_proba(self._features.transform(items))
        out = []
        for row in proba:
            best = int(row.argmax())
            guess, conf = str(self._clf.classes_[best]), float(row[best])
            ok = guess != RARE and conf >= self.report.threshold
            out.append(Prediction(guess if ok else None, conf, guess))
        return out


def _choose_c(items, labels, groups, grid, folds) -> tuple[float, list[tuple[str, float]]]:
    """Validación agrupada por comercio para cada C. Devuelve la mejor y sus predicciones
    fuera de pliegue (categoría, confianza), con las que se elige el umbral."""
    splits = list(GroupKFold(n_splits=folds).split(items, labels, groups))
    prepared = []
    for tr, va in splits:
        train_items = [items[i] for i in tr]
        features = _Features().fit(train_items)
        prepared.append(
            (tr, va, features.transform(train_items), features.transform([items[i] for i in va]))
        )
    best: tuple[float, float, list] | None = None
    for C in sorted(grid):
        oof: list[tuple[str, float]] = [("", 0.0)] * len(items)
        for tr, va, X_tr, X_va in prepared:
            y_tr = labels[tr]
            if len(set(y_tr)) < 2:  # un pliegue con una sola clase: se predice esa
                for i in va:
                    oof[i] = (str(y_tr[0]), 1.0)
                continue
            clf = LogisticRegression(C=C, max_iter=3000, class_weight="balanced").fit(X_tr, y_tr)
            proba = clf.predict_proba(X_va)
            for i, row in zip(va, proba, strict=True):
                k = int(row.argmax())
                oof[i] = (str(clf.classes_[k]), float(row[k]))
        acc = float(np.mean([p[0] == y for p, y in zip(oof, labels, strict=True)]))
        if best is None or acc > best[1] + 1e-12:  # empate → se queda la C más pequeña
            best = (C, acc, oof)
    assert best is not None
    return best[0], best[2]


def evaluate_new_merchants(
    examples: Sequence[Example],
    *,
    folds: int = 5,
    target_precision: float = TARGET_PRECISION,
) -> dict:
    """Evaluación honesta para el ADR y `pos finance suggest --evaluate`: GroupKFold por
    comercio; en cada pliegue se entrena como en producción (C y umbral con validación interna
    del propio entrenamiento) y se mide en comercios que no ha visto. Solo cifras agregadas."""
    items = [x for x in examples if x.category]
    groups = np.array([merchant_key(x.description) for x in items])
    if len(set(groups)) < folds:
        raise NotEnoughData(f"Hacen falta al menos {folds} comercios distintos")
    labels = np.array([x.category for x in items])
    hits = proposed = proposed_ok = 0
    for tr, va in GroupKFold(n_splits=folds).split(items, labels, groups):
        model = Categorizer.train([items[i] for i in tr], target_precision=target_precision)
        for i, p in zip(va, model.predict([items[i] for i in va]), strict=True):
            hits += p.best_guess == labels[i]
            if p.category is not None:
                proposed += 1
                proposed_ok += p.category == labels[i]
    n = len(items)
    return {
        "examples": n,
        "merchants": len(set(groups)),
        "folds": folds,
        "accuracy": hits / n,
        "coverage": proposed / n,
        "precision_covered": proposed_ok / proposed if proposed else math.nan,
    }
