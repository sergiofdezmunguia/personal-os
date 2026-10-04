"""Validación y descripción de reglas RRULE (subconjunto soportado), compartidas por
tareas y calendario."""

from __future__ import annotations

import re


class RRuleError(ValueError):
    pass


_ALLOWED = {"FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "COUNT", "UNTIL"}
_FREQS = {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}
_DAYS = {"MO", "TU", "WE", "TH", "FR", "SA", "SU"}


def normalize_rrule(rule: str) -> str:
    """Valida y devuelve la regla en forma canónica (claves en orden fijo, sin 'RRULE:')."""
    text = rule.strip()
    if text.upper().startswith("RRULE:"):
        text = text[6:]
    parts: dict[str, str] = {}
    for chunk in filter(None, text.split(";")):
        if "=" not in chunk:
            raise RRuleError(f"Regla inválida: {chunk!r}")
        key, value = chunk.split("=", 1)
        key, value = key.strip().upper(), value.strip().upper()
        if key not in _ALLOWED:
            raise RRuleError(f"Parte de RRULE no soportada: {key}")
        parts[key] = value

    if parts.get("FREQ") not in _FREQS:
        raise RRuleError(f"FREQ debe ser uno de {sorted(_FREQS)}")
    if "INTERVAL" in parts and (not parts["INTERVAL"].isdigit() or int(parts["INTERVAL"]) < 1):
        raise RRuleError("INTERVAL debe ser un entero >= 1")
    if "BYDAY" in parts:
        days = parts["BYDAY"].split(",")
        if not days or any(d not in _DAYS for d in days):
            raise RRuleError("BYDAY admite solo MO,TU,WE,TH,FR,SA,SU (sin ordinales)")
    if "BYMONTHDAY" in parts:
        for d in parts["BYMONTHDAY"].split(","):
            if not re.fullmatch(r"-?\d{1,2}", d) or not (1 <= abs(int(d)) <= 31):
                raise RRuleError("BYMONTHDAY inválido")
    if "COUNT" in parts and "UNTIL" in parts:
        raise RRuleError("COUNT y UNTIL son excluyentes")
    if "COUNT" in parts and (not parts["COUNT"].isdigit() or int(parts["COUNT"]) < 1):
        raise RRuleError("COUNT debe ser un entero >= 1")
    if "UNTIL" in parts and not re.fullmatch(r"\d{8}", parts["UNTIL"]):
        raise RRuleError("UNTIL debe ser una fecha YYYYMMDD")

    order = ["FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "COUNT", "UNTIL"]
    return ";".join(f"{k}={parts[k]}" for k in order if k in parts)


def describe(rule: str) -> str:
    """Descripción corta para la CLI."""
    parts = dict(p.split("=", 1) for p in rule.split(";"))
    names = {
        "DAILY": ("día", "días"),
        "WEEKLY": ("semana", "semanas"),
        "MONTHLY": ("mes", "meses"),
        "YEARLY": ("año", "años"),
    }
    interval = int(parts.get("INTERVAL", "1"))
    singular, plural = names[parts["FREQ"]]
    text = f"cada {singular}" if interval == 1 else f"cada {interval} {plural}"
    if "BYDAY" in parts:
        text += f" ({parts['BYDAY']})"
    if "BYMONTHDAY" in parts:
        text += f" (día {parts['BYMONTHDAY']})"
    return text
