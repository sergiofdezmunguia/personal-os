"""Bandeja de extractos, categorías puestas por Claude y cierre mensual. Datos INVENTADOS."""

from __future__ import annotations

import asyncio

import pytest
from typer.testing import CliRunner

from personal_os import bootstrap
from personal_os.cli.main import app as cli
from personal_os.core.config import ConfigError, load_config, write_config_template
from personal_os.core.events import ChangeContext
from personal_os.finance.close import CashYield, month_close
from personal_os.finance.importers import ParsedStatement, ParsedTransaction
from personal_os.finance.models import FinanceError
from personal_os.mcp_server.server import build_server
from tests import santander_fake as fake

CLI = ChangeContext.cli()
MCP = ChangeContext(actor="mcp")
TR_YIELD = CashYield("trade_republic", interest_rate=2.5, saveback_rate=1.0)


def _configure(tmp_path, extra: str) -> None:
    path = write_config_template()
    path.write_text('timezone = "Atlantic/Canary"\n\n' + extra, encoding="utf-8")


@pytest.fixture
def inbox(tmp_path):
    d = tmp_path / "icloud" / "Extractos"
    d.mkdir(parents=True)
    _configure(
        tmp_path,
        f'[finance]\ninbox_dir = "{d}"\n\n[finance.cash_yield.trade_republic]\n'
        "interest_rate = 2.5\nsaveback_rate = 1.0\n",
    )
    return d


@pytest.fixture
def app(clock, inbox):
    application = bootstrap.open_app(clock=clock)
    yield application
    application.db.close()


@pytest.fixture
def svc(app):
    return bootstrap.finance_service(app)


def tr_statement(rows: list[tuple[str, str, float]], start: float = 0.0) -> ParsedStatement:
    txs, bal = [], round(start * 100)
    for d, desc, amount in rows:
        cents = round(amount * 100)
        bal += cents
        txs.append(ParsedTransaction(d, d, desc, cents, bal))
    return ParsedStatement(
        source="trade_republic_pdf",
        institution="trade_republic",
        account_name="Trade Republic · Cuenta corriente",
        account_identifier="DE00000000000000000042",
        currency="EUR",
        transactions=txs,
        balance_end_cents=bal,
        account_kind="broker",
    )


SEPT_TR = [
    ("2026-09-01", "Transferencia: Incoming transfer from Persona Inventada", 1000.00),
    ("2026-09-10", "Transacción con tarjeta: Comercio Inventado", -100.00),
    ("2026-10-01", "Interés: Interest payment", 1.50),
    ("2026-10-02", "Bonificación: Saveback cash reward", 1.00),
    ("2026-10-15", "Transacción con tarjeta: Otro Comercio", -10.00),
]


def import_tr(svc, rows=SEPT_TR):
    st = tr_statement(rows)
    return svc.import_statement(CLI, st, file_name="tr.pdf", file_sha256=str(hash(tuple(rows))))


# ------------------------------------------------------------------ configuración


def test_finance_config_is_parsed(inbox):
    cfg = load_config()
    assert cfg.finance.inbox_dir == inbox
    (y,) = cfg.finance.cash_yield
    assert (y.institution, y.interest_rate, y.saveback_rate, y.saveback_cap_cents) == (
        "trade_republic",
        2.5,
        1.0,
        None,
    )


def test_finance_config_rejects_bad_rates(tmp_path):
    _configure(tmp_path, "[finance.cash_yield.trade_republic]\ninterest_rate = 250\n")
    with pytest.raises(ConfigError):
        load_config()


def test_finance_config_is_optional():
    write_config_template()
    cfg = load_config()
    assert cfg.finance.inbox_dir is None and cfg.finance.cash_yield == ()


# ------------------------------------------------------------------ bandeja


def test_inbox_imports_and_archives(app, svc, inbox):
    (inbox / "transactions.xls").write_bytes(fake.build(fake.SEPTEMBER))
    (inbox / "foto.pdf").write_bytes(b"%PDF-1.4 no es un extracto")
    (inbox / "notas.txt").write_text("ignorado")
    report = bootstrap.finance_inbox(app)

    assert [i.file_name for i in report.imported] == ["transactions.xls"]
    assert report.rows_new == 8
    assert (inbox / "importados" / "2026-09" / "transactions.xls").exists()
    assert not (inbox / "transactions.xls").exists()
    (failed,) = report.failed
    assert failed.file_name == "foto.pdf" and (inbox / "foto.pdf").exists()  # se queda
    assert (inbox / "notas.txt").exists()

    # El mismo extracto otra vez: no duplica y también se archiva (sin pisar el anterior).
    (inbox / "transactions.xls").write_bytes(fake.build(fake.SEPTEMBER))
    again = bootstrap.finance_inbox(app)
    (dup,) = [i for i in again.items if i.file_name == "transactions.xls"]
    assert again.rows_new == 0 and dup.result.already_imported
    assert (inbox / "importados" / "2026-09" / "transactions (1).xls").exists()


def test_inbox_runs_inside_sync(app, inbox):
    (inbox / "transactions.xls").write_bytes(fake.build(fake.SEPTEMBER))
    outcome = bootstrap.run_sync(app, only="finance")
    (report,) = outcome.reports
    assert report.provider == "finance_inbox"
    assert report.stats["files_imported"] == 1 and report.stats["transactions_new"] == 8


def test_inbox_missing_folder_does_not_break_sync(app, inbox):
    inbox.rmdir()
    (report,) = bootstrap.run_sync(app, only="finance").reports
    assert report.errors and "inbox_dir" in report.errors[0]


def test_inbox_cli(app, inbox):
    (inbox / "transactions.xls").write_bytes(fake.build(fake.SEPTEMBER))
    out = CliRunner().invoke(cli, ["finance", "inbox"])
    assert out.exit_code == 0, out.output
    assert "8 nuevos" in out.output and "importados/2026-09" in out.output
    assert "La bandeja está vacía" in CliRunner().invoke(cli, ["finance", "inbox"]).output


# ------------------------------------------------------------------ categorías de Claude


def test_claude_categorizes_but_never_overrides_the_user(svc):
    import_tr(svc)
    card, other = svc.list_transactions(search="con tarjeta", limit=None)
    t = svc.set_category(MCP, card.id, "compras")
    assert t.by_claude and t.categorized_by == "mcp"
    assert [x.id for x in svc.list_transactions(by_claude=True)] == [card.id]

    svc.set_category(CLI, other.id, "ocio")
    with pytest.raises(FinanceError, match="a mano"):
        svc.set_category(MCP, other.id, "compras")
    # El usuario sí corrige lo de Claude, y deja de contar como «de Claude».
    assert not svc.set_category(CLI, card.id, "restaurantes").by_claude
    # Las reglas tampoco lo pisan.
    svc.add_rule(CLI, "comercio", "supermercado")
    svc.apply_rules(CLI, recategorize=True)
    assert svc.get_transaction(card.id).category == "restaurantes"


def test_mcp_categorize_and_close_tools(app, svc):
    import_tr(svc)
    server = build_server(lambda: app)

    def call(name, **args):
        result = asyncio.run(server.call_tool(name, args))
        assert not result.is_error, result
        return result.structured_content

    pending = call("list_transactions", uncategorized=True, month="2026-09")["transactions"]
    out = call(
        "categorize_transactions",
        assignments=[
            {"id": pending[0]["id"], "category": "compras"},
            {"id": pending[1]["id"], "category": "no-existe"},
        ],
    )
    assert len(out["categorized"]) == 1 and len(out["errors"]) == 1
    assert call("list_transactions", category="compras")["transactions"][0]["categorized_by_claude"]

    close = call("finance_month_close")  # por defecto, el mes anterior (2026-09)
    assert close["month"] == "2026-09" and close["statements_complete"]
    assert close["categorized_by_claude"] == 1
    (y,) = close["cash_yield"]
    assert y["interest_rate"] == 2.5 and y["card_spend_cents"] == 10000
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert tools["finance_month_close"].annotations.read_only_hint is True


# ------------------------------------------------------------------ cierre mensual


def test_month_close_cash_yield(svc):
    import_tr(svc)
    for t in svc.list_transactions(search="interest", limit=None):
        svc.set_category(CLI, t.id, "intereses")
    for t in svc.list_transactions(search="saveback", limit=None):
        svc.set_category(CLI, t.id, "bonificaciones")

    mc = month_close(svc, "2026-09", yields=(TR_YIELD,))
    (y,) = mc.yields
    # 9 días a 1.000 € y 21 a 900 € → 27.900 €·día × 2,5 % / 365 = 1,91 €
    assert y.average_balance_cents == 93000
    assert y.interest_expected_cents == 191 and y.interest_received_cents == 150
    assert y.interest_ratio == pytest.approx(0.785, abs=0.001)
    assert y.card_spend_cents == 10000 and y.saveback_expected_cents == 100
    assert y.saveback_received_cents == 100 and y.saveback_ratio == 1.0


def test_month_close_pending_payouts_and_cap(svc):
    import_tr(svc, SEPT_TR[:2])  # aún no hay movimientos de octubre
    capped = CashYield("trade_republic", 2.5, 1.0, saveback_cap_cents=50)
    (y,) = month_close(svc, "2026-09", yields=(capped,)).yields
    assert y.interest_received_cents is None and y.saveback_received_cents is None
    assert y.saveback_expected_cents == 50


def test_month_close_coverage_and_readiness(svc):
    import_tr(svc, SEPT_TR[:2])
    mc = month_close(svc, "2026-09")
    (cov,) = mc.coverage
    # El último movimiento es del 10 de septiembre, pero se importó después (5 de octubre).
    assert cov.complete and mc.complete
    assert not mc.ready and mc.summary.uncategorized == 2
    assert month_close(svc, "2026-10").complete is False  # octubre sigue abierto


def test_month_close_compares_with_previous_month(svc):
    import_tr(
        svc,
        [
            ("2026-08-05", "Transacción con tarjeta: Super Inventado", -40.00),
            ("2026-09-05", "Transacción con tarjeta: Super Inventado", -70.00),
        ],
    )
    svc.add_rule(CLI, "super inventado", "supermercado")
    (d,) = month_close(svc, "2026-09").deltas
    assert (d.slug, d.total_cents, d.previous_cents, d.delta_cents) == (
        "supermercado",
        -7000,
        -4000,
        -3000,
    )


def test_close_cli(app, svc):
    import_tr(svc)
    out = CliRunner().invoke(cli, ["finance", "close", "2026-09"])
    assert out.exit_code == 0, out.output
    assert "Cierre 2026-09 · pendiente" in out.output
    assert "Intereses al 2,5 %" in out.output and "Saveback 1 %" in out.output
    assert CliRunner().invoke(cli, ["finance", "close", "2026-13"]).exit_code == 1


# ------------------------------------------------------------------ doctor


def test_doctor_inbox_and_open_month(app, svc, inbox, clock):
    from personal_os.ops.doctor import OK, WARN, run_doctor

    def area(name):
        return [c for c in run_doctor(offline=True, clock=clock) if c.area == name]

    assert [c.status for c in area("extractos")] == [OK]
    (inbox / "nuevo.pdf").write_bytes(b"%PDF")
    assert [c.status for c in area("extractos")] == [WARN]

    import_tr(svc)
    # Día 5: aún en el margen para subir extractos; no avisa de septiembre.
    assert not any("sin cerrar" in c.message for c in area("finanzas"))
    clock.advance(days=5)
    assert any("2026-09 sin cerrar" in c.message and c.status == WARN for c in area("finanzas"))
