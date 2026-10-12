from __future__ import annotations

import json
from pathlib import Path

import pytest

from dropguard.contract import load_contract
from dropguard.engine import DataError, load_profile, profile_csv, validate_csv

CONTRACT = '''
[dataset]
name = "events"
primary_key = "event_id"
min_rows = 1
max_rows = 100

[columns.event_id]
type = "integer"
nullable = false
min = 1

[columns.created_at]
type = "datetime"
nullable = false

[columns.status]
type = "string"
nullable = false
allowed = ["open", "closed"]

[columns.amount]
type = "number"
nullable = true
min = 0
max = 100
max_null_rate = 0.25

[drift]
max_row_count_change = 0.25
max_null_rate_increase = 0.10
'''

GOOD = (
    "event_id,created_at,status,amount\n"
    "101,2025-01-01T10:00:00,open,10.5\n"
    "102,2025-01-02T10:00:00,closed,22\n"
    "103,2025-01-03T10:00:00,open,30\n"
    "104,2025-01-04T10:00:00,closed,40\n"
)


def setup_files(tmp_path: Path, csv_text: str = GOOD) -> tuple[Path, Path]:
    contract_path = tmp_path / "contract.toml"
    contract_path.write_text(CONTRACT, encoding="utf-8")
    csv_path = tmp_path / "batch.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    return contract_path, csv_path


def test_contract_valid_batch_passes_and_snapshot_contains_no_values(tmp_path: Path) -> None:
    contract_path, csv_path = setup_files(tmp_path)
    contract = load_contract(contract_path)
    profile = profile_csv(csv_path)

    result = validate_csv(csv_path, contract, profile)

    assert result.status == "PASS"
    assert result.row_count == 4
    assert all(check.status == "pass" for check in result.checks)
    assert profile["columns"] == ["event_id", "created_at", "status", "amount"]
    assert profile["null_rates"]["amount"] == 0
    assert "101" not in json.dumps(profile)
    assert "open" not in json.dumps(profile)


def test_reports_type_null_allowed_value_key_and_range_failures_without_data_values(tmp_path: Path) -> None:
    bad = (
        "event_id,created_at,status,amount\n"
        "101,not-a-date,secret-status,-1\n"
        "101,2025-01-02T10:00:00,open,\n"
    )
    contract_path, csv_path = setup_files(tmp_path, bad)
    result = validate_csv(csv_path, load_contract(contract_path))
    checks = {check.name: check.status for check in result.checks}
    rendered = json.dumps({"checks": [check.__dict__ for check in result.checks], "issues": result.issues})

    assert result.status == "FAIL"
    assert checks["created_at type"] == "fail"
    assert checks["status allowed values"] == "fail"
    assert checks["amount range"] == "fail"
    assert checks["amount nulls"] == "fail"
    assert checks["Primary key uniqueness"] == "fail"
    assert "secret-status" not in rendered
    assert "not-a-date" not in rendered
    assert result.metrics["issues_observed"] >= result.metrics["issues_shown"]


def test_detects_contract_and_baseline_schema_row_count_and_null_drift(tmp_path: Path) -> None:
    contract_path, baseline_source = setup_files(tmp_path)
    baseline = profile_csv(baseline_source)
    changed = tmp_path / "changed.csv"
    changed.write_text(
        "event_id,created_at,status,amount,new_field\n"
        "201,2025-02-01T10:00:00,open,,x\n",
        encoding="utf-8",
    )

    result = validate_csv(changed, load_contract(contract_path), baseline)
    statuses = {check.name: check.status for check in result.checks}

    assert statuses["Contract columns"] == "fail"
    assert statuses["Baseline schema drift"] == "fail"
    assert statuses["Row-count drift"] == "fail"
    assert statuses["amount null-rate drift"] == "fail"
    assert result.metrics["baseline"]["row_count_change"] == -0.75

def test_numeric_primary_keys_are_compared_by_value(tmp_path: Path) -> None:
    contract_path, csv_path = setup_files(
        tmp_path,
        "event_id,created_at,status,amount\n"
        "1,2025-01-01T10:00:00,open,10\n"
        "01,2025-01-02T10:00:00,closed,20\n",
    )

    result = validate_csv(csv_path, load_contract(contract_path))

    uniqueness = next(check for check in result.checks if check.name == "Primary key uniqueness")
    assert uniqueness.status == "fail"
    assert uniqueness.observed == "1 duplicates"


def test_allowed_extra_columns_are_warnings_but_do_not_fail(tmp_path: Path) -> None:
    contract_path, _ = setup_files(tmp_path)
    contract_text = CONTRACT.replace("max_rows = 100", "max_rows = 100\nallow_extra_columns = true")
    contract_path.write_text(contract_text, encoding="utf-8")
    csv_path = tmp_path / "extra.csv"
    csv_path.write_text(GOOD.replace("amount\n", "amount,source\n").replace("10.5\n", "10.5,api\n").replace("22\n", "22,api\n").replace("30\n", "30,api\n").replace("40\n", "40,api\n"), encoding="utf-8")

    result = validate_csv(csv_path, load_contract(contract_path))

    assert result.status == "PASS"
    assert next(check for check in result.checks if check.name == "Contract columns").status == "warn"


def test_rejects_duplicate_headers_and_malformed_baselines(tmp_path: Path) -> None:
    duplicated = tmp_path / "duplicate-header.csv"
    duplicated.write_text("event_id,event_id\n1,2\n", encoding="utf-8")
    with pytest.raises(DataError, match="duplicate columns"):
        profile_csv(duplicated)

    invalid = tmp_path / "baseline.json"
    invalid.write_text('{"format":"other"}', encoding="utf-8")
    with pytest.raises(DataError, match="not a DropGuard profile"):
        load_profile(invalid)


def test_profile_rejects_malformed_rows_and_empty_batches(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.csv"
    malformed.write_text("id,name\n1,Ada,extra\n", encoding="utf-8")
    with pytest.raises(DataError, match="Malformed CSV row"):
        profile_csv(malformed)

    empty = tmp_path / "empty.csv"
    empty.write_text("id,name\n", encoding="utf-8")
    with pytest.raises(DataError, match="no data rows"):
        profile_csv(empty)
