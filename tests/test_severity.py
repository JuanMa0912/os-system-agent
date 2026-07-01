from os_system_agent.severity import Severity, classify_freshness


def test_within_window_is_info():
    assert classify_freshness(10, 60, 120) is Severity.INFO


def test_between_thresholds_is_warning():
    assert classify_freshness(90, 60, 120) is Severity.WARNING


def test_past_critical_is_critical():
    assert classify_freshness(200, 60, 120) is Severity.CRITICAL


def test_unknown_delay_fails_closed():
    assert classify_freshness(None, 60, 120) is Severity.CRITICAL
