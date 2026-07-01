"""Run the versioned golden cases in evals/ so they execute in CI."""

from pathlib import Path

import pytest
import yaml

from os_system_agent.redaction import redact
from os_system_agent.severity import Severity, classify_freshness

CASES_DIR = Path(__file__).resolve().parent.parent / "evals" / "cases"


def _load(name: str):
    return yaml.safe_load((CASES_DIR / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", _load("severity_cases.yaml"), ids=lambda c: c["name"])
def test_severity_golden_cases(case):
    result = classify_freshness(
        case["delay_minutes"],
        case["warning_after_minutes"],
        case["critical_after_minutes"],
    )
    assert result is Severity(case["expected"])


@pytest.mark.parametrize("case", _load("redaction_cases.yaml"), ids=lambda c: c["name"])
def test_redaction_golden_cases(case):
    assert case["must_not_contain"] not in redact(case["input"])
