"""Parsing and validation for user-authored TOML data contracts."""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SUPPORTED_TYPES = {"string", "integer", "number", "boolean", "date", "datetime"}


class ContractError(ValueError):
    """Raised when a contract is invalid or cannot be read."""


@dataclass(frozen=True)
class ColumnRule:
    name: str
    type: str
    nullable: bool = False
    allowed: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    max_null_rate: float | None = None


@dataclass(frozen=True)
class Contract:
    name: str
    primary_key: str
    columns: dict[str, ColumnRule]
    min_rows: int
    max_rows: int | None
    allow_extra_columns: bool
    max_row_count_change: float | None
    max_null_rate_increase: float | None


def _unknown_keys(values: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = set(values) - allowed
    if unknown:
        raise ContractError(f"Unknown {context} key(s): {', '.join(sorted(unknown))}")


def _number(value: Any, key: str, *, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ContractError(f"{key} must be a finite number")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ContractError(f"{key} must be at least {minimum}")
    if maximum is not None and number > maximum:
        raise ContractError(f"{key} must be at most {maximum}")
    return number


def _integer(value: Any, key: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractError(f"{key} must be an integer greater than or equal to {minimum}")
    return value


def load_contract(path: Path) -> Contract:
    """Load TOML and reject unsupported or inconsistent configuration early."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ContractError(f"Cannot read contract {path}: {exc}") from exc

    _unknown_keys(data, {"dataset", "columns", "drift"}, "top-level")
    dataset = data.get("dataset")
    columns = data.get("columns")
    drift = data.get("drift", {})
    if not isinstance(dataset, dict) or not isinstance(columns, dict) or not columns:
        raise ContractError("Contract must define [dataset] and at least one [columns.<name>] table")
    if not isinstance(drift, dict):
        raise ContractError("[drift] must be a table")
    _unknown_keys(dataset, {"name", "primary_key", "min_rows", "max_rows", "allow_extra_columns"}, "dataset")
    _unknown_keys(drift, {"max_row_count_change", "max_null_rate_increase"}, "drift")

    name = dataset.get("name")
    primary_key = dataset.get("primary_key")
    if not isinstance(name, str) or not name.strip():
        raise ContractError("dataset.name must be a non-empty string")
    if not isinstance(primary_key, str) or not primary_key.strip():
        raise ContractError("dataset.primary_key must be a non-empty string")
    min_rows = _integer(dataset.get("min_rows", 1), "dataset.min_rows")
    max_rows_value = dataset.get("max_rows")
    max_rows = None if max_rows_value is None else _integer(max_rows_value, "dataset.max_rows")
    if max_rows is not None and max_rows < min_rows:
        raise ContractError("dataset.max_rows must be greater than or equal to min_rows")
    allow_extra = dataset.get("allow_extra_columns", False)
    if not isinstance(allow_extra, bool):
        raise ContractError("dataset.allow_extra_columns must be true or false")

    rules: dict[str, ColumnRule] = {}
    for column_name, raw_rule in columns.items():
        if not isinstance(column_name, str) or not column_name.strip() or not isinstance(raw_rule, dict):
            raise ContractError("Each [columns.<name>] entry must be a named TOML table")
        _unknown_keys(raw_rule, {"type", "nullable", "allowed", "min", "max", "max_null_rate"}, f"columns.{column_name}")
        kind = raw_rule.get("type")
        if not isinstance(kind, str) or kind not in SUPPORTED_TYPES:
            raise ContractError(f"columns.{column_name}.type must be one of {', '.join(sorted(SUPPORTED_TYPES))}")
        nullable = raw_rule.get("nullable", False)
        if not isinstance(nullable, bool):
            raise ContractError(f"columns.{column_name}.nullable must be true or false")
        allowed_value = raw_rule.get("allowed", [])
        if not isinstance(allowed_value, list) or any(not isinstance(item, str) for item in allowed_value):
            raise ContractError(f"columns.{column_name}.allowed must be an array of strings")
        minimum_value = raw_rule.get("min")
        maximum_value = raw_rule.get("max")
        minimum = None if minimum_value is None else _number(minimum_value, f"columns.{column_name}.min")
        maximum = None if maximum_value is None else _number(maximum_value, f"columns.{column_name}.max")
        if minimum is not None and maximum is not None and maximum < minimum:
            raise ContractError(f"columns.{column_name}.max must be greater than or equal to min")
        if (minimum is not None or maximum is not None) and kind not in {"integer", "number"}:
            raise ContractError(f"columns.{column_name}.min/max are only valid for integer and number types")
        null_rate_value = raw_rule.get("max_null_rate")
        max_null_rate = None if null_rate_value is None else _number(
            null_rate_value, f"columns.{column_name}.max_null_rate", minimum=0, maximum=1
        )
        rules[column_name] = ColumnRule(
            name=column_name,
            type=kind,
            nullable=nullable,
            allowed=tuple(allowed_value),
            minimum=minimum,
            maximum=maximum,
            max_null_rate=max_null_rate,
        )

    if primary_key not in rules:
        raise ContractError("dataset.primary_key must name a declared column")
    if rules[primary_key].nullable:
        raise ContractError("The primary-key column must set nullable = false")
    row_change_value = drift.get("max_row_count_change", 0.5)
    max_row_count_change = _number(row_change_value, "drift.max_row_count_change", minimum=0)
    null_change_value = drift.get("max_null_rate_increase", 0.1)
    max_null_rate_increase = _number(null_change_value, "drift.max_null_rate_increase", minimum=0, maximum=1)
    return Contract(
        name=name.strip(),
        primary_key=primary_key,
        columns=rules,
        min_rows=min_rows,
        max_rows=max_rows,
        allow_extra_columns=allow_extra,
        max_row_count_change=max_row_count_change,
        max_null_rate_increase=max_null_rate_increase,
    )
