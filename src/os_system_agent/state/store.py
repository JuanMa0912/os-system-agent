"""Persistent agent memory in SQLite — memory that is READ (spec 006, plan.md §7).

Four tables, each with its reader implemented here:

============== ============================== =========================================
Table          What it stores                 Reader
============== ============================== =========================================
``incident``   Incident history, open + close :meth:`StateStore.broken_since`,
                                              :meth:`StateStore.incident_count`
``ask_intent_miss`` Questions with no intent   :meth:`StateStore.intent_backlog`
``budget_day`` Model spend per business day   :meth:`StateStore.reserve_budget`
``ledger``     Append-only record per query   :meth:`StateStore.reconstruct_queries`
============== ============================== =========================================

The ledger is the point of the module. The portal audits sessions and logins but
not queries, so this is the only trace of what the agent looked at. It is
append-only (SQLite triggers, not a code convention), hash-chained (tampering is
detectable by :meth:`StateStore.verify_chain`), and written *before* the call so
a failed attempt leaves a trace too.

Fail-closed everywhere: a missing file, a missing schema, a naive timestamp, an
unknown severity or a negative cost raises :class:`StateError` instead of
degrading quietly. Every text value passes through :func:`redact` on write *and*
on read — redacting only on write would let through whatever an older version of
the redactor already stored.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final

from os_system_agent.redaction import redact
from os_system_agent.severity import Severity

SCHEMA_VERSION: Final = 1
SCHEMA_PATH: Final = Path(__file__).with_name("schema.sql")

#: Previous hash of the very first ledger row. Not a real digest, so a row
#: claiming to be first cannot be moved elsewhere in the chain.
GENESIS_HASH: Final = "0" * 64

#: File mode for the database. It holds business figures and query history; it
#: is nobody else's business on a shared box.
DB_MODE: Final = 0o600

#: Free text is truncated after redaction (never before: cutting a secret in
#: half could defeat the very pattern that would have caught it).
MAX_TEXT_LEN: Final = 1000

#: SQLite lock wait. Two agent processes may write at once (plan.md §2.1).
BUSY_TIMEOUT_SECONDS: Final = 10.0

_REQUIRED_TABLES: Final = ("schema_meta", "incident", "ask_intent_miss", "budget_day", "ledger")
_REQUIRED_TRIGGERS: Final = ("ledger_no_update", "ledger_no_delete")

_ATTEMPTED: Final = "attempted"
_OUTCOMES: Final = frozenset({"success", "failure"})

_WHITESPACE = re.compile(r"\s+")

Clock = Callable[[], datetime]


class StateError(RuntimeError):
    """Raised when the state store is missing, malformed, or misused."""


class BudgetExhausted(StateError):
    """Raised *before* spending when a call would exceed the daily USD cap."""


class LedgerTampered(StateError):
    """Raised when the ledger hash chain does not verify."""


# --------------------------------------------------------------------------- #
# Records returned by the readers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class IncidentRecord:
    """One incident, open (``closed_at is None``) or already closed."""

    id: int
    empresa: str
    job_id: str
    severity: Severity
    opened_at: datetime
    closed_at: datetime | None
    evidence: str


@dataclass(frozen=True)
class IntentMiss:
    """A question the router could not map, with its demand evidence."""

    question: str
    hits: int
    first_seen_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True)
class BudgetDay:
    """Model spend accumulated on one business day."""

    day: date
    costo_usd: float
    calls: int
    updated_at: datetime | None


@dataclass(frozen=True)
class LedgerEntry:
    """One raw ledger row (attempt or outcome), redacted on read."""

    seq: int
    ts: datetime
    actor: str
    intent: str
    route: str
    params: str
    status: str
    detail: str
    ref_seq: int | None
    prev_hash: str
    row_hash: str


@dataclass(frozen=True)
class LedgerQuery:
    """One query reconstructed from its attempt row and its outcome row."""

    seq: int
    ts: datetime
    actor: str
    intent: str
    route: str
    params: str
    #: ``attempted`` means no outcome was ever written — the call died mid-flight.
    status: str
    detail: str
    resolved_at: datetime | None

    @property
    def unresolved(self) -> bool:
        """True when the attempt never got an outcome row."""
        return self.status == _ATTEMPTED


@dataclass(frozen=True)
class ChainReport:
    """Result of verifying the ledger hash chain."""

    ok: bool
    rows: int
    broken_at: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class SchemaStatus:
    """What the migration would find on disk, without writing anything."""

    exists: bool
    version: int | None
    expected: int
    missing_tables: tuple[str, ...]
    missing_triggers: tuple[str, ...]

    @property
    def up_to_date(self) -> bool:
        return (
            self.exists
            and self.version == self.expected
            and not self.missing_tables
            and not self.missing_triggers
        )


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _log(event: str, **fields: object) -> None:
    """Write one structured, secret-free JSON line to stderr (mcp_server.py pattern)."""
    payload: dict[str, object] = {"event": event}
    for key, value in fields.items():
        payload[key] = redact(value) if isinstance(value, str) else value
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False), file=sys.stderr, flush=True)


def utcnow() -> datetime:
    """Current UTC time (injectable as a :data:`Clock` in tests)."""
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    """Serialize an aware datetime as UTC ISO-8601; fail closed on a naive one."""
    if moment.tzinfo is None:
        raise StateError(
            "timestamp must be timezone-aware; a naive datetime is ambiguous and "
            "would silently store the wrong instant"
        )
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _parse_ts(raw: object) -> datetime:
    """Parse a stored timestamp back into an aware UTC datetime."""
    try:
        moment = datetime.fromisoformat(str(raw))
    except ValueError as exc:
        raise StateError(f"stored timestamp is not ISO-8601: {exc}") from exc
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _clean(text: object) -> str:
    """Redact, then truncate. Applied on write and on read.

    Order matters: truncating first could split a secret in half and defeat the
    pattern that would have masked it.
    """
    if text is None:
        return ""
    out = redact(str(text))
    return out if len(out) <= MAX_TEXT_LEN else out[:MAX_TEXT_LEN] + "...[truncated]"


def _require_text(value: str, *, field: str) -> str:
    """Fail closed on an empty identifier — an unlabeled row is unreadable later."""
    cleaned = _clean(value).strip()
    if not cleaned:
        raise StateError(f"{field} must be a non-empty string")
    return cleaned


def _require_amount(value: float, *, field: str) -> float:
    """Fail closed on a negative, NaN or infinite amount."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StateError(f"{field} must be a number, got {type(value).__name__}")
    amount = float(value)
    # NaN pierde toda comparacion: `nan > cap` es False, asi que un NaN colado
    # aqui pasaria el freno de presupuesto sin frenar nada.
    if not math.isfinite(amount):
        raise StateError(f"{field} must be a finite number")
    if amount < 0:
        raise StateError(f"{field} must not be negative, got {amount}")
    return amount


def _positive(value: int) -> int:
    """Fail closed on a non-positive LIMIT (a 0 limit silently reads as 'no data')."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise StateError(f"limit must be a positive integer, got {value!r}")
    return value


def _as_severity(value: Severity | str) -> Severity:
    """Coerce to the shared §12 severity enum; fail closed on anything else."""
    try:
        return Severity(str(value))
    except ValueError as exc:
        raise StateError(
            f"unknown severity {value!r}; expected one of {[s.value for s in Severity]}"
        ) from exc


def _normalize_question(question: str) -> str:
    """Redact, lowercase and collapse whitespace so the same ask counts as one."""
    normalized = _WHITESPACE.sub(" ", _clean(question).strip().lower())
    if not normalized:
        raise StateError("question must be a non-empty string")
    return normalized


def _dump_params(params: Mapping[str, Any] | str) -> str:
    """Serialize call parameters canonically (sorted) and redacted."""
    if isinstance(params, str):
        return _clean(params)
    return _clean(json.dumps(params, sort_keys=True, ensure_ascii=False, default=str))


def _broken(rows: int, seq: int, reason: str) -> ChainReport:
    """Build a break report and log it. Carries sequence numbers and a reason —
    never row content, which is exactly what an audit trail must not spill."""
    # Cadena rota = posible manipulacion del unico rastro de auditoria que
    # tenemos: es SECURITY en el modelo de §12, no un WARNING.
    _log("state.ledger.chain_broken", seq=seq, reason=reason, severity=Severity.SECURITY.value)
    return ChainReport(ok=False, rows=rows, broken_at=seq, reason=reason)


def chain_hash(payload: Mapping[str, Any], prev_hash: str) -> str:
    """Return ``sha256(prev_hash || canonical(payload))`` as hex.

    Public so a test — or a future auditor — can recompute a row's hash without
    reaching into the store's internals.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256()
    digest.update(prev_hash.encode("utf-8"))
    digest.update(b"\x1f")  # separador: impide que dos campos pegados colisionen
    digest.update(canonical.encode("utf-8"))
    return digest.hexdigest()


def _ledger_payload(
    *,
    seq: int,
    ts: str,
    actor: str,
    intent: str,
    route: str,
    params: str,
    status: str,
    detail: str,
    ref_seq: int | None,
) -> dict[str, Any]:
    """The exact content covered by a ledger row's hash (``seq`` included, so a
    reordered row is as detectable as an edited one)."""
    return {
        "seq": seq,
        "ts": ts,
        "actor": actor,
        "intent": intent,
        "route": route,
        "params": params,
        "status": status,
        "detail": detail,
        "ref_seq": ref_seq,
    }


# --------------------------------------------------------------------------- #
# Connection, schema, migration
# --------------------------------------------------------------------------- #


def _harden(path: Path) -> None:
    """Restrict the database (and its WAL sidecars) to the owner.

    On Windows ``chmod`` only toggles the read-only bit; the control is real on
    the Linux box where the agent runs, and harmless here.
    """
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if candidate.exists():
            try:
                os.chmod(candidate, DB_MODE)
            except OSError as exc:
                raise StateError(
                    f"could not restrict permissions on {candidate.name}: {exc}"
                ) from exc


def connect(path: Path | str, *, create: bool = False) -> sqlite3.Connection:
    """Open the state database with the project's PRAGMAs and 0600 permissions.

    Fails closed when the file is missing and ``create`` is False: a monitor that
    silently starts an empty history is worse than one that refuses to start.
    """
    db_path = Path(path)
    if not db_path.exists():
        if not create:
            raise StateError(
                f"state database not found: {db_path} — run "
                f"'uv run python scripts/state_init.py --db {db_path} --apply' first"
            )
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # Crear el fichero nosotros, ya con 0600, ANTES de que lo cree sqlite: si
        # lo crea sqlite queda una ventana en la que es legible por todos, y en
        # esa ventana ya puede haber filas dentro.
        try:
            os.close(os.open(db_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, DB_MODE))
        except FileExistsError:
            pass  # otro proceso gano la carrera; su fichero tambien nacio 0600
        except OSError as exc:
            raise StateError(f"could not create state database {db_path}: {exc}") from exc

    try:
        conn = sqlite3.connect(
            db_path,
            timeout=BUSY_TIMEOUT_SECONDS,
            isolation_level=None,  # transacciones explicitas: ver StateStore._write_tx
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
    except sqlite3.Error as exc:
        raise StateError(f"could not open state database {db_path}: {exc}") from exc

    _harden(db_path)
    return conn


def connect_readonly(path: Path | str) -> sqlite3.Connection:
    """Open the database read-only (``--check`` must never write, not even a WAL)."""
    db_path = Path(path)
    if not db_path.exists():
        raise StateError(f"state database not found: {db_path}")
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    except sqlite3.Error as exc:
        raise StateError(f"could not open state database read-only {db_path}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    return conn


def _objects(conn: sqlite3.Connection, kind: str) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,)).fetchall()
    return {str(row["name"]) for row in rows}


def schema_status(conn: sqlite3.Connection) -> SchemaStatus:
    """Report what is on disk without writing a single byte."""
    tables = _objects(conn, "table")
    triggers = _objects(conn, "trigger")
    version: int | None = None
    if "schema_meta" in tables:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?", ("schema_version",)
        ).fetchone()
        if row is not None:
            try:
                version = int(row["value"])
            except (TypeError, ValueError):
                version = None
    return SchemaStatus(
        exists=bool(tables & set(_REQUIRED_TABLES)),
        version=version,
        expected=SCHEMA_VERSION,
        missing_tables=tuple(t for t in _REQUIRED_TABLES if t not in tables),
        missing_triggers=tuple(t for t in _REQUIRED_TRIGGERS if t not in triggers),
    )


def apply_schema(conn: sqlite3.Connection) -> SchemaStatus:
    """Apply ``schema.sql`` idempotently and stamp the version.

    Every object is ``CREATE ... IF NOT EXISTS``, so running this twice changes
    nothing — which is what makes the migration safe to put in a boot script.
    """
    try:
        ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise StateError(f"could not read schema file {SCHEMA_PATH}: {exc}") from exc
    try:
        conn.executescript(ddl)
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("schema_version", str(SCHEMA_VERSION)),
        )
    except sqlite3.Error as exc:
        raise StateError(f"could not apply schema: {exc}") from exc
    return schema_status(conn)


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #


class StateStore:
    """Typed access to the agent's persistent memory.

    The clock is injected (same pattern as ``collector.Runner``) so time-dependent
    readers are testable without sleeping or patching the module.
    """

    def __init__(self, conn: sqlite3.Connection, *, clock: Clock = utcnow) -> None:
        self._conn = conn
        self._clock = clock

    # -- lifecycle ---------------------------------------------------------- #

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        """The underlying connection (for ``PRAGMA integrity_check`` and tests)."""
        return self._conn

    def _now(self, at: datetime | None) -> str:
        return _iso(at if at is not None else self._clock())

    @contextmanager
    def _write_tx(self) -> Iterator[sqlite3.Cursor]:
        """One explicit ``BEGIN IMMEDIATE`` transaction.

        IMMEDIATE (not DEFERRED) because every writer here reads before it writes
        — the ledger head, the day's spend — and a deferred transaction would let
        a second process read the same head and build a forked chain.
        """
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            yield cur
        except BaseException:
            self._conn.rollback()
            raise
        self._conn.commit()

    # -- incident ----------------------------------------------------------- #

    def open_incident(
        self,
        *,
        empresa: str,
        job_id: str,
        severity: Severity | str,
        at: datetime | None = None,
        evidence: str = "",
    ) -> int:
        """Open an incident, or return the id of the one already open.

        Re-opening an already-open incident would restart "broken since", which
        is the one question this table exists to answer.
        """
        empresa = _require_text(empresa, field="empresa")
        job_id = _require_text(job_id, field="job_id")
        level = _as_severity(severity)
        ts = self._now(at)
        with self._write_tx() as cur:
            existing = cur.execute(
                "SELECT id FROM incident WHERE empresa = ? AND job_id = ? AND closed_at IS NULL",
                (empresa, job_id),
            ).fetchone()
            if existing is not None:
                return int(existing["id"])
            cur.execute(
                "INSERT INTO incident (empresa, job_id, severity, opened_at, evidence) "
                "VALUES (?, ?, ?, ?, ?)",
                (empresa, job_id, level.value, ts, _clean(evidence)),
            )
            return int(cur.lastrowid or 0)

    def close_incident(self, *, empresa: str, job_id: str, at: datetime | None = None) -> bool:
        """Close the open incident for this job. False if there was none."""
        empresa = _require_text(empresa, field="empresa")
        job_id = _require_text(job_id, field="job_id")
        ts = self._now(at)
        with self._write_tx() as cur:
            cur.execute(
                "UPDATE incident SET closed_at = ? "
                "WHERE empresa = ? AND job_id = ? AND closed_at IS NULL",
                (ts, empresa, job_id),
            )
            return cur.rowcount > 0

    def broken_since(self, *, empresa: str, job_id: str) -> datetime | None:
        """READER — "since when has it been broken?". None when nothing is open."""
        row = self._conn.execute(
            "SELECT opened_at FROM incident "
            "WHERE empresa = ? AND job_id = ? AND closed_at IS NULL "
            "ORDER BY opened_at LIMIT 1",
            (_require_text(empresa, field="empresa"), _require_text(job_id, field="job_id")),
        ).fetchone()
        return None if row is None else _parse_ts(row["opened_at"])

    def incident_count(
        self,
        *,
        empresa: str,
        since: datetime,
        job_id: str | None = None,
        until: datetime | None = None,
    ) -> int:
        """READER — "how many times this week?". Counts openings in the window."""
        params: list[Any] = [_require_text(empresa, field="empresa"), _iso(since)]
        sql = "SELECT COUNT(*) AS n FROM incident WHERE empresa = ? AND opened_at >= ?"
        if until is not None:
            sql += " AND opened_at < ?"
            params.append(_iso(until))
        if job_id is not None:
            sql += " AND job_id = ?"
            params.append(_require_text(job_id, field="job_id"))
        row = self._conn.execute(sql, params).fetchone()
        return int(row["n"])

    def incident_history(
        self,
        *,
        empresa: str,
        job_id: str | None = None,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[IncidentRecord]:
        """READER — the incident history itself, newest first, redacted on read."""
        params: list[Any] = [_require_text(empresa, field="empresa")]
        sql = (
            "SELECT id, empresa, job_id, severity, opened_at, closed_at, evidence "
            "FROM incident WHERE empresa = ?"
        )
        if job_id is not None:
            sql += " AND job_id = ?"
            params.append(_require_text(job_id, field="job_id"))
        if since is not None:
            sql += " AND opened_at >= ?"
            params.append(_iso(since))
        sql += " ORDER BY opened_at DESC, id DESC LIMIT ?"
        params.append(_positive(limit))
        return [
            IncidentRecord(
                id=int(row["id"]),
                empresa=_clean(row["empresa"]),
                job_id=_clean(row["job_id"]),
                severity=_as_severity(str(row["severity"])),
                opened_at=_parse_ts(row["opened_at"]),
                closed_at=None if row["closed_at"] is None else _parse_ts(row["closed_at"]),
                evidence=_clean(row["evidence"]),
            )
            for row in self._conn.execute(sql, params).fetchall()
        ]

    # -- ask_intent_miss ---------------------------------------------------- #

    def record_intent_miss(self, question: str, *, at: datetime | None = None) -> int:
        """Record a question the router could not map. Returns its new hit count."""
        normalized = _normalize_question(question)
        ts = self._now(at)
        with self._write_tx() as cur:
            cur.execute(
                "INSERT INTO ask_intent_miss (question_norm, hits, first_seen_at, last_seen_at) "
                "VALUES (?, 1, ?, ?) "
                "ON CONFLICT(question_norm) DO UPDATE SET "
                "hits = hits + 1, last_seen_at = excluded.last_seen_at",
                (normalized, ts, ts),
            )
            row = cur.execute(
                "SELECT hits FROM ask_intent_miss WHERE question_norm = ?", (normalized,)
            ).fetchone()
            return int(row["hits"])

    def intent_backlog(self, *, limit: int = 20, min_hits: int = 1) -> list[IntentMiss]:
        """READER — the backlog of intents to build, most-asked first."""
        rows = self._conn.execute(
            "SELECT question_norm, hits, first_seen_at, last_seen_at FROM ask_intent_miss "
            "WHERE hits >= ? ORDER BY hits DESC, last_seen_at DESC, question_norm LIMIT ?",
            (_positive(min_hits), _positive(limit)),
        ).fetchall()
        return [
            IntentMiss(
                question=_clean(row["question_norm"]),
                hits=int(row["hits"]),
                first_seen_at=_parse_ts(row["first_seen_at"]),
                last_seen_at=_parse_ts(row["last_seen_at"]),
            )
            for row in rows
        ]

    # -- budget_day --------------------------------------------------------- #

    def reserve_budget(
        self,
        *,
        day: date,
        cap_usd: float,
        estimated_usd: float,
        at: datetime | None = None,
    ) -> BudgetDay:
        """READER + brake — charge the estimate only if it fits under the cap.

        The check and the charge happen in one transaction, and the charge lands
        BEFORE the call: a process that dies mid-call still leaves the cost
        counted (same fail-closed reasoning as the login budget, plan.md §2.1).
        Raises :class:`BudgetExhausted` instead of spending.
        """
        cap = _require_amount(cap_usd, field="cap_usd")
        estimate = _require_amount(estimated_usd, field="estimated_usd")
        key = day.isoformat()
        ts = self._now(at)
        with self._write_tx() as cur:
            row = cur.execute(
                "SELECT costo_usd, calls FROM budget_day WHERE day = ?", (key,)
            ).fetchone()
            spent = float(row["costo_usd"]) if row is not None else 0.0
            calls = int(row["calls"]) if row is not None else 0
            if spent + estimate > cap:
                _log(
                    "state.budget.refused",
                    day=key,
                    spent_usd=round(spent, 6),
                    estimate_usd=round(estimate, 6),
                    cap_usd=round(cap, 6),
                )
                raise BudgetExhausted(
                    f"daily model budget for {key} would be exceeded: "
                    f"spent {spent:.4f} + estimate {estimate:.4f} > cap {cap:.4f} USD"
                )
            cur.execute(
                "INSERT INTO budget_day (day, costo_usd, calls, updated_at) VALUES (?, ?, 1, ?) "
                "ON CONFLICT(day) DO UPDATE SET costo_usd = costo_usd + excluded.costo_usd, "
                "calls = calls + 1, updated_at = excluded.updated_at",
                (key, estimate, ts),
            )
            return BudgetDay(
                day=day,
                costo_usd=spent + estimate,
                calls=calls + 1,
                updated_at=_parse_ts(ts),
            )

    def budget_for(self, day: date) -> BudgetDay:
        """READER — what has been spent today. Zero for a day never charged."""
        row = self._conn.execute(
            "SELECT costo_usd, calls, updated_at FROM budget_day WHERE day = ?",
            (day.isoformat(),),
        ).fetchone()
        if row is None:
            return BudgetDay(day=day, costo_usd=0.0, calls=0, updated_at=None)
        return BudgetDay(
            day=day,
            costo_usd=float(row["costo_usd"]),
            calls=int(row["calls"]),
            updated_at=_parse_ts(row["updated_at"]),
        )

    def budget_remaining(self, day: date, cap_usd: float) -> float:
        """READER — USD still available today (never negative)."""
        cap = _require_amount(cap_usd, field="cap_usd")
        return max(0.0, cap - self.budget_for(day).costo_usd)

    # -- ledger ------------------------------------------------------------- #

    def _append_ledger(
        self,
        cur: sqlite3.Cursor,
        *,
        ts: str,
        actor: str,
        intent: str,
        route: str,
        params: str,
        status: str,
        detail: str,
        ref_seq: int | None,
    ) -> int:
        """Append one hash-chained row inside an open IMMEDIATE transaction."""
        head = cur.execute("SELECT seq, row_hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
        prev_hash = GENESIS_HASH if head is None else str(head["row_hash"])
        seq = 1 if head is None else int(head["seq"]) + 1
        payload = _ledger_payload(
            seq=seq,
            ts=ts,
            actor=actor,
            intent=intent,
            route=route,
            params=params,
            status=status,
            detail=detail,
            ref_seq=ref_seq,
        )
        row_hash = chain_hash(payload, prev_hash)
        try:
            cur.execute(
                "INSERT INTO ledger (seq, ts, actor, intent, route, params, status, detail, "
                "ref_seq, prev_hash, row_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    seq,
                    ts,
                    actor,
                    intent,
                    route,
                    params,
                    status,
                    detail,
                    ref_seq,
                    prev_hash,
                    row_hash,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # Mensaje util sin filtrar contenido: solo el intento referenciado.
            raise StateError(
                f"could not append ledger row (status={status}, ref_seq={ref_seq}): {exc}"
            ) from exc
        return seq

    def record_attempt(
        self,
        *,
        actor: str,
        intent: str,
        route: str,
        params: Mapping[str, Any] | str = "",
        at: datetime | None = None,
    ) -> int:
        """Record a query BEFORE it is made. Returns the ledger sequence number.

        Writing after the call would lose exactly the calls worth auditing: the
        ones that timed out, were refused, or killed the process.
        """
        actor = _require_text(actor, field="actor")
        intent = _require_text(intent, field="intent")
        route = _require_text(route, field="route")
        payload = _dump_params(params)
        ts = self._now(at)
        with self._write_tx() as cur:
            seq = self._append_ledger(
                cur,
                ts=ts,
                actor=actor,
                intent=intent,
                route=route,
                params=payload,
                status=_ATTEMPTED,
                detail="",
                ref_seq=None,
            )
        _log("state.ledger.attempt", seq=seq, actor=actor, intent=intent, route=route)
        return seq

    def record_outcome(
        self,
        seq: int,
        *,
        status: str,
        detail: str = "",
        at: datetime | None = None,
    ) -> int:
        """Append the outcome of attempt ``seq`` as a NEW row.

        Not an UPDATE of the attempt: append-only means the attempt row stays
        exactly as it was written, and the outcome is a second fact.
        """
        if status not in _OUTCOMES:
            raise StateError(f"outcome status must be one of {sorted(_OUTCOMES)}, got {status!r}")
        ts = self._now(at)
        with self._write_tx() as cur:
            attempt = cur.execute(
                "SELECT actor, intent, route, params, status FROM ledger WHERE seq = ?", (seq,)
            ).fetchone()
            if attempt is None or str(attempt["status"]) != _ATTEMPTED:
                raise StateError(f"ledger sequence {seq} is not an open attempt")
            # El intento nunca cambia de estado (es append-only), asi que "ya
            # resuelto" se pregunta a la fila de desenlace, no al intento.
            resolved = cur.execute("SELECT seq FROM ledger WHERE ref_seq = ?", (seq,)).fetchone()
            if resolved is not None:
                raise StateError(
                    f"ledger sequence {seq} already has an outcome (seq {int(resolved['seq'])})"
                )
            outcome_seq = self._append_ledger(
                cur,
                ts=ts,
                actor=str(attempt["actor"]),
                intent=str(attempt["intent"]),
                route=str(attempt["route"]),
                params=str(attempt["params"]),
                status=status,
                detail=_clean(detail),
                ref_seq=seq,
            )
        _log("state.ledger.outcome", seq=outcome_seq, ref_seq=seq, status=status)
        return outcome_seq

    @contextmanager
    def audited_call(
        self,
        *,
        actor: str,
        intent: str,
        route: str,
        params: Mapping[str, Any] | str = "",
    ) -> Iterator[int]:
        """Wrap a call so the attempt is on disk before it starts.

        Usage makes the ordering structural rather than a rule to remember::

            with store.audited_call(actor="agent", intent="venta_dia", route="/api/x"):
                data = client.get(...)
        """
        seq = self.record_attempt(actor=actor, intent=intent, route=route, params=params)
        try:
            yield seq
        except BaseException as exc:
            # Redactar primero y truncar despues (ver _clean): el mensaje de una
            # excepcion de red puede traer la URL entera, cookies incluidas.
            self.record_outcome(seq, status="failure", detail=f"{type(exc).__name__}: {exc}")
            raise
        self.record_outcome(seq, status="success")

    def read_ledger(
        self,
        *,
        limit: int = 100,
        since: datetime | None = None,
        intent: str | None = None,
    ) -> list[LedgerEntry]:
        """READER — raw ledger rows (attempts and outcomes), newest first."""
        params: list[Any] = []
        sql = (
            "SELECT seq, ts, actor, intent, route, params, status, detail, ref_seq, "
            "prev_hash, row_hash FROM ledger WHERE 1 = 1"
        )
        if since is not None:
            sql += " AND ts >= ?"
            params.append(_iso(since))
        if intent is not None:
            sql += " AND intent = ?"
            params.append(_require_text(intent, field="intent"))
        sql += " ORDER BY seq DESC LIMIT ?"
        params.append(_positive(limit))
        return [
            LedgerEntry(
                seq=int(row["seq"]),
                ts=_parse_ts(row["ts"]),
                actor=_clean(row["actor"]),
                intent=_clean(row["intent"]),
                route=_clean(row["route"]),
                params=_clean(row["params"]),
                status=str(row["status"]),
                detail=_clean(row["detail"]),
                ref_seq=None if row["ref_seq"] is None else int(row["ref_seq"]),
                prev_hash=str(row["prev_hash"]),
                row_hash=str(row["row_hash"]),
            )
            for row in self._conn.execute(sql, params).fetchall()
        ]

    def reconstruct_queries(
        self, *, limit: int = 50, since: datetime | None = None
    ) -> list[LedgerQuery]:
        """READER — what the agent actually queried, attempt joined to outcome.

        This is the trace the portal does not keep. An entry still in
        ``attempted`` never got an outcome: the call died mid-flight.
        """
        params: list[Any] = []
        sql = (
            "SELECT a.seq AS seq, a.ts AS ts, a.actor AS actor, a.intent AS intent, "
            "a.route AS route, a.params AS params, o.status AS outcome_status, "
            "o.ts AS outcome_ts, o.detail AS outcome_detail "
            "FROM ledger AS a LEFT JOIN ledger AS o ON o.ref_seq = a.seq "
            "WHERE a.status = ?"
        )
        params.append(_ATTEMPTED)
        if since is not None:
            sql += " AND a.ts >= ?"
            params.append(_iso(since))
        sql += " ORDER BY a.seq DESC LIMIT ?"
        params.append(_positive(limit))
        return [
            LedgerQuery(
                seq=int(row["seq"]),
                ts=_parse_ts(row["ts"]),
                actor=_clean(row["actor"]),
                intent=_clean(row["intent"]),
                route=_clean(row["route"]),
                params=_clean(row["params"]),
                status=_ATTEMPTED if row["outcome_status"] is None else str(row["outcome_status"]),
                detail=_clean(row["outcome_detail"]),
                resolved_at=None if row["outcome_ts"] is None else _parse_ts(row["outcome_ts"]),
            )
            for row in self._conn.execute(sql, params).fetchall()
        ]

    def verify_chain(self) -> ChainReport:
        """READER — recompute the whole hash chain and report the first break.

        The triggers stop an honest client from editing the ledger; the chain
        catches whoever drops the triggers, edits the file, or removes a row.
        The report carries sequence numbers and a reason — never row content.
        """
        rows = self._conn.execute(
            "SELECT seq, ts, actor, intent, route, params, status, detail, ref_seq, "
            "prev_hash, row_hash FROM ledger ORDER BY seq"
        ).fetchall()
        total = len(rows)
        prev_hash = GENESIS_HASH
        expected_seq = 1
        for row in rows:
            seq = int(row["seq"])
            if seq != expected_seq:
                return _broken(total, seq, f"sequence gap: expected {expected_seq}, found {seq}")
            if str(row["prev_hash"]) != prev_hash:
                return _broken(total, seq, "prev_hash does not match the previous row")
            payload = _ledger_payload(
                seq=seq,
                ts=str(row["ts"]),
                actor=str(row["actor"]),
                intent=str(row["intent"]),
                route=str(row["route"]),
                params=str(row["params"]),
                status=str(row["status"]),
                detail=str(row["detail"]),
                ref_seq=None if row["ref_seq"] is None else int(row["ref_seq"]),
            )
            if chain_hash(payload, prev_hash) != str(row["row_hash"]):
                return _broken(total, seq, "row_hash does not match the row content")
            prev_hash = str(row["row_hash"])
            expected_seq += 1
        return ChainReport(ok=True, rows=total)

    def assert_chain_intact(self) -> None:
        """Fail closed when the ledger no longer verifies."""
        report = self.verify_chain()
        if not report.ok:
            raise LedgerTampered(
                f"ledger hash chain broken at seq {report.broken_at}: {report.reason}"
            )


def open_state(
    path: Path | str,
    *,
    create: bool = False,
    clock: Clock = utcnow,
) -> StateStore:
    """Open the state database and verify its schema. Fails closed on both.

    ``create=True`` also applies the migration, so the boot path is one call.
    """
    conn = connect(path, create=create)
    try:
        status = apply_schema(conn) if create else schema_status(conn)
        if not status.up_to_date:
            raise StateError(
                f"state schema at {path} is not usable "
                f"(version={status.version}, expected={status.expected}, "
                f"missing tables={list(status.missing_tables)}, "
                f"missing triggers={list(status.missing_triggers)}) — run "
                f"'uv run python scripts/state_init.py --db {path} --apply'"
            )
    except BaseException:
        conn.close()
        raise
    return StateStore(conn, clock=clock)
