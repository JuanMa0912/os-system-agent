"""Persistent agent memory (spec 006, plan.md §7).

Re-exports the public surface of :mod:`os_system_agent.state.store` so callers
write ``from os_system_agent.state import open_state`` and never have to know
which module holds which reader.
"""

from __future__ import annotations

from os_system_agent.state.store import (
    DB_MODE,
    GENESIS_HASH,
    SCHEMA_PATH,
    SCHEMA_VERSION,
    BudgetDay,
    BudgetExhausted,
    ChainReport,
    IncidentRecord,
    IntentMiss,
    LedgerEntry,
    LedgerQuery,
    LedgerTampered,
    SchemaStatus,
    StateError,
    StateStore,
    apply_schema,
    chain_hash,
    connect,
    connect_readonly,
    open_state,
    schema_status,
    utcnow,
)

__all__ = [
    "DB_MODE",
    "GENESIS_HASH",
    "SCHEMA_PATH",
    "SCHEMA_VERSION",
    "BudgetDay",
    "BudgetExhausted",
    "ChainReport",
    "IncidentRecord",
    "IntentMiss",
    "LedgerEntry",
    "LedgerQuery",
    "LedgerTampered",
    "SchemaStatus",
    "StateError",
    "StateStore",
    "apply_schema",
    "chain_hash",
    "connect",
    "connect_readonly",
    "open_state",
    "schema_status",
    "utcnow",
]
