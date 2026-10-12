from __future__ import annotations

import json
from pathlib import Path

from dropguard.cli import main

CONTRACT = '''
[dataset]
name = "events"
primary_key = "event_id"
min_rows = 1

[columns.event_id]
type = "integer"
nullable = false

[columns.status]
type = "string"
nullable = false
allowed = ["open", "closed"]
'''


def test_snapshot_then_validate_writes_reports_and_returns_zero(tmp_path: Path) -> None:
    source = tmp_path / "good.csv"
    source.write_text("event_id,status\n1,open\n2,closed\n", encoding="utf-8")
    contract = tmp_path / "contract.toml"
    contract.write_text(CONTRACT, encoding="utf-8")
    baseline = tmp_path / "baseline.json"
    json_report = tmp_path / "reports" / "result.json"
    markdown_report = tmp_path / "reports" / "result.md"

    assert main(["snapshot", "--input", str(source), "--output", str(baseline)]) == 0
    result = main([
        "validate", "--contract", str(contract), "--input", str(source), "--baseline", str(baseline),
        "--json-report", str(json_report), "--markdown-report", str(markdown_report),
    ])

    assert result == 0
    report = json.loads(json_report.read_text(encoding="utf-8"))
    assert report["status"] == "PASS"
    assert "Primary key uniqueness" in {check["name"] for check in report["checks"]}
    assert "# DropGuard report" in markdown_report.read_text(encoding="utf-8")


def test_validation_failure_returns_one_and_does_not_include_cell_values(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "bad.csv"
    source.write_text("event_id,status\n1,secret-value\n1,open\n", encoding="utf-8")
    contract = tmp_path / "contract.toml"
    contract.write_text(CONTRACT, encoding="utf-8")

    exit_code = main(["validate", "--contract", str(contract), "--input", str(source)])

    assert exit_code == 1
    assert "secret-value" not in capsys.readouterr().out


def test_report_output_cannot_overwrite_input(tmp_path: Path) -> None:
    source = tmp_path / "good.csv"
    source.write_text("event_id,status\n1,open\n", encoding="utf-8")
    contract = tmp_path / "contract.toml"
    contract.write_text(CONTRACT, encoding="utf-8")

    assert main([
        "validate", "--contract", str(contract), "--input", str(source), "--json-report", str(source)
    ]) == 2
    assert source.read_text(encoding="utf-8") == "event_id,status\n1,open\n"
