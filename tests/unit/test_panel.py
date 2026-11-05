from __future__ import annotations

import dataclasses
import re

import pytest
from typer.testing import CliRunner

from personal_os import bootstrap
from personal_os.cli.main import app as cli
from personal_os.core.config import write_config_template
from personal_os.core.events import ChangeContext
from personal_os.finance import insights
from personal_os.finance.importers import ParsedStatement, ParsedTransaction
from personal_os.ops import panel

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


def _import(svc, items: list[tuple[str, str, int]]) -> None:
    """[(fecha, concepto, céntimos)] de una cuenta inventada, con saldo encadenado."""
    bal, txs = 500000, []
    for day, desc, cents in items:
        bal += cents
        txs.append(ParsedTransaction(day, day, desc, cents, bal))
    st = ParsedStatement(
        "santander_xls", "santander", "Cuenta Ficticia", "ES0000000000000000001234", "EUR", txs
    )
    svc.import_statement(CLI, st, file_name="f.xls", file_sha256="x")
    for pattern, category in [
        ("nomina", "nomina"), ("alquiler", "alquiler"), ("super ficticio", "supermercado"),
        ("bar ficticio", "restaurantes"), ("streaming", "suscripciones"),
    ]:  # fmt: skip
        svc.add_rule(CLI, pattern, category)


def _months(svc) -> None:
    """Jul–sep: nómina 2.000, alquiler 600 el día 1, súper 50 los días 3 y 20, bar 30 el
    día 10 (100 en septiembre) y streaming 10. Octubre (en curso, día 5): nómina, alquiler,
    súper 80 el día 3 y un movimiento sin categoría."""
    items = []
    for m in ("07", "08", "09"):
        items += [
            (f"2026-{m}-01", "Nomina Empresa Ficticia", 200000),
            (f"2026-{m}-01", "Recibo Alquiler", -60000),
            (f"2026-{m}-03", "Compra Super Ficticio", -5000),
            (f"2026-{m}-08", "Compra Streaming", -1000),
            (f"2026-{m}-10", "Compra Bar Ficticio", -10000 if m == "09" else -3000),
            (f"2026-{m}-20", "Compra Super Ficticio", -5000),
        ]
    items += [
        ("2026-10-01", "Nomina Empresa Ficticia", 200000),
        ("2026-10-01", "Recibo Alquiler", -60000),
        ("2026-10-03", "Compra Super Ficticio", -8000),
        ("2026-10-04", "Compra Algo Raro", -1500),
    ]
    _import(svc, items)


def test_merchant_names_are_cleaned():
    assert insights.merchant("Compra Bar Ficticio, Madrid, Tarj. :*1234") == "Bar ficticio"
    assert insights.merchant("Transacción con tarjeta: Café Inventado") == "Cafe inventado"
    assert insights.merchant("Pago Movil En Taxi Falso, Madrid") == "Taxi falso"


def test_habit_and_pace(svc):
    _months(svc)
    p = insights.build(svc, today=TODAY)
    assert p.habit_months == ["2026-07", "2026-08", "2026-09"]
    h = p.habit
    assert (h.salary_cents, h.income_cents) == (200000, 200000)
    assert h.fixed_cents == 61000  # alquiler + streaming
    assert h.variable_cents == round((13000 * 2 + 20000) / 3)
    pace = p.pace
    assert (pace.day, pace.days) == (5, 31)
    assert pace.variable_cents == 8000 and pace.fixed_cents == 60000
    assert pace.variable_habit_to_day_cents == 5000  # súper del día 3
    assert pace.delta_cents == 3000
    assert pace.cumulative == [60000, 60000, 68000, 68000, 68000]
    assert pace.cumulative_habit[0] == 60000 and len(pace.cumulative_habit) == 31
    assert pace.projected_cents == 61000 + round(8000 * 31 / 5)
    assert p.uncategorized == 1 and "sin categoría" in p.warnings[0]


def test_categories_merchants_and_focus(svc):
    _months(svc)
    p = insights.build(svc, today=TODAY)
    by = {c.slug: c for c in p.categories}
    assert p.categories[0].slug == "vivienda" and by["vivienda"].fixed
    bar = by["restaurantes"]
    assert bar.monthly_cents == round(16000 / 3) and not bar.fixed
    (m,) = bar.merchants
    assert m.name == "Bar ficticio" and m.per_month == 1.0 and len(m.movements) == 3
    assert m.movements[0].day == "2026-09-10"  # más reciente primero
    assert by["supermercado"].to_day_cents == 8000 and by["supermercado"].habit_to_day_cents == 5000
    assert [f.category.slug for f in p.focus] == ["supermercado", "restaurantes"]
    assert p.focus[0].saving_cents == 2000


def test_flows_and_saving(svc):
    _months(svc)
    p = insights.build(svc, today=TODAY)
    sep = next(f for f in p.trend if f.month == "2026-09")
    assert (sep.expense_cents, sep.invested_cents) == (61000 + 20000, 0)
    assert sep.saving_cents == 200000 - 81000 and round(sep.saving_rate, 3) == 0.595
    assert p.trend[-1].in_progress and p.trend[-1].month == "2026-10"
    assert len(p.trend) == insights.TREND_MONTHS
    assert [h.label for h in p.holdings] == ["Efectivo Cuenta Ficticia"]


def test_render_works_without_javascript(svc):
    _months(svc)
    page = panel.render(insights.build(svc, today=TODAY), generated_at="05/10/2026 09:00")
    assert page.startswith("<!doctype html>") and "<title>Pulso del mes</title>" in page
    assert re.findall(r'(?:src|href)="https?:', page) == []  # nada externo
    for h in ("h-pace", "h-now", "h-rank", "h-money", "h-focus", "h-trend"):
        assert f'id="{h}"' in page
    assert page.count('<svg class="chart"') == 3
    # Cifras escritas en el HTML (no dependen del JS) y desglose con <details>.
    assert "80 €" in page and "Bar ficticio" in page and "<details" in page
    assert "Compra Bar Ficticio" in page  # movimientos en el desglose
    assert "NaN" not in page and "None" not in page


def test_render_escapes_text(svc):
    _import(svc, [("2026-09-01", "Compra <script>alert(1)</script>", -100)])
    page = panel.render(insights.build(svc, today=TODAY), generated_at="x")
    assert "<script>alert(1)" not in page


def test_empty_database_renders(svc):
    page = panel.render(insights.build(svc, today=TODAY), generated_at="x")
    assert "Pulso del mes" in page and "No hay ingresos" in page


def test_panel_lives_in_the_scriptable_mailbox(app, tmp_path):
    mailbox = tmp_path / "Scriptable" / "personal-os"
    cfg = dataclasses.replace(
        app.config, apple=dataclasses.replace(app.config.apple, mailbox_dir=mailbox)
    )
    assert panel.panel_dir(cfg) == mailbox / "panel"
    assert panel.panel_dir(app.config) == app.config.data_dir / "panel"  # sin buzón


def test_pos_panel_command(app, svc, monkeypatch):
    _months(svc)
    opened = []
    monkeypatch.setattr(panel, "open_in_browser", lambda path: opened.append(path) or True)
    res = CliRunner().invoke(cli, ["panel"])
    assert res.exit_code == 0, res.output
    target = app.config.data_dir / "panel" / "index.html"
    assert target.read_text(encoding="utf-8").startswith("<!doctype") and opened == [target]
    assert CliRunner().invoke(cli, ["panel", "--no-open"]).exit_code == 0 and len(opened) == 1


def test_headline(svc, clock):
    _months(svc)
    early = panel.headline(insights.build(svc, today=TODAY))
    assert early.startswith("Vas 30 € por encima") and "supermercado (+30 €)" in early
    assert "ahorrarás" not in early  # día 5: la proyección aún no se da
    later = panel.headline(insights.build(svc, today="2026-10-20"))
    assert "ahorrarás" in later
