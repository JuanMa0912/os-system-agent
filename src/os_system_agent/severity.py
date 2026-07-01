"""Alert severity model and pure classifiers (CLAUDE.md §12)."""

from __future__ import annotations

from enum import StrEnum


class Severity(StrEnum):
    """Consistent severity levels used across alerts and reports."""

    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    SECURITY = "SECURITY"


def classify_freshness(
    delay_minutes: float | None,
    warning_after_minutes: float,
    critical_after_minutes: float,
) -> Severity:
    """Classify ETL freshness by how late the data/signal is.

    Fails closed: an unknown delay (``None``) is treated as CRITICAL, because
    "we don't know" must never look healthier than "we know it's late".
    """
    if delay_minutes is None:
        return Severity.CRITICAL
    if delay_minutes >= critical_after_minutes:
        return Severity.CRITICAL
    if delay_minutes >= warning_after_minutes:
        return Severity.WARNING
    return Severity.INFO
