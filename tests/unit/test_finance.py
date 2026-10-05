from __future__ import annotations

import asyncio
import hashlib

import pytest
from typer.testing import CliRunner

from personal_os import bootstrap
from personal_os.cli.main import app as cli
from personal_os.core.config import write_config_template
from personal_os.core.events import ChangeContext, EventLog
from personal_os.finance.importers import (
    ParsedStatement,
    ParsedTransaction,
    StatementError,
    detect,
    mask_card_numbers,
    santander,
)
from personal_os.finance.models import FinanceError, fmt_eur
from personal_os.finance.service import normalize
from personal_os.mcp_server.server import build_server
from tests import santander_fake as fake

CLI = ChangeContext.cli()
MCP = ChangeContext(actor="mcp")


@pytest.fixture
def app(clock):
    write_config_template()
    application = bootstrap.open_app(clock=clock)
    yield application
    application.db.close()


@pytest.fixture
def svc(app):
    return bootstrap.finance_service(app)


def do_import(svc, data: bytes, name="extracto.xls"):
    return svc.import_statement(
        CLI, detect(data, name), file_name=name, file_sha256=hashlib.sha256(data).hexdigest()
    )


# ------------------------------------------------------------------ parser Santander


def test_parse_santander_structure():
    st = santander.parse(fake.build(fake.SEPTEMBER))
    assert st.source == "santander_xls" and st.institution == "santander"
    assert st.account_name == "Cuenta Online Santander" and st.last4 == "1234"
    assert len(st.transactions) == 8 and st.period == ("2026-09-01", "2026-09-28")
    first = st.transactions[0]
    assert first.booking_date == "2026-09-01" and first.amount_cents == 150000
    assert first.balance_after_cents == 250000
    assert st.transactions[1].amount_cents == -4530
    assert st.balance_end_cents == st.transactions[-1].balance_after_cents and not st.warnings


def test_parser_never_keeps_the_holder_name():
    st = santander.parse(fake.build(fake.SEPTEMBER))
    assert fake.FAKE_HOLDER.lower() not in repr(st).lower()


def test_card_numbers_are_masked():
    st = santander.parse(fake.build(fake.SEPTEMBER))
    assert "4000000000009999" not in repr(st)
    assert "Tarjeta ····9999 ," in st.transactions[1].description


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Tarjeta 4000000000001234 , Comision", "Tarjeta ····1234 , Comision"),
        ("Cajero 000011112222 El 01/01", "Cajero 000011112222 El 01/01"),  # 12 dígitos: no
        ("Tarj. :*000123", "Tarj. :*000123"),
        ("ref 1234567890123456789 fin", "ref ····6789 fin"),
    ],
)
def test_mask_card_numbers(text, expected):
    assert mask_card_numbers(text) == expected


def test_header_balance_mismatch_is_a_warning_not_an_error():
    st = santander.parse(fake.build(fake.SEPTEMBER, header_balance="1,00 EUR"))
    assert st.balance_end_cents == 100 and "tarjeta pendientes" in st.warnings[0]


def test_broken_balance_chain_is_rejected():
    rows = list(fake.SEPTEMBER)
    d, v, c, a, b = rows[3]
    rows[3] = (d, v, c, a, b + 1.0)  # saldo manipulado
    with pytest.raises(StatementError, match="no encadena"):
        santander.parse(fake.build(rows))


def test_missing_row_is_detected_by_balance_chain():
    rows = fake.SEPTEMBER[:2] + fake.SEPTEMBER[3:]
    with pytest.raises(StatementError, match="incompleto o editado"):
        santander.parse(fake.build(rows))


def test_bad_date_and_unknown_formats():
    rows = list(fake.SEPTEMBER)
    rows[0] = ("2026-09-01", *rows[0][1:])
    with pytest.raises(StatementError, match="dd/mm/aaaa"):
        santander.parse(fake.build(rows))
    assert not santander.looks_like(b"FECHA;CONCEPTO\n")
    with pytest.raises(StatementError, match="No reconozco"):
        detect(b"no es un excel", "x.csv")


@pytest.mark.parametrize(
    ("text", "cents"),
    [("1.234,56 EUR", 123456), ("-12,3", -1230), ("0,05 €", 5), ("12.000", 1200000)],
)
def test_money_text(text, cents):
    assert santander._parse_money_text(text) == cents


def test_float_amounts_round_to_exact_cents():
    assert santander._cents(-0.1 - 0.2) == -30 and santander._cents(1234.565) == 123457


# ------------------------------------------------------------------ importar


def test_import_creates_account_without_storing_iban(app, svc):
    res = do_import(svc, fake.build(fake.SEPTEMBER))
    assert res.rows_new == 8 and res.rows_duplicate == 0 and not res.already_imported
    (acc,) = svc.list_accounts()
    assert acc.last4 == "1234" and acc.label.endswith("····1234")
    dump = "\n".join(app.db._conn.iterdump())
    assert fake.FAKE_IBAN not in dump and fake.FAKE_HOLDER not in dump
    events = EventLog(app.db, app.clock).query(type_prefix="finance.")
    assert {e.type for e in events} >= {"finance.account.created", "finance.import.completed"}


def test_same_file_twice_does_nothing(svc):
    data = fake.build(fake.SEPTEMBER)
    do_import(svc, data)
    again = do_import(svc, data)
    assert again.already_imported and again.rows_new == 0
    assert len(svc.list_transactions(limit=None)) == 8 and len(svc.list_imports()) == 1


def test_overlapping_statements_only_add_new_rows(svc):
    do_import(svc, fake.build(fake.SEPTEMBER))
    res = do_import(svc, fake.build(fake.SEPTEMBER[4:] + fake.OCTOBER), "octubre.xls")
    assert res.rows_new == 2 and res.rows_duplicate == 4 and not res.warnings
    assert len(svc.list_transactions(limit=None)) == 10


def test_identical_purchases_same_day_are_both_kept(svc):
    do_import(svc, fake.build(fake.SEPTEMBER))
    cafes = svc.list_transactions(search="cafe inventado")
    assert len(cafes) == 2  # mismo día, concepto e importe; distinto saldo


def test_fully_identical_rows_without_balance_use_ordinal(svc):
    tx = ParsedTransaction("2026-09-03", None, "Compra igual", -100, None)

    def st(n):
        return ParsedStatement("x", "santander", "C", fake.FAKE_IBAN, "EUR", [tx] * n)

    svc.import_statement(CLI, st(2), file_name="a", file_sha256="a")
    res = svc.import_statement(CLI, st(3), file_name="b", file_sha256="b")
    assert res.rows_new == 1 and len(svc.list_transactions(limit=None)) == 3


def test_gap_between_statements_is_warned(svc):
    do_import(svc, fake.build(fake.SEPTEMBER[:3]))
    res = do_import(svc, fake.build(fake.OCTOBER), "octubre.xls")
    assert res.rows_new == 2 and "Posible hueco" in res.warnings[0]


def test_contiguous_statements_have_no_gap_warning(svc):
    do_import(svc, fake.build(fake.SEPTEMBER))
    assert not do_import(svc, fake.build(fake.OCTOBER), "octubre.xls").warnings


def test_two_accounts_are_kept_apart(svc):
    do_import(svc, fake.build(fake.SEPTEMBER))
    do_import(svc, fake.build(fake.SEPTEMBER, iban="ES0000000000000000005678"), "otra.xls")
    assert sorted(a.last4 for a in svc.list_accounts()) == ["1234", "5678"]
    assert len(svc.list_transactions(limit=None)) == 16


# ------------------------------------------------------------------ reglas y categorías


def test_normalize_ignores_case_accents_and_spaces():
    assert normalize("  Compra  CAFÉ\tInventado ") == "compra cafe inventado"


def test_rules_categorize_new_and_existing_rows(svc):
    do_import(svc, fake.build(fake.SEPTEMBER))
    rule, applied = svc.add_rule(CLI, "mercadona", "supermercado")
    assert rule.status == "active" and applied == 1
    res = do_import(svc, fake.build(fake.OCTOBER), "octubre.xls")
    assert res.categorized == 1  # «MERCADONA FICTICIA» en mayúsculas también casa


def test_direction_and_priority(svc):
    do_import(svc, fake.build(fake.SEPTEMBER))
    svc.add_rule(CLI, "empresa ficticia", "otros-gastos", direction="debit")
    assert svc.list_transactions(category="otros-gastos") == []  # la nómina es un abono
    svc.add_rule(CLI, "transferencia", "traspaso", priority=50)
    svc.add_rule(CLI, "nomina", "nomina", priority=10)
    svc.apply_rules(CLI, recategorize=True)
    (nomina,) = svc.list_transactions(category="nomina")
    assert nomina.amount_cents == 150000
    (tr,) = svc.list_transactions(category="traspaso")
    assert "TRADE REPUBLIC" in tr.description


def test_regex_rule_and_invalid_inputs(svc):
    do_import(svc, fake.build(fake.SEPTEMBER))
    _, applied = svc.add_rule(CLI, r"^(recibo|pago movil)", "suministros", match_type="regex")
    assert applied == 2
    with pytest.raises(FinanceError, match="regular"):
        svc.add_rule(CLI, "(", "ocio", match_type="regex")
    with pytest.raises(FinanceError, match="desconocida"):
        svc.add_rule(CLI, "x", "no-existe")
    with pytest.raises(FinanceError, match="vacío"):
        svc.add_rule(CLI, "  ", "ocio")


def test_manual_category_always_wins(svc):
    do_import(svc, fake.build(fake.SEPTEMBER))
    (fuel,) = svc.list_transactions(search="gasolinera")
    svc.set_category(CLI, fuel.id[:-2], "ocio")  # prefijo único
    svc.add_rule(CLI, "gasolinera", "transporte")
    svc.apply_rules(CLI, recategorize=True)
    assert svc.get_transaction(fuel.id).category == "ocio"
    cleared = svc.set_category(CLI, fuel.id, None)  # al quitarla, actúan las reglas
    assert cleared.category == "transporte" and cleared.category_source == "rule"


def test_deleting_a_rule_frees_its_transactions(svc):
    do_import(svc, fake.build(fake.SEPTEMBER))
    rule, _ = svc.add_rule(CLI, "cajero", "efectivo")
    svc.delete_rule(CLI, rule.id)
    assert svc.list_transactions(category="efectivo") == []
    assert svc.get_rule(rule.id).status == "rejected"


def test_rules_proposed_by_claude_need_user_approval(svc):
    do_import(svc, fake.build(fake.SEPTEMBER))
    rule, applied = svc.add_rule(MCP, "cafe", "restaurantes", reason="cafeterías")
    assert rule.status == "proposed" and applied == 0
    assert svc.list_transactions(category="restaurantes") == []
    with pytest.raises(FinanceError, match="usuario"):
        svc.review_rule(MCP, rule.id, approve=True)
    approved, applied = svc.review_rule(CLI, rule.id, approve=True)
    assert approved.status == "active" and applied == 2
    with pytest.raises(FinanceError, match="no está pendiente"):
        svc.review_rule(CLI, rule.id, approve=False)


# ------------------------------------------------------------------ resumen


def test_month_summary(svc):
    do_import(svc, fake.build(fake.SEPTEMBER))
    svc.add_rule(CLI, "nomina", "nomina")
    svc.add_rule(CLI, "trade republic", "inversion")
    svc.add_rule(CLI, "mercadona", "supermercado")
    svc.add_rule(CLI, "cafe", "restaurantes")
    s = svc.month_summary("2026-09")
    assert s.income_cents == 150000
    assert s.expense_cents == -(4530 + 320 + 320)
    assert s.transfer_cents == -20000  # fuera del neto
    assert s.uncategorized == 3 and s.uncategorized_cents == -(6000 + 5000 + 7215)
    assert s.net_cents == 150000 - 5170 - 18215
    assert s.transactions == 8
    assert svc.month_summary("2026-10").transactions == 0
    with pytest.raises(FinanceError, match="Mes inválido"):
        svc.month_summary("2026-13")


def test_fmt_eur():
    assert fmt_eur(-123456) == "-1.234,56 €" and fmt_eur(5) == "0,05 €"


# ------------------------------------------------------------------ CLI


def test_cli_flow(tmp_path, app):
    runner = CliRunner()
    f = tmp_path / "movimientos.xls"
    f.write_bytes(fake.build(fake.SEPTEMBER))

    res = runner.invoke(cli, ["finance", "import", str(f), "--dry-run"])
    assert res.exit_code == 0 and "simulación" in res.output
    assert runner.invoke(cli, ["finance", "list"]).output.startswith("No hay")

    res = runner.invoke(cli, ["finance", "import", str(f)])
    assert res.exit_code == 0 and "8 nuevos" in res.output and "····1234" in res.output
    assert "ya se había importado" in runner.invoke(cli, ["finance", "import", str(f)]).output

    res = runner.invoke(cli, ["finance", "rule", "add", "mercadona", "supermercado", "--debit"])
    assert res.exit_code == 0 and "1 movimientos categorizados" in res.output
    res = runner.invoke(cli, ["finance", "list", "-c", "supermercado"])
    txn_id = res.output.split()[0]
    assert runner.invoke(cli, ["finance", "categorize", txn_id, "compras"]).exit_code == 0
    assert "compras*" in runner.invoke(cli, ["finance", "list", "-c", "compras"]).output

    res = runner.invoke(cli, ["finance", "summary", "2026-09"])
    assert res.exit_code == 0 and "Ingresos" in res.output and "Sin categoría" in res.output
    assert runner.invoke(cli, ["finance", "rule", "preview", "cajero"]).output.startswith("1 ")
    assert runner.invoke(cli, ["finance", "categorize", txn_id]).exit_code == 1

    svc = bootstrap.finance_service(app)
    svc.add_rule(MCP, "cajero", "efectivo")
    svc.add_rule(MCP, "gasolinera", "transporte")
    assert runner.invoke(cli, ["finance", "rule", "approve"]).exit_code == 1
    res = runner.invoke(cli, ["finance", "rule", "approve", "--all"])
    assert res.exit_code == 0 and "2 movimientos categorizados" in res.output

    bad = tmp_path / "otro.csv"
    bad.write_text("a;b\n")
    assert runner.invoke(cli, ["finance", "import", str(bad)]).exit_code == 1


# ------------------------------------------------------------------ MCP


def test_mcp_finance_tools(app, svc):
    do_import(svc, fake.build(fake.SEPTEMBER))
    server = build_server(lambda: app)

    def call(name, **args):
        result = asyncio.run(server.call_tool(name, args))
        assert not result.is_error, result
        return result.structured_content

    summary = call("finance_summary", month="2026-09")
    assert summary["income_cents"] == 0 and summary["uncategorized"] == 8
    assert summary["net_cents"] == summary["uncategorized_cents"]
    assert any(c["slug"] == "supermercado" for c in summary["categories"])

    txs = call("list_transactions", month="2026-09", search="cafe")
    assert txs["count"] == 2

    proposed = call(
        "propose_category_rule", pattern="cafe", category="restaurantes", reason="cafeterías"
    )
    assert proposed["status"] == "proposed" and proposed["would_match"] == 2
    assert call("list_transactions", category="restaurantes")["count"] == 0  # no se aplica
    rules = call("list_category_rules")["rules"]
    assert rules[0]["created_by"] == "mcp"
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert tools["list_transactions"].annotations.read_only_hint is True


# ------------------------------------------------------------------ doctor


def test_doctor_finance_checks(app, svc, clock):
    from personal_os.ops.doctor import OK, SKIP, WARN, run_doctor

    def fin():
        return [c for c in run_doctor(offline=True, clock=clock) if c.area == "finanzas"]

    assert {c.status for c in fin()} == {SKIP}
    do_import(svc, fake.build(fake.SEPTEMBER))
    (check,) = fin()
    assert check.status == OK and "8 movimientos sin categoría" in check.message
    svc.add_rule(MCP, "cafe", "restaurantes")
    assert any(c.status == WARN and "propuestas" in c.message for c in fin())
    clock.advance(**{"days": 40})
    assert any(c.status == WARN and "Última importación" in c.message for c in fin())


# ------------------------------------------------------------------ subcategorías


def test_subcategories_roll_up_in_summary_and_filters(svc):
    do_import(svc, fake.build(fake.SEPTEMBER))
    svc.add_rule(CLI, "gasolinera", "combustible")
    svc.add_rule(CLI, "cajero", "transporte")  # directamente en la principal
    s = svc.month_summary("2026-09")
    (transporte,) = [c for c in s.by_category if c.slug == "transporte"]
    assert transporte.total_cents == -11000 and transporte.count == 2
    assert [(c.slug, c.total_cents) for c in transporte.children] == [("combustible", -6000)]
    assert s.expense_cents == -11000
    assert len(svc.list_transactions(category="transporte")) == 2  # incluye subcategorías
    assert len(svc.list_transactions(category="combustible")) == 1


def test_add_category_and_subcategory(svc):
    c = svc.add_category(CLI, "bicicleta", "Bicicleta", parent="transporte")
    assert c.kind == "expense" and c.parent == "transporte"
    top = svc.add_category(CLI, "mascotas", "Mascotas", kind="expense")
    assert top.parent is None
    with pytest.raises(FinanceError, match="un nivel"):
        svc.add_category(CLI, "ruedas", "Ruedas", parent="bicicleta")
    with pytest.raises(FinanceError, match="Ya existe"):
        svc.add_category(CLI, "vuelos", "Vuelos")
    with pytest.raises(FinanceError, match="tipo"):
        svc.add_category(CLI, "x-ingreso", "X", parent="transporte", kind="income")
    with pytest.raises(FinanceError, match="kind"):
        svc.add_category(CLI, "sin-tipo", "Sin tipo")
    with pytest.raises(FinanceError, match="usuario"):
        svc.add_category(MCP, "desde-claude", "X", kind="expense")


def test_edit_rule_moves_its_transactions(svc):
    do_import(svc, fake.build(fake.SEPTEMBER))
    rule, _ = svc.add_rule(CLI, "gasolinera", "transporte")
    (fuel,) = svc.list_transactions(search="gasolinera")
    svc.set_category(CLI, fuel.id, "ocio")  # manual: no se mueve
    svc.set_category(CLI, fuel.id, None)
    updated, moved = svc.update_rule(CLI, rule.id, category="combustible")
    assert updated.category == "combustible" and moved == 1
    assert svc.get_transaction(fuel.id).category == "combustible"
    with pytest.raises(FinanceError, match="usuario"):
        svc.update_rule(MCP, rule.id, category="vuelos")


def test_edit_rule_priority_recomputes(svc):
    do_import(svc, fake.build(fake.SEPTEMBER))
    svc.add_rule(CLI, "transferencia", "traspaso", priority=50)
    nomina, _ = svc.add_rule(CLI, "nomina", "nomina", priority=100)
    assert svc.list_transactions(category="nomina") == []
    svc.update_rule(CLI, nomina.id, priority=10)
    assert len(svc.list_transactions(category="nomina")) == 1


def test_cli_categories_tree_and_rule_edit(app):
    runner = CliRunner()
    out = runner.invoke(cli, ["finance", "categories"]).output
    assert "  vuelos" in out and out.index("transporte") < out.index("  vuelos")
    res = runner.invoke(
        cli, ["finance", "category", "add", "bici", "Bici", "--parent", "transporte"]
    )
    assert res.exit_code == 0 and "dentro de transporte" in res.output
    svc = bootstrap.finance_service(app)
    rule, _ = svc.add_rule(CLI, "x", "transporte")
    res = runner.invoke(cli, ["finance", "rule", "edit", rule.id, "-c", "bici"])
    assert res.exit_code == 0 and "→ bici" in res.output
    assert runner.invoke(cli, ["finance", "rule", "edit", rule.id]).exit_code == 1
