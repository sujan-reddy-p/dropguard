"""Command-line interface for profiling and guarding incoming CSV drops."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from . import __version__
from .contract import ContractError, load_contract
from .engine import DataError, load_profile, profile_csv, validate_csv
from .report import render_console, write_json_report, write_markdown_report

console = Console()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dropguard",
        description="Catch contract violations and batch drift before CSV data enters a pipeline.",
    )
    parser.add_argument("--version", action="version", version=f"DropGuard {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    snapshot = commands.add_parser("snapshot", help="Profile a reviewed-good CSV as a value-free baseline")
    snapshot.add_argument("--input", required=True, type=Path, help="Known-good CSV batch")
    snapshot.add_argument("--output", required=True, type=Path, help="Path for the JSON baseline profile")

    validate = commands.add_parser("validate", help="Check a CSV against its contract and optional baseline")
    validate.add_argument("--contract", required=True, type=Path, help="TOML data contract")
    validate.add_argument("--input", required=True, type=Path, help="Incoming CSV batch")
    validate.add_argument("--baseline", type=Path, help="Reviewed-good profile from `dropguard snapshot`")
    validate.add_argument("--json-report", type=Path, help="Optional machine-readable report destination")
    validate.add_argument("--markdown-report", type=Path, help="Optional Markdown report destination")
    return parser


def _path(path: Path) -> Path:
    return path.expanduser()


def _ensure_distinct(input_path: Path, destinations: list[Path], protected: list[Path]) -> None:
    source = _path(input_path).resolve()
    protected_paths = {source, *(_path(path).resolve() for path in protected)}
    output_paths = [_path(path).resolve() for path in destinations]
    if len(set(output_paths)) != len(output_paths):
        raise ValueError("Report destinations must be different files")
    overwritten = [str(path) for path in output_paths if path in protected_paths]
    if overwritten:
        raise ValueError(f"Refusing to overwrite an input or configuration file: {', '.join(overwritten)}")


def _snapshot(args: argparse.Namespace) -> int:
    source = _path(args.input)
    destination = _path(args.output)
    _ensure_distinct(source, [destination], [])
    profile = profile_csv(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    console.print(Panel(
        f"[bold green]BASELINE SAVED[/]\n{source.name} · {profile['row_count']:,} rows · "
        f"{len(profile['columns'])} columns\n[dim]No source values were copied into the profile.[/]\n{destination}",
        title="DropGuard snapshot",
        border_style="green",
    ))
    return 0


def _validate(args: argparse.Namespace) -> int:
    source = _path(args.input)
    contract_path = _path(args.contract)
    baseline_path = _path(args.baseline) if args.baseline else None
    destinations = [_path(path) for path in (args.json_report, args.markdown_report) if path]
    protected = [contract_path, *([baseline_path] if baseline_path else [])]
    _ensure_distinct(source, destinations, protected)
    contract = load_contract(contract_path)
    baseline = load_profile(baseline_path) if baseline_path else None
    result = validate_csv(source, contract, baseline)
    render_console(result, console)
    if args.json_report:
        write_json_report(result, _path(args.json_report))
        console.print(f"JSON report: {_path(args.json_report)}")
    if args.markdown_report:
        write_markdown_report(result, _path(args.markdown_report))
        console.print(f"Markdown report: {_path(args.markdown_report)}")
    return 1 if result.status == "FAIL" else 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            return _snapshot(args)
        return _validate(args)
    except (ContractError, DataError, OSError, ValueError) as exc:
        console.print(f"[bold red]DropGuard error:[/] {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
