from pathlib import Path

import pytest

from os_system_agent.catalog import CatalogError, EtlJob, load_catalog

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CATALOG = REPO_ROOT / "config" / "alert-rules.example.yml"


def test_example_catalog_loads_with_expected_fields():
    jobs = load_catalog(EXAMPLE_CATALOG)
    assert len(jobs) == 1
    job = jobs[0]
    assert isinstance(job, EtlJob)
    assert job.id == "daily_sales"
    assert job.name == "Daily Sales Load"
    assert job.empresa == "NombreEmpresa"
    assert job.server == "server232"
    assert job.systemd_unit == "daily_sales.service"
    assert job.expected_finish_before == "08:00"
    assert job.freshness.warning_after_minutes == 1500
    assert job.freshness.critical_after_minutes == 1560
    assert job.log_path == "/var/log/daily_sales"
    assert job.output_path is None
    assert job.alert_telegram is True


def test_missing_file_fails_closed(tmp_path):
    with pytest.raises(CatalogError):
        load_catalog(tmp_path / "does_not_exist.yml")


def test_missing_empresa_fails_closed(tmp_path):
    # A well-formed jobs list but no top-level `empresa` must fail closed:
    # an unlabeled report could be attributed to the wrong company.
    bad = tmp_path / "noempresa.yml"
    bad.write_text(
        "jobs:\n"
        "  - id: j1\n"
        "    server: server232\n"
        "    freshness:\n"
        "      max_delay_minutes_warning: 60\n"
        "      max_delay_minutes_critical: 120\n",
        encoding="utf-8",
    )
    with pytest.raises(CatalogError):
        load_catalog(bad)


def test_empresa_is_applied_to_every_job(tmp_path):
    ok = tmp_path / "ok.yml"
    ok.write_text(
        "empresa: Mercamio\n"
        "jobs:\n"
        "  - id: j1\n"
        "    server: server232\n"
        "    freshness:\n"
        "      max_delay_minutes_warning: 60\n"
        "      max_delay_minutes_critical: 120\n"
        "  - id: j2\n"
        "    server: server232\n"
        "    freshness:\n"
        "      max_delay_minutes_warning: 60\n"
        "      max_delay_minutes_critical: 120\n",
        encoding="utf-8",
    )
    jobs = load_catalog(ok)
    assert {j.empresa for j in jobs} == {"Mercamio"}


def test_warning_greater_than_critical_fails_closed(tmp_path):
    bad = tmp_path / "bad.yml"
    bad.write_text(
        "empresa: TestCo\n"
        "jobs:\n"
        "  - id: j1\n"
        "    server: server232\n"
        "    freshness:\n"
        "      max_delay_minutes_warning: 200\n"
        "      max_delay_minutes_critical: 100\n",
        encoding="utf-8",
    )
    with pytest.raises(CatalogError):
        load_catalog(bad)


def test_no_jobs_list_fails_closed(tmp_path):
    bad = tmp_path / "nojobs.yml"
    bad.write_text("something: else\n", encoding="utf-8")
    with pytest.raises(CatalogError):
        load_catalog(bad)


def test_empty_jobs_list_fails_closed(tmp_path):
    bad = tmp_path / "empty.yml"
    bad.write_text("jobs: []\n", encoding="utf-8")
    with pytest.raises(CatalogError):
        load_catalog(bad)


def test_missing_id_fails_closed(tmp_path):
    bad = tmp_path / "noid.yml"
    bad.write_text(
        "empresa: TestCo\n"
        "jobs:\n"
        "  - server: server232\n"
        "    freshness:\n"
        "      max_delay_minutes_warning: 60\n"
        "      max_delay_minutes_critical: 120\n",
        encoding="utf-8",
    )
    with pytest.raises(CatalogError):
        load_catalog(bad)


def test_missing_server_fails_closed(tmp_path):
    bad = tmp_path / "noserver.yml"
    bad.write_text(
        "empresa: TestCo\n"
        "jobs:\n"
        "  - id: j1\n"
        "    freshness:\n"
        "      max_delay_minutes_warning: 60\n"
        "      max_delay_minutes_critical: 120\n",
        encoding="utf-8",
    )
    with pytest.raises(CatalogError):
        load_catalog(bad)


def test_non_numeric_threshold_fails_closed(tmp_path):
    bad = tmp_path / "nonnum.yml"
    bad.write_text(
        "empresa: TestCo\n"
        "jobs:\n"
        "  - id: j1\n"
        "    server: server232\n"
        "    freshness:\n"
        "      max_delay_minutes_warning: soon\n"
        "      max_delay_minutes_critical: 120\n",
        encoding="utf-8",
    )
    with pytest.raises(CatalogError):
        load_catalog(bad)


def test_duplicate_ids_fail_closed(tmp_path):
    bad = tmp_path / "dupe.yml"
    bad.write_text(
        "empresa: TestCo\n"
        "jobs:\n"
        "  - id: j1\n"
        "    server: server232\n"
        "    freshness:\n"
        "      max_delay_minutes_warning: 60\n"
        "      max_delay_minutes_critical: 120\n"
        "  - id: j1\n"
        "    server: server232\n"
        "    freshness:\n"
        "      max_delay_minutes_warning: 60\n"
        "      max_delay_minutes_critical: 120\n",
        encoding="utf-8",
    )
    with pytest.raises(CatalogError):
        load_catalog(bad)


def test_invalid_yaml_fails_closed(tmp_path):
    bad = tmp_path / "invalid.yml"
    bad.write_text("jobs: [unclosed\n", encoding="utf-8")
    with pytest.raises(CatalogError):
        load_catalog(bad)
