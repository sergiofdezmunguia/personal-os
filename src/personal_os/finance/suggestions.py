"""Propuestas de categoría del modelo aprendido (ADR 0017): servicio de dominio.

- `refresh` entrena `categorizer.Categorizer` con lo ya categorizado de la base y guarda una
  propuesta `pending` para cada movimiento **sin categoría** en el que el modelo supera su umbral
  de abstención. No toca `fin_transactions`: el resumen del mes no cambia hasta que se aprueba.
- Solo el usuario (actor `cli`) aprueba o rechaza. Aprobar = `FinanceService.set_category`
  como el usuario, y solo si el movimiento sigue sin categoría: nunca pisa lo manual, lo de las
  reglas ni lo de Claude (`superseded`). Rechazar es para siempre en ese movimiento, y lo
  aprobado no se vuelve a proponer aunque luego se quite la categoría (queda su historial).
- Nada se guarda del modelo: ni vectores ni coeficientes; solo la categoría propuesta y su
  confianza, que son datos del propio usuario en su base.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from personal_os.core.clock import Clock, to_iso
from personal_os.core.db import Database
from personal_os.core.events import ChangeContext, EventLog
from personal_os.finance.models import FinanceError, NotFound, Transaction
from personal_os.finance.service import FinanceService

STATUSES = ("pending", "accepted", "rejected", "superseded")


@dataclass(frozen=True)
class Suggestion:
    transaction: Transaction
    category: str
    confidence: float
    status: str
    model: str
    created_at: str


@dataclass(frozen=True)
class RefreshResult:
    report: object  # categorizer.TrainingReport
    candidates: int  # movimientos sin categoría que se han mirado
    proposed: int  # con propuesta pendiente tras esta pasada
    abstained: int  # sin propuesta: poca confianza o categoría rara
    skipped_reviewed: int  # ya revisados (rechazados o aprobados): no se vuelven a proponer
    superseded: int  # propuestas viejas cuyo movimiento ya tiene categoría


@dataclass(frozen=True)
class ReviewResult:
    suggestion: Suggestion
    applied: bool  # False si el movimiento ya tenía categoría (queda `superseded`)


_SELECT = (
    "SELECT s.category AS s_category, s.confidence, s.status, s.model, s.created_at AS s_created,"
    " t.* FROM fin_category_suggestions s JOIN fin_transactions t ON t.id = s.transaction_id"
)


def _row_to_suggestion(row) -> Suggestion:
    d = dict(row)
    s = {k: d.pop(k) for k in ("s_category", "confidence", "status", "model", "s_created")}
    return Suggestion(
        Transaction(**d), s["s_category"], s["confidence"], s["status"], s["model"], s["s_created"]
    )


class SuggestionService:
    def __init__(
        self, db: Database, clock: Clock, events: EventLog, finance: FinanceService
    ) -> None:
        self.db = db
        self.clock = clock
        self.events = events
        self.finance = finance

    # ------------------------------------------------------------------ datos del modelo

    def _examples(self, where: str, params: Sequence = ()) -> list[tuple[str, object]]:
        from personal_os.finance.categorizer import Example

        rows = self.db.all(
            "SELECT t.id, t.description, t.amount_cents, t.booking_date, t.category, a.kind"
            " FROM fin_transactions t JOIN fin_accounts a ON a.id = t.account_id"
            f" WHERE {where} ORDER BY t.booking_date, t.rowid",
            params,
        )
        return [
            (
                r["id"],
                Example(
                    r["description"], r["amount_cents"], r["booking_date"], r["kind"], r["category"]
                ),
            )
            for r in rows
        ]

    def training_examples(self) -> list:
        """Todo lo categorizado (a mano, por reglas o por Claude): lo que el modelo imita."""
        return [ex for _, ex in self._examples("t.category IS NOT NULL")]

    # ------------------------------------------------------------------ proponer

    def refresh(
        self, ctx: ChangeContext, *, month: str | None = None, target_precision: float | None = None
    ) -> RefreshResult:
        """Entrena con la base y recalcula las propuestas pendientes de lo no categorizado
        (de un mes o de todo). Lanza `FinanceError` si no hay datos para aprender."""
        from personal_os.finance import categorizer

        if month:
            self.finance._check_month(month)  # antes de entrenar: falla al momento
        kwargs = {} if target_precision is None else {"target_precision": target_precision}
        try:
            model = categorizer.Categorizer.train(self.training_examples(), **kwargs)
        except categorizer.NotEnoughData as exc:
            raise FinanceError(str(exc)) from exc
        r = model.report
        tag = f"tfidf C={r.C:g} umbral={r.threshold:.3f} n={r.examples}"
        now = to_iso(self.clock.now())
        where, params = "t.category IS NULL", []
        if month:
            where += " AND t.booking_date LIKE ?"
            params.append(month + "-%")
        # Candidatos, revisadas y escritura en la misma transacción (BEGIN IMMEDIATE): una
        # revisión del usuario no puede colarse entre la lectura y la escritura.
        with self.db.transaction():
            candidates = self._examples(where, params)
            reviewed = {
                row["transaction_id"]
                for row in self.db.all(
                    "SELECT transaction_id FROM fin_category_suggestions"
                    " WHERE status IN ('rejected', 'accepted')"
                )
            }
            todo = [(tid, ex) for tid, ex in candidates if tid not in reviewed]
            preds = model.predict([ex for _, ex in todo])
            superseded = self._supersede_stale(now).rowcount
            proposed = 0
            for (tid, _), p in zip(todo, preds, strict=True):
                self.db.execute(
                    "DELETE FROM fin_category_suggestions WHERE transaction_id = ?"
                    " AND status IN ('pending', 'superseded')",
                    (tid,),
                )
                if p.category is None:
                    continue
                self.db.execute(
                    "INSERT INTO fin_category_suggestions(transaction_id, category, confidence,"
                    " status, model, created_at) VALUES (?,?,?,'pending',?,?)",
                    (tid, p.category, round(p.confidence, 4), tag, now),
                )
                proposed += 1
            result = RefreshResult(
                report=r,
                candidates=len(candidates),
                proposed=proposed,
                abstained=len(todo) - proposed,
                skipped_reviewed=len(candidates) - len(todo),
                superseded=superseded,
            )
            self.events.record(
                ctx,
                "finance.suggestions.refreshed",
                "fin_category_suggestion",
                "*",
                {
                    "month": month,
                    "candidates": result.candidates,
                    "proposed": proposed,
                    "abstained": result.abstained,
                    "skipped_reviewed": result.skipped_reviewed,
                    "superseded": superseded,
                    "C": r.C,
                    "threshold": r.threshold,
                    "examples": r.examples,
                },
            )
        return result

    def _supersede_stale(self, now: str):
        return self.db.execute(
            "UPDATE fin_category_suggestions SET status = 'superseded', reviewed_at = ?"
            " WHERE status = 'pending' AND transaction_id IN"
            " (SELECT id FROM fin_transactions WHERE category IS NOT NULL)",
            (now,),
        )

    # ------------------------------------------------------------------ consultar

    def list(
        self, *, status: str = "pending", month: str | None = None, limit: int | None = None
    ) -> list[Suggestion]:
        """Propuestas (más seguras primero). Las `pending` solo de movimientos que siguen sin
        categoría: si ya la tienen, la propuesta no cuenta."""
        if status not in STATUSES:
            raise FinanceError(f"Estado desconocido {status!r}: {', '.join(STATUSES)}")
        sql, params = _SELECT + " WHERE s.status = ?", [status]
        if status == "pending":
            sql += " AND t.category IS NULL"
        if month:
            self.finance._check_month(month)
            sql += " AND t.booking_date LIKE ?"
            params.append(month + "-%")
        sql += " ORDER BY s.confidence DESC, t.booking_date DESC, t.rowid DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [_row_to_suggestion(r) for r in self.db.all(sql, params)]

    def get(self, txn_ref: str) -> Suggestion:
        rows = self.db.all(_SELECT + " WHERE s.transaction_id LIKE ?", (txn_ref + "%",))
        if len(rows) != 1:
            raise NotFound(
                f"'{txn_ref}' no identifica una única propuesta ({len(rows)} coincidencias)"
            )
        return _row_to_suggestion(rows[0])

    # ------------------------------------------------------------------ revisar

    def review(self, ctx: ChangeContext, txn_ref: str, *, approve: bool) -> ReviewResult:
        """Aprueba (pone la categoría como el usuario) o rechaza una propuesta pendiente."""
        if ctx.actor != "cli":
            raise FinanceError("Las propuestas del modelo solo las aprueba o rechaza el usuario")
        now = to_iso(self.clock.now())
        with self.db.transaction():  # leer y decidir dentro: un refresh no puede colarse
            s = self.get(txn_ref)
            if s.status != "pending":
                raise FinanceError(
                    f"La propuesta de {s.transaction.id} no está pendiente ({s.status})"
                )
            tid = s.transaction.id
            if s.transaction.category is not None:
                status, applied = "superseded", False
            elif approve:
                self.finance.set_category(ctx, tid, s.category)
                status, applied = "accepted", True
            else:
                status, applied = "rejected", False
            self.db.execute(
                "UPDATE fin_category_suggestions SET status = ?, reviewed_at = ?, reviewed_by = ?"
                " WHERE transaction_id = ? AND status = 'pending'",
                (status, now, ctx.actor, tid),
            )
            self.events.record(
                ctx,
                f"finance.suggestion.{status}",
                "fin_transaction",
                tid,
                {"category": s.category, "confidence": s.confidence},
            )
        return ReviewResult(self.get(tid), applied)
