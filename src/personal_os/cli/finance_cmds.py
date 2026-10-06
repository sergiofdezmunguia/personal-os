"""`pos finance …`"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import typer

from personal_os import bootstrap as wiring
from personal_os.cli.output import echo_json, fail
from personal_os.core.events import ChangeContext
from personal_os.finance.importers import StatementError, detect
from personal_os.finance.models import FinanceError, fmt_eur

app = typer.Typer(
    help="Finanzas: importar extractos, categorizar y resúmenes.", no_args_is_help=True
)
rule_app = typer.Typer(help="Reglas de categorización.", no_args_is_help=True)
app.add_typer(rule_app, name="rule")


def _svc():
    return wiring.finance_service(wiring.open_app())


def _this_month() -> str:
    application = wiring.open_app()
    try:
        tz = ZoneInfo(application.config.timezone)
        return datetime.now(tz).strftime("%Y-%m")
    finally:
        application.db.close()


def _tx_line(t) -> str:
    cat = t.category or "·"
    mark = "*" if t.category_source == "manual" else ""
    return f"{t.id}  {t.booking_date}  {fmt_eur(t.amount_cents):>14}  {cat + mark:<16} {t.description[:70]}"


@app.command("import")
def import_(
    file: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    dry_run: bool = typer.Option(False, "--dry-run", help="Solo analiza; no guarda nada"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Importa un extracto (Santander .xls o PDF «Extracto de cuenta» de Trade Republic). Idempotente: reimportar no duplica."""
    data = file.read_bytes()
    try:
        statement = detect(data, file.name)
    except StatementError as exc:
        fail(str(exc))
    period = statement.period
    if dry_run:
        typer.echo(
            f"{statement.source}: {statement.account_name} ····{statement.last4} · "
            f"{len(statement.transactions)} movimientos · {period[0]} → {period[1]}"
        )
        for w in statement.warnings:
            typer.secho(f"  aviso: {w}", fg=typer.colors.YELLOW)
        typer.echo("(simulación: no se ha guardado nada)")
        return
    svc = _svc()
    try:
        result = svc.import_statement(
            ChangeContext.cli(),
            statement,
            file_name=file.name,
            file_sha256=hashlib.sha256(data).hexdigest(),
        )
    except FinanceError as exc:
        fail(str(exc))
    if as_json:
        echo_json(result)
        return
    typer.echo(f"✓ {result.account.label} · {period[0]} → {period[1]}")
    typer.echo(
        f"  {result.rows_new} nuevos · {result.rows_duplicate} ya existían · "
        f"{result.categorized} categorizados por reglas"
    )
    for w in result.warnings:
        typer.secho(f"  aviso: {w}", fg=typer.colors.YELLOW)
    pending = len(svc.list_transactions(uncategorized=True, limit=None))
    if pending:
        typer.echo(f"  {pending} movimientos sin categoría: `pos finance list --uncategorized`")


@app.command("accounts")
def accounts(as_json: bool = typer.Option(False, "--json")) -> None:
    """Cuentas conocidas."""
    items = _svc().list_accounts()
    if as_json:
        echo_json(items)
        return
    if not items:
        typer.echo("No hay cuentas. Importa un extracto con `pos finance import <fichero>`.")
    for a in items:
        typer.echo(f"{a.id}  {a.label}  ({a.institution}, {a.currency})")


@app.command("imports")
def imports(as_json: bool = typer.Option(False, "--json")) -> None:
    """Historial de importaciones."""
    items = _svc().list_imports()
    if as_json:
        echo_json(items)
        return
    for i in items:
        typer.echo(
            f"{i['imported_at'][:16]}  ····{i['last4']}  {i['period_from']} → {i['period_to']}  "
            f"{i['rows_new']} nuevos / {i['rows_duplicate']} dup.  {i['file_name']}"
        )


@app.command("list")
def list_(
    month: str | None = typer.Option(None, "--month", "-m", help="YYYY-MM"),
    category: str | None = typer.Option(None, "--category", "-c"),
    uncategorized: bool = typer.Option(False, "--uncategorized", "-u"),
    search: str | None = typer.Option(None, "--search", "-s", help="Texto en el concepto"),
    account: str | None = typer.Option(None, "--account", help="id o últimos 4 del IBAN"),
    limit: int = typer.Option(50, "--limit", "-n"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Movimientos (más recientes primero). `*` = categoría puesta a mano."""
    svc = _svc()
    try:
        acc = svc.get_account(account).id if account else None
        items = svc.list_transactions(
            month=month,
            account_id=acc,
            category=category,
            uncategorized=uncategorized,
            search=search,
            limit=limit,
        )
    except FinanceError as exc:
        fail(str(exc))
    if as_json:
        echo_json(items)
        return
    if not items:
        typer.echo("No hay movimientos con esos filtros.")
    for t in items:
        typer.echo(_tx_line(t))


@app.command("categorize")
def categorize(
    txn: str = typer.Argument(..., help="id o prefijo del movimiento"),
    category: str | None = typer.Argument(None, help="slug (ver `pos finance categories`)"),
    clear: bool = typer.Option(False, "--clear", help="Quita la categoría manual"),
) -> None:
    """Pone a mano la categoría de un movimiento (las reglas nunca la pisan)."""
    if (category is None) == (not clear):
        fail("Indica una categoría o --clear")
    try:
        t = _svc().set_category(ChangeContext.cli(), txn, None if clear else category)
    except FinanceError as exc:
        fail(str(exc))
    typer.echo(f"✓ {_tx_line(t)}")


@app.command("categories")
def categories(as_json: bool = typer.Option(False, "--json")) -> None:
    """Categorías disponibles (las subcategorías, sangradas bajo su principal)."""
    items = _svc().list_categories()
    if as_json:
        echo_json(items)
        return
    for c in items:
        slug = f"  {c.slug}" if c.parent else c.slug
        typer.echo(f"{slug:<22} {c.kind:<9} {c.name}")


category_app = typer.Typer(help="Categorías propias.", no_args_is_help=True)
app.add_typer(category_app, name="category")


@category_app.command("add")
def category_add(
    slug: str,
    name: str,
    parent: str | None = typer.Option(None, "--parent", help="Categoría principal"),
    kind: str | None = typer.Option(
        None, "--kind", help="expense | income | transfer (sin --parent)"
    ),
) -> None:
    """Crea una categoría o una subcategoría (--parent)."""
    try:
        c = _svc().add_category(ChangeContext.cli(), slug, name, parent=parent, kind=kind)
    except FinanceError as exc:
        fail(str(exc))
    typer.echo(f"✓ {c.slug} ({c.kind}{', dentro de ' + c.parent if c.parent else ''})")


@app.command("summary")
def summary(
    month: str | None = typer.Argument(None, help="YYYY-MM (por defecto, el mes actual)"),
    account: str | None = typer.Option(None, "--account"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Resumen mensual: ingresos, gastos, neto y gasto por categoría (sin traspasos)."""
    svc = _svc()
    month = month or _this_month()
    try:
        acc = svc.get_account(account).id if account else None
        s = svc.month_summary(month, account_id=acc)
    except FinanceError as exc:
        fail(str(exc))
    if as_json:
        echo_json({**s.__dict__, "net_cents": s.net_cents})
        return
    typer.echo(f"Resumen {s.month} · {s.transactions} movimientos")
    typer.echo(f"  Ingresos       {fmt_eur(s.income_cents):>14}")
    typer.echo(f"  Gastos         {fmt_eur(s.expense_cents):>14}")
    if s.uncategorized:
        typer.echo(
            f"  Sin categoría  {fmt_eur(s.uncategorized_cents):>14}  ({s.uncategorized} mov.)"
        )
    typer.echo(f"  Neto           {fmt_eur(s.net_cents):>14}")
    if s.transfer_cents:
        typer.echo(f"  (traspasos e inversión, fuera del neto: {fmt_eur(s.transfer_cents)})")
    typer.echo("")
    for c in s.by_category:
        typer.echo(f"  {c.name:<32} {fmt_eur(c.total_cents):>14}  {c.count:>3}")
        for sub in c.children:
            typer.echo(f"    · {sub.name:<28} {fmt_eur(sub.total_cents):>14}  {sub.count:>3}")


# --------------------------------------------------------------------------- reglas


def _rule_line(r) -> str:
    direction = {"debit": " (cargos)", "credit": " (abonos)"}.get(r.direction or "", "")
    kind = "regex " if r.match_type == "regex" else ""
    return f"{r.id}  [{r.status}]  p{r.priority}  {kind}{r.pattern!r}{direction} → {r.category}"


@rule_app.command("add")
def rule_add(
    pattern: str,
    category: str,
    regex: bool = typer.Option(False, "--regex", help="El patrón es una expresión regular"),
    debit: bool = typer.Option(False, "--debit", help="Solo cargos"),
    credit: bool = typer.Option(False, "--credit", help="Solo abonos"),
    priority: int = typer.Option(100, "--priority", help="Menor = se evalúa antes"),
    reason: str = typer.Option("", "--reason"),
) -> None:
    """Crea una regla activa y la aplica a lo no categorizado."""
    if debit and credit:
        fail("--debit y --credit son excluyentes")
    try:
        rule, applied = _svc().add_rule(
            ChangeContext.cli(),
            pattern,
            category,
            match_type="regex" if regex else "contains",
            direction="debit" if debit else "credit" if credit else None,
            priority=priority,
            reason=reason,
        )
    except FinanceError as exc:
        fail(str(exc))
    typer.echo(f"✓ {_rule_line(rule)}\n  {applied} movimientos categorizados")


@rule_app.command("list")
def rule_list(
    status: str | None = typer.Option(None, "--status", help="active | proposed | rejected"),
) -> None:
    """Reglas (las `proposed` vienen de Claude y esperan tu aprobación)."""
    for r in _svc().list_rules(status):
        typer.echo(_rule_line(r) + (f"\n      motivo: {r.reason}" if r.reason else ""))


@rule_app.command("preview")
def rule_preview(
    pattern: str,
    regex: bool = typer.Option(False, "--regex"),
) -> None:
    """Qué movimientos casarían con un patrón (no guarda nada)."""
    try:
        items = _svc().preview_rule(pattern, match_type="regex" if regex else "contains")
    except FinanceError as exc:
        fail(str(exc))
    typer.echo(f"{len(items)} movimientos")
    for t in items[:30]:
        typer.echo(_tx_line(t))


@rule_app.command("edit")
def rule_edit(
    ref: str,
    category: str | None = typer.Option(None, "--category", "-c"),
    priority: int | None = typer.Option(None, "--priority"),
) -> None:
    """Cambia la categoría destino o la prioridad de una regla y recoloca sus movimientos."""
    if category is None and priority is None:
        fail("Indica --category y/o --priority")
    try:
        rule, moved = _svc().update_rule(
            ChangeContext.cli(), ref, category=category, priority=priority
        )
    except FinanceError as exc:
        fail(str(exc))
    typer.echo(f"✓ {_rule_line(rule)}\n  {moved} movimientos recolocados")


@rule_app.command("approve")
def rule_approve(
    refs: list[str] = typer.Argument(None, help="ids o prefijos de reglas propuestas"),
    all_: bool = typer.Option(False, "--all", help="Aprueba todas las propuestas"),
) -> None:
    """Aprueba reglas propuestas (por Claude) y las aplica."""
    svc = _svc()
    if all_:
        refs = [r.id for r in svc.list_rules("proposed")]
    if not refs:
        fail(
            "Indica qué reglas aprobar o usa --all (ver `pos finance rule list --status proposed`)"
        )
    total = 0
    for ref in refs:
        try:
            rule, applied = svc.review_rule(ChangeContext.cli(), ref, approve=True)
        except FinanceError as exc:
            fail(str(exc))
        total += applied
        typer.echo(f"✓ {_rule_line(rule)}")
    typer.echo(f"  {total} movimientos categorizados")


@rule_app.command("reject")
def rule_reject(ref: str) -> None:
    """Rechaza una regla propuesta."""
    try:
        rule, _ = _svc().review_rule(ChangeContext.cli(), ref, approve=False)
    except FinanceError as exc:
        fail(str(exc))
    typer.echo(f"✓ {_rule_line(rule)}")


@rule_app.command("delete")
def rule_delete(ref: str) -> None:
    """Desactiva una regla y libera los movimientos que había categorizado."""
    try:
        rule = _svc().delete_rule(ChangeContext.cli(), ref)
    except FinanceError as exc:
        fail(str(exc))
    typer.echo(f"✓ {_rule_line(rule)}")


@rule_app.command("apply")
def rule_apply(
    recategorize: bool = typer.Option(
        False, "--recategorize", help="Recalcula también lo categorizado por reglas (no lo manual)"
    ),
) -> None:
    """Aplica las reglas activas."""
    n = _svc().apply_rules(ChangeContext.cli(), recategorize=recategorize)
    typer.echo(f"✓ {n} movimientos categorizados")
