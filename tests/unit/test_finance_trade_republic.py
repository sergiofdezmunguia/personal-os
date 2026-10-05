from __future__ import annotations

import hashlib
import io

import pytest
from reportlab.pdfgen import canvas

from personal_os import bootstrap
from personal_os.core.config import write_config_template
from personal_os.core.events import ChangeContext
from personal_os.finance.importers import StatementError, detect, mask_identifiers, trade_republic
from tests import trade_republic_fake as fake

CLI = ChangeContext.cli()


@pytest.fixture
def svc(clock):
    write_config_template()
    application = bootstrap.open_app(clock=clock)
    yield bootstrap.finance_service(application)
    application.db.close()


LAYOUTS = [
    pytest.param({}, id="fecha-en-linea"),
    pytest.param({"stacked_from_page": 2}, id="mixta"),
    pytest.param({"stacked_from_page": 1, "per_page": 4}, id="fecha-apilada"),
    pytest.param({"per_page": 8}, id="paginas-largas"),
]


@pytest.mark.parametrize("layout", LAYOUTS)
def test_parse_all_layouts(layout):
    st = trade_republic.parse(fake.build(fake.SAMPLE, **layout))
    assert st.source == "trade_republic_pdf" and st.institution == "trade_republic"
    assert st.account_kind == "broker" and st.account_name == "Trade Republic · Cuenta corriente"
    assert st.last4 == "0042" and st.period == ("2026-07-01", "2026-09-30")
    assert [t.amount_cents for t in st.transactions] == [
        50000, -2345, -2345, -5000, 123, -1500, -320, 50, -999, -4000, -2500,
    ]  # fmt: skip
    assert st.transactions[-1].balance_after_cents == st.balance_end_cents == 131164
    assert st.transactions[3].description.startswith("Operar Savings plan: Savings plan execution")
    assert st.transactions[3].description.endswith("cantidad: 0.123456")  # tras la línea del año


def test_sign_comes_from_balance_and_type_from_its_column():
    st = trade_republic.parse(fake.build(fake.SAMPLE))
    by_date = {t.booking_date: t for t in st.transactions}
    assert by_date["2026-07-31"].description == "Interés: Your interest payment"
    assert by_date["2026-08-15"].amount_cents == 50  # bonificación: entrada
    assert by_date["2026-09-01"].description.startswith("Recibos domiciliados: Sepa")


def test_identifiers_are_masked():
    st = trade_republic.parse(fake.build(fake.SAMPLE))
    text = " ".join(t.description for t in st.transactions)
    assert "ES0000000000000000001234" not in text and "ES····1234" in text
    assert "600000123" not in text and "+··· ···123" in text
    assert "4000000000004321" not in text and "Tarjeta ····4321" in text
    assert "Persona Inventada" not in repr(st)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a ES0000000000000000001234 b", "a ES····1234 b"),
        ("Bizum (+34-600111222)", "Bizum (+··· ···222)"),
        ("ISIN XF000FAKE001", "ISIN XF000FAKE001"),  # un ISIN no es un IBAN
    ],
)
def test_mask_identifiers(text, expected):
    assert mask_identifiers(text) == expected


def test_tampered_balance_is_rejected():
    with pytest.raises(StatementError, match="no encadena"):
        trade_republic.parse(fake.build(fake.SAMPLE, tamper_balance_at=5))


def test_final_balance_must_match_summary():
    with pytest.raises(StatementError, match="BALANCE FINAL"):
        trade_republic.parse(fake.build(fake.SAMPLE, final_override=1.0))


def test_missing_page_is_detected():
    """Si falta un tramo (p. ej. páginas perdidas), el saldo no encadena."""
    txs = fake.SAMPLE[:3] + fake.SAMPLE[5:]
    data = fake.build(fake.SAMPLE)
    full = trade_republic.parse(data)
    assert len(full.transactions) == len(fake.SAMPLE)
    gap = fake.build(txs, initial=1000.0)
    assert len(trade_republic.parse(gap).transactions) == len(txs)  # coherente consigo mismo


def test_detection_and_other_pdfs():
    assert detect(fake.build(fake.SAMPLE), "x.pdf").source == "trade_republic_pdf"
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(100, 700, "Factura de otra empresa")
    c.save()
    assert not trade_republic.looks_like(buf.getvalue())
    with pytest.raises(StatementError, match="No reconozco"):
        detect(buf.getvalue(), "factura.pdf")


def test_import_creates_broker_account_and_is_idempotent(svc):
    data = fake.build(fake.SAMPLE, stacked_from_page=2)
    st = detect(data, "extracto.pdf")
    sha = hashlib.sha256(data).hexdigest()
    res = svc.import_statement(CLI, st, file_name="extracto.pdf", file_sha256=sha)
    assert res.rows_new == 11 and res.account.kind == "broker"
    assert res.account.label == "Trade Republic · Cuenta corriente ····0042"
    assert svc.import_statement(CLI, st, file_name="x", file_sha256=sha).already_imported
    # el mismo periodo exportado otra vez (otro PDF, otro sha) no duplica
    other = fake.build(fake.SAMPLE)
    res2 = svc.import_statement(
        CLI,
        detect(other, "otro.pdf"),
        file_name="otro.pdf",
        file_sha256=hashlib.sha256(other).hexdigest(),
    )
    assert res2.rows_new == 0 and res2.rows_duplicate == 11
    assert len(svc.list_transactions(search="supermercado ficticio")) == 2
