"""Tests for the persisted login budget (spec 006 RNF-02, plan 006 §2.1).

These are the tests that protect people who do not know this agent exists. The
agent box and the offices share an egress IP, and the portal blocks an IP after
10 failed logins in 15 minutes, so a bug here does not degrade the agent — it
locks colleagues out of their own portal.

Every test below asserts a property that must survive a refactor, not an
implementation detail: the counter is on disk, it is written *before* the
request, only one process may log in at a time, and a portal that is down costs
zero budget.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from os_system_agent.portal.budget import (
    LOGIN_FAILURE_WINDOW,
    MAX_LOGIN_FAILURES_PER_WINDOW,
    PORTAL_IP_FAILURE_LIMIT,
    BudgetState,
    LoginBudget,
    default_budget_path,
    login_guard,
)
from os_system_agent.portal.client import HealthResult
from os_system_agent.portal.errors import (
    BudgetStateError,
    LoginBudgetExhaustedError,
    LoginLockedError,
    PreflightFailedError,
)

T0 = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


class FrozenClock:
    """A clock the test moves by hand, so no test waits on real time."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def make_budget(tmp_path: Path, clock: FrozenClock | None = None) -> LoginBudget:
    return LoginBudget(tmp_path / "var" / "budget.json", clock=clock or FrozenClock())


def healthy() -> HealthResult:
    return HealthResult(ok=True, status=200)


def unhealthy() -> HealthResult:
    return HealthResult(ok=False, status=503, detail="db down")


# --------------------------------------------------------------------------
# Propiedad 1 — el contador vive en disco, no en memoria.
# --------------------------------------------------------------------------


def test_el_contador_sobrevive_al_reinicio_del_proceso(tmp_path: Path) -> None:
    """Un crash-loop es justo lo que produce fallos repetidos.

    Si el contador viviera en memoria se reiniciaria en cada arranque y el
    agente martillearia el portal indefinidamente. Se simula el reinicio
    construyendo una instancia nueva sobre el mismo archivo.
    """
    clock = FrozenClock()
    primera = make_budget(tmp_path, clock)
    with primera.lock():
        primera.reserve()

    # "El proceso muere aqui." Nada en memoria sobrevive; el archivo si.
    segunda = LoginBudget(primera.path, clock=clock)
    assert segunda.snapshot().failures == 1
    assert segunda.remaining() == MAX_LOGIN_FAILURES_PER_WINDOW - 1


def test_reserve_escribe_a_disco_antes_de_devolver(tmp_path: Path) -> None:
    """El incremento va ANTES de que salga la peticion, no despues de fallar.

    Se lee el archivo crudo dentro del mismo bloque para comprobar que ya esta
    en disco cuando ``reserve`` devuelve — que es el instante en que el llamador
    tiene permiso de enviar el login.
    """
    budget = make_budget(tmp_path)
    with budget.lock():
        budget.reserve()
        en_disco = json.loads(budget.path.read_text(encoding="utf-8"))
        assert en_disco["failures"] == 1


def test_dejamos_la_mayoria_del_limite_compartido_a_las_personas() -> None:
    """El presupuesto propio debe ser una fraccion pequena del limite del portal."""
    assert MAX_LOGIN_FAILURES_PER_WINDOW < PORTAL_IP_FAILURE_LIMIT / 2


# --------------------------------------------------------------------------
# Propiedad 2 — agotamiento y ventana.
# --------------------------------------------------------------------------


def test_se_agota_tras_el_maximo_y_rehusa_seguir(tmp_path: Path) -> None:
    budget = make_budget(tmp_path)
    with budget.lock():
        for _ in range(MAX_LOGIN_FAILURES_PER_WINDOW):
            budget.reserve()
        with pytest.raises(LoginBudgetExhaustedError) as exc:
            budget.reserve()
    # El error dice cuanto esperar: sin eso, el llamador solo puede adivinar.
    assert exc.value.retry_after_seconds > 0


def test_la_ventana_se_reinicia_al_expirar(tmp_path: Path) -> None:
    clock = FrozenClock()
    budget = make_budget(tmp_path, clock)
    with budget.lock():
        for _ in range(MAX_LOGIN_FAILURES_PER_WINDOW):
            budget.reserve()
        assert budget.snapshot().exhausted

    clock.advance(LOGIN_FAILURE_WINDOW + timedelta(seconds=1))
    assert budget.snapshot().failures == 0
    assert not budget.snapshot().exhausted


def test_justo_antes_de_expirar_sigue_agotado(tmp_path: Path) -> None:
    """El borde importa: reiniciar un segundo antes regalaria intentos."""
    clock = FrozenClock()
    budget = make_budget(tmp_path, clock)
    with budget.lock():
        for _ in range(MAX_LOGIN_FAILURES_PER_WINDOW):
            budget.reserve()

    clock.advance(LOGIN_FAILURE_WINDOW - timedelta(seconds=1))
    assert budget.snapshot().exhausted


def test_un_429_quema_la_ventana_entera(tmp_path: Path) -> None:
    """Si el portal ya nos limito, el cupo compartido esta gastado.

    Sale mas barato aguantar una ventana que quiza no debiamos que descubrir que
    si la debiamos dejando a alguien fuera.
    """
    budget = make_budget(tmp_path)
    with budget.lock():
        budget.record_rate_limited(retry_after_seconds=900)
    assert budget.snapshot().exhausted


def test_un_login_exitoso_limpia_el_contador(tmp_path: Path) -> None:
    budget = make_budget(tmp_path)
    with budget.lock():
        budget.reserve()
        budget.record_success()
    assert budget.snapshot().failures == 0


# --------------------------------------------------------------------------
# Propiedad 3 — un solo login a la vez, entre procesos.
# --------------------------------------------------------------------------


def test_dos_procesos_producen_un_solo_login(tmp_path: Path) -> None:
    """Dos instancias sobre el mismo archivo simulan dos procesos del agente.

    Ademas de proteger el contador, esto protege al humano: el portal revoca las
    sesiones previas del mismo usuario en cada login, asi que dos logins
    concurrentes se expulsarian mutuamente.
    """
    primera = make_budget(tmp_path)
    segunda = LoginBudget(primera.path)

    with primera.lock():
        with pytest.raises(LoginLockedError):
            with segunda.lock():
                pytest.fail("la segunda instancia no debio obtener el lock")


def test_el_lock_se_libera_al_salir_incluso_con_excepcion(tmp_path: Path) -> None:
    budget = make_budget(tmp_path)
    with pytest.raises(RuntimeError):
        with budget.lock():
            raise RuntimeError("fallo dentro del bloque")
    assert not budget.is_locked
    assert not budget.lock_path.exists()
    # Y se puede volver a tomar: un fallo no debe dejar al agente trabado.
    with budget.lock():
        assert budget.is_locked


def test_reserve_sin_lock_es_rechazado(tmp_path: Path) -> None:
    """Un contador incrementado fuera del lock es un contador con carrera."""
    budget = make_budget(tmp_path)
    with pytest.raises(BudgetStateError):
        budget.reserve()


def test_record_success_sin_lock_es_rechazado(tmp_path: Path) -> None:
    budget = make_budget(tmp_path)
    with pytest.raises(BudgetStateError):
        budget.record_success()


# --------------------------------------------------------------------------
# Propiedad 4 — el pre-vuelo evita gastar cupo ajeno.
# --------------------------------------------------------------------------


def test_preflight_fallido_no_gasta_presupuesto(tmp_path: Path) -> None:
    """El fallo mas comun (portal caido) debe costar cero cupo compartido."""
    budget = make_budget(tmp_path)
    antes = budget.snapshot().failures

    with pytest.raises(PreflightFailedError):
        with login_guard(budget, health_check=unhealthy):
            pytest.fail("no debio entrar al cuerpo con el portal caido")

    assert budget.snapshot().failures == antes
    assert not budget.lock_path.exists()


def test_login_guard_cuenta_el_intento_y_lo_limpia_al_exito(tmp_path: Path) -> None:
    with login_guard(budget := make_budget(tmp_path), health_check=healthy) as state:
        assert state.failures == 1  # ya contado antes de que el llamador envie nada
    assert budget.snapshot().failures == 0  # exito -> contador limpio


def test_login_guard_deja_el_intento_contado_si_el_login_falla(tmp_path: Path) -> None:
    """Si el login falla, el intento NO se devuelve al presupuesto."""
    budget = make_budget(tmp_path)
    with pytest.raises(RuntimeError):
        with login_guard(budget, health_check=healthy):
            raise RuntimeError("401 del portal")
    assert budget.snapshot().failures == 1


# --------------------------------------------------------------------------
# Fail-closed: un presupuesto que no se puede leer no se puede usar.
# --------------------------------------------------------------------------


def test_json_corrupto_rehusa_en_vez_de_asumir_cero(tmp_path: Path) -> None:
    budget = make_budget(tmp_path)
    budget.path.parent.mkdir(parents=True, exist_ok=True)
    budget.path.write_text("{no es json", encoding="utf-8")
    with pytest.raises(BudgetStateError):
        budget.snapshot()


def test_version_desconocida_rehusa(tmp_path: Path) -> None:
    budget = make_budget(tmp_path)
    budget.path.parent.mkdir(parents=True, exist_ok=True)
    budget.path.write_text(
        json.dumps({"version": 999, "window_started_at": T0.isoformat(), "failures": 0}),
        encoding="utf-8",
    )
    with pytest.raises(BudgetStateError):
        budget.snapshot()


@pytest.mark.parametrize("failures", [-1, "dos", True, None])
def test_contador_invalido_rehusa(tmp_path: Path, failures: object) -> None:
    budget = make_budget(tmp_path)
    budget.path.parent.mkdir(parents=True, exist_ok=True)
    budget.path.write_text(
        json.dumps(
            {"version": 1, "window_started_at": T0.isoformat(), "failures": failures},
        ),
        encoding="utf-8",
    )
    with pytest.raises(BudgetStateError):
        budget.snapshot()


def test_archivo_ausente_es_primer_arranque_no_error(tmp_path: Path) -> None:
    budget = make_budget(tmp_path)
    assert budget.snapshot().failures == 0


def test_timestamp_naive_se_trata_como_utc(tmp_path: Path) -> None:
    """Un timestamp sin zona compararia mal contra un `now` con zona.

    El riesgo concreto es reiniciar la ventana en silencio y regalar intentos.
    """
    budget = make_budget(tmp_path)
    budget.path.parent.mkdir(parents=True, exist_ok=True)
    budget.path.write_text(
        json.dumps(
            {
                "version": 1,
                "window_started_at": T0.replace(tzinfo=None).isoformat(),
                "failures": 1,
            }
        ),
        encoding="utf-8",
    )
    assert budget.snapshot().failures == 1


def test_ruta_relativa_es_rechazada(tmp_path: Path) -> None:
    """Una ruta relativa seria un presupuesto por directorio de trabajo."""
    with pytest.raises(BudgetStateError):
        LoginBudget(Path("var/budget.json"))


def test_override_relativo_por_entorno_es_rechazado(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OS_PORTAL_BUDGET_PATH", "var/relativo.json")
    with pytest.raises(BudgetStateError):
        default_budget_path()


def test_la_ruta_por_defecto_es_absoluta() -> None:
    assert default_budget_path().is_absolute()


@pytest.mark.skipif(sys.platform == "win32", reason="los modos POSIX no aplican en Windows")
def test_el_archivo_no_es_legible_por_otros(tmp_path: Path) -> None:
    budget = make_budget(tmp_path)
    with budget.lock():
        budget.reserve()
    assert os.stat(budget.path).st_mode & 0o077 == 0


# --------------------------------------------------------------------------
# BudgetState — aritmetica pura.
# --------------------------------------------------------------------------


def test_seconds_until_window_ends_nunca_es_negativo() -> None:
    state = BudgetState(window_started_at=T0, failures=1)
    muy_despues = T0 + LOGIN_FAILURE_WINDOW * 10
    assert state.seconds_until_window_ends(muy_despues) == 0


def test_remaining_nunca_es_negativo() -> None:
    state = BudgetState(window_started_at=T0, failures=MAX_LOGIN_FAILURES_PER_WINDOW + 5)
    assert state.remaining == 0
    assert state.exhausted
