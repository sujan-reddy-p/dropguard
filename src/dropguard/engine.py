"""Streaming CSV profiling, contract checks, and baseline drift detection."""

from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .contract import ColumnRule, Contract

PROFILE_FORMAT = "dropguard-profile-v1"
MAX_ISSUES = 8


class DataError(ValueError):
    """Raised when an input or baseline cannot be read as a supported file."""


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    observed: str
    expected: str
    detail: str = ""


@dataclass(frozen=True)
class ValidationResult:
    dataset: str
    source: str
    status: str
    row_count: int
    column_count: int
    checks: list[Check]
    metrics: dict[str, Any]
    issues: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "source": self.source,
            "status": self.status,
            "row_count": self.row_count,
            "column_count": self.column_count,
            "checks": [asdict(check) for check in self.checks],
            "metrics": self.metrics,
            "issues": self.issues,
        }


def _open_csv(path: Path):
    try:
        return path.open("r", newline="", encoding="utf-8-sig")
    except OSError as exc:
        raise DataError(f"Cannot open CSV {path}: {exc}") from exc


def _read_header(reader: csv.reader, path: Path) -> list[str]:
    try:
        header = next(reader)
    except StopIteration as exc:
        raise DataError(f"CSV is empty: {path}") from exc
    except (csv.Error, UnicodeDecodeError) as exc:
        raise DataError(f"Cannot read CSV header in {path}: {exc}") from exc
    normalized = [name.strip() for name in header]
    if not normalized or any(not name for name in normalized):
        raise DataError("CSV header contains an empty column name")
    duplicates = sorted({name for name in normalized if normalized.count(name) > 1})
    if duplicates:
        raise DataError(f"CSV header contains duplicate columns: {', '.join(duplicates)}")
    return normalized


def profile_csv(path: Path) -> dict[str, Any]:
    """Create a value-free snapshot suitable for comparison with later batches."""
    null_counts: dict[str, int] = {}
    row_count = 0
    with _open_csv(path) as source:
        reader = csv.reader(source, strict=True)
        columns = _read_header(reader, path)
        null_counts = dict.fromkeys(columns, 0)
        try:
            for row in reader:
                line_number = reader.line_num
                if not row:
                    continue
                if len(row) != len(columns):
                    raise DataError(f"Malformed CSV row at line {line_number}: expected {len(columns)} fields, got {len(row)}")
                row_count += 1
                for column, value in zip(columns, row, strict=True):
                    if not value.strip():
                        null_counts[column] += 1
        except (csv.Error, UnicodeDecodeError) as exc:
            raise DataError(f"Cannot read CSV data in {path}: {exc}") from exc
    if row_count == 0:
        raise DataError("Cannot create a baseline from a CSV with no data rows")
    return {
        "format": PROFILE_FORMAT,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source_name": path.name,
        "row_count": row_count,
        "columns": columns,
        "null_rates": {name: null_counts[name] / row_count for name in columns},
    }


def load_profile(path: Path) -> dict[str, Any]:
    """Load a previously reviewed snapshot, rejecting malformed or foreign files."""
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataError(f"Cannot read baseline {path}: {exc}") from exc
    if not isinstance(profile, dict) or profile.get("format") != PROFILE_FORMAT:
        raise DataError(f"Baseline {path} is not a DropGuard profile")
    columns = profile.get("columns")
    null_rates = profile.get("null_rates")
    row_count = profile.get("row_count")
    if (
        not isinstance(columns, list)
        or not columns
        or any(not isinstance(column, str) for column in columns)
        or len(set(columns)) != len(columns)
        or isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or row_count <= 0
        or not isinstance(null_rates, dict)
    ):
        raise DataError(f"Baseline {path} has invalid profile fields")
    if set(null_rates) != set(columns) or any(
        isinstance(rate, bool)
        or not isinstance(rate, (int, float))
        or not math.isfinite(rate)
        or not 0 <= rate <= 1
        for rate in null_rates.values()
    ):
        raise DataError(f"Baseline {path} has invalid null-rate metrics")
    return profile


def _parse_value(value: str, rule: ColumnRule) -> int | float | date | datetime | bool | str:
    if rule.type == "string":
        return value
    if rule.type == "integer":
        return int(value)
    if rule.type == "number":
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("not a finite number")
        return parsed
    if rule.type == "boolean":
        normalized = value.casefold()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0"}:
            return False
        raise ValueError("expected true, false, 1, or 0")
    if rule.type == "date":
        return date.fromisoformat(value)
    if rule.type == "datetime":
        return datetime.fromisoformat(value)
    raise ValueError(f"unsupported type {rule.type}")


def _add_check(
    checks: list[Check], name: str, status: str, observed: object, expected: str, detail: str = ""
) -> None:
    checks.append(Check(name, status, str(observed), expected, detail))


def validate_csv(path: Path, contract: Contract, baseline: dict[str, Any] | None = None) -> ValidationResult:
    """Validate one CSV batch without retaining source row values in the report."""
    checks: list[Check] = []
    issues: list[dict[str, Any]] = []
    issue_count = 0
    row_count = 0
    row_shape_errors = 0
    parse_errors = 0
    type_errors = dict.fromkeys(contract.columns, 0)
    null_counts = dict.fromkeys(contract.columns, 0)
    observed_null_counts: dict[str, int] = {}
    allowed_errors = dict.fromkeys(contract.columns, 0)
    range_errors = dict.fromkeys(contract.columns, 0)
    primary_values: set[object] = set()
    duplicate_keys = 0
    first_line: dict[str, int] = {}
    header: list[str]
    source_columns: set[str]

    def add_issue(line: int, rule: str, message: str) -> None:
        nonlocal issue_count
        issue_count += 1
        if len(issues) < MAX_ISSUES:
            issues.append({"line": line, "rule": rule, "message": message})

    with _open_csv(path) as source:
        reader = csv.reader(source, strict=True)
        header = _read_header(reader, path)
        source_columns = set(header)
        missing = sorted(set(contract.columns) - source_columns)
        extra = sorted(source_columns - set(contract.columns))
        if missing:
            _add_check(checks, "Contract columns", "fail", f"missing: {', '.join(missing)}", "all declared columns present")
        elif extra and not contract.allow_extra_columns:
            _add_check(checks, "Contract columns", "fail", f"unexpected: {', '.join(extra)}", "no undeclared columns")
        elif extra:
            _add_check(checks, "Contract columns", "warn", f"extra: {', '.join(extra)}", "declared columns required", "Extra fields are permitted by the contract.")
        else:
            _add_check(checks, "Contract columns", "pass", len(header), f"{len(contract.columns)} declared columns")

        indexes = {name: index for index, name in enumerate(header)}
        observed_null_counts = dict.fromkeys(header, 0)
        try:
            for row in reader:
                line_number = reader.line_num
                if not row:
                    continue
                row_count += 1
                if len(row) != len(header):
                    row_shape_errors += 1
                    add_issue(line_number, "row_shape", f"Expected {len(header)} fields, found {len(row)}.")
                for name, index in indexes.items():
                    if index >= len(row) or not row[index].strip():
                        observed_null_counts[name] += 1
                for name, rule in contract.columns.items():
                    index = indexes.get(name)
                    if index is None:
                        continue
                    value = row[index].strip() if index < len(row) else ""
                    if not value:
                        null_counts[name] += 1
                        if not rule.nullable:
                            first_line.setdefault(f"null:{name}", line_number)
                            add_issue(line_number, "not_null", f"Missing required value in {name}.")
                        continue
                    try:
                        parsed = _parse_value(value, rule)
                    except (ValueError, OverflowError):
                        type_errors[name] += 1
                        first_line.setdefault(f"type:{name}", line_number)
                        add_issue(line_number, "type", f"Value in {name} does not match its declared type.")
                        continue
                    if rule.allowed and value not in rule.allowed:
                        allowed_errors[name] += 1
                        first_line.setdefault(f"allowed:{name}", line_number)
                        add_issue(line_number, "allowed_values", f"Value in {name} is outside the allowed set.")
                    if (
                        isinstance(parsed, (int, float))
                        and not isinstance(parsed, bool)
                        and (
                            (rule.minimum is not None and parsed < rule.minimum)
                            or (rule.maximum is not None and parsed > rule.maximum)
                        )
                    ):
                        range_errors[name] += 1
                        first_line.setdefault(f"range:{name}", line_number)
                        add_issue(line_number, "range", f"Value in {name} is outside its declared range.")
                key_index = indexes.get(contract.primary_key)
                if key_index is not None:
                    key_value = row[key_index].strip() if key_index < len(row) else ""
                    if key_value:
                        key_rule = contract.columns[contract.primary_key]
                        try:
                            normalized_key: object = _parse_value(key_value, key_rule)
                        except (ValueError, OverflowError):
                            normalized_key = key_value
                        if normalized_key in primary_values:
                            duplicate_keys += 1
                            first_line.setdefault("duplicate_key", line_number)
                            add_issue(line_number, "unique_key", f"Duplicate primary key in {contract.primary_key}.")
                        else:
                            primary_values.add(normalized_key)
        except (csv.Error, UnicodeDecodeError) as exc:
            parse_errors += 1
            add_issue(reader.line_num, "csv_parse", f"CSV parsing stopped at line {reader.line_num}: {exc}")

    if row_shape_errors:
        _add_check(checks, "Row shape", "fail", row_shape_errors, "every row matches the header width", f"First issue at line {next((i['line'] for i in issues if i['rule'] == 'row_shape'), '?')}.")
    else:
        _add_check(checks, "Row shape", "pass", 0, "every row matches the header width")
    if parse_errors:
        _add_check(checks, "CSV syntax", "fail", parse_errors, "valid CSV", "Parsing stopped at the first malformed row.")
    else:
        _add_check(checks, "CSV syntax", "pass", "valid", "valid CSV")

    row_count_ok = row_count >= contract.min_rows and (contract.max_rows is None or row_count <= contract.max_rows)
    row_expected = f">= {contract.min_rows}" + (f" and <= {contract.max_rows}" if contract.max_rows is not None else "")
    _add_check(checks, "Batch row count", "pass" if row_count_ok else "fail", row_count, row_expected)

    for name, rule in contract.columns.items():
        if name not in indexes:
            continue
        type_count = type_errors[name]
        _add_check(
            checks,
            f"{name} type",
            "fail" if type_count else "pass",
            f"{type_count} invalid",
            rule.type,
            f"First invalid value at line {first_line.get(f'type:{name}', '?')}." if type_count else "",
        )
        null_count = null_counts[name]
        null_rate = null_count / row_count if row_count else 0.0
        if not rule.nullable and null_count:
            null_status = "fail"
            null_expected = "no null or empty values"
        elif rule.max_null_rate is not None and null_rate > rule.max_null_rate:
            null_status = "fail"
            null_expected = f"null rate <= {rule.max_null_rate:.1%}"
        else:
            null_status = "pass"
            null_expected = "nullable" if rule.nullable else "no null or empty values"
        _add_check(
            checks,
            f"{name} nulls",
            null_status,
            f"{null_count} ({null_rate:.1%})",
            null_expected,
            f"First null at line {first_line.get(f'null:{name}', '?')}." if null_count and not rule.nullable else "",
        )
        if rule.allowed:
            count = allowed_errors[name]
            _add_check(checks, f"{name} allowed values", "fail" if count else "pass", f"{count} invalid", f"one of {', '.join(rule.allowed)}")
        if rule.minimum is not None or rule.maximum is not None:
            count = range_errors[name]
            expected_range = f"{rule.minimum if rule.minimum is not None else '-∞'} to {rule.maximum if rule.maximum is not None else '∞'}"
            _add_check(checks, f"{name} range", "fail" if count else "pass", f"{count} out of range", expected_range)

    if contract.primary_key in indexes:
        _add_check(
            checks,
            "Primary key uniqueness",
            "fail" if duplicate_keys else "pass",
            f"{duplicate_keys} duplicates",
            f"unique {contract.primary_key}",
            f"First duplicate at line {first_line.get('duplicate_key', '?')}." if duplicate_keys else "",
        )

    baseline_metrics: dict[str, Any] | None = None
    if baseline is not None:
        baseline_columns = set(baseline["columns"])
        removed = sorted(baseline_columns - source_columns)
        added = sorted(source_columns - baseline_columns)
        schema_changed = bool(removed or added)
        schema_status = "warn" if schema_changed and contract.allow_extra_columns else "fail" if schema_changed else "pass"
        schema_detail = "; ".join(part for part in (
            f"removed: {', '.join(removed)}" if removed else "",
            f"added: {', '.join(added)}" if added else "",
        ) if part)
        _add_check(checks, "Baseline schema drift", schema_status, schema_detail or "unchanged", "same columns", schema_detail)

        baseline_rows = baseline["row_count"]
        row_change = abs(row_count - baseline_rows) / baseline_rows
        row_change_failed = row_change > contract.max_row_count_change
        _add_check(
            checks,
            "Row-count drift",
            "fail" if row_change_failed else "pass",
            f"{row_count} ({row_change:+.1%} change from {baseline_rows})" if row_count >= baseline_rows else f"{row_count} (-{row_change:.1%} from {baseline_rows})",
            f"absolute change <= {contract.max_row_count_change:.1%}",
        )

        current_null_rates = {
            name: observed_null_counts[name] / row_count if row_count else 0.0 for name in header
        }
        null_drift = {
            name: current_null_rates[name] - baseline["null_rates"][name]
            for name in set(current_null_rates) & set(baseline["null_rates"])
            if current_null_rates[name] - baseline["null_rates"][name] > contract.max_null_rate_increase
        }
        for name, increase in sorted(null_drift.items()):
            checks.append(Check(
                f"{name} null-rate drift", "fail", f"+{increase:.1%}",
                f"increase <= {contract.max_null_rate_increase:.1%}",
            ))
        if not null_drift:
            _add_check(checks, "Null-rate drift", "pass", "within thresholds", f"increase <= {contract.max_null_rate_increase:.1%}")
        baseline_metrics = {
            "row_count": baseline_rows,
            "row_count_change": round((row_count - baseline_rows) / baseline_rows, 6),
            "null_rate_increases": {name: round(value, 6) for name, value in sorted(null_drift.items())},
        }

    status = "FAIL" if any(check.status == "fail" for check in checks) else "PASS"
    null_rates = {name: observed_null_counts[name] / row_count if row_count else 0.0 for name in header}
    column_metrics = {
        name: {
            "null_count": null_counts[name],
            "null_rate": null_rates.get(name, 0.0),
            "type_errors": type_errors[name],
            "allowed_value_errors": allowed_errors[name],
            "range_errors": range_errors[name],
        }
        for name in contract.columns
    }
    metrics: dict[str, Any] = {
        "null_rates": null_rates,
        "columns": column_metrics,
        "primary_key_duplicates": duplicate_keys,
        "row_shape_errors": row_shape_errors,
        "issues_observed": issue_count,
        "issues_shown": len(issues),
        "issues_truncated": issue_count > len(issues),
        "baseline": baseline_metrics,
    }
    return ValidationResult(
        dataset=contract.name,
        source=path.name,
        status=status,
        row_count=row_count,
        column_count=len(header),
        checks=checks,
        metrics=metrics,
        issues=issues,
    )
