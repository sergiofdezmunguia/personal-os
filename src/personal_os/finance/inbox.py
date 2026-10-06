"""Bandeja de extractos: una carpeta (en iCloud Drive) donde el usuario deja los ficheros del
banco desde el iPhone o el PC. Se importan y se mueven a `importados/AAAA-MM/`.

Determinista y sin red: detecta el formato, importa (idempotente) y archiva. Lo que no se
reconoce o no cuadra se queda donde estaba, con el motivo en el informe: nunca se borra nada.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from personal_os.core.events import ChangeContext
from personal_os.finance.importers import StatementError, detect
from personal_os.finance.models import FinanceError, ImportResult

SUFFIXES = {".xls", ".xlsx", ".pdf"}
ARCHIVE = "importados"


@dataclass
class InboxItem:
    file_name: str
    result: ImportResult | None = None
    error: str | None = None
    archived_to: str | None = None  # ruta relativa a la bandeja


@dataclass
class InboxReport:
    items: list[InboxItem] = field(default_factory=list)

    @property
    def imported(self) -> list[InboxItem]:
        return [i for i in self.items if i.result and not i.result.already_imported]

    @property
    def failed(self) -> list[InboxItem]:
        return [i for i in self.items if i.error]

    @property
    def rows_new(self) -> int:
        return sum(i.result.rows_new for i in self.imported)


def pending_files(inbox: Path) -> list[Path]:
    """Ficheros candidatos (sin entrar en subcarpetas ni en ocultos/temporales)."""
    if not inbox.is_dir():
        return []
    return sorted(
        p
        for p in inbox.iterdir()
        if p.is_file() and p.suffix.lower() in SUFFIXES and not p.name.startswith((".", "~$"))
    )


def _archive(inbox: Path, src: Path, month: str) -> Path:
    dest_dir = inbox / ARCHIVE / month
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    n = 1
    while dest.exists():
        dest = dest_dir / f"{src.stem} ({n}){src.suffix}"
        n += 1
    src.rename(dest)
    return dest


def process_inbox(svc, ctx: ChangeContext, inbox: Path, *, today: str) -> InboxReport:
    """Importa todo lo pendiente de la bandeja. `today` (YYYY-MM-DD) solo se usa para archivar
    lo que no tiene movimientos."""
    report = InboxReport()
    for path in pending_files(inbox):
        item = InboxItem(file_name=path.name)
        report.items.append(item)
        try:
            data = path.read_bytes()
            statement = detect(data, path.name)
            item.result = svc.import_statement(
                ctx,
                statement,
                file_name=path.name,
                file_sha256=hashlib.sha256(data).hexdigest(),
            )
        except (StatementError, FinanceError) as exc:
            item.error = str(exc)
            continue
        except OSError as exc:  # p. ej. iCloud aún no ha descargado el fichero
            item.error = f"No se pudo leer: {exc}"
            continue
        month = (statement.period[1] or today)[:7]
        try:
            item.archived_to = str(_archive(inbox, path, month).relative_to(inbox))
        except OSError as exc:
            item.error = f"Importado, pero no se pudo mover a {ARCHIVE}/: {exc}"
    return report
