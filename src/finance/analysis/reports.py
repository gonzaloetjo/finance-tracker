"""Tiny helpers for rendering analysis DataFrames on the CLI."""

from __future__ import annotations

from typing import Literal

import pandas as pd
import typer

OutputFormat = Literal["table", "csv", "json"]


def emit(df: pd.DataFrame, fmt: OutputFormat = "table") -> None:
    """Print a DataFrame in the chosen format."""
    if df.empty:
        if fmt == "csv":
            typer.echo("")
        elif fmt == "json":
            typer.echo("[]")
        else:
            typer.echo("(no rows)")
        return

    if fmt == "csv":
        typer.echo(df.to_csv(index=False).rstrip())
        return
    if fmt == "json":
        # Default orient='records' + ISO dates keeps it portable.
        typer.echo(df.to_json(orient="records", date_format="iso"))
        return

    # Pretty table via pandas
    with pd.option_context(
        "display.max_rows",
        200,
        "display.max_columns",
        40,
        "display.width",
        200,
        "display.float_format",
        "{:.2f}".format,
    ):
        typer.echo(df.to_string(index=False))


def fmt_from_flags(csv: bool, json_: bool) -> OutputFormat:
    if csv and json_:
        raise typer.BadParameter("--csv and --json are mutually exclusive")
    if csv:
        return "csv"
    if json_:
        return "json"
    return "table"


__all__ = ["emit", "fmt_from_flags", "OutputFormat"]
