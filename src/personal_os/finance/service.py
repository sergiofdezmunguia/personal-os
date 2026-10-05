"""Servicio de finanzas: única puerta de entrada para modificar cuentas, movimientos y reglas.

- Importar es idempotente: el mismo fichero (sha256) no se procesa dos veces y cada
  movimiento tiene una huella estable, así que extractos solapados solo añaden lo nuevo.
- Categorizar es determinista (reglas por prioridad). Lo puesto a mano nunca se pisa.
- Las reglas creadas por el actor `mcp` nacen `proposed`: solo el usuario las aprueba.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable

from personal_os.core.clock import Clock, to_iso
from personal_os.core.db import Database
from personal_os.core.events import ChangeContext, EventLog
from personal_os.core.ids import new_id
from personal_os.finance.importers import ParsedStatement
from personal_os.finance.models import (
    Account,
    Category,
    CategoryTotal,
    FinanceError,
    ImportResult,
    MonthSummary,
    NotFound,
    Rule,
    Transaction,
)

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def normalize(text: str) -> str:
    """Minúsculas, sin acentos y con espacios colapsados: lo que comparan las reglas."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(plain.lower().split())


def _fingerprint(account_id: str, tx, n: int) -> str:
    key = "|".join(
        str(x)
        for x in (
            account_id,
            tx.booking_date,
            tx.value_date,
            tx.amount_cents,
            tx.balance_after_cents,
            normalize(tx.description),
            n,
        )
    )
    return hashlib.sha256(key.encode()).hexdigest()


def rule_matches(rule: Rule, description: str, amount_cents: int) -> bool:
    if rule.direction == "debit" and amount_cents >= 0:
        return False
    if rule.direction == "credit" and amount_cents < 0:
        return False
    text = normalize(description)
    if rule.match_type == "contains":
        return normalize(rule.pattern) in text
    return re.search(rule.pattern, text, re.IGNORECASE) is not None


class FinanceService:
    def __init__(self, db: Database, clock: Clock, events: EventLog) -> None:
        self.db = db
        self.clock = clock
        self.events = events

    def _now(self) -> str:
        return to_iso(self.clock.now())

    # ---------------------------------------------------------------- consultas

    def list_accounts(self) -> list[Account]:
        return [Account(**dict(r)) for r in self.db.all("SELECT * FROM fin_accounts ORDER BY name")]

    def get_account(self, ref: str) -> Account:
        rows = self.db.all(
            "SELECT * FROM fin_accounts WHERE id LIKE ? OR last4 = ?", (ref + "%", ref)
        )
        if len(rows) != 1:
            raise NotFound(f"'{ref}' no identifica una única cuenta ({len(rows)} coincidencias)")
        return Account(**dict(rows[0]))

    def list_categories(self) -> list[Category]:
        rows = self.db.all(
            "SELECT * FROM fin_categories ORDER BY kind, COALESCE(parent, slug), parent IS NOT NULL, name"
        )
        return [Category(**dict(r)) for r in rows]

    def get_category(self, slug: str) -> Category:
        row = self.db.one("SELECT * FROM fin_categories WHERE slug = ?", (slug,))
        if row is None:
            known = ", ".join(c.slug for c in self.list_categories())
            raise NotFound(f"Categoría desconocida {slug!r}. Disponibles: {known}")
        return Category(**dict(row))

    def get_transaction(self, ref: str) -> Transaction:
        rows = self.db.all("SELECT * FROM fin_transactions WHERE id LIKE ?", (ref + "%",))
        if len(rows) != 1:
            raise NotFound(f"'{ref}' no identifica un único movimiento ({len(rows)} coincidencias)")
        return Transaction(**dict(rows[0]))

    def list_transactions(
        self,
        *,
        month: str | None = None,
        account_id: str | None = None,
        category: str | None = None,
        uncategorized: bool = False,
        search: str | None = None,
        limit: int | None = 200,
    ) -> list[Transaction]:
        where, params = [], []
        if month:
            self._check_month(month)
            where.append("booking_date LIKE ?")
            params.append(month + "-%")
        if account_id:
            where.append("account_id = ?")
            params.append(account_id)
        if category:
            where.append(
                "category IN (SELECT slug FROM fin_categories WHERE slug = ? OR parent = ?)"
            )
            params += [category, category]
        if uncategorized:
            where.append("category IS NULL")
        sql = "SELECT * FROM fin_transactions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY booking_date DESC, rowid DESC"
        txs = [Transaction(**dict(r)) for r in self.db.all(sql, params)]
        if search:
            needle = normalize(search)
            txs = [t for t in txs if needle in normalize(t.description)]
        return txs[:limit] if limit else txs

    def list_rules(self, status: str | None = None) -> list[Rule]:
        sql, params = "SELECT * FROM fin_rules", []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY status, priority, created_at"
        return [Rule(**dict(r)) for r in self.db.all(sql, params)]

    def get_rule(self, ref: str) -> Rule:
        rows = self.db.all("SELECT * FROM fin_rules WHERE id LIKE ?", (ref + "%",))
        if len(rows) != 1:
            raise NotFound(f"'{ref}' no identifica una única regla ({len(rows)} coincidencias)")
        return Rule(**dict(rows[0]))

    def list_imports(self) -> list[dict]:
        rows = self.db.all(
            "SELECT i.*, a.name AS account_name, a.last4 FROM fin_imports i"
            " JOIN fin_accounts a ON a.id = i.account_id ORDER BY i.imported_at DESC"
        )
        return [dict(r) for r in rows]

    def last_import_at(self) -> str | None:
        row = self.db.one("SELECT MAX(imported_at) AS m FROM fin_imports")
        return row["m"] if row else None

    # ---------------------------------------------------------------- importar

    def import_statement(
        self, ctx: ChangeContext, statement: ParsedStatement, *, file_name: str, file_sha256: str
    ) -> ImportResult:
        now = self._now()
        warnings = list(statement.warnings)
        with self.db.transaction():
            account = self._ensure_account(ctx, statement, now)
            seen = self.db.one(
                "SELECT id FROM fin_imports WHERE account_id = ? AND file_sha256 = ?",
                (account.id, file_sha256),
            )
            if seen is not None:
                return ImportResult(
                    import_id=None,
                    account=account,
                    rows_total=len(statement.transactions),
                    rows_new=0,
                    rows_duplicate=len(statement.transactions),
                    categorized=0,
                    already_imported=True,
                    warnings=["Este fichero ya se había importado; no se ha hecho nada"],
                )

            gap = self._gap_warning(account.id, statement)
            if gap:
                warnings.append(gap)

            import_id = new_id("import")
            period_from, period_to = statement.period
            self.db.execute(
                "INSERT INTO fin_imports(id, account_id, source, file_name, file_sha256,"
                " period_from, period_to, rows_total, rows_new, rows_duplicate,"
                " balance_end_cents, actor, imported_at) VALUES (?,?,?,?,?,?,?,?,0,0,?,?,?)",
                (
                    import_id,
                    account.id,
                    statement.source,
                    file_name,
                    file_sha256,
                    period_from,
                    period_to,
                    len(statement.transactions),
                    statement.balance_end_cents,
                    ctx.actor,
                    now,
                ),
            )
            counts: Counter = Counter()
            new_ids = []
            for tx in statement.transactions:
                ident = (
                    tx.booking_date,
                    tx.value_date,
                    tx.amount_cents,
                    tx.balance_after_cents,
                    normalize(tx.description),
                )
                n = counts[ident]
                counts[ident] += 1
                txn_id = new_id("transaction")
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO fin_transactions(id, account_id, import_id,"
                    " booking_date, value_date, description, amount_cents, currency,"
                    " balance_after_cents, fingerprint, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        txn_id,
                        account.id,
                        import_id,
                        tx.booking_date,
                        tx.value_date,
                        tx.description,
                        tx.amount_cents,
                        statement.currency,
                        tx.balance_after_cents,
                        _fingerprint(account.id, tx, n),
                        now,
                        now,
                    ),
                )
                if cur.rowcount:
                    new_ids.append(txn_id)
            duplicates = len(statement.transactions) - len(new_ids)
            self.db.execute(
                "UPDATE fin_imports SET rows_new = ?, rows_duplicate = ? WHERE id = ?",
                (len(new_ids), duplicates, import_id),
            )
            categorized = self._apply_rules(ctx, new_ids)
            self.events.record(
                ctx,
                "finance.import.completed",
                "fin_import",
                import_id,
                {
                    "account_id": account.id,
                    "source": statement.source,
                    "rows_new": len(new_ids),
                    "rows_duplicate": duplicates,
                    "categorized": categorized,
                    "period": [period_from, period_to],
                },
            )
        return ImportResult(
            import_id=import_id,
            account=account,
            rows_total=len(statement.transactions),
            rows_new=len(new_ids),
            rows_duplicate=duplicates,
            categorized=categorized,
            already_imported=False,
            warnings=warnings,
        )

    def _ensure_account(self, ctx: ChangeContext, st: ParsedStatement, now: str) -> Account:
        row = self.db.one(
            "SELECT * FROM fin_accounts WHERE institution = ? AND external_ref = ?",
            (st.institution, st.external_ref),
        )
        if row is not None:
            return Account(**dict(row))
        acc_id = new_id("account")
        self.db.execute(
            "INSERT INTO fin_accounts(id, name, institution, kind, currency, external_ref, last4,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                acc_id,
                st.account_name,
                st.institution,
                "bank",
                st.currency,
                st.external_ref,
                st.last4,
                now,
                now,
            ),
        )
        self.events.record(
            ctx,
            "finance.account.created",
            "fin_account",
            acc_id,
            {"institution": st.institution, "name": st.account_name},
        )
        return Account(**dict(self.db.one("SELECT * FROM fin_accounts WHERE id = ?", (acc_id,))))

    def _gap_warning(self, account_id: str, st: ParsedStatement) -> str | None:
        """Si el extracto empieza después de lo ya importado, el saldo debe enlazar con el
        último movimiento conocido; si no, falta un tramo."""
        if not st.transactions or st.transactions[0].balance_after_cents is None:
            return None
        first = st.transactions[0]
        last = self.db.one(
            "SELECT booking_date, balance_after_cents FROM fin_transactions"
            " WHERE account_id = ? ORDER BY booking_date DESC, rowid DESC LIMIT 1",
            (account_id,),
        )
        if last is None or last["balance_after_cents"] is None:
            return None
        if last["booking_date"] > first.booking_date:
            return None  # solapa con lo ya importado: la huella evita duplicados
        if last["balance_after_cents"] == first.balance_after_cents - first.amount_cents:
            return None
        return (
            f"Posible hueco: el saldo no enlaza entre el {last['booking_date']} (último "
            f"movimiento importado) y el {first.booking_date}. Importa también ese periodo."
        )

    # ---------------------------------------------------------------- categorizar

    def _apply_rules(self, ctx: ChangeContext, txn_ids: Iterable[str] | None) -> int:
        rules = self.list_rules("active")
        if not rules:
            return 0
        sql = "SELECT * FROM fin_transactions WHERE category_source IS NULL"
        params: list = []
        ids = list(txn_ids) if txn_ids is not None else None
        if ids is not None:
            if not ids:
                return 0
            sql += f" AND id IN ({','.join('?' * len(ids))})"
            params = ids
        now, done = self._now(), 0
        for row in self.db.all(sql, params):
            for rule in rules:
                if rule_matches(rule, row["description"], row["amount_cents"]):
                    self.db.execute(
                        "UPDATE fin_transactions SET category = ?, category_source = 'rule',"
                        " rule_id = ?, updated_at = ? WHERE id = ?",
                        (rule.category, rule.id, now, row["id"]),
                    )
                    done += 1
                    break
        if done:
            self.events.record(
                ctx, "finance.rules.applied", "fin_transaction", "*", {"categorized": done}
            )
        return done

    def apply_rules(self, ctx: ChangeContext, *, recategorize: bool = False) -> int:
        """Aplica las reglas activas a lo no categorizado. Con `recategorize`, también vuelve
        a calcular lo categorizado por reglas (nunca lo manual)."""
        with self.db.transaction():
            if recategorize:
                self.db.execute(
                    "UPDATE fin_transactions SET category = NULL, category_source = NULL,"
                    " rule_id = NULL WHERE category_source = 'rule'"
                )
            return self._apply_rules(ctx, None)

    def set_category(self, ctx: ChangeContext, txn_ref: str, slug: str | None) -> Transaction:
        """Categoría manual (gana siempre a las reglas). `None` la quita y deja actuar a las reglas."""
        txn = self.get_transaction(txn_ref)
        if slug is not None:
            self.get_category(slug)
        with self.db.transaction():
            self.db.execute(
                "UPDATE fin_transactions SET category = ?, category_source = ?, rule_id = NULL,"
                " updated_at = ? WHERE id = ?",
                (slug, "manual" if slug else None, self._now(), txn.id),
            )
            self.events.record(
                ctx,
                "finance.transaction.categorized",
                "fin_transaction",
                txn.id,
                {"from": txn.category, "to": slug, "source": "manual"},
            )
            if slug is None:
                self._apply_rules(ctx, [txn.id])
        return self.get_transaction(txn.id)

    # ---------------------------------------------------------------- reglas

    def preview_rule(
        self, pattern: str, *, match_type: str = "contains", direction: str | None = None
    ) -> list[Transaction]:
        """Movimientos que casarían con una regla (sin guardarla)."""
        probe = self._build_rule(pattern, "otros-gastos", match_type, direction, 0, "", "-")
        return [
            t
            for t in self.list_transactions(limit=None)
            if rule_matches(probe, t.description, t.amount_cents)
        ]

    def _build_rule(
        self, pattern, category, match_type, direction, priority, reason, actor
    ) -> Rule:
        if not pattern.strip():
            raise FinanceError("El patrón no puede estar vacío")
        if match_type not in ("contains", "regex"):
            raise FinanceError("match_type debe ser 'contains' o 'regex'")
        if direction not in (None, "debit", "credit"):
            raise FinanceError("direction debe ser 'debit', 'credit' o vacío")
        if match_type == "regex":
            try:
                re.compile(pattern)
            except re.error as exc:
                raise FinanceError(f"Expresión regular inválida: {exc}") from exc
        now = self._now()
        return Rule(
            id=new_id("rule"),
            pattern=pattern.strip(),
            match_type=match_type,
            direction=direction,
            category=category,
            priority=priority,
            status="proposed" if actor == "mcp" else "active",
            reason=reason,
            created_by=actor,
            created_at=now,
            updated_at=now,
        )

    def add_rule(
        self,
        ctx: ChangeContext,
        pattern: str,
        category: str,
        *,
        match_type: str = "contains",
        direction: str | None = None,
        priority: int = 100,
        reason: str = "",
    ) -> tuple[Rule, int]:
        """Crea una regla. Desde el MCP queda `proposed` (no se aplica). Devuelve la regla y
        cuántos movimientos ha categorizado."""
        self.get_category(category)
        rule = self._build_rule(
            pattern, category, match_type, direction, priority, reason, ctx.actor
        )
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO fin_rules(id, pattern, match_type, direction, category, priority,"
                " status, reason, created_by, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rule.id,
                    rule.pattern,
                    rule.match_type,
                    rule.direction,
                    rule.category,
                    rule.priority,
                    rule.status,
                    rule.reason,
                    rule.created_by,
                    rule.created_at,
                    rule.updated_at,
                ),
            )
            self.events.record(
                ctx,
                f"finance.rule.{'proposed' if rule.status == 'proposed' else 'created'}",
                "fin_rule",
                rule.id,
                {"pattern": rule.pattern, "category": rule.category, "match_type": match_type},
            )
            applied = self._apply_rules(ctx, None) if rule.status == "active" else 0
        return rule, applied

    def review_rule(self, ctx: ChangeContext, ref: str, *, approve: bool) -> tuple[Rule, int]:
        if ctx.actor == "mcp":
            raise FinanceError("Las reglas solo las aprueba o rechaza el usuario")
        rule = self.get_rule(ref)
        if rule.status != "proposed":
            raise FinanceError(f"La regla {rule.id} no está pendiente (estado: {rule.status})")
        status = "active" if approve else "rejected"
        with self.db.transaction():
            self.db.execute(
                "UPDATE fin_rules SET status = ?, updated_at = ? WHERE id = ?",
                (status, self._now(), rule.id),
            )
            self.events.record(
                ctx,
                f"finance.rule.{'approved' if approve else 'rejected'}",
                "fin_rule",
                rule.id,
                {},
            )
            applied = self._apply_rules(ctx, None) if approve else 0
        return self.get_rule(rule.id), applied

    def delete_rule(self, ctx: ChangeContext, ref: str) -> Rule:
        """Desactiva una regla (queda como `rejected`) y libera lo que había categorizado."""
        if ctx.actor == "mcp":
            raise FinanceError("Las reglas solo las gestiona el usuario")
        rule = self.get_rule(ref)
        with self.db.transaction():
            self.db.execute(
                "UPDATE fin_rules SET status = 'rejected', updated_at = ? WHERE id = ?",
                (self._now(), rule.id),
            )
            self.db.execute(
                "UPDATE fin_transactions SET category = NULL, category_source = NULL,"
                " rule_id = NULL WHERE rule_id = ?",
                (rule.id,),
            )
            self.events.record(ctx, "finance.rule.deleted", "fin_rule", rule.id, {})
            self._apply_rules(ctx, None)
        return self.get_rule(rule.id)

    # ---------------------------------------------------------------- resumen

    @staticmethod
    def _check_month(month: str) -> None:
        if not _MONTH_RE.match(month):
            raise FinanceError(f"Mes inválido {month!r} (formato YYYY-MM)")

    def month_summary(self, month: str, *, account_id: str | None = None) -> MonthSummary:
        self._check_month(month)
        sql = (
            "SELECT t.category AS slug, c.name AS name, c.kind AS kind, c.parent AS parent,"
            " SUM(t.amount_cents) AS total, COUNT(*) AS n"
            " FROM fin_transactions t LEFT JOIN fin_categories c ON c.slug = t.category"
            " WHERE t.booking_date LIKE ?"
        )
        params: list = [month + "-%"]
        if account_id:
            sql += " AND t.account_id = ?"
            params.append(account_id)
        sql += " GROUP BY t.category"
        rows = self.db.all(sql, params)
        names = {c.slug: c for c in self.list_categories()}
        groups: dict[str | None, dict] = {}
        for r in rows:
            top = r["parent"] or r["slug"]
            g = groups.setdefault(top, {"total": 0, "n": 0, "children": []})
            g["total"] += r["total"]
            g["n"] += r["n"]
            if r["parent"]:
                g["children"].append(
                    CategoryTotal(r["slug"], r["name"], r["kind"], r["total"], r["n"])
                )
        by_cat = sorted(
            (
                CategoryTotal(
                    slug=top,
                    name=names[top].name if top else "Sin categoría",
                    kind=names[top].kind if top else None,
                    total_cents=g["total"],
                    count=g["n"],
                    children=sorted(g["children"], key=lambda c: c.total_cents),
                )
                for top, g in groups.items()
            ),
            key=lambda c: c.total_cents,
        )

        def total(kind):
            return sum(c.total_cents for c in by_cat if c.kind == kind)

        uncat = next((c for c in by_cat if c.slug is None), None)
        return MonthSummary(
            month=month,
            income_cents=total("income"),
            expense_cents=total("expense"),
            transfer_cents=total("transfer"),
            uncategorized_cents=uncat.total_cents if uncat else 0,
            by_category=by_cat,
            transactions=sum(c.count for c in by_cat),
            uncategorized=uncat.count if uncat else 0,
        )

    # ---------------------------------------------------------------- categorías

    def add_category(
        self,
        ctx: ChangeContext,
        slug: str,
        name: str,
        *,
        parent: str | None = None,
        kind: str | None = None,
    ) -> Category:
        """Crea una categoría (o subcategoría de `parent`, de la que hereda el tipo)."""
        if ctx.actor == "mcp":
            raise FinanceError("Las categorías solo las crea el usuario")
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,40}", slug):
            raise FinanceError("El slug solo admite minúsculas, números y guiones")
        if self.db.one("SELECT 1 FROM fin_categories WHERE slug = ?", (slug,)):
            raise FinanceError(f"Ya existe la categoría {slug!r}")
        if parent is not None:
            p = self.get_category(parent)
            if p.parent is not None:
                raise FinanceError("Solo hay un nivel de subcategorías")
            if kind is not None and kind != p.kind:
                raise FinanceError(f"Una subcategoría de {p.slug} es de tipo {p.kind}")
            kind = p.kind
        if kind not in ("expense", "income", "transfer"):
            raise FinanceError("kind debe ser expense, income o transfer")
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO fin_categories(slug, name, kind, parent) VALUES (?,?,?,?)",
                (slug, name.strip(), kind, parent),
            )
            self.events.record(
                ctx,
                "finance.category.created",
                "fin_category",
                slug,
                {"name": name, "kind": kind, "parent": parent},
            )
        return self.get_category(slug)

    def update_rule(
        self,
        ctx: ChangeContext,
        ref: str,
        *,
        category: str | None = None,
        priority: int | None = None,
    ) -> tuple[Rule, int]:
        """Cambia destino o prioridad de una regla. Los movimientos que ya categorizaba se
        recolocan; con nueva prioridad se recalcula todo lo categorizado por reglas."""
        if ctx.actor == "mcp":
            raise FinanceError("Las reglas solo las gestiona el usuario")
        rule = self.get_rule(ref)
        if category is not None:
            self.get_category(category)
        now = self._now()
        with self.db.transaction():
            self.db.execute(
                "UPDATE fin_rules SET category = COALESCE(?, category),"
                " priority = COALESCE(?, priority), updated_at = ? WHERE id = ?",
                (category, priority, now, rule.id),
            )
            moved = 0
            if category is not None:
                moved = self.db.execute(
                    "UPDATE fin_transactions SET category = ?, updated_at = ?"
                    " WHERE rule_id = ? AND category_source = 'rule'",
                    (category, now, rule.id),
                ).rowcount
            self.events.record(
                ctx,
                "finance.rule.updated",
                "fin_rule",
                rule.id,
                {"category": category, "priority": priority},
            )
            if priority is not None:
                self.db.execute(
                    "UPDATE fin_transactions SET category = NULL, category_source = NULL,"
                    " rule_id = NULL WHERE category_source = 'rule'"
                )
                moved = self._apply_rules(ctx, None)
        return self.get_rule(rule.id), moved
