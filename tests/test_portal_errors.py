"""Tests for the portal error taxonomy (spec 006 T3, plan §2.2).

Two things are under test here, and the second matters more than the first:

* every answer maps to the right outcome, and the terminal/retryable split is
  exactly right — a wrong "retryable" is what burns a rate limit shared with
  the offices;
* **no exception message ever carries a secret.** A control without a test is
  theatre, so each message is searched for the literal cookie and password
  values the fixtures feed in.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import yaml

from os_system_agent.portal.errors import (
    MAX_BODY_BYTES,
    RATE_LIMIT_WINDOW_SECONDS,
    AccountDisabledError,
    Outcome,
    RateLimitedError,
    ResponseContractError,
    TerminalAuthError,
    TransientError,
    UnexpectedStatusError,
    classify,
    error_for,
    map_transport_error,
    retry_after_seconds,
)

CASES_DIR = Path(__file__).resolve().parent.parent / "evals" / "cases"

# Fake values used to prove nothing leaks. They are not credentials.
FAKE_COOKIE = "vp_session=a1b2c3d4e5f6a1b2c3d4e5f6"
FAKE_PASSWORD = "sup3r-s3cret-portal-pass"
FAKE_CSRF = "x-csrf-token-value-9f8e7d6c"


def _load(name: str) -> list[dict]:
    return yaml.safe_load((CASES_DIR / name).read_text(encoding="utf-8"))


GOLDEN_CASES = _load("portal_errors_cases.yaml")


def test_hay_al_menos_doce_casos_dorados() -> None:
    assert len(GOLDEN_CASES) >= 12


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=lambda c: c["name"])
def test_taxonomia_casos_dorados(case) -> None:
    outcome = classify(case["status"], case["headers"] or {}, case["body"])
    assert outcome is Outcome(case["outcome"])
    assert outcome.is_terminal is case["terminal"]

    error = error_for(
        case["status"],
        case["headers"] or {},
        case["body"],
        method="GET",
        path="/api/health",
    )
    if not case["raises"]:
        assert error is None
        return

    assert error is not None
    assert error.outcome is outcome
    assert error.is_terminal is case["terminal"]
    assert error.status == case["status"]

    if "retry_after_seconds" in case:
        assert isinstance(error, RateLimitedError)
        assert error.retry_after_seconds == case["retry_after_seconds"]
    if "effective_retry_after_seconds" in case:
        assert isinstance(error, RateLimitedError)
        assert error.effective_retry_after_seconds == case["effective_retry_after_seconds"]


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=lambda c: c["name"])
def test_ningun_mensaje_dorado_filtra_el_cuerpo_ni_las_cabeceras(case) -> None:
    # The fixtures deliberately carry a fake session cookie and a fake password;
    # neither may survive into the message the operator sees.
    error = error_for(
        case["status"],
        case["headers"] or {},
        case["body"],
        method="GET",
        path="/api/health",
    )
    if error is None:
        return
    rendered = f"{error} {error!r}"
    assert "vp_session" not in rendered
    assert "n0-deberia-salir-nunca" not in rendered


def test_401_y_403_son_terminales() -> None:
    unauthorized = error_for(401, {}, "", method="GET", path="/api/x")
    forbidden = error_for(403, {}, "", method="GET", path="/api/x")
    assert isinstance(unauthorized, TerminalAuthError)
    assert isinstance(forbidden, AccountDisabledError)
    assert unauthorized.is_terminal is True
    assert forbidden.is_terminal is True


def test_transitorio_no_es_terminal() -> None:
    error = error_for(503, {}, "", method="GET", path="/api/x")
    assert isinstance(error, TransientError)
    assert error.is_terminal is False


def test_429_es_terminal_en_esta_ventana() -> None:
    error = error_for(429, {"Retry-After": "42"}, "", method="GET", path="/api/x")
    assert isinstance(error, RateLimitedError)
    assert error.is_terminal is True
    assert error.retry_after_seconds == 42
    assert error.effective_retry_after_seconds == 42


def test_retry_after_negativo_se_ignora() -> None:
    assert retry_after_seconds({"retry-after": "-5"}) is None
    assert retry_after_seconds({}) is None
    assert retry_after_seconds({"RETRY-AFTER": "10"}) == 10  # case-insensitive


def test_429_sin_cabecera_espera_la_ventana_completa() -> None:
    error = error_for(429, {}, "", method="GET", path="/api/x")
    assert isinstance(error, RateLimitedError)
    assert error.retry_after_seconds is None
    assert error.effective_retry_after_seconds == RATE_LIMIT_WINDOW_SECONDS


def test_cuerpo_gigante_es_ruptura_de_contrato() -> None:
    # Never parse an oversized 200: a runaway or hostile body must not cost us
    # memory, and it is certainly not the agreed contract.
    huge = '{"updatedAt":"2026-08-24","pad":"' + "x" * (MAX_BODY_BYTES + 10) + '"}'
    assert classify(200, {}, huge) is Outcome.CONTRACT


def test_mensaje_no_filtra_cookie_password_ni_csrf() -> None:
    headers = {
        "Set-Cookie": f"{FAKE_COOKIE}; Path=/; HttpOnly",
        "Cookie": FAKE_COOKIE,
        "X-CSRF-Token": FAKE_CSRF,
        "Authorization": "Basic YWdlbnQ6c3VwZXItc2VjcmV0",
    }
    body = f'{{"password":"{FAKE_PASSWORD}","token":"{FAKE_CSRF}"}}'
    for status in (401, 403, 429, 503, 404, 200):
        error = error_for(status, headers, body, method="GET", path="/api/margenes/data")
        if error is None:
            continue
        rendered = f"{error} {error!r} {error.args}"
        assert FAKE_COOKIE not in rendered
        assert FAKE_PASSWORD not in rendered
        assert FAKE_CSRF not in rendered
        assert "Basic YWdlbnQ6c3VwZXItc2VjcmV0" not in rendered
        # Still useful: the message must say what happened and where.
        assert "/api/margenes/data" in rendered


def test_mensaje_es_util_aunque_no_filtre() -> None:
    error = error_for(401, {}, "", method="GET", path="/api/ventas")
    assert error is not None
    assert "401" in str(error)
    assert "/api/ventas" in str(error)
    assert "no retry" in str(error)


def test_timeout_de_httpx_es_transitorio_no_terminal() -> None:
    exc = httpx.ReadTimeout("timed out")
    mapped = map_transport_error(exc, method="GET", path="/api/health")
    assert isinstance(mapped, TransientError)
    assert mapped.is_terminal is False
    assert "ReadTimeout" in str(mapped)


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectTimeout("connect timed out"),
        httpx.ConnectError("name resolution failed"),
        httpx.ReadError("connection reset"),
        httpx.PoolTimeout("pool exhausted"),
    ],
)
def test_todo_fallo_de_transporte_es_transitorio(exc) -> None:
    mapped = map_transport_error(exc, method="GET", path="/api/health")
    assert mapped.outcome is Outcome.TRANSIENT
    assert mapped.is_terminal is False


def test_map_transport_error_no_filtra_la_url_de_httpx() -> None:
    # httpx puts the request URL in some transport errors. If a base URL were
    # ever misconfigured with credentials, chaining that message would publish
    # them; only the exception class name is kept.
    leaky = httpx.ConnectError(
        f"failed to connect to https://agent:{FAKE_PASSWORD}@portal.example/api/health?d=1"
    )
    mapped = map_transport_error(leaky, method="GET", path="/api/health")
    rendered = f"{mapped} {mapped!r}"
    assert FAKE_PASSWORD not in rendered
    assert "portal.example" not in rendered


def test_estado_desconocido_falla_cerrado() -> None:
    error = error_for(418, {}, "", method="GET", path="/api/x")
    assert isinstance(error, UnexpectedStatusError)
    assert error.is_terminal is True


def test_doscientos_con_cuerpo_no_json_es_contrato() -> None:
    error = error_for(200, {}, "not json at all", method="GET", path="/api/x")
    assert isinstance(error, ResponseContractError)
    assert error.is_terminal is True


def test_no_data_no_produce_excepcion() -> None:
    # "There is no data for that range" is an answer to render (RF-05), not a
    # failure to raise.
    assert classify(200, {}, '{"updatedAt":null}') is Outcome.NO_DATA
    assert error_for(200, {}, '{"updatedAt":null}', method="GET", path="/api/x") is None
