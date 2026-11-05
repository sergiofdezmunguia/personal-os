"""Open banking (Enable Banking): parser, enlace con lo ya importado y paso de `pos sync`.
Todo con datos inventados."""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from personal_os import bootstrap
from personal_os.adapters.enablebanking import BankingError, BankSession
from personal_os.core.config import write_config_template
from personal_os.core.events import ChangeContext
from personal_os.finance.importers import (
    PROVISIONAL,
    ParsedStatement,
    ParsedTransaction,
    StatementError,
)
from personal_os.finance.importers import enablebanking as eb
from personal_os.finance.models import FinanceError

CLI = ChangeContext.cli()


def tx(day, amount, *, ref=None, cdi=None, text=None, status="BOOK"):
    """Movimiento como lo da la API. Santander: importe sin signo + indicador."""
    return {
        "entry_reference": ref,
        "transaction_amount": {"currency": "EUR", "amount": f"{abs(amount):.2f}"},
        "credit_debit_indicator": cdi or ("DBIT" if amount < 0 else "CRDT"),
        "status": status,
        "booking_date": day,
        "value_date": day,
        "remittance_information": [text] if text else None,
    }


def bal(amount, kind="CLBD"):
    return {"balance_type": kind, "balance_amount": {"currency": "EUR", "amount": f"{amount}"}}


def parse(txs, balance, **kw):
    args = dict(
        institution="santander",
        account_name="Santander",
        account_kind="bank",
        balances=[bal(balance)],
        transactions=txs,
        fallback_description="Movimiento sin concepto",
    )
    return eb.parse(**{**args, **kw})


@pytest.fixture
def app(clock):
    write_config_template()
    application = bootstrap.open_app(clock=clock)
    yield application
    application.db.close()


@pytest.fixture
def svc(app):
    return bootstrap.finance_service(app)


def seed(svc, institution, rows, *, kind="bank", ids=False):
    """Cuenta ya importada de un extracto: rows = [(día, céntimos)], saldo encadenado desde 0."""
    bal_ = 0
    txs = []
    for i, (day, cents) in enumerate(rows):
        bal_ += cents
        txs.append(
            ParsedTransaction(
                day, day, f"COMPRA {i}", cents, bal_, external_id=f"real-{i}" if ids else None
            )
        )
    st = ParsedStatement(
        source="fake",
        institution=institution,
        account_name=institution,
        account_identifier="ES0000000000000000009999",
        currency="EUR",
        transactions=txs,
        balance_end_cents=bal_,
        account_kind=kind,
    )
    return svc.import_statement(CLI, st, file_name="extracto", file_sha256=institution + "x")


def do_import(svc, account, st, sha="eb1"):
    return svc.import_statement(CLI, st, file_name="ob", file_sha256=sha, account_id=account.id)


# ------------------------------------------------------------------ parser


def test_parse_signs_balance_chain_and_order():
    # La API da del más reciente al más antiguo.
    st = parse(
        [
            tx("2026-10-04", -20, ref="r3", text="COMPRA TARJ 1234567890123456 BAR"),
            tx("2026-10-03", 500, ref="r2", text="NOMINA"),
            tx("2026-10-02", -30, ref="r1", text="SUPER"),
            tx("2026-10-04", -5, ref="p", status="PDNG"),
        ],
        "1450.00",
    )
    assert [t.external_id for t in st.transactions] == ["r1", "r2", "r3"]
    assert [t.amount_cents for t in st.transactions] == [-3000, 50000, -2000]
    assert [t.balance_after_cents for t in st.transactions] == [97000, 147000, 145000]
    assert st.balance_end_cents == 145000
    assert "1234567890123456" not in st.transactions[2].description


def test_parse_signed_amounts_and_provisional_ids_are_stable():
    raw = [
        {**tx("2026-10-03", -1.5), "transaction_amount": {"amount": "-1.500000"}},
        {**tx("2026-10-03", -1.5), "transaction_amount": {"amount": "-1.500000"}},
    ]
    a = parse(raw, "10", institution="trade_republic", fallback_description="TR")
    b = parse(raw, "10", institution="trade_republic", fallback_description="TR")
    ids = [t.external_id for t in a.transactions]
    assert all(i.startswith(PROVISIONAL) for i in ids) and len(set(ids)) == 2
    assert ids == [t.external_id for t in b.transactions]
    assert [t.amount_cents for t in a.transactions] == [-150, -150]
    assert a.transactions[0].description == "TR"


def test_parse_prefers_booked_balance_and_rejects_bad_input():
    st = parse([], "0", balances=[bal("5", "ITAV"), bal("7", "CLBD")])
    assert st.balance_end_cents == 700
    with pytest.raises(StatementError):
        parse([], "0", balances=[])
    with pytest.raises(StatementError):
        parse([tx("2026-10-01", -1, cdi="XXXX")], "0")


# ------------------------------------------------------------------ importar


def test_santander_links_statement_rows_then_adds_only_new(svc):
    seed(svc, "santander", [("2026-09-28", 100000), ("2026-09-30", -2500), ("2026-10-01", -1000)])
    account = svc.list_accounts()[0]
    st = parse(
        [
            tx("2026-10-03", -400, ref="r4"),
            tx("2026-10-01", -10, ref="r3"),
            tx("2026-09-30", -25, ref="r2"),
        ],
        "961.00",
    )
    res = do_import(svc, account, st)
    assert (res.rows_new, res.rows_linked) == (1, 2)
    again = do_import(svc, account, st, sha="eb2")
    assert (again.rows_new, again.rows_linked) == (0, 0)
    assert len(svc.list_transactions(limit=50)) == 4


def test_balance_mismatch_imports_nothing(svc):
    days = [f"2026-09-{d:02d}" for d in range(20, 27)]
    seed(svc, "santander", [(d, -100) for d in days])
    account = svc.list_accounts()[0]
    # Mismos movimientos, pero el saldo del banco no encaja con el encadenado.
    st = parse([tx(d, -1, ref=f"r{i}") for i, d in enumerate(reversed(days))], "999")
    with pytest.raises(FinanceError, match="no cuadra"):
        do_import(svc, account, st)
    assert len(svc.list_transactions(limit=50)) == len(days)


def test_trade_republic_provisional_rows_match_then_csv_adopts_them(svc):
    seed(svc, "trade_republic", [("2026-10-01", 5000), ("2026-10-02", -150)], kind="broker",
         ids=True)  # fmt: skip
    account = svc.list_accounts()[0]
    raw = [
        {**tx("2026-10-04", -12.4), "transaction_amount": {"amount": "-12.400000"}},
        {**tx("2026-10-03", -1.5), "transaction_amount": {"amount": "-1.500000"}},  # = día 2
        {**tx("2026-10-01", 50), "transaction_amount": {"amount": "50.000000"}},
    ]
    st = parse(raw, "36.10", institution="trade_republic", fallback_description="TR")
    res = do_import(svc, account, st)
    assert res.rows_new == 1  # solo el de 12,40
    assert do_import(svc, account, st, sha="eb2").rows_new == 0
    new = [t for t in svc.list_transactions(limit=50) if t.amount_cents == -1240]
    assert new[0].external_id.startswith(PROVISIONAL)

    # Llega el CSV con id real para ese movimiento: lo adopta, no lo duplica.
    csv = ParsedStatement(
        source="trade_republic_csv",
        institution="trade_republic",
        account_name="TR",
        account_identifier="",
        currency="EUR",
        transactions=[ParsedTransaction("2026-10-04", None, "Pago: BAR", -1240, 3610, "real-9")],
        account_kind="broker",
        adopt_days=3,
    )
    out = svc.import_statement(CLI, csv, file_name="csv", file_sha256="csv", account_id=account.id)
    assert (out.rows_new, out.rows_linked) == (0, 1)
    ids = sorted(t.external_id for t in svc.list_transactions(limit=50))
    assert ids == ["real-0", "real-1", "real-9"]
    # TR no da concepto por open banking: el del CSV sustituye al provisional.
    adopted = [t for t in svc.list_transactions(limit=50) if t.external_id == "real-9"]
    assert adopted[0].description == "Pago: BAR"


# ------------------------------------------------------------------ pos sync


class FakeClient:
    def __init__(self, sessions, data, fail=None):
        self._sessions, self.data, self.fail, self.calls = sessions, data, fail, 0

    def sessions(self):
        return self._sessions

    def balances(self, uid):
        self.calls += 1
        if self.fail:
            raise BankingError(self.fail)
        return self.data[uid][0]

    def transactions(self, uid, *, date_from=None):
        return [t for t in self.data[uid][1] if t["booking_date"] >= date_from]


def session(alias="santander", valid="2027-04-30T00:00:00Z"):
    return BankSession(alias, "s-1", valid, ("acc-1",))


def test_bank_sync_imports_and_throttles(app, svc, clock):
    seed(svc, "santander", [("2026-09-30", 100000)])
    client = FakeClient(
        [session()], {"acc-1": ([bal("990.00")], [tx("2026-10-02", -10, ref="r1")])}
    )
    report = bootstrap.bank_sync(app, client=client)
    assert report.stats["transactions_new"] == 1 and not report.errors
    clock.advance(hours=2)
    assert bootstrap.bank_sync(app, client=client).stats == {"skipped_recent": 1}
    assert client.calls == 1
    assert bootstrap.bank_sync(app, client=client, force=True).stats["transactions_new"] == 0


def test_bank_sync_reports_problems_without_raising(app, svc, clock):
    soon = (clock.now() + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    expired = BankSession("traderepublic", "s-2", "2026-01-01T00:00:00Z", ("acc-2",))
    client = FakeClient([session(valid=soon), expired], {}, fail="HTTP 401")
    report = bootstrap.bank_sync(app, client=client)
    text = " ".join(report.errors + report.warnings)
    assert "caduca el" in text and "caducó" in text
    assert any("exactamente una cuenta" in e for e in report.errors)  # santander sin importar
    seed(svc, "santander", [("2026-09-30", 100)])
    report = bootstrap.bank_sync(app, client=client)
    assert any("HTTP 401" in e for e in report.errors)


def test_bank_sync_is_off_without_credentials(app):
    assert bootstrap.bank_sync(app) is None


def _tr_case(svc, bank_balance, last_amount):
    days = [f"2026-09-{d:02d}" for d in range(20, 26)]
    seed(svc, "trade_republic", [(d, -100) for d in days], kind="broker", ids=True)
    account = svc.list_accounts()[0]
    raw = [tx(d, -1) for d in reversed(days[:-1])]
    raw.insert(0, tx(days[-1], last_amount))
    st = parse(raw, bank_balance, institution="trade_republic", fallback_description="TR")
    return account, st


def test_constant_offset_is_accepted_with_warning(svc):
    # Importado: −1 € al día desde 0 → −6 € al final. El banco dice 14 €: +20 € constantes.
    account, st = _tr_case(svc, "14", -1)
    res = do_import(svc, account, st)
    assert res.rows_new == 0 and any("+20.00 €" in w for w in res.warnings)


def test_varying_offset_is_rejected(svc):
    # El último día el banco tiene otro importe (−3 €): ese día no empareja, los demás sí
    # pero su saldo ya no encaja de forma constante con el del último.
    account, st = _tr_case(svc, "14", -3)
    st = dataclasses.replace(
        st,
        transactions=[
            dataclasses.replace(t, balance_after_cents=t.balance_after_cents + 100 * i)
            for i, t in enumerate(st.transactions)
        ],
    )
    with pytest.raises(FinanceError, match="no cuadra"):
        do_import(svc, account, st)


def test_second_bank_query_after_constant_offset_is_accepted(svc):
    # CSV: −1 € al día del 20 al 25 de septiembre (saldo encadenado desde 0).
    days = [f"2026-09-{d:02d}" for d in range(20, 26)]
    seed(svc, "trade_republic", [(d, -100) for d in days], kind="broker", ids=True)
    account = svc.list_accounts()[0]
    new_days = ["2026-09-26", "2026-09-27", "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"]
    raw = [tx(d, -2) for d in reversed(new_days)] + [tx(d, -1) for d in reversed(days)]

    def query(balance, sha):
        st = parse(raw, balance, institution="trade_republic", fallback_description="TR")
        return do_import(svc, account, st, sha=sha)

    # 1.ª consulta: desfase constante con el CSV (+20 €) y 6 días nuevos provisionales.
    res = query("2", "eb1")
    assert res.rows_new == 6 and any("+20.00 €" in w for w in res.warnings)
    # 2.ª consulta: TR descuenta del saldo un pago pendiente (−5 €) que aún no lista. Los 6
    # días provisionales ya conocidos no deben compararse con su propio saldo anterior.
    res = query("-3", "eb2")
    assert res.rows_new == 0 and any("+15.00 €" in w for w in res.warnings)
