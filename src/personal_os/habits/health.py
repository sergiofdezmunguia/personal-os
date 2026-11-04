"""Parser del registro diario de Apple Salud (ADR 0013).

Una línea por día, escrita por el atajo «Salud del día» en
`<bandeja>/salud/salud.txt`:

    2026-11-05; pasos=8423; ejercicio=45; sueño=7,2; kcal=2100; peso=72,5

Claves conocidas (todas opcionales): pasos, ejercicio (min), sueño (h), kcal (consumidas,
p. ej. de Fitia), activas (kcal en movimiento), peso (kg). Atajos puede escribir los números con
separador de miles («8.423») o coma decimal. Si un día aparece varias veces gana la última
línea. Lo que no se entiende se ignora y se cuenta.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta

FILE = "salud.txt"
FOLDER = "salud"
INTEGER = {"pasos", "ejercicio", "kcal", "activas"}
DECIMAL = {"sueno", "peso"}
ALIASES = {"steps": "pasos", "ejercicio_min": "ejercicio", "minutos": "ejercicio",
           "sleep": "sueno", "dormido": "sueno", "calorias": "kcal", "energia": "kcal",
           "activa": "activas", "weight": "peso"}  # fmt: skip
GOALS = {"pasos": 8000, "ejercicio": 30, "sueno": 7.0}

_DAY = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})")


def _key(text: str) -> str:
    k = unicodedata.normalize("NFD", text.strip().lower())
    k = "".join(c for c in k if unicodedata.category(c) != "Mn")
    k = re.sub(r"[^a-z_]", "", k)
    return ALIASES.get(k, k)


def _number(text: str, *, integer: bool) -> float | None:
    t = re.sub(r"[^\d,.\-]", "", text)
    if not t or not re.search(r"\d", t):
        return None
    if integer:
        t = re.sub(r"[.,]\d{1,2}$", "", t) if re.search(r"[.,]\d{1,2}$", t) else t
        t = re.sub(r"[.,]", "", t)
    else:
        t = t.replace(".", "").replace(",", ".") if "," in t else t
    try:
        value = float(t)
    except ValueError:
        return None
    return round(value) if integer else round(value, 2)


@dataclass(frozen=True)
class Day:
    day: str
    values: dict[str, float] = field(default_factory=dict)

    def get(self, key: str) -> float | None:
        return self.values.get(key)

    def met(self, key: str) -> bool | None:
        v = self.values.get(key)
        return None if v is None or key not in GOALS else v >= GOALS[key]


def parse(text: str) -> tuple[dict[str, Day], int]:
    """Devuelve {día: Day} y cuántas líneas no se entendieron."""
    days: dict[str, Day] = {}
    bad = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        m = _DAY.match(line)
        if not m:
            bad += 1
            continue
        try:
            date.fromisoformat(m.group(1))
        except ValueError:
            bad += 1
            continue
        values: dict[str, float] = {}
        for part in line[m.end() :].split(";"):
            if "=" not in part and ":" not in part:
                continue
            k, _, v = part.replace(":", "=", 1).partition("=")
            key = _key(k)
            if key not in INTEGER | DECIMAL:
                continue
            n = _number(v, integer=key in INTEGER)
            if n is not None and n >= 0:
                values[key] = n
        if not values:
            bad += 1
            continue
        days[m.group(1)] = Day(m.group(1), values)
    return days, bad


@dataclass(frozen=True)
class Summary:
    """Últimos `n` días hasta `today` (incluido el de ayer: hoy aún no está completo)."""

    days: list[Day]  # del más antiguo al más reciente; los que faltan, vacíos
    averages: dict[str, float]
    streaks: dict[str, int]  # días seguidos cumpliendo el objetivo, hasta ayer


def summarize(days: dict[str, Day], *, today: str, n: int = 28) -> Summary:
    end = date.fromisoformat(today) - timedelta(days=1)
    window = [(end - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]
    seq = [days.get(d, Day(d)) for d in window]
    averages = {}
    for key in INTEGER | DECIMAL:
        vals = [d.values[key] for d in seq if key in d.values]
        if vals:
            averages[key] = round(sum(vals) / len(vals), 1)
    streaks = {}
    for key in GOALS:
        count = 0
        for d in reversed(seq):
            if not d.met(key):
                break
            count += 1
        streaks[key] = count
    return Summary(seq, averages, streaks)
