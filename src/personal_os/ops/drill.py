"""Simulacro de restauración: demuestra que un backup se puede usar de verdad.

1. Toma el backup indicado (o crea uno nuevo).
2. Lo verifica (descompresión, sha256, integrity_check, filas).
3. Lo restaura en un directorio temporal y aplica migraciones.
4. Abre el Personal OS sobre esa copia y lee tareas y eventos con los servicios de dominio.
5. Compara con el manifiesto. No toca la base de datos real.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path

from personal_os import bootstrap
from personal_os.core import backup as bk


@dataclass
class DrillReport:
    backup: bk.BackupInfo | None = None
    steps: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(ok for _, ok, _ in self.steps)

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.steps.append((name, ok, detail))
        return ok


def run_drill(application: bootstrap.App, ref: str | None = None) -> DrillReport:
    cfg = application.config
    report = DrillReport()
    try:
        if ref:
            info = bk.resolve_backup(cfg.backup_dir, ref)
            report.add("backup", True, f"existente: {info.path.name}")
        else:
            info = bk.create_backup(cfg.db_path, cfg.backup_dir, application.clock)
            report.add("backup", True, f"creado: {info.path.name}")
    except (bk.BackupError, OSError) as exc:
        report.add("backup", False, str(exc))
        return report
    report.backup = info

    check = bk.verify_backup(info)
    if not report.add(
        "verificación", check.ok, "; ".join(check.problems) or "sha256, integridad y filas OK"
    ):
        return report

    with tempfile.TemporaryDirectory(prefix="pos-drill-") as tmp:
        data_dir = Path(tmp)
        try:
            bk.restore_into(info, data_dir / "pos.db")
        except OSError as exc:
            report.add("restauración", False, str(exc))
            return report
        report.add("restauración", True, f"en {data_dir}")

        drill_cfg = replace(cfg, data_dir=data_dir)
        restored = bootstrap.open_app(config=drill_cfg, clock=application.clock)  # migra
        try:
            stats = bk.inspect_db(drill_cfg.db_path)
            expected = (info.manifest or {}).get("tables", {})
            mismatched = [t for t, n in expected.items() if stats.tables.get(t) != n]
            report.add(
                "filas tras migrar",
                stats.integrity == "ok" and not mismatched,
                f"integridad {stats.integrity}; "
                + (
                    f"difieren: {mismatched}"
                    if mismatched
                    else f"{sum(expected.values())} filas coinciden"
                ),
            )
            tasks = bootstrap.task_service(restored).list_tasks(include_closed=True)
            events = bootstrap.calendar_service(restored).list(include_cancelled=True)
            ok = len(tasks) == expected.get("tasks", 0) and len(events) == expected.get(
                "calendar_events", 0
            )
            report.add(
                "lectura con el dominio",
                ok,
                f"{len(tasks)} tareas y {len(events)} eventos legibles por los servicios",
            )
        finally:
            restored.db.close()
    return report
