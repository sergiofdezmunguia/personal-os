from __future__ import annotations

import asyncio
import dataclasses
import hashlib
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from personal_os import bootstrap
from personal_os.cli.main import app as cli
from personal_os.core.config import write_config_template
from personal_os.core.events import ChangeContext
from personal_os.finance import investments as inv
from personal_os.finance.importers import (
    ParsedActivity,
    ParsedStatement,
    ParsedValuation,
    StatementError,
    detect,
    trade_republic_csv,
    trade_republic_networth,
)
from personal_os.finance.inbox import process_inbox
from personal_os.finance.models import FinanceError
from personal_os.mcp_server.server import build_server
from tests import trade_republic_activity_fake as fake

CLI = ChangeContext.cli()
TODAY = "2026-10-05"  # el reloj de los tests


@pytest.fixture
def app(clock):
    write_config_template()
    application = bootstrap.open_app(clock=clock)
    yield application
    application.db.close()


@pytest.fixture
def svc(app):
    return bootstrap.finance_service(app)


def do_import(svc, data: bytes, name="fichero"):
    return inv.import_document(
        svc, CLI, detect(data, name), file_name=name, file_sha256=hashlib.sha256(data).hexdigest()
    )


# ------------------------------------------------------------------ parser CSV


def test_csv_is_detected_and_sorted_with_running_balance():
    act = detect(fake.build_csv(), "Exportación de transacción.csv")
    assert isinstance(act, ParsedActivity) and act.source == "trade_republic_csv"
    txs = act.cash.transactions
    assert len(txs) == len(fake.ROWS) and act.period == ("2026-06-01", "2026-10-02")
    assert [t.booking_date for t in txs] == sorted(t.booking_date for t in txs)
    assert (
        txs[-1].balance_after_cents == act.cash.balance_end_cents == fake.cash_at(fake.ROWS, "2099")
    )
    assert all(t.external_id for t in txs) and act.cash.adopt_days == 3
    assert act.warnings == []


def test_csv_descriptions_use_the_account_statement_prefixes_and_mask_identifiers():
    txs = detect(fake.build_csv(), "x.csv").cash.transactions
    text = {t.description.split(":")[0] for t in txs}
    assert text == {
        "Transferencia", "Transacción con tarjeta", "Operar", "Rendimientos", "Interés",
        "Bonificación", "Recibos domiciliados",
    }  # fmt: skip
    joined = " ".join(t.description for t in txs)
    assert "ES····1234" in joined and "ES0000000000000000001234" not in joined
    assert "····4321" in joined and "4000000000004321" not in joined
    assert "+··· ···222" in joined
    assert "Persona Inventada" not in joined and "9999" not in joined  # contraparte nunca


def test_csv_amount_includes_fee_and_tax():
    by_id = {t.external_id: t for t in detect(fake.build_csv(), "x.csv").cash.transactions}
    for row in fake.ROWS:
        assert by_id[row.tid].amount_cents == row.cash_cents


def test_csv_trades():
    trades = detect(fake.build_csv(), "x.csv").trades
    kinds = [t.kind for t in trades]
    assert kinds.count("buy") == 13 and kinds.count("sell") == 1 and kinds.count("dividend") == 1
    sell = next(t for t in trades if t.kind == "sell")
    assert sell.shares == Decimal("-1.5") and sell.fee_cents == -100 and sell.amount_cents == 1800
    assert sum(t.savings_plan for t in trades) == 12  # la compra suelta no es del plan
    assert all(t.executed_at.endswith("Z") for t in trades)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"currency": "USD"}, "divisa"),
        ({"tid": "0000fake-0000-0000-0000-000000000000"}, "repetido"),
        ({"amount": "doce"}, "formato"),
    ],
)
def test_csv_rejects_bad_rows(change, error):
    rows = [*fake.ROWS[:-1], dataclasses.replace(fake.ROWS[-1], **change)]
    with pytest.raises(StatementError, match=error):
        trade_republic_csv.parse(fake.build_csv(rows))


def test_csv_unknown_type_is_imported_with_a_warning():
    rows = [*fake.ROWS, fake.Row("2026-10-03", "SOMETHING_NEW", "1.00", "x", tid="new-1")]
    act = trade_republic_csv.parse(fake.build_csv(rows))
    assert act.cash.transactions[-1].description == "Something new: x"
    assert "SOMETHING_NEW" in act.warnings[0]


# ------------------------------------------------------------------ parser PDF patrimonio


def test_networth_pdf():
    val = detect(fake.build_networth(), "Patrimonio neto.pdf")
    assert isinstance(val, ParsedValuation) and val.as_of == "2026-09-30"
    assert val.securities_cents == 56200
    assert val.cash_cents == fake.cash_at(fake.ROWS, "2026-09-30")
    assert val.total_cents == val.securities_cents + val.cash_cents
    assert [(p.isin, p.shares, p.value_cents) for p in val.positions] == [
        ("XF000FAKE001", Decimal("8.000000"), 44000),
        ("XF000FAKE002", Decimal("4.000000"), 10400),
        ("XF000FAKE003", Decimal("1.500000"), 1800),
    ]
    assert val.positions[1].name == "FAKE Bonds ETF (Dist)"  # «unidades» en otra línea
    assert all(p.price_date == "2026-09-30" for p in val.positions)
    assert "Persona Inventada" not in repr(val)


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"declared": 4}, "declara 4"),
        ({"securities": "999,00"}, "suma de las posiciones"),
        (
            {"positions": [dataclasses.replace(fake.POSITIONS[0], value="999,00")]},
            "no cuadra",
        ),
    ],
)
def test_networth_pdf_is_validated(kwargs, error):
    with pytest.raises(StatementError, match=error):
        trade_republic_networth.parse(fake.build_networth(**kwargs))


# ------------------------------------------------------------------ importar


def test_import_activity_creates_account_and_is_idempotent(svc):
    r = do_import(svc, fake.build_csv())
    assert r.rows_new == len(fake.ROWS) and r.trades_new == 15 and r.rows_linked == 0
    (acc,) = svc.list_accounts()
    assert acc.institution == "trade_republic" and acc.kind == "broker"
    assert do_import(svc, fake.build_csv()).already_imported
    # Una exportación posterior (otro fichero) solo añade lo nuevo.
    rows = [*fake.ROWS, fake.Row("2026-10-04", "CARD_TRANSACTION", "-1.00", "Kiosco", tid="n-1")]
    r2 = do_import(svc, fake.build_csv(rows))
    assert (r2.rows_new, r2.rows_duplicate, r2.trades_new) == (1, len(fake.ROWS), 0)


def _pdf_like_statement(days: tuple[str, ...], shift: str | None = None) -> ParsedStatement:
    """Lo que habría importado el PDF «Extracto de cuenta»: mismos movimientos, sin id."""
    act = detect(fake.build_csv(), "x.csv")
    txs = []
    for t in act.cash.transactions:
        if t.booking_date[:7] in days:
            day = "2026-09-17" if t.booking_date == shift else t.booking_date
            txs.append(dataclasses.replace(t, external_id=None, booking_date=day))
    return ParsedStatement(
        source="trade_republic_pdf",
        institution="trade_republic",
        account_name="Trade Republic · Cuenta corriente",
        account_identifier="DE00000000000000000042",
        currency="EUR",
        transactions=txs,
        account_kind="broker",
    )


def test_csv_links_rows_already_imported_from_the_pdf_keeping_categories(svc):
    pdf = _pdf_like_statement(("2026-09",), shift="2026-09-16")  # una compra, un día después
    svc.import_statement(CLI, pdf, file_name="extracto.pdf", file_sha256="a")
    card = next(t for t in svc.list_transactions(limit=None) if "Tienda" in t.description)
    svc.set_category(CLI, card.id, "compras")

    r = do_import(svc, fake.build_csv())
    assert r.rows_linked == len(pdf.transactions)
    assert r.rows_new == len(fake.ROWS) - len(pdf.transactions)
    assert len(svc.list_accounts()) == 1  # el CSV va a la cuenta de TR existente
    all_txs = svc.list_transactions(limit=None)
    assert len(all_txs) == len(fake.ROWS) and all(t.external_id for t in all_txs)
    kept = svc.get_transaction(card.id)
    assert kept.category == "compras" and kept.category_source == "manual"


def test_partial_export_is_rejected(svc):
    svc.import_statement(CLI, _pdf_like_statement(("2026-09",)), file_name="e.pdf", file_sha256="a")
    partial = [r for r in fake.ROWS if r.date >= "2026-06-02"]  # sin el ingreso inicial
    with pytest.raises(FinanceError, match="exportación parcial"):
        do_import(svc, fake.build_csv(partial))
    assert len(svc.list_transactions(limit=None)) == len(
        _pdf_like_statement(("2026-09",)).transactions
    )


def test_account_pdf_is_refused_once_the_csv_is_used(svc):
    do_import(svc, fake.build_csv())
    with pytest.raises(FinanceError, match="exportación CSV"):
        svc.import_statement(
            CLI, _pdf_like_statement(("2026-07",)), file_name="e.pdf", file_sha256="b"
        )


def test_import_valuation_checks_against_trades_and_cash(svc):
    r = do_import(svc, fake.build_networth())
    assert r.rows_new == 3 and "importa también la exportación CSV" in r.warnings[0]
    do_import(svc, fake.build_csv())
    again = do_import(svc, fake.build_networth(cash_cents=1))  # mismo día, otro fichero
    assert any("Sustituye" in w for w in again.warnings)
    assert any("efectivo" in w for w in again.warnings)
    clean = do_import(svc, fake.build_networth(as_of="30.09.2026"))
    assert not [w for w in clean.warnings if "no cuadran" in w or "efectivo" in w]
    wrong = [dataclasses.replace(fake.POSITIONS[0], units="9,000000", value="495,00")]
    r = do_import(svc, fake.build_networth(positions=[*wrong, *fake.POSITIONS[1:]]))
    assert any("no cuadran" in w for w in r.warnings)


def test_inbox_imports_csv_and_networth(svc, tmp_path):
    (tmp_path / "Exportación de transacción.csv").write_bytes(fake.build_csv())
    (tmp_path / "Patrimonio neto.pdf").write_bytes(fake.build_networth())
    report = process_inbox(svc, CLI, tmp_path, today=TODAY)
    assert not report.failed and len(report.imported) == 2
    assert {i.archived_to.split("/")[1] for i in report.items} == {"2026-10", "2026-09"}


# ------------------------------------------------------------------ cálculos


def test_holdings_use_average_cost(svc):
    do_import(svc, fake.build_csv())
    acc = svc.list_accounts()[0].id
    h = inv.holdings(svc, acc)
    assert (h["XF000FAKE001"].shares, h["XF000FAKE001"].cost_cents) == (8, 40000)
    assert (h["XF000FAKE002"].shares, h["XF000FAKE002"].cost_cents) == (4, 10000)
    corp = h["XF000FAKE003"]
    assert corp.shares == Decimal("1.5") and corp.cost_cents == 1550  # (30 + 1) / 2
    assert corp.realized_cents == 1700 - 1550
    assert h["XF000FAKE001"].dividends_cents == 243
    assert inv.holdings(svc, acc, as_of="2026-06-30")["XF000FAKE001"].shares == 2


def test_portfolio(svc):
    do_import(svc, fake.build_csv())
    (p,) = inv.portfolios(svc)
    assert p.as_of is None and p.value_cents is None and p.warnings
    do_import(svc, fake.build_networth())
    (p,) = inv.portfolios(svc)
    assert p.as_of == "2026-09-30" and p.value_cents == 56200 and p.cost_cents == 51550
    assert p.gain_cents == 4650 and p.gain_pct == 9.02
    assert p.positions[0].isin == "XF000FAKE001" and p.positions[0].gain_cents == 4000
    assert p.invested_after_cents == 0 and p.dividends_cents == 243 and p.realized_cents == 150


def test_plan_and_contributions(svc):
    do_import(svc, fake.build_csv())
    plan = inv.deduce_plan(svc, TODAY)
    assert [(p.isin, p.amount_cents, p.per_month, p.monthly_cents) for p in plan] == [
        ("XF000FAKE001", 5000, 2, 10000),
        ("XF000FAKE002", 2500, 1, 2500),
    ]
    months = inv.contributions(svc, months=5, today=TODAY)
    assert [m.month for m in months] == ["2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]
    assert [m.plan_ok for m in months] == [None, True, True, True, None]
    june = months[0]
    assert (june.plan_cents, june.manual_cents) == (12500, 3100)
    assert months[2].sold_cents == 1700 and months[2].net_cents == 12500 - 1700


def test_missed_plan_execution_is_flagged(svc):
    rows = [r for r in fake.ROWS if not (r.date == "2026-08-16" and r.type == "BUY")]
    do_import(svc, fake.build_csv(rows))
    aug = next(m for m in inv.contributions(svc, months=3, today=TODAY) if m.month == "2026-08")
    assert aug.plan_ok is False and aug.plan_short == ["XF000FAKE001"]


def test_net_worth_uses_market_value_only_near_a_valuation(svc):
    do_import(svc, fake.build_csv())
    do_import(svc, fake.build_networth())
    points = {p.month: p for p in inv.net_worth(svc, months=5, today=TODAY)}
    assert points["2026-08"].investments_basis == "coste"
    assert points["2026-08"].investments_cents == 30000 + 7500 + 1550
    sep = points["2026-09"]
    assert sep.investments_basis == "mercado" and sep.investments_cents == 56200
    assert sep.total_cents == 56200 + fake.cash_at(fake.ROWS, "2026-09-30")
    assert points["2026-10"].day == TODAY and points["2026-10"].investments_basis == "mercado"
    assert not any(p.missing for p in points.values())


def test_net_worth_flags_accounts_without_data(svc):
    do_import(svc, fake.build_csv())
    svc.import_statement(
        CLI,
        dataclasses.replace(
            _pdf_like_statement(("2026-09",)),
            institution="santander",
            account_identifier="ES0000000000000000005678",
            source="santander_xls",
            account_kind="bank",
        ),
        file_name="s.xls",
        file_sha256="s",
    )
    july = inv.net_worth(svc, months=4, today=TODAY)[0]
    assert july.month == "2026-07" and len(july.missing) == 1 and len(july.cash) == 1


# ------------------------------------------------------------------ interfaces


def test_cli_investment_commands(app, svc, tmp_path):
    runner = CliRunner()
    f = tmp_path / "Exportación de transacción.csv"
    f.write_bytes(fake.build_csv())
    res = runner.invoke(cli, ["finance", "import", str(f), "--dry-run"])
    assert res.exit_code == 0 and "15 operaciones" in res.output
    res = runner.invoke(cli, ["finance", "import", str(f)])
    assert res.exit_code == 0 and "15 operaciones" in res.output
    p = tmp_path / "Patrimonio neto.pdf"
    p.write_bytes(fake.build_networth())
    assert runner.invoke(cli, ["finance", "import", str(p)]).exit_code == 0

    res = runner.invoke(cli, ["finance", "portfolio"])
    assert (
        res.exit_code == 0 and "valoración del 2026-09-30" in res.output and "+9,02 %" in res.output
    )
    res = runner.invoke(cli, ["finance", "contributions", "-n", "5"])
    assert res.exit_code == 0 and "FAKE World ETF" in res.output and "✓" in res.output
    res = runner.invoke(cli, ["finance", "networth", "-n", "5"])
    assert res.exit_code == 0 and "inversiones a coste" in res.output
    assert runner.invoke(cli, ["finance", "networth", "--json"]).exit_code == 0


def test_mcp_investments_overview(app, svc):
    do_import(svc, fake.build_csv())
    do_import(svc, fake.build_networth())
    server = build_server(lambda: app)
    result = asyncio.run(server.call_tool("investments_overview", {"months": 3}))
    assert not result.is_error, result
    data = result.structured_content
    assert data["portfolios"][0]["value_cents"] == 56200
    assert data["portfolios"][0]["positions"][0]["shares"] == "8.000000"
    assert len(data["plan"]) == 2 and len(data["net_worth"]) == 3
    assert data["contributions"][-1]["plan_ok"] is None  # mes en curso


def test_doctor_investments_check(app, svc, clock):
    from personal_os.ops.doctor import OK, WARN, run_doctor

    def check():
        return [c for c in run_doctor(offline=True, clock=clock) if c.area == "inversiones"]

    assert check() == []
    do_import(svc, fake.build_csv())
    assert check()[0].status == WARN and "ninguna valoración" in check()[0].message
    do_import(svc, fake.build_networth())
    assert check()[0].status == OK
    clock.advance(days=45)
    assert check()[0].status == WARN and "2026-09-30" in check()[0].message
