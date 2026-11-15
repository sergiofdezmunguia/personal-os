"""Categorizador aprendido (ADR 0017): modelo puro, propuestas, revisión y evaluación. Solo con
datos inventados (`tests/categorizer_fake.py`)."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from personal_os import bootstrap
from personal_os.cli.main import app as cli
from personal_os.core.config import write_config_template
from personal_os.core.events import ChangeContext
from personal_os.finance import categorizer
from personal_os.finance.categorizer import Categorizer, Example, NotEnoughData
from personal_os.finance.models import FinanceError
from tests import categorizer_fake as fake

CLI = ChangeContext.cli()
MCP = ChangeContext(actor="mcp")


# ------------------------------------------------------------------ modelo puro


def test_text_cleaning_and_merchant_key():
    assert categorizer.clean("Compra MERCADONA 1234, S/C Tenerife, Tarjeta ····9999") == (
        "mercadona s c"
    )
    assert categorizer.merchant_key("Pago Movil En Farmacia López 22") == "farmacia lopez"
    assert categorizer.merchant_key("1234 ····") == "(vacio)"


def test_pick_threshold_is_the_lowest_confidence_reaching_the_target():
    import numpy as np

    conf = np.array([0.99, 0.9, 0.8, 0.7, 0.6])
    ok = np.array([True, True, True, False, False])
    assert categorizer.pick_threshold(conf, ok, 0.75) == 0.7  # 3/4 aciertos
    assert categorizer.pick_threshold(conf, ok, 1.0) == 0.8
    assert categorizer.pick_threshold(conf, ~ok, 0.9) > 1  # nunca: no propone nada
    assert categorizer.pick_threshold(conf[:0], ok[:0], 0.9) > 1


def test_train_is_deterministic_and_reports_validation():
    data = fake.generate(500, seed=1)
    a, b = Categorizer.train(data), Categorizer.train(data)
    assert a.report == b.report
    r = a.report
    assert r.examples == 500 and r.categories == 12 and r.C in categorizer.C_GRID
    assert 0 < r.cv_coverage <= 1 and r.cv_precision >= 0.85
    new = fake.generate(100, seed=99)
    assert a.predict(new) == b.predict(new)


def test_abstains_below_threshold_and_never_proposes_rare_categories():
    data = fake.generate(500, seed=2)
    rare = [
        Example(f"Compra Tienda Rara {i}", -999, "2026-01-0" + str(i + 1), "bank", "rara")
        for i in range(3)
    ]
    model = Categorizer.train(data + rare)
    assert model.report.rare_examples == 3
    preds = model.predict(fake.generate(200, seed=5) + rare)
    assert all(p.category is None for p in preds if p.confidence < model.report.threshold)
    assert all(p.category != categorizer.RARE and p.category != "rara" for p in preds)
    assert any(p.category is None for p in preds)


def test_impossible_target_means_no_proposals():
    data = fake.generate(300, seed=4)
    model = Categorizer.train(data, target_precision=1.01)
    assert model.report.threshold > 1 and model.report.cv_precision is None
    assert all(p.category is None for p in model.predict(data[:50]))


def test_not_enough_data():
    with pytest.raises(NotEnoughData):
        Categorizer.train(fake.generate(10))
    one_class = [dataclasses.replace(x, category="supermercado") for x in fake.generate(60)]
    with pytest.raises(NotEnoughData):
        Categorizer.train(one_class)


def test_reproducible_evaluation_on_new_merchants():
    """La cifra del ADR 0017 con datos inventados (1.500 movimientos, semilla 7, 5 pliegues por
    comercio). Los datos sintéticos son más fáciles que los reales: el generador comparte
    trozos de nombre entre comercios de la misma categoría."""
    ev = categorizer.evaluate_new_merchants(fake.generate(1500, seed=7))
    assert ev["merchants"] == 638
    assert round(ev["accuracy"], 3) == 0.879
    assert round(ev["coverage"], 3) == 0.977
    assert round(ev["precision_covered"], 3) == 0.895


# ------------------------------------------------------------------ servicio


@pytest.fixture
def app(clock):
    write_config_template()
    application = bootstrap.open_app(clock=clock)
    yield application
    application.db.close()


@pytest.fixture
def svc(app):
    return bootstrap.finance_service(app)


@pytest.fixture
def sug(app):
    return bootstrap.suggestion_service(app)


def _load(svc, n=260, labelled=220, seed=3):
    """Importa movimientos inventados y categoriza a mano los `labelled` más antiguos. Devuelve
    (sin categoría, etiqueta verdadera por id)."""
    examples = fake.generate(n, seed=seed)
    svc.import_statement(CLI, fake.statement(examples), file_name="f", file_sha256="f")
    truth = {(x.booking_date, x.description, x.amount_cents): x.category for x in examples}
    txs = sorted(svc.list_transactions(limit=None), key=lambda t: (t.booking_date, t.id))
    label = {t.id: truth[(t.booking_date, t.description, t.amount_cents)] for t in txs}
    for t in txs[:labelled]:
        svc.set_category(CLI, t.id, label[t.id])
    return txs[labelled:], label


def test_refresh_only_proposes_for_uncategorized_and_changes_nothing(svc, sug, clock):
    pending, _ = _load(svc)
    month = pending[0].booking_date[:7]
    before = svc.month_summary(month)
    res = sug.refresh(CLI)
    assert res.report.examples == 220 and res.candidates == len(pending)
    assert res.proposed + res.abstained == len(pending) and res.proposed > 0
    items = sug.list()
    assert len(items) == res.proposed
    assert {s.transaction.id for s in items} <= {t.id for t in pending}
    assert all(s.transaction.category is None for s in items)
    assert [s.confidence for s in items] == sorted((s.confidence for s in items), reverse=True)
    assert all(s.confidence >= res.report.threshold for s in items)
    assert items[0].created_at == "2026-10-05T08:00:00Z" and "C=" in items[0].model
    assert svc.month_summary(month) == before  # nada cambia hasta aprobar
    # Recalcular no duplica.
    assert sug.refresh(CLI).proposed == res.proposed and len(sug.list()) == res.proposed


def test_refresh_by_month(svc, sug):
    pending, _ = _load(svc)
    month = pending[-1].booking_date[:7]
    res = sug.refresh(CLI, month=month)
    assert res.candidates == sum(t.booking_date.startswith(month) for t in pending)
    assert all(s.transaction.booking_date.startswith(month) for s in sug.list())
    with pytest.raises(FinanceError):
        sug.refresh(CLI, month="2026-13")


def test_approve_sets_user_category_and_reject_is_forever(svc, sug):
    _load(svc)
    sug.refresh(CLI)
    first, second = sug.list()[:2]
    res = sug.review(CLI, first.transaction.id, approve=True)
    assert res.applied and res.suggestion.status == "accepted"
    t = svc.get_transaction(first.transaction.id)
    assert (t.category, t.category_source, t.categorized_by) == (first.category, "manual", "cli")
    rej = sug.review(CLI, second.transaction.id, approve=False)
    assert rej.suggestion.status == "rejected" and not rej.applied
    assert svc.get_transaction(second.transaction.id).category is None
    again = sug.refresh(CLI)
    assert again.skipped_reviewed == 1  # el rechazado (el aprobado ya no es candidato)
    assert again.candidates == 39
    assert second.transaction.id not in {s.transaction.id for s in sug.list()}
    with pytest.raises(FinanceError, match="no está pendiente"):
        sug.review(CLI, first.transaction.id, approve=True)
    types = [r["type"] for r in svc.db.all("SELECT type FROM events ORDER BY seq")]
    assert "finance.suggestion.accepted" in types and "finance.suggestion.rejected" in types


def test_never_overrides_manual_rules_or_claude(svc, sug):
    _load(svc)
    sug.refresh(CLI)
    a, b, c = sug.list()[:3]
    # Mientras tanto: el usuario categoriza uno a mano, Claude otro y una regla el tercero.
    svc.set_category(CLI, a.transaction.id, "ocio")
    svc.set_category(MCP, b.transaction.id, "ocio")
    svc.add_rule(CLI, c.transaction.description, "ocio")
    for s in (a, b, c):
        assert s.transaction.id not in {x.transaction.id for x in sug.list()}
        res = sug.review(CLI, s.transaction.id, approve=True)
        assert not res.applied and res.suggestion.status == "superseded"
        assert svc.get_transaction(s.transaction.id).category == "ocio"


def test_only_the_user_reviews(svc, sug):
    _load(svc)
    sug.refresh(MCP)  # Claude puede pedir propuestas…
    s = sug.list()[0]
    for actor in ("mcp", "system"):
        with pytest.raises(FinanceError, match="solo las aprueba"):  # …pero no aprobarlas
            sug.review(ChangeContext(actor=actor), s.transaction.id, approve=True)


def test_refresh_without_data_fails_cleanly(sug):
    with pytest.raises(FinanceError, match="al menos"):
        sug.refresh(CLI)


def test_cli_suggest_list_approve_reject(app, svc):
    _load(svc)
    runner = CliRunner()
    out = runner.invoke(cli, ["finance", "suggest"])
    assert out.exit_code == 0, out.output
    assert "propuestas de 40 sin categoría" in out.output and "validación interna" in out.output
    listed = runner.invoke(cli, ["finance", "suggestion", "list", "--json"])
    items = json.loads(listed.output)
    ids = [i["transaction"]["id"] for i in items]
    ok = runner.invoke(cli, ["finance", "suggestion", "approve", ids[0]])
    assert ok.exit_code == 0 and "1 categorizados" in ok.output
    no = runner.invoke(cli, ["finance", "suggestion", "reject", ids[1]])
    assert no.exit_code == 0 and "✗" in no.output
    rest = runner.invoke(cli, ["finance", "suggestion", "approve", "--all"])
    assert rest.exit_code == 0 and f"{len(ids) - 2} categorizados" in rest.output
    assert runner.invoke(cli, ["finance", "suggestion", "approve"]).exit_code != 0
    month = svc.list_transactions(limit=1)[0].booking_date[:7]
    close = runner.invoke(cli, ["finance", "close", month])
    assert close.exit_code == 0 and "propuestas del modelo" not in close.output


def test_cli_evaluate_reports_only_aggregates(app, svc):
    _load(svc)
    out = CliRunner().invoke(cli, ["finance", "suggest", "--evaluate"])
    assert out.exit_code == 0, out.output
    assert "Comercios nuevos" in out.output and "acierta" in out.output
    assert not svc.db.all("SELECT 1 FROM fin_category_suggestions")


def test_clock_is_used_for_review_time(app, svc, clock):
    _load(svc)
    sug = bootstrap.suggestion_service(app)
    sug.refresh(CLI)
    s = sug.list()[0]
    clock.set(datetime(2026, 10, 6, 9, 30, tzinfo=UTC))
    sug.review(CLI, s.transaction.id, approve=False)
    row = svc.db.one(
        "SELECT reviewed_at, reviewed_by FROM fin_category_suggestions WHERE transaction_id = ?",
        (s.transaction.id,),
    )
    assert (row["reviewed_by"], row["reviewed_at"]) == ("cli", "2026-10-06T09:30:00Z")


def test_reviewed_history_survives_refresh(svc, sug):
    """Lo aprobado y lo rechazado no vuelven a `pending` aunque se quite la categoría; lo
    `superseded` sí se puede volver a proponer si el movimiento vuelve a quedar sin categoría."""
    _load(svc)
    sug.refresh(CLI)
    a, b = sug.list()[:2]
    sug.review(CLI, a.transaction.id, approve=True)
    svc.set_category(CLI, a.transaction.id, None)  # el usuario se arrepiente y la quita
    svc.set_category(CLI, b.transaction.id, "ocio")  # categorizado por otra vía
    res = sug.refresh(CLI)
    assert res.superseded == 1 and sug.get(b.transaction.id).status == "superseded"
    accepted = sug.get(a.transaction.id)
    assert accepted.status == "accepted" and accepted.transaction.category is None
    row = svc.db.one(
        "SELECT reviewed_by FROM fin_category_suggestions WHERE transaction_id = ?",
        (a.transaction.id,),
    )
    assert row["reviewed_by"] == "cli"
    assert a.transaction.id not in {s.transaction.id for s in sug.list()}
    svc.set_category(CLI, b.transaction.id, None)
    sug.refresh(CLI)
    assert sug.get(b.transaction.id).status == "pending"


def test_list_rejects_unknown_status_and_month_is_checked_before_training(sug):
    with pytest.raises(FinanceError, match="Estado desconocido"):
        sug.list(status="foo")
    with pytest.raises(FinanceError, match="Mes inválido"):
        sug.refresh(CLI, month="2026-13")  # sin datos: si entrenara antes, el error sería otro


def test_cli_close_mentions_pending_suggestions_and_approve_flags(app, svc):
    pending, _ = _load(svc)
    runner = CliRunner()
    assert runner.invoke(cli, ["finance", "suggest"]).exit_code == 0
    month = pending[-1].booking_date[:7]
    n = len(bootstrap.suggestion_service(app).list(month=month))
    close = runner.invoke(cli, ["finance", "close", month])
    assert close.exit_code == 0
    assert (f"{n} propuestas del modelo por revisar" in close.output) == bool(n)
    first = bootstrap.suggestion_service(app).list()[0].transaction.id
    both = runner.invoke(cli, ["finance", "suggestion", "approve", first, "--all"])
    assert both.exit_code != 0 and "no ambos" in both.output
    twice = runner.invoke(cli, ["finance", "suggestion", "approve", first, first])
    assert twice.exit_code == 0 and "1 categorizados" in twice.output
