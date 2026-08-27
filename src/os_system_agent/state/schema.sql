-- Persistent agent memory (spec 006, plan.md §7).
--
-- Idempotent by construction: every object is CREATE ... IF NOT EXISTS, so
-- applying this file twice is a no-op and the migration can be re-run safely.
--
-- PRAGMAs (journal_mode=WAL, foreign_keys=ON) are NOT here on purpose: they are
-- per-connection settings, so they live in store.py where every connection is
-- opened. Putting them here would only configure the connection that migrates.
--
-- Every timestamp column is UTC ISO-8601 ("2026-08-25T13:04:00+00:00"), written
-- by store.py. Every free-text column is already redacted on write; store.py
-- redacts again on read, because a row written by an older version of the
-- redactor is exactly what a write-only guard would let through.

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- incident — history with an opening and a closing.
-- Reader: "since when has it been broken?" and "how many times this week?".
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS incident (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    empresa   TEXT NOT NULL,
    job_id    TEXT NOT NULL,
    severity  TEXT NOT NULL CHECK (severity IN ('INFO', 'WARNING', 'CRITICAL', 'SECURITY')),
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    evidence  TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_incident_lookup
    ON incident (empresa, job_id, opened_at);

-- Un solo incidente abierto por (empresa, job). El indice parcial lo hace
-- imposible en el motor, no por convencion: si el codigo se equivoca y abre dos,
-- la insercion falla en vez de partir en dos la historia de "desde cuando".
CREATE UNIQUE INDEX IF NOT EXISTS idx_incident_one_open
    ON incident (empresa, job_id) WHERE closed_at IS NULL;

-- ---------------------------------------------------------------------------
-- ask_intent_miss — questions the router could not map to an intent.
-- Reader: the backlog of intents to build, ordered by real demand.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ask_intent_miss (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    question_norm TEXT NOT NULL UNIQUE,
    hits          INTEGER NOT NULL DEFAULT 1 CHECK (hits >= 1),
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_intent_miss_demand
    ON ask_intent_miss (hits DESC, last_seen_at DESC);

-- ---------------------------------------------------------------------------
-- budget_day — model spend per business day.
-- Reader: stops the turn BEFORE spending, not after (plan.md §8).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS budget_day (
    day        TEXT PRIMARY KEY,
    costo_usd  REAL NOT NULL DEFAULT 0.0 CHECK (costo_usd >= 0.0),
    calls      INTEGER NOT NULL DEFAULT 0 CHECK (calls >= 0),
    updated_at TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- ledger — append-only record of every query the agent makes.
--
-- The portal audits sessions and logins but NOT queries (spec §3), so this is
-- the only trace of what the agent looked at. Two properties make it evidence
-- instead of a log file:
--
--   1. Append-only, enforced by triggers below — not by a code convention.
--   2. Hash-chained: row_hash = sha256(prev_hash || canonical content), so
--      altering or removing a row breaks every hash after it.
--
-- An attempt is written BEFORE the call (status 'attempted'); the outcome is a
-- SEPARATE row referencing it (ref_seq), because updating the attempt row in
-- place is exactly what append-only forbids. A row that never got its outcome
-- row is a call that died mid-flight — and that is a fact worth keeping.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ledger (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    actor     TEXT NOT NULL,
    intent    TEXT NOT NULL,
    route     TEXT NOT NULL,
    params    TEXT NOT NULL,
    status    TEXT NOT NULL CHECK (status IN ('attempted', 'success', 'failure')),
    detail    TEXT NOT NULL DEFAULT '',
    ref_seq   INTEGER REFERENCES ledger (seq),
    prev_hash TEXT NOT NULL,
    row_hash  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ledger_ts ON ledger (ts);

-- Un intento tiene como mucho un desenlace. Sin este indice, dos filas de exito
-- para el mismo intento darian dos historias distintas de la misma consulta.
CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_one_outcome
    ON ledger (ref_seq) WHERE ref_seq IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS ledger_no_update
BEFORE UPDATE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'ledger is append-only: UPDATE is forbidden');
END;

CREATE TRIGGER IF NOT EXISTS ledger_no_delete
BEFORE DELETE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'ledger is append-only: DELETE is forbidden');
END;
