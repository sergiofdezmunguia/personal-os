"""Cotizaciones públicas por ISIN (ADR 0012). Solo lectura y solo datos de mercado: a estas
fuentes solo se envía el ISIN, nunca nada del usuario.

1. justETF (`/api/etfs/<isin>/quote`, en EUR): último cierre y el anterior.
2. Yahoo Finance (búsqueda por ISIN → símbolo → gráfico diario), solo si cotiza en EUR.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import httpx

UA = {"User-Agent": "Mozilla/5.0 (personal-os)"}
TIMEOUT = 15


class QuoteError(Exception):
    pass


@dataclass(frozen=True)
class Quote:
    isin: str
    day: str  # YYYY-MM-DD
    price: Decimal
    currency: str
    source: str


class QuotesClient:
    def __init__(self, http: httpx.Client | None = None) -> None:
        self.http = http or httpx.Client(headers=UA, timeout=TIMEOUT, follow_redirects=True)

    def quotes(self, isin: str) -> list[Quote]:
        """Últimos cierres conocidos (el más reciente primero). Prueba las fuentes en orden."""
        errors = []
        for fetch in (self._justetf, self._yahoo):
            try:
                found = fetch(isin)
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
                errors.append(f"{fetch.__name__.strip('_')}: {exc}")
                continue
            if found:
                return found
        raise QuoteError(f"Sin cotización para {isin} ({'; '.join(errors) or 'sin datos'})")

    def _justetf(self, isin: str) -> list[Quote]:
        r = self.http.get(
            f"https://www.justetf.com/api/etfs/{isin}/quote",
            params={"locale": "en", "currency": "EUR", "isin": isin},
        )
        r.raise_for_status()
        d = r.json()
        out = [Quote(isin, d["latestQuoteDate"], Decimal(str(d["latestQuote"]["raw"])), "EUR",
                     "justetf")]  # fmt: skip
        if d.get("previousQuoteDate") and d.get("previousQuote"):
            out.append(Quote(isin, d["previousQuoteDate"],
                             Decimal(str(d["previousQuote"]["raw"])), "EUR", "justetf"))  # fmt: skip
        return out

    def _yahoo(self, isin: str) -> list[Quote]:
        r = self.http.get(
            "https://query2.finance.yahoo.com/v1/finance/search",
            params={"q": isin, "quotesCount": 5, "newsCount": 0},
        )
        r.raise_for_status()
        for q in r.json().get("quotes", []):
            c = self.http.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{q['symbol']}",
                params={"range": "5d", "interval": "1d"},
            )
            c.raise_for_status()
            res = c.json()["chart"]["result"][0]
            if res["meta"].get("currency") != "EUR":
                continue
            closes = res["indicators"]["quote"][0]["close"]
            pairs = [(ts, v) for ts, v in zip(res["timestamp"], closes, strict=False) if v]
            return [
                Quote(
                    isin,
                    datetime.fromtimestamp(ts, UTC).date().isoformat(),
                    Decimal(str(round(v, 4))),
                    "EUR",
                    "yahoo",
                )
                for ts, v in reversed(pairs[-2:])
            ]
        return []


@dataclass(frozen=True)
class History:
    symbol: str
    currency: str
    closes: list[tuple[str, Decimal]]  # (día, cierre), del más antiguo al más reciente


def history(client: QuotesClient, symbol: str, *, range_: str = "1y") -> History:
    """Cierres diarios de un símbolo de Yahoo (p. ej. «APC.DE»)."""
    try:
        r = client.http.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            params={"range": range_, "interval": "1d"},
        )
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        closes = res["indicators"]["quote"][0]["close"]
        pairs = [
            (datetime.fromtimestamp(ts, UTC).date().isoformat(), Decimal(str(round(v, 4))))
            for ts, v in zip(res["timestamp"], closes, strict=False)
            if v
        ]
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise QuoteError(f"Sin histórico para {symbol} ({type(exc).__name__})") from exc
    if not pairs:
        raise QuoteError(f"Sin histórico para {symbol}")
    return History(symbol, res["meta"].get("currency", ""), pairs)
