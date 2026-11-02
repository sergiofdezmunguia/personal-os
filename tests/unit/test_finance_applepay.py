"""Pagos de Apple Pay → comercio en movimientos sin concepto. Datos inventados."""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from personal_os import bootstrap
from personal_os.core.config import write_config_template
from personal_os.core.events import ChangeContext
from personal_os.finance.importers import ParsedStatement, ParsedTransaction, applepay

HOLE = "Trade Republic · sin concepto"
TZ = ZoneInfo("Europe/Madrid")
LOG = """2026-10-03T13:05:00+02:00; 12,40 €; CAFE EJEMPLO; Tarjeta de prueba
2026-10-03T21:40:00+02:00; 1.234,50 €; TIENDA GRANDE; Tarjeta de prueba
2026-10-04T09:00:00+02:00; ; ; 

2026-10-04T10:00:00+02:00; 3,00 €; METRO FICTICIO; Tarjeta de prueba
basura sin formato
"""


def test_parse_skips_non_payments():
    payments, skipped = applepay.parse(LOG)
    assert [p.amount_cents for p in payments] == [1240, 123450, 300]
    assert payments[0].merchant == "CAFE EJEMPLO" and payments[0].at.utcoffset()
    assert skipped == 2


@pytest.fixture
def svc(clock):
    write_config_template()
    app = bootstrap.open_app(clock=clock)
    yield bootstrap.finance_service(app)
    app.db.close()


def seed(svc, rows):
    txs = [ParsedTransaction(d, None, desc, c, None, f"prov:{i}") for i, (d, c, desc) in
           enumerate(rows)]  # fmt: skip
    st = ParsedStatement("fake", "trade_republic", "TR", "ES00", "EUR", txs, account_kind="broker")
    svc.import_statement(ChangeContext.cli(), st, file_name="f", file_sha256="x")


def test_enrich_matches_amount_and_nearest_day(svc):
    svc.add_rule(ChangeContext.cli(), pattern="metro ficticio", category="transporte-publico")
    seed(svc, [
        ("2026-10-03", -1240, HOLE),
        ("2026-10-05", -1240, HOLE),  # mismo importe, más lejos: no es el del café
        ("2026-10-06", -300, HOLE),
        ("2026-10-04", -999, "Pago: YA TENÍA CONCEPTO"),
    ])  # fmt: skip
    payments, _ = applepay.parse(LOG)
    n = svc.enrich_from_card_payments(ChangeContext.system(), payments, placeholders=[HOLE], tz=TZ)
    assert n == 2
    by_date = {t.booking_date: t for t in svc.list_transactions(limit=None)}
    assert by_date["2026-10-03"].description == "CAFE EJEMPLO"
    assert by_date["2026-10-05"].description == HOLE
    assert by_date["2026-10-06"].category == "transporte-publico"
    # Idempotente: el café ya no es un hueco y el otro 12,40 no casa en fecha.
    again = svc.enrich_from_card_payments(
        ChangeContext.system(), payments, placeholders=[HOLE], tz=TZ
    )
    assert again == 0


def test_sync_step_reads_inbox_file(clock, tmp_path):
    cfg = write_config_template()
    inbox = tmp_path / "bandeja"
    (inbox / "applepay").mkdir(parents=True)
    (inbox / "applepay" / "pagos.txt").write_text(LOG, encoding="utf-8")
    cfg.write_text(cfg.read_text().replace("[finance]", f'[finance]\ninbox_dir = "{inbox}"', 1))
    app = bootstrap.open_app(clock=clock)
    try:
        seed(bootstrap.finance_service(app), [("2026-10-03", -1240, HOLE)])
        report = bootstrap.applepay_enrich(app)
        assert report.stats == {"payments": 3, "lines_ignored": 2, "transactions_enriched": 1}
    finally:
        app.db.close()
