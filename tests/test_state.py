"""Tests for the persistent agent memory (spec 006 T10, plan.md §7).

Two things this file insists on:

* **Every table has a reader, and every reader has a test.** A table nobody reads
  is not memory.
* **Every security control has a test.** The append-only triggers, the hash
  chain, the write-before-the-call ordering, the 0600 file mode and the redaction
  on write *and* on read are all exercised here. A control without a test is
  theatre.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

import state_init
from os_system_agent.severity import Severity
from os_system_agent.state import (
    GENESIS_HASH,
    SCHEMA_VERSION,
    BudgetExhausted,
    LedgerTampered,
    StateError,
    StateStore,
    apply_schema,
    chain_hash,
    connect,
    connect_readonly,
    open_state,
    schema_status,
)

NOW = datetime(2026, 8, 25, 13, 0, 0, tzinfo=UTC)
DAY = date(2026, 8, 25)

# Forma de secreto que el redactor actual sí enmascara. Valor inventado.
SECRET_TEXT = "password=super-secret-value-xyz"
SECRET_VALUE = "super-secret-value-xyz"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def store(db_path: Path) -> Iterator[StateStore]:
    with open_state(db_path, create=True, clock=lambda: NOW) as opened:
        yield opened


# --------------------------------------------------------------------------- #
# Schema, migration, file hygiene
# --------------------------------------------------------------------------- #


def test_migracion_es_idempotente(db_path: Path) -> None:
    conn = connect(db_path, create=True)
    try:
        first = apply_schema(conn)
        second = apply_schema(conn)  # aplicarla dos veces no debe romper nada
        assert first.up_to_date and second.up_to_date
        assert second.version == SCHEMA_VERSION
        assert second.missing_tables == () and second.missing_triggers == ()
    finally:
        conn.close()


def test_pragmas_wal_y_foreign_keys(store: StateStore) -> None:
    conn = store.connection
    assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
    assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1


def test_integrity_check_limpio(store: StateStore) -> None:
    assert str(store.connection.execute("PRAGMA integrity_check").fetchone()[0]) == "ok"


def test_base_ausente_falla_cerrado(db_path: Path) -> None:
    with pytest.raises(StateError, match="not found"):
        open_state(db_path, create=False)


def test_base_sin_esquema_falla_cerrado(db_path: Path) -> None:
    # Fichero creado pero nunca migrado: abrirlo debe rehusar, no arrancar con
    # una memoria vacia que parezca "no ha pasado nada".
    connect(db_path, create=True).close()
    with pytest.raises(StateError, match="schema"):
        open_state(db_path, create=False)


@pytest.mark.skipif(sys.platform == "win32", reason="chmod bits are POSIX-only")
def test_fichero_de_base_es_0600(store: StateStore, db_path: Path) -> None:
    # El fichero guarda cifras de negocio y el rastro de consultas: nadie mas
    # en el box tiene por que leerlo.
    assert stat.S_IMODE(os.stat(db_path).st_mode) == 0o600


def test_timestamp_naive_falla_cerrado(store: StateStore) -> None:
    naive = datetime(2026, 8, 25, 13, 0, 0)  # sin tzinfo: justo lo que debe rehusar
    with pytest.raises(StateError, match="timezone-aware"):
        store.open_incident(empresa="X", job_id="j", severity=Severity.CRITICAL, at=naive)


# --------------------------------------------------------------------------- #
# incident — lector: "¿desde cuándo está roto?" y "¿cuántas veces esta semana?"
# --------------------------------------------------------------------------- #


def test_lee_historia_desde_cuando_esta_roto(store: StateStore) -> None:
    opened_at = NOW - timedelta(hours=30)
    store.open_incident(
        empresa="Dinastia", job_id="daily_sales", severity=Severity.CRITICAL, at=opened_at
    )
    assert store.broken_since(empresa="Dinastia", job_id="daily_sales") == opened_at
    assert store.broken_since(empresa="Dinastia", job_id="otro") is None


def test_lee_historia_cuantas_veces_esta_semana(store: StateStore) -> None:
    for days_ago in (1, 3, 5, 20):  # el de hace 20 dias cae fuera de la ventana
        moment = NOW - timedelta(days=days_ago)
        store.open_incident(
            empresa="Mercamio", job_id="rotacion", severity=Severity.WARNING, at=moment
        )
        store.close_incident(empresa="Mercamio", job_id="rotacion", at=moment + timedelta(hours=1))
    assert store.incident_count(empresa="Mercamio", since=NOW - timedelta(days=7)) == 3
    assert store.incident_count(empresa="Mercamio", since=NOW - timedelta(days=30)) == 4


def test_lee_historia_completa_mas_reciente_primero(store: StateStore) -> None:
    store.open_incident(empresa="M", job_id="a", severity="CRITICAL", at=NOW - timedelta(days=2))
    store.close_incident(empresa="M", job_id="a", at=NOW - timedelta(days=2, hours=-1))
    store.open_incident(empresa="M", job_id="b", severity="WARNING", at=NOW)
    history = store.incident_history(empresa="M")
    assert [r.job_id for r in history] == ["b", "a"]
    assert history[0].closed_at is None and history[1].closed_at is not None
    assert history[0].severity is Severity.WARNING


def test_incidente_abierto_no_se_duplica(store: StateStore) -> None:
    # Reabrir reiniciaria el "desde cuando", que es justo lo que la tabla existe
    # para responder.
    first = store.open_incident(empresa="M", job_id="a", severity="CRITICAL", at=NOW)
    again = store.open_incident(
        empresa="M", job_id="a", severity="CRITICAL", at=NOW + timedelta(hours=5)
    )
    assert first == again
    assert store.broken_since(empresa="M", job_id="a") == NOW


def test_cerrar_incidente_libera_el_desde_cuando(store: StateStore) -> None:
    store.open_incident(empresa="M", job_id="a", severity="CRITICAL", at=NOW)
    assert store.close_incident(empresa="M", job_id="a", at=NOW + timedelta(hours=1)) is True
    assert store.broken_since(empresa="M", job_id="a") is None
    assert store.close_incident(empresa="M", job_id="a", at=NOW) is False


def test_severidad_desconocida_falla_cerrado(store: StateStore) -> None:
    with pytest.raises(StateError, match="unknown severity"):
        store.open_incident(empresa="M", job_id="a", severity="MUY_MALO", at=NOW)


def test_incidente_redacta_evidencia_al_escribir(store: StateStore) -> None:
    store.open_incident(
        empresa="M", job_id="a", severity="CRITICAL", at=NOW, evidence=f"login failed {SECRET_TEXT}"
    )
    raw = store.connection.execute("SELECT evidence FROM incident").fetchone()[0]
    assert SECRET_VALUE not in raw
    assert SECRET_VALUE not in store.incident_history(empresa="M")[0].evidence


# --------------------------------------------------------------------------- #
# ask_intent_miss — lector: backlog ordenado por frecuencia
# --------------------------------------------------------------------------- #


def test_backlog_ordenado_por_frecuencia(store: StateStore) -> None:
    for _ in range(3):
        store.record_intent_miss("cuanto vendimos ayer", at=NOW)
    store.record_intent_miss("que margen dejo el alcohol", at=NOW)
    for _ in range(2):
        store.record_intent_miss("como va la rotacion", at=NOW)

    backlog = store.intent_backlog()
    assert [m.hits for m in backlog] == [3, 2, 1]
    assert backlog[0].question == "cuanto vendimos ayer"


def test_preguntas_equivalentes_cuentan_como_una(store: StateStore) -> None:
    store.record_intent_miss("Cuanto  vendimos   AYER", at=NOW)
    hits = store.record_intent_miss("cuanto vendimos ayer", at=NOW + timedelta(minutes=1))
    assert hits == 2
    backlog = store.intent_backlog()
    assert len(backlog) == 1
    assert backlog[0].first_seen_at == NOW
    assert backlog[0].last_seen_at == NOW + timedelta(minutes=1)


def test_backlog_filtra_por_demanda_minima(store: StateStore) -> None:
    store.record_intent_miss("una sola vez", at=NOW)
    store.record_intent_miss("dos veces", at=NOW)
    store.record_intent_miss("dos veces", at=NOW)
    assert [m.question for m in store.intent_backlog(min_hits=2)] == ["dos veces"]


def test_backlog_redacta_la_pregunta(store: StateStore) -> None:
    store.record_intent_miss(f"entra con {SECRET_TEXT} y dime la venta", at=NOW)
    assert SECRET_VALUE not in store.intent_backlog()[0].question


def test_pregunta_vacia_falla_cerrado(store: StateStore) -> None:
    with pytest.raises(StateError, match="non-empty"):
        store.record_intent_miss("   ", at=NOW)


# --------------------------------------------------------------------------- #
# budget_day — lector: frena ANTES de gastar
# --------------------------------------------------------------------------- #


def test_frena_por_presupuesto_antes_de_gastar(store: StateStore) -> None:
    store.reserve_budget(day=DAY, cap_usd=1.0, estimated_usd=0.60, at=NOW)
    with pytest.raises(BudgetExhausted, match="would be exceeded"):
        store.reserve_budget(day=DAY, cap_usd=1.0, estimated_usd=0.60, at=NOW)
    # El intento rehusado no cobra: si cobrara, "frenar" seria "gastar y avisar".
    spent = store.budget_for(DAY)
    assert spent.costo_usd == pytest.approx(0.60)
    assert spent.calls == 1
    assert store.budget_remaining(DAY, cap_usd=1.0) == pytest.approx(0.40)


def test_presupuesto_cobra_antes_de_la_llamada(store: StateStore, db_path: Path) -> None:
    # El cobro va antes: si el proceso muere a mitad de la llamada, el costo ya
    # esta contado (mismo razonamiento fail-closed que el presupuesto de login).
    # La prueba de "ya esta en disco" es que otra conexion lo ve.
    store.reserve_budget(day=DAY, cap_usd=5.0, estimated_usd=1.25, at=NOW)
    witness = StateStore(connect_readonly(db_path))
    try:
        assert witness.budget_for(DAY).costo_usd == pytest.approx(1.25)
    finally:
        witness.close()


def test_presupuesto_sobrevive_al_reinicio(db_path: Path) -> None:
    with open_state(db_path, create=True, clock=lambda: NOW) as first:
        first.reserve_budget(day=DAY, cap_usd=2.0, estimated_usd=0.75, at=NOW)
    with open_state(db_path, clock=lambda: NOW) as second:
        assert second.budget_for(DAY).costo_usd == pytest.approx(0.75)
        assert second.budget_remaining(DAY, cap_usd=2.0) == pytest.approx(1.25)


def test_dia_sin_gasto_lee_cero(store: StateStore) -> None:
    empty = store.budget_for(date(2026, 1, 1))
    assert empty.costo_usd == 0.0 and empty.calls == 0 and empty.updated_at is None


def test_presupuesto_refusa_importes_invalidos(store: StateStore) -> None:
    with pytest.raises(StateError, match="negative"):
        store.reserve_budget(day=DAY, cap_usd=1.0, estimated_usd=-1.0, at=NOW)
    with pytest.raises(StateError, match="finite"):
        store.reserve_budget(day=DAY, cap_usd=1.0, estimated_usd=float("nan"), at=NOW)


def test_presupuesto_rehusado_deja_linea_de_log(store: StateStore, capsys) -> None:
    store.reserve_budget(day=DAY, cap_usd=0.5, estimated_usd=0.5, at=NOW)
    with pytest.raises(BudgetExhausted):
        store.reserve_budget(day=DAY, cap_usd=0.5, estimated_usd=0.1, at=NOW)
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert any(e["event"] == "state.budget.refused" for e in events)


# --------------------------------------------------------------------------- #
# ledger — append-only, encadenado, escrito ANTES de la llamada
# --------------------------------------------------------------------------- #


def _attempt(store: StateStore, intent: str = "venta_dia") -> int:
    return store.record_attempt(
        actor="os_system_agent",
        intent=intent,
        route="/api/ventas/data",
        params={"fecha": "2026-08-24"},
        at=NOW,
    )


def test_ledger_rechaza_update(store: StateStore) -> None:
    _attempt(store)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.connection.execute("UPDATE ledger SET detail = 'editado' WHERE seq = 1")


def test_ledger_rechaza_delete(store: StateStore) -> None:
    _attempt(store)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.connection.execute("DELETE FROM ledger WHERE seq = 1")
    assert len(store.read_ledger()) == 1


def test_ledger_escribe_el_intento_antes_de_la_llamada(store: StateStore, db_path: Path) -> None:
    # La prueba de "antes" es que OTRA conexion ya ve la fila mientras la llamada
    # todavia esta corriendo. Si se escribiera despues, esto no existiria.
    seen: list[str] = []
    with store.audited_call(actor="agent", intent="venta_dia", route="/api/ventas/data"):
        witness = connect_readonly(db_path)
        try:
            seen = [str(r["status"]) for r in witness.execute("SELECT status FROM ledger")]
        finally:
            witness.close()
    assert seen == ["attempted"]


def test_ledger_guarda_el_intento_fallido(store: StateStore) -> None:
    with (
        pytest.raises(TimeoutError),
        store.audited_call(actor="agent", intent="venta_dia", route="/api/ventas/data"),
    ):
        raise TimeoutError("portal no responde")

    queries = store.reconstruct_queries()
    assert len(queries) == 1
    assert queries[0].status == "failure"
    assert "TimeoutError" in queries[0].detail


def test_ledger_reconstruye_lo_consultado(store: StateStore) -> None:
    with store.audited_call(
        actor="agent", intent="venta_dia", route="/api/ventas/data", params={"fecha": "2026-08-24"}
    ):
        pass
    seq = _attempt(store, intent="margen_categoria")  # muere sin desenlace

    queries = store.reconstruct_queries()
    assert [q.intent for q in queries] == ["margen_categoria", "venta_dia"]
    unresolved = queries[0]
    assert unresolved.seq == seq
    assert unresolved.status == "attempted" and unresolved.unresolved is True
    assert unresolved.resolved_at is None
    done = queries[1]
    assert done.status == "success" and done.route == "/api/ventas/data"
    assert "2026-08-24" in done.params


def test_ledger_desenlace_exige_un_intento_abierto(store: StateStore) -> None:
    seq = _attempt(store)
    store.record_outcome(seq, status="success", at=NOW)
    with pytest.raises(StateError, match="already has an outcome"):
        store.record_outcome(seq, status="failure", at=NOW)  # dos historias, una consulta
    with pytest.raises(StateError, match="not an open attempt"):
        store.record_outcome(999, status="success", at=NOW)
    assert store.verify_chain().ok is True


def test_ledger_estado_de_desenlace_invalido_falla_cerrado(store: StateStore) -> None:
    seq = _attempt(store)
    with pytest.raises(StateError, match="outcome status"):
        store.record_outcome(seq, status="attempted", at=NOW)


def test_ledger_redacta_al_escribir(store: StateStore) -> None:
    store.record_attempt(
        actor="agent",
        intent="venta_dia",
        route="/api/ventas/data",
        params={"cookie": SECRET_TEXT},
        at=NOW,
    )
    raw = store.connection.execute("SELECT params FROM ledger WHERE seq = 1").fetchone()[0]
    assert SECRET_VALUE not in raw


def test_ledger_redacta_al_leer_lo_ya_guardado(store: StateStore) -> None:
    # Simula una fila escrita por una version anterior del redactor: encadenada
    # correctamente, pero con el secreto en claro. Redactar solo al escribir la
    # dejaria salir tal cual.
    payload = {
        "seq": 1,
        "ts": "2026-08-25T13:00:00+00:00",
        "actor": "agent",
        "intent": "venta_dia",
        "route": "/api/ventas/data",
        "params": SECRET_TEXT,
        "status": "attempted",
        "detail": "",
        "ref_seq": None,
    }
    row_hash = chain_hash(payload, GENESIS_HASH)
    store.connection.execute(
        "INSERT INTO ledger (seq, ts, actor, intent, route, params, status, detail, ref_seq, "
        "prev_hash, row_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            payload["seq"],
            payload["ts"],
            payload["actor"],
            payload["intent"],
            payload["route"],
            payload["params"],
            payload["status"],
            payload["detail"],
            payload["ref_seq"],
            GENESIS_HASH,
            row_hash,
        ),
    )
    assert store.verify_chain().ok is True  # la fila es legitima, no manipulada
    assert SECRET_VALUE not in store.read_ledger()[0].params
    assert SECRET_VALUE not in store.reconstruct_queries()[0].params


def test_cadena_intacta_verifica(store: StateStore) -> None:
    for i in range(3):
        with store.audited_call(actor="agent", intent=f"i{i}", route="/api/x"):
            pass
    report = store.verify_chain()
    assert report.ok is True and report.rows == 6 and report.broken_at is None
    store.assert_chain_intact()


def test_cadena_manipulada_se_detecta(store: StateStore, capsys) -> None:
    for i in range(3):
        _attempt(store, intent=f"i{i}")
    # Modelo de atacante realista: quien pueda editar el fichero puede tirar el
    # trigger. La cadena es la defensa que sobrevive a eso.
    store.connection.execute("DROP TRIGGER ledger_no_update")
    store.connection.execute("UPDATE ledger SET route = '/api/otra' WHERE seq = 2")
    capsys.readouterr()

    report = store.verify_chain()
    assert report.ok is False
    assert report.broken_at == 2
    assert report.reason is not None and "row_hash" in report.reason
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert any(
        e["event"] == "state.ledger.chain_broken" and e["severity"] == Severity.SECURITY.value
        for e in events
    )
    with pytest.raises(LedgerTampered, match="seq 2"):
        store.assert_chain_intact()


def test_cadena_detecta_fila_borrada(store: StateStore) -> None:
    for i in range(3):
        _attempt(store, intent=f"i{i}")
    store.connection.execute("DROP TRIGGER ledger_no_delete")
    store.connection.execute("DELETE FROM ledger WHERE seq = 2")

    report = store.verify_chain()
    assert report.ok is False
    assert report.broken_at == 3
    assert report.reason is not None and "gap" in report.reason


def test_cadena_vacia_es_valida(store: StateStore) -> None:
    report = store.verify_chain()
    assert report.ok is True and report.rows == 0


def test_read_ledger_filtra_por_intent_y_fecha(store: StateStore) -> None:
    _attempt(store, intent="venta_dia")
    _attempt(store, intent="margen_categoria")
    assert [e.intent for e in store.read_ledger(intent="venta_dia")] == ["venta_dia"]
    assert store.read_ledger(since=NOW + timedelta(days=1)) == []


def test_limite_no_positivo_falla_cerrado(store: StateStore) -> None:
    with pytest.raises(StateError, match="positive"):
        store.read_ledger(limit=0)


# --------------------------------------------------------------------------- #
# scripts/state_init.py
# --------------------------------------------------------------------------- #


def test_state_init_apply_y_luego_check(db_path: Path, capsys) -> None:
    assert state_init.main(["--db", str(db_path), "--apply"]) == 0
    assert state_init.main(["--db", str(db_path), "--apply"]) == 0  # idempotente
    capsys.readouterr()
    assert state_init.main(["--db", str(db_path), "--check", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["up_to_date"] is True
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["missing_triggers"] == []


def test_state_init_check_no_escribe(db_path: Path) -> None:
    # `--check` no puede tocar la base: la abre en mode=ro. SQLite si puede crear
    # los sidecars -wal/-shm que necesita para leer, asi que lo que se afirma es
    # lo que importa: la base sale byte a byte identica.
    state_init.main(["--db", str(db_path), "--apply"])
    before = (db_path.stat().st_mtime_ns, hashlib.sha256(db_path.read_bytes()).hexdigest())
    assert state_init.main(["--db", str(db_path), "--check"]) == 0
    assert (db_path.stat().st_mtime_ns, hashlib.sha256(db_path.read_bytes()).hexdigest()) == before


def test_conexion_de_solo_lectura_rechaza_escrituras(db_path: Path) -> None:
    # El control que respalda a `--check`: no es que no escribamos, es que no
    # podemos. Un `open_state` normal si escribe; este no.
    state_init.main(["--db", str(db_path), "--apply"])
    conn = connect_readonly(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute(
                "INSERT INTO budget_day (day, costo_usd, calls, updated_at) VALUES (?, ?, ?, ?)",
                ("2026-08-25", 1.0, 1, "2026-08-25T13:00:00+00:00"),
            )
    finally:
        conn.close()


def test_state_init_check_sin_base_avisa_y_no_crea(db_path: Path) -> None:
    assert state_init.main(["--db", str(db_path), "--check"]) == 1
    assert not db_path.exists()


def test_state_init_exige_un_modo(db_path: Path) -> None:
    # Sin --check ni --apply no hay accion por defecto: crear una base es una
    # escritura, y no se hace por descuido.
    with pytest.raises(SystemExit):
        state_init.main(["--db", str(db_path)])


def test_state_init_reporta_cadena_rota(db_path: Path, capsys) -> None:
    state_init.main(["--db", str(db_path), "--apply"])
    with open_state(db_path, clock=lambda: NOW) as opened:
        _attempt(opened)
        opened.connection.execute("DROP TRIGGER ledger_no_update")
        opened.connection.execute("UPDATE ledger SET actor = 'otro' WHERE seq = 1")
    capsys.readouterr()
    assert state_init.main(["--db", str(db_path), "--check", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["chain_ok"] is False


def test_state_init_detecta_base_no_ignorada_por_git(tmp_path: Path) -> None:
    # `*.log` sí está en .gitignore; una ruta fuera del repo no aplica.
    assert state_init._is_git_ignored(state_init.REPO_ROOT / "cualquiera.log") is True
    assert state_init._is_git_ignored(tmp_path / "state.db") is None


def test_state_init_avisa_si_la_base_es_commiteable(capsys) -> None:
    state_init._warn_if_committable({"db": "var/state.db", "git_ignored": False})
    assert "WARNING" in capsys.readouterr().err


def test_schema_status_sobre_base_vacia(db_path: Path) -> None:
    conn = connect(db_path, create=True)
    try:
        status = schema_status(conn)
        assert status.up_to_date is False
        assert set(status.missing_triggers) == {"ledger_no_update", "ledger_no_delete"}
    finally:
        conn.close()
