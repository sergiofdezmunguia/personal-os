"""Precios diarios por ISIN (ADR 0012): cliente HTTP simulado y datos inventados."""

from __future__ import annotations

import hashlib
from decimal import Decimal

import httpx
import pytest

from personal_os import bootstrap
from personal_os.adapters.quotes import Quote, QuoteError, QuotesClient
from personal_os.core.config import write_config_template
from personal_os.core.events import ChangeContext
from personal_os.finance import investments as inv
from personal_os.finance.importers import detect
from tests import trade_republic_activity_fake as fake

CLI = ChangeContext.cli()
TODAY = "2026-10-05"


@pytest.fixture
def app(clock):
    write_config_template()
    application = bootstrap.open_app(clock=clock)
    yield application
    application.db.close()


@pytest.fixture
def svc(app):
    s = bootstrap.finance_service(app)
    data = fake.build_csv()
    inv.import_document(s, CLI, detect(data, "f.csv"), file_name="f.csv",
                        file_sha256=hashlib.sha256(data).hexdigest())  # fmt: skip
    return s


def _justetf(isin, price="12.34", prev="12.00"):
    return {
        "latestQuote": {"raw": float(price), "localized": price},
        "latestQuoteDate": "2026-10-02",
        "previousQuote": {"raw": float(prev), "localized": prev},
        "previousQuoteDate": "2026-10-01",
    }


def client(handler) -> QuotesClient:
    return QuotesClient(httpx.Client(transport=httpx.MockTransport(handler)))


def test_justetf_quote():
    def handler(req):
        assert (
            req.url.path == "/api/etfs/XF000FAKE001/quote" and req.url.params["currency"] == "EUR"
        )
        return httpx.Response(200, json=_justetf("XF000FAKE001"))

    got = client(handler).quotes("XF000FAKE001")
    assert got == [
        Quote("XF000FAKE001", "2026-10-02", Decimal("12.34"), "EUR", "justetf"),
        Quote("XF000FAKE001", "2026-10-01", Decimal("12.0"), "EUR", "justetf"),
    ]


def test_falls_back_to_yahoo_only_in_eur():
    def handler(req):
        if "justetf" in req.url.host:
            return httpx.Response(404)
        if req.url.path.endswith("/search"):
            return httpx.Response(
                200, json={"quotes": [{"symbol": "FAKE.US"}, {"symbol": "FAKE.DE"}]}
            )
        cur = "USD" if "FAKE.US" in req.url.path else "EUR"
        return httpx.Response(200, json={"chart": {"result": [{
            "meta": {"currency": cur},
            "timestamp": [1790000000, 1790086400],
            "indicators": {"quote": [{"close": [10.5, 10.75]}]},
        }]}})  # fmt: skip

    got = client(handler).quotes("XF000FAKE002")
    assert [(q.price, q.source) for q in got] == [
        (Decimal("10.75"), "yahoo"),
        (Decimal("10.5"), "yahoo"),
    ]


def test_no_source_raises():
    with pytest.raises(QuoteError, match="XF000FAKE009"):
        client(lambda req: httpx.Response(500)).quotes("XF000FAKE009")


def test_live_portfolio_values_today_holdings(svc):
    isins = inv.open_isins(svc)
    assert isins and all(i.startswith("XF000FAKE") for i in isins)
    rows = []
    for i in isins:
        rows += [
            (i, "2026-10-02", Decimal("20"), "EUR", "t"),
            (i, "2026-10-01", Decimal("19"), "EUR", "t"),
        ]
    assert inv.store_prices(svc, rows, fetched_at="x") == 2 * len(isins)
    assert inv.store_prices(svc, rows, fetched_at="y") == 0  # mismo precio: nada nuevo
    (lp,) = inv.live_portfolios(svc, today=TODAY)
    assert lp.complete and lp.as_of == "2026-10-02"
    shares = sum(p.shares for p in lp.positions)
    assert lp.value_cents == int(shares * 2000)
    assert lp.day_change_cents == int(shares * 100)
    # Un precio de hace más de una semana no vale.
    assert not inv.live_portfolios(svc, today="2026-10-20")[0].complete


class FakeQuotes:
    def __init__(self):
        self.calls = 0

    def quotes(self, isin):
        self.calls += 1
        return [Quote(isin, "2026-10-02", Decimal("20"), "EUR", "fake")]


def test_update_prices_is_throttled(app, svc, clock):
    fq = FakeQuotes()
    report = bootstrap.update_prices(app, client=fq)
    assert report.stats["prices_new"] == len(inv.open_isins(svc)) and not report.errors
    assert bootstrap.update_prices(app, client=fq) is None  # hace menos de 6 h
    clock.advance(hours=7)
    assert bootstrap.update_prices(app, client=fq).stats["prices_new"] == 0
