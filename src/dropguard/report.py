"""Console, Markdown, and JSON presentations for validation results."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .engine import ValidationResult


def _escape(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("`", "\\`").replace("\n", " ")


def render_console(result: ValidationResult, console: Console | None = None) -> None:
    console = console or Console()
    passed = result.status == "PASS"
    warning_count = sum(check.status == "warn" for check in result.checks)
    status_label = "FAIL" if not passed else f"PASS · {warning_count} WARNINGS" if warning_count else "PASS"
    status_style = "red" if not passed else "yellow" if warning_count else "green"
    header = Text.assemble(
        (f" {status_label} ", "bold black on yellow" if warning_count and passed else f"bold white on {status_style}"),
        (f"  {result.dataset}  ·  {result.source}", "bold"),
    )
    metadata = Text(f"{result.row_count:,} rows  ·  {result.column_count} columns  ·  {len(result.checks)} checks")
    summary = Panel(Group(header, metadata), border_style=status_style, padding=(0, 1))
    table = Table(box=box.SIMPLE_HEAVY, expand=True, header_style="bold")
    table.add_column("Check", min_width=20)
    table.add_column("Result", width=8)
    table.add_column("Observed", overflow="fold")
    table.add_column("Expected", overflow="fold")
    for check in result.checks:
        style = {"pass": "green", "warn": "yellow", "fail": "red"}[check.status]
        table.add_row(check.name, Text(check.status.upper(), style=f"bold {style}"), check.observed, check.expected)
    parts: list[Any] = [summary, table]
    if result.issues:
        issue_lines = [
            f"[bold]Line {issue['line']} · {issue['rule']}[/]  {issue['message']}"
            for issue in result.issues
        ]
        issue_title = (
            f"Issue samples ({len(result.issues)} shown of {result.metrics['issues_observed']}; "
            "no source values shown)"
        )
        parts.append(Panel("\n".join(issue_lines), title=issue_title, border_style="yellow"))
    console.print(Group(*parts))


def render_markdown(result: ValidationResult) -> str:
    lines = [
        f"# DropGuard report — {result.dataset}",
        "",
        f"- **Status:** {result.status}",
        f"- **Input:** `{_escape(result.source)}`",
        f"- **Rows:** {result.row_count:,}",
        f"- **Columns:** {result.column_count}",
        "",
        "## Checks",
        "",
        "| Check | Status | Observed | Expected | Note |",
        "| --- | --- | --- | --- | --- |",
    ]
    for check in result.checks:
        lines.append(
            f"| {_escape(check.name)} | **{check.status.upper()}** | {_escape(check.observed)} | "
            f"{_escape(check.expected)} | {_escape(check.detail)} |"
        )
    if result.issues:
        observed = result.metrics["issues_observed"]
        lines.extend([
            "",
            "## Issue samples",
            "",
            f"Showing {len(result.issues)} of {observed} observed issues; source values are omitted.",
            "",
        ])
        lines.extend(
            f"- Line {issue['line']} · `{_escape(issue['rule'])}`: {_escape(issue['message'])}"
            for issue in result.issues
        )
    metrics = json.dumps(result.metrics, indent=2)
    fence = "`" * max(3, max((len(match) for match in re.findall(r"`+", metrics)), default=0) + 1)
    lines.extend(["", "## Metrics", "", f"{fence}json", metrics, fence, ""])
    return "\n".join(lines)


def write_json_report(result: ValidationResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_markdown_report(result: ValidationResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(result), encoding="utf-8")
