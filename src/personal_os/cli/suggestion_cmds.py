"""`pos finance suggest` y `pos finance suggestion …`: propuestas del modelo (ADR 0016)."""

from __future__ import annotations

import math

import typer

from personal_os import bootstrap as wiring
from personal_os.cli.output import echo_json, fail
from personal_os.core.events import ChangeContext
from personal_os.finance.models import FinanceError, fmt_eur

suggestion_app = typer.Typer(
    help="Propuestas de categoría del modelo aprendido (tú las apruebas).", no_args_is_help=True
)


def _svc():
    return wiring.suggestion_service(wiring.open_app())


def _line(s) -> str:
    t = s.transaction
    return (
        f"{t.id}  {t.booking_date}  {fmt_eur(t.amount_cents):>14}  "
        f"→ {s.category:<18} {s.confidence:>4.0%}  {t.description[:60]}"
    )


def _pct(v: float | None) -> str:
    return "—" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:.0%}"


def suggest(
    month: str | None = typer.Option(None, "--month", "-m", help="YYYY-MM (por defecto, todo)"),
    evaluate: bool = typer.Option(
        False, "--evaluate", help="Solo mide el modelo con tus datos (no propone nada)"
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Entrena con lo ya categorizado y propone categoría para lo que no tiene. Solo propone
    cuando está seguro; lo demás queda para ti, las reglas o el cierre con Claude."""
    svc = _svc()
    if evaluate:
        from personal_os.finance.categorizer import NotEnoughData, evaluate_new_merchants

        typer.echo("Evaluando con comercios que el modelo no ha visto (tarda un poco)…", err=True)
        try:
            ev = evaluate_new_merchants(svc.training_examples())
        except NotEnoughData as exc:
            fail(str(exc))
        if as_json:
            echo_json(ev)
            return
        typer.echo(
            f"Comercios nuevos ({ev['examples']} movimientos, {ev['merchants']} comercios, "
            f"{ev['folds']} pliegues): acierta {_pct(ev['accuracy'])} · se atreve con "
            f"{_pct(ev['coverage'])} · y ahí acierta {_pct(ev['precision_covered'])}"
        )
        return
    try:
        res = svc.refresh(ChangeContext.cli(), month=month)
    except FinanceError as exc:
        fail(str(exc))
    r = res.report
    if as_json:
        echo_json(res)
        return
    typer.echo(
        f"Modelo: {r.examples} movimientos de {r.merchants} comercios, {r.categories} categorías"
        f" (C={r.C:g}, umbral de confianza {_pct(min(r.threshold, 1.0))})."
    )
    typer.echo(
        f"  En comercios nuevos (validación interna) acierta {_pct(r.cv_accuracy)}; propone en"
        f" el {_pct(r.cv_coverage)} y ahí acierta {_pct(r.cv_precision)}."
    )
    typer.echo(
        f"✓ {res.proposed} propuestas de {res.candidates} sin categoría "
        f"({res.abstained} sin propuesta por poca confianza"
        + (f", {res.skipped_rejected} ya rechazadas" if res.skipped_rejected else "")
        + ")."
    )
    if res.proposed:
        typer.echo(
            "  Revísalas: `pos finance suggestion list` · aprueba con "
            "`pos finance suggestion approve <id>…` o `--all`"
        )


@suggestion_app.command("list")
def suggestion_list(
    month: str | None = typer.Option(None, "--month", "-m", help="YYYY-MM"),
    status: str = typer.Option(
        "pending", "--status", help="pending | accepted | rejected | superseded"
    ),
    limit: int = typer.Option(100, "--limit", "-n"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Propuestas del modelo, las más seguras primero."""
    try:
        items = _svc().list(status=status, month=month, limit=limit)
    except FinanceError as exc:
        fail(str(exc))
    if as_json:
        echo_json(items)
        return
    if not items:
        typer.echo("No hay propuestas con esos filtros (genera nuevas con `pos finance suggest`).")
    for s in items:
        typer.echo(_line(s))


@suggestion_app.command("approve")
def suggestion_approve(
    refs: list[str] = typer.Argument(None, help="ids o prefijos de movimientos con propuesta"),
    all_: bool = typer.Option(False, "--all", help="Aprueba todas las pendientes (del mes si -m)"),
    month: str | None = typer.Option(None, "--month", "-m", help="YYYY-MM (con --all)"),
) -> None:
    """Aprueba propuestas: el movimiento queda con esa categoría, como si la pusieras tú."""
    svc = _svc()
    try:
        if all_:
            refs = [s.transaction.id for s in svc.list(month=month)]
        if not refs:
            fail("Indica qué propuestas aprobar o usa --all (ver `pos finance suggestion list`)")
        applied = skipped = 0
        for ref in refs:
            res = svc.review(ChangeContext.cli(), ref, approve=True)
            if res.applied:
                applied += 1
                typer.echo(f"✓ {_line(res.suggestion)}")
            else:
                skipped += 1
                typer.echo(f"· {res.suggestion.transaction.id}: ya tenía categoría; no se toca")
    except FinanceError as exc:
        fail(str(exc))
    typer.echo(
        f"  {applied} categorizados" + (f", {skipped} ya tenían categoría" if skipped else "")
    )


@suggestion_app.command("reject")
def suggestion_reject(
    refs: list[str] = typer.Argument(..., help="ids o prefijos de movimientos con propuesta"),
) -> None:
    """Rechaza propuestas (no se vuelven a proponer para ese movimiento)."""
    svc = _svc()
    try:
        for ref in refs:
            res = svc.review(ChangeContext.cli(), ref, approve=False)
            typer.echo(f"✗ {_line(res.suggestion)}")
    except FinanceError as exc:
        fail(str(exc))
