from __future__ import annotations

import dataclasses
import json
from typing import NoReturn

import typer


def _plain(value: object) -> object:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return str(value)


def echo_json(data: object) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2, default=_plain))


def fail(message: str, code: int = 1) -> NoReturn:
    typer.secho(f"Error: {message}", err=True, fg=typer.colors.RED)
    raise typer.Exit(code)
