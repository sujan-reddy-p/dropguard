from __future__ import annotations

from pathlib import Path

import pytest

from dropguard.contract import ContractError, load_contract

VALID = '''
[dataset]
name = "events"
primary_key = "event_id"
min_rows = 1

[columns.event_id]
type = "integer"
nullable = false

[columns.created_at]
type = "datetime"
nullable = false

[columns.status]
type = "string"
nullable = false
allowed = ["open", "closed"]

[drift]
max_row_count_change = 0.25
max_null_rate_increase = 0.10
'''


def write_contract(tmp_path: Path, content: str = VALID) -> Path:
    path = tmp_path / "contract.toml"
    path.write_text(content, encoding="utf-8")
    return path


def test_loads_column_rules_and_drift_limits(tmp_path: Path) -> None:
    contract = load_contract(write_contract(tmp_path))

    assert contract.name == "events"
    assert contract.primary_key == "event_id"
    assert contract.columns["event_id"].type == "integer"
    assert contract.columns["status"].allowed == ("open", "closed")
    assert contract.max_row_count_change == 0.25
    assert contract.max_null_rate_increase == 0.1


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ('type = ["integer"]', "type must be one of"),
        ('nullable = true', "primary-key column must set nullable = false"),
        ('max_null_rate = 2', "must be at most 1"),
        ('typo = true', "Unknown columns.event_id key"),
    ],
)
def test_rejects_invalid_contracts(tmp_path: Path, replacement: str, message: str) -> None:
    content = VALID.replace("type = \"integer\"\nnullable = false", replacement + "\nnullable = false")
    if replacement == 'nullable = true':
        content = VALID.replace('type = "integer"\nnullable = false', 'type = "integer"\nnullable = true')
    if replacement == 'max_null_rate = 2':
        content = VALID.replace('type = "integer"\nnullable = false', 'type = "integer"\nnullable = false\nmax_null_rate = 2')
    with pytest.raises(ContractError, match=message):
        load_contract(write_contract(tmp_path, content))


def test_rejects_contract_with_unknown_top_level_key(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="Unknown top-level key"):
        load_contract(write_contract(tmp_path, VALID + "\n[typo]\nvalue = true\n"))
