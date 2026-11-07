"""`pos doctor`: frescura de precios, operaciones y open banking. Solo datos inventados, sin red.

Reloj de la fixture `clock`: lunes 2026-10-05. El CSV inventado llega hasta el 2026-10-02.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from personal_os import bootstrap
from personal_os.cli.main import app as cli
from personal_os.core.config import config_dir, data_dir, load_config, write_config_template
from personal_os.core.events import ChangeContext
from personal_os.finance import investments as inv
from personal_os.finance import satellite
from personal_os.finance.importers import detect
from personal_os.finance.importers import enablebanking as eb
from personal_os.ops.doctor import (
    FAIL,
    OK,
    SKIP,
    WARN,
    check_open_banking,
    check_prices,
    check_recent_trades,
    run_doctor,
)
from tests import trade_republic_activity_fake as fake

CTX = ChangeContext.cli()
TODAY = "2026-10-05"
PLACEHOLDER = bootstrap.BANKS["traderepublic"][2]
ISINS = ["XF000FAKE001", "XF000FAKE002", "XF000FAKE003"]
FAKE_SESSION_ID = "ses-inventada-0000"
FAKE_UID = "uid-inventado-0000"
AUTH = "autorización"


@pytest.fixture
def app(clock):
    write_config_template()
    application = bootstrap.open_app(clock=clock)
    yield application
    application.db.close()


@pytest.fixture
def svc(app):
    return bootstrap.finance_service(app)


def do_import(svc, data: bytes, name="f"):
    return inv.import_document(
        svc, CTX, detect(data, name), file_name=name, file_sha256=hashlib.sha256(data).hexdigest()
    )


@pytest.fixture
def traded(svc):
    """Cartera inventada: CSV hasta el 2026-10-02, sin ningún cierre descargado."""
    do_import(svc, fake.build_csv(), "f.csv")
    return svc


def prices(svc, day, *, isins=ISINS, currency="EUR"):
    rows = [(i, day, Decimal("10"), currency, "fake") for i in isins]
    inv.store_prices(svc, rows, fetched_at="2026-10-05T08:00:00Z")


def bank_tx(day, amount, *, text=None, ref=None):
    """Movimiento como lo da TR por open banking: fecha e importe, sin id salvo `ref`."""
    return {
        "entry_reference": ref,
        "transaction_amount": {"currency": "EUR", "amount": f"{abs(amount):.2f}"},
        "credit_debit_indicator": "DBIT" if amount < 0 else "CRDT",
        "status": "BOOK",
        "booking_date": day,
        "value_date": day,
        "remittance_information": [text] if text else None,
    }


def bank_import(svc, *txs, institution="trade_republic", balance=None):
    """Una respuesta de open banking, por el mismo camino que `bank_sync`."""
    (account,) = [a for a in svc.list_accounts() if a.institution == institution]
    if balance is None:
        balance = fake.cash_at(fake.ROWS, "2026-10-02") + sum(
            round(float(t["transaction_amount"]["amount"]) * 100)
            * (-1 if t["credit_debit_indicator"] == "DBIT" else 1)
            for t in txs
        )
    st = eb.parse(
        institution=institution,
        account_name=account.name,
        account_kind=account.kind,
        balances=[
            {
                "balance_type": "CLBD",
                "balance_amount": {"currency": "EUR", "amount": f"{balance / 100:.2f}"},
            }
        ],
        transactions=list(txs),
        fallback_description=PLACEHOLDER,
    )
    sha = hashlib.sha256(json.dumps(txs, sort_keys=True).encode()).hexdigest()
    result = svc.import_statement(
        CTX, st, file_name="open banking", file_sha256=sha, account_id=account.id
    )
    assert result.rows_new == len(txs)
    return result


def provisional_id(svc, cents):
    return svc.db.one(
        "SELECT id FROM fin_transactions WHERE external_id LIKE 'prov:%' AND amount_cents = ?",
        (cents,),
    )["id"]


def run(check, app):
    return check(load_config(), app.clock)


def only(checks, status):
    return [c for c in checks if c.status == status]


def write_sessions(sessions: dict, *, raw: str | None = None):
    cred = config_dir() / "enablebanking"
    cred.mkdir(parents=True, exist_ok=True)
    (cred / "sessions.json").write_text(raw if raw is not None else json.dumps(sessions))
    return cred


def session(valid_until):
    return {"session_id": FAKE_SESSION_ID, "valid_until": valid_until, "accounts": [FAKE_UID]}


def market(svc, symbol, day, *, currency="EUR"):
    satellite.store_history(
        svc, symbol, currency, [(day, Decimal("10"))], fetched_at="2026-10-05T08:00:00Z"
    )


def enable_satellite():
    (config_dir() / "config.toml").write_text(
        'timezone = "Atlantic/Canary"\n'
        "[[finance.satellite.watch]]\n"
        'isin = "XF000FAKE009"\nsymbol = "FAKE9.DE"\nname = "FAKE Nine"\n'
    )


# ------------------------------------------------------------------ precios


def test_no_open_positions_no_price_lines(app):
    assert run(check_prices, app) == []


def test_monday_with_friday_close_is_ok(app, traded):
    prices(traded, "2026-10-02")
    (c,) = run(check_prices, app)
    assert c.area == "precios" and c.status == OK and "3 posiciones con" in c.message


def test_sold_isin_is_not_checked(app, svc):
    sale = fake.Row(
        "2026-10-02",
        "SELL",
        "18.00",
        "Sell trade XF000FAKE003 FAKE Corp, quantity: 1.5",
        "XF000FAKE003",
        "FAKE Corp",
        "STOCK",
        "-1.5",
        "12.00",
        tid="0000fake-0000-0000-0000-000000000999",
    )
    do_import(svc, fake.build_csv([*fake.ROWS, sale]), "f.csv")
    prices(svc, "2026-10-02", isins=ISINS[:2])
    (c,) = run(check_prices, app)
    assert c.status == OK and "XF000FAKE003" not in c.message and "2 posiciones" in c.message


def test_position_without_close_warns_with_isin_and_force(app, traded):
    prices(traded, "2026-10-02", isins=ISINS[:2])
    checks = run(check_prices, app)
    (w,) = only(checks, WARN)
    assert "1 posición abierta sin cotización" in w.message and "XF000FAKE003" in w.message
    assert "--force" in w.hint
    assert only(checks, OK)


@pytest.mark.parametrize(
    "last, today, missed",
    [
        ("2026-10-02", "2026-10-05", 0),  # lunes con cierre del viernes
        ("2026-10-02", "2026-10-03", 0),  # sábado
        ("2026-10-02", "2026-10-04", 0),  # domingo
        ("2026-10-05", "2026-10-05", 0),  # hoy
        ("2027-03-25", "2027-03-30", 2),  # Semana Santa 2027
        ("2026-12-23", "2026-12-28", 2),  # Navidad 2026
        ("2026-09-29", "2026-10-05", 3),
    ],
)
def test_weekdays_between(last, today, missed):
    assert inv.weekdays_between(last, today) == missed


def test_two_missed_sessions_ok_three_warn(app, traded):
    prices(traded, "2026-09-30")
    assert only(run(check_prices, app), WARN) == []
    traded.db.execute("DELETE FROM fin_prices")
    prices(traded, "2026-09-29")
    (w,) = only(run(check_prices, app), WARN)
    assert "sin cierre nuevo" in w.message and "festivos" in w.message
    assert "ya no se usa" not in w.message


def test_stale_positions_singular_and_plural(app, traded):
    prices(traded, "2026-09-29", isins=ISINS[:1])
    prices(traded, "2026-10-02", isins=ISINS[1:])
    checks = run(check_prices, app)
    assert any(c.status == WARN and "1 posición sin cierre nuevo" in c.message for c in checks)
    assert any(c.status == OK and "2 posiciones con cierre" in c.message for c in checks)


def test_unused_close_text_without_pdf_or_satellite(app, traded):
    prices(traded, "2026-09-27")
    (w,) = only(run(check_prices, app), WARN)
    assert "ya no se usa" in w.message and "a coste" in w.message
    assert "PDF" not in w.message and "satélite" not in w.message


def test_unused_close_text_with_pdf_and_satellite(app, traded):
    prices(traded, "2026-09-27")
    do_import(traded, fake.build_networth(), "v.pdf")
    enable_satellite()
    w = next(c for c in only(run(check_prices, app), WARN) if "ya no se usa" in c.message)
    assert "valoración del PDF" in w.message and "a coste" not in w.message
    assert "el tope del satélite puede bajar" in w.message


def test_close_seven_days_old_is_still_used_eight_is_not(app, traded):
    prices(traded, "2026-09-28")
    (w,) = only(run(check_prices, app), WARN)
    assert "ya no se usa" not in w.message
    assert inv.live_portfolios(traded, today=TODAY)[0].complete is True
    traded.db.execute("DELETE FROM fin_prices")
    prices(traded, "2026-09-27")
    (w,) = only(run(check_prices, app), WARN)
    assert "ya no se usa" in w.message
    assert inv.live_portfolios(traded, today=TODAY)[0].complete is False


def test_price_lists_at_most_three_positions(app, svc):
    extra = [
        fake.plan("2026-09-25", f"XF000FAKEX{i}", f"FAKE Extra {i}", "1.00", "1.00") for i in (1, 2)
    ]
    rows = [replace(r, tid=f"0000fake-0000-0000-0000-00000000099{i}") for i, r in enumerate(extra)]
    do_import(svc, fake.build_csv(sorted([*fake.ROWS, *rows], key=lambda r: r.date)), "f.csv")
    (w,) = only(run(check_prices, app), WARN)
    assert "5 posiciones" in w.message and "y 2 más" in w.message


def test_future_close_is_ignored(app, traded):
    prices(traded, "2026-10-09")
    (w,) = only(run(check_prices, app), WARN)
    assert "sin cotización" in w.message


def test_usd_close_does_not_count(app, traded):
    prices(traded, "2026-10-02", currency="USD")
    (w,) = only(run(check_prices, app), WARN)
    assert "sin cotización" in w.message and "3 posiciones" in w.message


def test_satellite_active_without_closes_warns(app, traded):
    prices(traded, "2026-10-02")
    enable_satellite()
    (w,) = only(run(check_prices, app), WARN)
    assert "Satélite: 2 símbolos sin cierre" in w.message
    assert "FAKE9.DE" in w.message and "EUNL.DE" in w.message


def test_satellite_active_with_recent_closes_is_ok(app, traded):
    prices(traded, "2026-10-02")
    enable_satellite()
    market(traded, "FAKE9.DE", "2026-10-02")
    market(traded, "EUNL.DE", "2026-10-02")
    checks = run(check_prices, app)
    assert only(checks, WARN) == []
    assert any("Satélite: 2 símbolos con cierre" in c.message for c in checks)


def test_satellite_two_missed_sessions_ok_three_warn(app, traded):
    prices(traded, "2026-10-02")
    enable_satellite()
    market(traded, "EUNL.DE", "2026-10-02")
    market(traded, "FAKE9.DE", "2026-09-30")  # 2 sesiones perdidas
    assert only(run(check_prices, app), WARN) == []
    traded.db.execute("DELETE FROM fin_market_prices WHERE symbol = 'FAKE9.DE'")
    market(traded, "FAKE9.DE", "2026-09-29")  # 3 sesiones perdidas
    (w,) = only(run(check_prices, app), WARN)
    assert "Satélite: 1 símbolo sin cierre" in w.message
    assert "FAKE9.DE" in w.message and "EUNL.DE" not in w.message


@pytest.mark.parametrize(
    "day, currency",
    [("2026-10-02", "USD"), ("2026-10-09", "EUR")],
    ids=["otra divisa", "fecha futura"],
)
def test_satellite_ignores_foreign_currency_and_future_closes(app, traded, day, currency):
    prices(traded, "2026-10-02")
    enable_satellite()
    market(traded, "EUNL.DE", "2026-10-02")
    market(traded, "FAKE9.DE", day, currency=currency)
    (w,) = only(run(check_prices, app), WARN)
    assert "FAKE9.DE" in w.message and "EUNL.DE" not in w.message


def test_partly_migrated_database_skips_only_the_satellite(app, traded):
    prices(traded, "2026-10-02")
    enable_satellite()
    traded.db.execute("DROP TABLE fin_market_prices")  # base migrada hasta la 0007
    (c,) = run(check_prices, app)
    assert c.status == OK and "Satélite" not in c.message


def test_database_without_price_table_has_no_price_lines(app, traded):
    traded.db.execute("DROP TABLE fin_prices")
    assert run(check_prices, app) == []


def test_satellite_inactive_has_no_line(app, traded):
    prices(traded, "2026-10-02")
    assert not [c for c in run(check_prices, app) if "Satélite" in c.message]


# ------------------------------------------------------------------ operaciones


def test_no_trades_no_lines(app, svc):
    assert run(check_recent_trades, app) == []


def test_recent_csv_without_bank_data_says_no_trace(app, traded):
    (c,) = run(check_recent_trades, app)
    assert c.status == OK and "hasta 2026-10-02" in c.message
    assert "Sin datos de open banking" in c.message and "no deja ningún rastro" in c.message


def test_recent_csv_with_bank_data_cannot_detect_what_did_not_arrive(app, traded):
    bank_import(traded, bank_tx("2026-10-03", 5.00))  # un abono no es indicio
    (c,) = run(check_recent_trades, app)
    assert c.status == OK and "hasta 2026-10-02" in c.message
    assert "Una compra que aún no haya llegado no se puede detectar" in c.message
    assert "último dato nuevo: 2026-10-05" in c.message


def test_plan_amount_charge_is_probable_and_warns_at_seven_days(app, traded, clock):
    bank_import(traded, bank_tx("2026-10-03", -50.00))
    (c,) = run(check_recent_trades, app)
    assert c.status == OK and "1 probable compra," in c.message
    assert "1 cargo en" in c.message and "Podría no ser una compra" in c.message
    assert "la probable compra del 2026-10-03" in c.message
    clock.advance(days=4)  # 6 días: todavía no
    assert only(run(check_recent_trades, app), WARN) == []
    clock.advance(days=1)  # 7 días exactos (decisión 1 del usuario)
    (w,) = run(check_recent_trades, app)
    assert (
        w.status == WARN and "1 probable compra en" in w.message and "del 2026-10-03" in w.message
    )
    assert "Podría no ser una compra; la cartera no la incluye" in w.message
    assert "uv run pos finance inbox" in w.hint


def test_charge_on_the_same_day_as_the_csv_counts(app, traded):
    bank_import(traded, bank_tx("2026-10-02", -50.00))
    (c,) = run(check_recent_trades, app)
    assert "1 probable compra," in c.message and "desde el último CSV (2026-10-02)" in c.message
    assert "posteriores" not in c.message


def test_oldest_probable_decides_the_warning(app, traded, clock):
    bank_import(traded, bank_tx("2026-10-03", -50.00), bank_tx("2026-10-05", -25.00))
    clock.advance(days=4)  # 6 días del más antiguo
    (c,) = run(check_recent_trades, app)
    assert c.status == OK and "2 cargos en" in c.message and "2 probables compras," in c.message
    assert "la probable compra más antigua del 2026-10-03" in c.message
    assert "Podrían no ser compras" in c.message
    clock.advance(days=1)  # 7 días del más antiguo, 5 del más reciente
    (w,) = run(check_recent_trades, app)
    assert w.status == WARN and "2 probables compras en" in w.message
    assert "la más antigua del 2026-10-03" in w.message
    assert "Podrían no ser compras; la cartera no las incluye" in w.message


def test_today_uses_the_configured_timezone(app, traded, clock):
    bank_import(traded, bank_tx("2026-10-03", -50.00))
    # Sigue siendo el día 9 en UTC, pero ya es el 10 en la zona configurada: 7 días exactos.
    clock.set(datetime(2026, 10, 9, 23, 30, tzinfo=UTC))
    (w,) = run(check_recent_trades, app)
    assert w.status == WARN


def test_bank_data_day_uses_the_configured_timezone(app, traded, clock):
    clock.set(datetime(2026, 10, 5, 23, 30, tzinfo=UTC))
    bank_import(traded, bank_tx("2026-10-03", 5.00))
    write_sessions({"traderepublic": session("2027-01-01T00:00:00Z")})
    (c,) = run(check_recent_trades, app)
    assert "último dato nuevo: 2026-10-06" in c.message
    assert any(
        "último dato nuevo del 2026-10-06" in c.message for c in run(check_open_banking, app)
    )


def test_unidentified_charge_never_warns(app, traded, clock):
    bank_import(traded, bank_tx("2026-10-03", -7.77))
    (c,) = run(check_recent_trades, app)
    assert c.status == OK and "1 sin identificar" in c.message and "0 probables" in c.message
    assert "probable compra más antigua" not in c.message
    clock.advance(days=30)  # dentro del plazo del CSV, muy por encima de los 7 días
    (c,) = run(check_recent_trades, app)
    assert c.status == OK and "1 sin identificar" in c.message


def test_old_probable_warns_but_unidentified_stay_in_an_ok_line(app, traded, clock):
    bank_import(traded, bank_tx("2026-10-03", -50.00), bank_tx("2026-10-03", -7.77))
    clock.advance(days=10)
    (w, c) = run(check_recent_trades, app)
    assert w.status == WARN and "1 probable compra" in w.message
    assert "sin identificar" not in w.message
    assert c.status == OK and "1 cargo sin identificar" in c.message
    assert "Podría no ser una compra; la cartera no lo incluye" in c.message


def test_unrelated_movements_are_not_clues(app, traded):
    bank_import(
        traded,
        bank_tx("2026-10-03", -6.66, text="Comercio Inventado"),  # con comercio
        bank_tx("2026-10-04", -8.88),  # otra categoría (abajo)
        bank_tx("2026-10-04", 9.99),  # abono
    )
    traded.set_category(CTX, provisional_id(traded, -888), "ocio")
    (c,) = run(check_recent_trades, app)
    assert c.status == OK and "Sin cargos pendientes" in c.message


def test_inversion_category_is_probable(app, traded):
    bank_import(traded, bank_tx("2026-10-03", -7.77))
    traded.set_category(CTX, provisional_id(traded, -777), "inversion")
    (c,) = run(check_recent_trades, app)
    assert "1 probable compra, 0 sin identificar" in c.message


def test_later_csv_adopts_provisional_and_clue_disappears(app, traded):
    bank_import(traded, bank_tx("2026-10-03", -50.00))
    row = replace(
        fake.plan("2026-10-03", "XF000FAKE001", "FAKE World ETF", "50.00", "50.00"),
        tid="0000fake-0000-0000-0000-000000000998",
    )
    do_import(traded, fake.build_csv([*fake.ROWS, row]), "g.csv")
    (c,) = run(check_recent_trades, app)
    assert c.status == OK and "hasta 2026-10-03" in c.message and "Sin cargos" in c.message


def test_provisional_older_than_csv_is_not_a_clue(app, traded):
    bank_import(traded, bank_tx("2026-09-30", -7.77), bank_tx("2026-10-01", 7.77))
    (c,) = run(check_recent_trades, app)
    assert c.status == OK and "Sin cargos" in c.message


def test_real_bank_id_is_not_a_clue(app, traded):
    bank_import(traded, bank_tx("2026-10-03", -7.77, ref="ref-inventada-1"))
    (c,) = run(check_recent_trades, app)
    assert c.status == OK and "Sin cargos" in c.message


def test_known_until_falls_back_to_last_trade_without_csv(app, traded):
    traded.db.execute("UPDATE fin_imports SET source = 'otra' WHERE source = 'trade_republic_csv'")
    (c,) = run(check_recent_trades, app)
    assert c.status == OK and "hasta 2026-09-16" in c.message


def test_stale_csv_warns(app, traded, clock):
    clock.advance(days=38)  # 41 días desde el 2026-10-02
    checks = run(check_recent_trades, app)
    (w,) = only(checks, WARN)
    assert "2026-10-02" in w.message and "41 días" in w.message and "desconocidas" in w.message


def test_csv_exactly_at_the_limit_does_not_warn(app, traded, clock):
    clock.advance(days=37)  # 40 días justos desde el 2026-10-02
    assert only(run(check_recent_trades, app), WARN) == []


def test_valuation_mismatch_warns_immediately(app, traded):
    do_import(traded, fake.build_networth(positions=fake.POSITIONS[:-1]), "v.pdf")
    (w,) = only(run(check_recent_trades, app), WARN)
    assert "1 posición de" in w.message and "2026-09-30" in w.message
    assert "podría" in w.message and "bonificación" in w.message


def test_matching_valuation_does_not_warn(app, traded):
    do_import(traded, fake.build_networth(), "v.pdf")
    assert only(run(check_recent_trades, app), WARN) == []


def test_account_without_trades_is_ignored(app, traded):
    txs = [bank_tx("2026-10-03", -7.77)]
    other = eb.parse(
        institution="santander",
        account_name="Santander",
        account_kind="bank",
        balances=[
            {"balance_type": "CLBD", "balance_amount": {"currency": "EUR", "amount": "100.00"}}
        ],
        transactions=txs,
        fallback_description=bootstrap.BANKS["santander"][2],
    )
    traded.import_statement(CTX, other, file_name="ob", file_sha256="santander-fake")
    (c,) = run(check_recent_trades, app)
    assert "Santander" not in c.message and "Sin datos de open banking" in c.message


def test_messages_have_no_amounts_or_absolute_claims(app, traded, clock):
    bank_import(traded, bank_tx("2026-10-03", -50.00), bank_tx("2026-10-03", -7.77))
    do_import(traded, fake.build_networth(positions=fake.POSITIONS[:-1]), "v.pdf")
    clock.advance(days=45)
    prices(traded, "2026-09-01")
    write_sessions({"traderepublic": session("2027-01-01T00:00:00Z")})
    checks = [
        *run(check_recent_trades, app),
        *run(check_prices, app),
        *run(check_open_banking, app),
    ]
    assert checks
    for c in checks:
        text = c.message + c.hint
        for needle in ("50,00", "50.00", "7,77", "7.77", "€", "no hay compras pendientes"):
            assert needle not in text, c


# ------------------------------------------------------------------ open banking


def test_open_banking_not_configured_is_skip(app):
    (c,) = run(check_open_banking, app)
    assert c.area == "open banking" and c.status == SKIP


def test_empty_sessions_warn_with_auth_hint(app):
    write_sessions({})
    (c,) = run(check_open_banking, app)
    assert c.status == WARN and "uv run pos finance bank-auth <banco>" in c.hint


@pytest.mark.parametrize(
    "valid_until, status, text",
    [
        ("2027-01-01T00:00:00Z", OK, "vigente hasta 2027-01-01"),
        ("2026-10-19T08:00:00Z", OK, "vigente hasta 2026-10-19"),  # ahora + 14 días justos
        ("", OK, "vigencia desconocida"),
        ("2026-10-19T07:59:59Z", WARN, "caduca el 2026-10-19"),  # un segundo antes
        ("2026-10-05T08:00:00Z", WARN, "caduca el 2026-10-05"),  # justo ahora: aún no caducó
        ("2026-10-05T07:59:59Z", WARN, "caducó el 2026-10-05"),
        ("2026-10-01T00:00:00Z", WARN, "caducó el 2026-10-01"),
    ],
)
def test_authorization_validity_without_private_key(app, traded, valid_until, status, text):
    cred = write_sessions({"traderepublic": session(valid_until)})
    assert list(cred.glob("*.pem")) == []  # sin clave: demuestra que no se carga
    bank_import(traded, bank_tx("2026-10-03", 5.00))
    checks = run(check_open_banking, app)
    (c,) = [c for c in checks if AUTH in c.message]
    assert c.status == status and text in c.message
    if status == WARN:
        assert "bank-auth traderepublic" in c.hint


def test_sync_and_doctor_agree_on_expiry(app, traded):
    """Misma comparación que `bank_sync`: una sesión caducada para uno lo está para el otro."""
    write_sessions({"traderepublic": session("2026-10-05T07:59:59Z")})
    (c,) = [c for c in run(check_open_banking, app) if AUTH in c.message]
    assert "caducó" in c.message
    client = SimpleNamespace(sessions=lambda: bootstrap.bank_sessions(app.config))
    report = bootstrap.bank_sync(app, force=True, client=client)
    assert any("caducó" in e for e in report.errors)


@pytest.mark.parametrize(
    "valid_until, days, line",
    [
        ("2027-01-01T00:00:00Z", 0, "autorización vigente"),
        ("2027-01-01T00:00:00Z", 8, "sin datos nuevos"),
        ("", 0, "vigencia desconocida"),
        ("2026-10-15T00:00:00Z", 0, "caduca el"),
        ("2026-10-01T00:00:00Z", 0, "caducó el"),
    ],
)
def test_session_ids_never_shown(app, traded, clock, valid_until, days, line):
    write_sessions(
        {
            "traderepublic": session(valid_until),
            "santander": session("2027-01-01T00:00:00Z"),  # sin datos: «ningún dato»
            "desconocido": session("2027-01-01T00:00:00Z"),
        }
    )
    bank_import(traded, bank_tx("2026-10-03", -7.77))
    clock.advance(days=days)
    checks = run_doctor(offline=True, clock=clock)
    assert any(line in c.message for c in checks), checks
    for c in checks:
        assert FAKE_SESSION_ID not in str(c) and FAKE_UID not in str(c)


def test_unknown_alias_warns_and_is_ignored(app):
    write_sessions({"desconocido": session("2027-01-01T00:00:00Z")})
    (c,) = run(check_open_banking, app)
    assert c.status == WARN and c.message == "desconocido: banco desconocido, se ignora"


def test_bank_data_exactly_seven_days_old_does_not_warn(app, traded, clock):
    write_sessions({"traderepublic": session("2027-01-01T00:00:00Z")})
    bank_import(traded, bank_tx("2026-10-03", 5.00))
    clock.advance(days=7)
    assert only(run(check_open_banking, app), WARN) == []
    clock.advance(seconds=1)
    (w,) = only(run(check_open_banking, app), WARN)
    assert "sin datos nuevos" in w.message


def test_old_bank_data_warns_and_cannot_tell_why(app, traded, clock):
    write_sessions({"traderepublic": session("2027-01-01T00:00:00Z")})
    bank_import(traded, bank_tx("2026-10-03", 5.00))
    checks = run(check_open_banking, app)
    assert only(checks, WARN) == [] and any("último dato nuevo" in c.message for c in checks)
    clock.advance(days=8)
    (w,) = only(run(check_open_banking, app), WARN)
    assert "sin datos nuevos" in w.message and "no lo distingue" in w.message
    assert "--only finance" in w.hint and "sin --force" in w.hint


def test_no_bank_data_at_all_warns(app, traded):
    write_sessions({"traderepublic": session("2027-01-01T00:00:00Z")})
    (w,) = only(run(check_open_banking, app), WARN)
    assert "ningún dato" in w.message and "no lo distingue" in w.message


def test_expired_session_does_not_add_stale_data_warning(app, traded, clock):
    write_sessions({"traderepublic": session("2026-10-06T00:00:00Z")})
    bank_import(traded, bank_tx("2026-10-03", 5.00))
    clock.advance(days=30)
    (w,) = run(check_open_banking, app)
    assert "caducó" in w.message


def test_corrupt_sessions_fails_only_that_area(app, traded, clock):
    write_sessions({}, raw="esto no es json")
    checks = run_doctor(offline=True, clock=clock)
    (c,) = [c for c in checks if c.area == "open banking"]
    assert c.status == FAIL and "esto no es json" not in c.message
    assert [c for c in checks if c.area == "precios"]
    assert [c for c in checks if c.area == "operaciones"]
    assert not [c for c in checks if c.area in ("precios", "operaciones") and c.status == FAIL]


# ------------------------------------------------------------------ solo lectura y sin red


def dump() -> list[str]:
    conn = sqlite3.connect(data_dir() / "pos.db")
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


def test_doctor_writes_nothing(app, traded, clock):
    bank_import(traded, bank_tx("2026-10-03", -50.00))
    write_sessions({"traderepublic": session("2027-01-01T00:00:00Z")})
    before = dump()
    assert [c for c in run_doctor(offline=True, clock=clock) if c.area == "operaciones"]
    assert dump() == before


def test_doctor_makes_no_network_calls(app, traded, clock, monkeypatch):
    calls = []
    monkeypatch.setattr(httpx.Client, "send", lambda self, *a, **k: calls.append(a))
    write_sessions({"traderepublic": session("2027-01-01T00:00:00Z")})
    bank_import(traded, bank_tx("2026-10-03", -50.00))
    enable_satellite()
    assert [c for c in run_doctor(clock=clock) if c.area == "open banking"]
    assert calls == []


def test_missing_database_gives_no_price_or_trade_lines(clock):
    checks = run_doctor(offline=True, clock=clock)
    assert not [c for c in checks if c.area in ("precios", "operaciones")]
    assert [c for c in checks if c.area == "open banking" and c.status == SKIP]


def test_unmigrated_database_gives_no_lines_and_no_failures(clock):
    from personal_os.core.db import Database

    Database(data_dir() / "pos.db").close()
    checks = run_doctor(offline=True, clock=clock)
    assert not [c for c in checks if c.area in ("precios", "operaciones")]
    assert not [c for c in checks if c.area == "open banking" and c.status == FAIL]


def test_warnings_do_not_change_exit_code(app, traded, tmp_path):
    scriptable = tmp_path / "Scriptable"
    (scriptable / "personal-os" / "outbox").mkdir(parents=True)
    (scriptable / "personal-os" / "inbox").mkdir(parents=True)
    (scriptable / bootstrap.BRIDGE_SCRIPT_NAME).write_text(bootstrap.BRIDGE_SOURCE.read_text())
    (config_dir() / "config.toml").write_text(
        f'timezone = "Atlantic/Canary"\n[apple]\nmailbox_dir = "{scriptable / "personal-os"}"\n'
    )
    write_sessions({"traderepublic": session("2020-01-01T00:00:00Z")})
    do_import(traded, fake.build_networth(positions=fake.POSITIONS[:-1]), "v.pdf")
    res = CliRunner().invoke(cli, ["doctor", "--offline"])
    assert res.exit_code == 0, res.output
    for text in ("sin cotización", "caducó", "participaciones distintas"):
        assert text in res.output
