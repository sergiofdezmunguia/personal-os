"""Modelo de finanzas.

- `Account`: cuenta de una entidad. Del IBAN solo se guardan su sha256 y los 4 últimos.
- `Transaction`: movimiento importado. Importes en céntimos (negativo = cargo).
- `Rule`: regla determinista de categorización. Las que propone Claude (MCP) nacen
  `proposed` y no se aplican hasta que el usuario las aprueba.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RuleStatus = Literal["active", "proposed", "rejected"]
CategoryKind = Literal["expense", "income", "transfer"]


@dataclass(frozen=True)
class Account:
    id: str
    name: str
    institution: str
    kind: str
    currency: str
    external_ref: str | None
    last4: str | None
    created_at: str
    updated_at: str

    @property
    def label(self) -> str:
        return f"{self.name} ····{self.last4}" if self.last4 else self.name


@dataclass(frozen=True)
class Transaction:
    id: str
    account_id: str
    import_id: str
    booking_date: str
    value_date: str | None
    description: str
    amount_cents: int
    currency: str
    balance_after_cents: int | None
    fingerprint: str
    category: str | None
    category_source: str | None
    rule_id: str | None
    notes: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Category:
    slug: str
    name: str
    kind: CategoryKind


@dataclass(frozen=True)
class Rule:
    id: str
    pattern: str
    match_type: str
    direction: str | None
    category: str
    priority: int
    status: RuleStatus
    reason: str
    created_by: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ImportResult:
    import_id: str | None  # None si el fichero ya se había importado
    account: Account
    rows_total: int
    rows_new: int
    rows_duplicate: int
    categorized: int
    already_imported: bool
    warnings: list[str]


@dataclass(frozen=True)
class CategoryTotal:
    slug: str | None
    name: str
    kind: str | None
    total_cents: int
    count: int


@dataclass(frozen=True)
class MonthSummary:
    month: str  # YYYY-MM
    income_cents: int
    expense_cents: int  # negativo
    transfer_cents: int
    uncategorized_cents: int
    by_category: list[CategoryTotal]
    transactions: int
    uncategorized: int

    @property
    def net_cents(self) -> int:
        return self.income_cents + self.expense_cents + self.uncategorized_cents


class FinanceError(Exception):
    pass


class NotFound(FinanceError):
    pass


def fmt_eur(cents: int) -> str:
    """-123456 → '-1.234,56 €'"""
    sign = "-" if cents < 0 else ""
    euros, rest = divmod(abs(cents), 100)
    return f"{sign}{euros:,}".replace(",", ".") + f",{rest:02d} €"
