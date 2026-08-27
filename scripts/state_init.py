#!/usr/bin/env python3
"""state_init.py — create or verify the agent's persistent memory (spec 006, T10).

Two modes, and neither is the default: you must pass one.

* ``--check`` opens the database **read-only** (``mode=ro``, so the connection
  cannot modify a single row) and reports what it finds — schema version, missing
  tables or triggers, and whether the append-only ledger's hash chain still
  verifies. SQLite may still create the ``-wal``/``-shm`` sidecars it needs to
  read a WAL database; the database itself is left byte-for-byte identical.
* ``--apply`` creates ``var/state.db`` (mode ``0600``) if absent and applies
  ``schema.sql``. The migration is idempotent: running it twice is a no-op.

Exit codes: ``0`` all good · ``1`` action needed (schema missing/outdated, or a
broken ledger chain) · ``2`` the store could not be opened or migrated.

Examples::

    uv run python scripts/state_init.py --db var/state.db --apply
    uv run python scripts/state_init.py --db var/state.db --check --json
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from os_system_agent.state import (
    SCHEMA_VERSION,
    SchemaStatus,
    StateError,
    StateStore,
    apply_schema,
    connect,
    connect_readonly,
    schema_status,
)

DEFAULT_DB = Path("var/state.db")
REPO_ROOT = Path(__file__).resolve().parent.parent

# Lo que `--check` espera encontrar cuando la base todavia no existe.
REQUIRED_TABLES = ("schema_meta", "incident", "ask_intent_miss", "budget_day", "ledger")
REQUIRED_TRIGGERS = ("ledger_no_update", "ledger_no_delete")


def _is_git_ignored(db_path: Path) -> bool | None:
    """Heuristic: does ``.gitignore`` cover this database?

    Not a replacement for ``git check-ignore`` — it is a cheap warning so a file
    holding business figures and query history does not land in a commit by
    accident. Returns ``None`` when the question does not apply (the database
    lives outside the repository).
    """
    try:
        relative = db_path.resolve().relative_to(REPO_ROOT)
    except ValueError:
        return None
    gitignore = REPO_ROOT / ".gitignore"
    if not gitignore.is_file():
        return False
    rel_posix = relative.as_posix()
    candidates = [rel_posix, db_path.name, *(p.as_posix() for p in relative.parents if p.name)]
    for raw_line in gitignore.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        pattern = line.rstrip("/").removeprefix("./")
        if any(fnmatch.fnmatch(candidate, pattern) for candidate in candidates):
            return True
    return False


def _chain_state(conn: sqlite3.Connection, status: SchemaStatus) -> tuple[bool | None, str | None]:
    """Verify the ledger chain when there is a ledger to verify."""
    if "ledger" in status.missing_tables:
        return None, None
    report = StateStore(conn).verify_chain()
    return report.ok, report.reason


def _report(
    *,
    action: str,
    db_path: Path,
    status: SchemaStatus,
    chain_ok: bool | None,
    chain_reason: str | None,
) -> dict[str, Any]:
    return {
        "action": action,
        "db": str(db_path),
        "schema_version": status.version,
        "expected_version": status.expected,
        "missing_tables": list(status.missing_tables),
        "missing_triggers": list(status.missing_triggers),
        "up_to_date": status.up_to_date,
        "chain_ok": chain_ok,
        "chain_reason": chain_reason,
        "git_ignored": _is_git_ignored(db_path),
    }


def _print_human(result: dict[str, Any]) -> None:
    print(f"[state_init] db={result['db']} action={result['action']}")
    print(
        f"[state_init] schema version={result['schema_version']} "
        f"expected={result['expected_version']} up_to_date={result['up_to_date']}"
    )
    if result["missing_tables"] or result["missing_triggers"]:
        print(
            f"[state_init] missing tables={result['missing_tables']} "
            f"triggers={result['missing_triggers']}"
        )
    if result["chain_ok"] is None:
        print("[state_init] ledger chain: not verified (no ledger table yet)")
    elif result["chain_ok"]:
        print("[state_init] ledger chain: intact")
    else:
        print(f"[state_init] ledger chain: BROKEN — {result['chain_reason']}", file=sys.stderr)


def _warn_if_committable(result: dict[str, Any]) -> None:
    """The database holds business figures and the query trail; it is not repo content."""
    if result["git_ignored"] is False:
        print(
            f"[state_init] WARNING: {result['db']} is inside the repository and .gitignore "
            "does not appear to cover it. Add a rule (e.g. 'var/') before committing.",
            file=sys.stderr,
        )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="path to the state database")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="verify only; writes nothing")
    mode.add_argument("--apply", action="store_true", help="create and migrate (idempotent)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path: Path = args.db

    if args.check and not db_path.exists():
        # No es un error del comando: es la respuesta. `--check` sobre una base que
        # aun no existe informa "hay que aplicar", y sigue sin escribir nada.
        missing = SchemaStatus(
            exists=False,
            version=None,
            expected=SCHEMA_VERSION,
            missing_tables=REQUIRED_TABLES,
            missing_triggers=REQUIRED_TRIGGERS,
        )
        result = _report(
            action="check",
            db_path=db_path,
            status=missing,
            chain_ok=None,
            chain_reason=None,
        )
        if args.json:
            print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        else:
            print(f"[state_init] db={db_path} action=check")
            print("[state_init] state database does not exist yet; run --apply to create it.")
        return 1

    try:
        if args.apply:
            conn = connect(db_path, create=True)
            try:
                status = apply_schema(conn)
                chain_ok, chain_reason = _chain_state(conn, status)
            finally:
                conn.close()
            action = "apply"
        else:
            conn = connect_readonly(db_path)
            try:
                status = schema_status(conn)
                chain_ok, chain_reason = _chain_state(conn, status)
            finally:
                conn.close()
            action = "check"
    except StateError as exc:
        print(f"[state_init] {exc}", file=sys.stderr)
        return 2

    result = _report(
        action=action,
        db_path=db_path,
        status=status,
        chain_ok=chain_ok,
        chain_reason=chain_reason,
    )

    if args.json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    else:
        _print_human(result)
    _warn_if_committable(result)

    # Fail closed: an outdated schema or a broken chain is "action needed", not OK.
    return 0 if result["up_to_date"] and result["chain_ok"] is not False else 1


if __name__ == "__main__":
    raise SystemExit(main())
