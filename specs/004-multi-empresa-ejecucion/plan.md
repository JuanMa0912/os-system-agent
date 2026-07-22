# Plan — 004 Multi-empresa agent + approved ETL execution

## Architecture

**Per-empresa, co-located.** One independent agent per company, running **on that
company's ETL server**. No agent ever reaches another empresa (no cross-company
SSH keys anywhere). The only shared resource is the operator's Telegram (a group),
reached **outbound** by each agent — the two company networks never interconnect.

```
   Empresa MERCAMIO (server232)              Empresa DINASTIA (su server)
   ├─ ETLs (systemd)                         ├─ ETLs (systemd)
   ├─ etl_monitor (RO) / etl_runner (exec)   ├─ etl_monitor (RO) / etl_runner (exec)
   ├─ os_system_agent  (este repo)           ├─ os_system_agent  (este repo)
   ├─ OpenClaw gateway (local)               ├─ OpenClaw gateway (local)
   └─ bot_mercamio ─┐                        └─ bot_dinastia ─┐
                    └── salida 443 ─► Telegram grupo ◄─ salida 443 ─┘
                                       (operador)
```

Same code, deployed N times. Company identity + jobs come from **per-server
config**, not from any runtime selection (no frontend, OpenClaw does not "select").

## Tooling

The project uses **`uv`** for env/deps on every box — `uv sync` to install, `uv run …`
to run. **No `pip`/`venv` by hand** (uv's venv has no `pip`). systemd units invoke
`uv run python scripts/…`. This holds for Mercamio, Dinastia, and any future empresa.

## Data flow

Core is unchanged; only an empresa label is threaded through:

```
catalog (empresa + jobs) → collect_statuses → JobStatus[] →
render report ("Reporte empresa X") → notify (openclaw message send)
```

Execution adds a second, gated path:

```
approval msg → parse → allowlist(empresa) → dry-run → run as etl_runner →
verify → report → append audit line
```

## Interfaces / data model

- **Catalog** is single-empresa. New **required** top-level `empresa: <name>`
  (fail closed if missing). Each `EtlJob` carries `empresa` (denormalized from the
  top-level value) so report builders label without changing `load_catalog`'s
  return type (`list[EtlJob]`).
- **Reports**: `render_daily_report` / `render_chat_report` gain an `empresa`
  param. Daily gets an `Empresa:` line; chat leads with `Reporte empresa <X>`.
  The existing `OS_SYSTEM_AGENT · ETL …` line is preserved (additive, no break).
- **Execution** (new module `src/os_system_agent/execution/`):
  - `allowlist.yml` per empresa: named actions → exact command + risk + rollback.
  - Approval grammar: `APPROVE os_system_agent <task_id> <action> <empresa> <window>`.
  - `run_action(action, empresa, now, *, dry_run)` — pure gate, then the runner.

## Bot / channel topology (confirmed)

**One shared bot (`cortana`)** delivers every empresa's report to one operator
**group**; messages are told apart by the `Reporte empresa <X>` label. cortana's
token lives in each box's OpenClaw config, and the shared group id is
`OS_TELEGRAM_TARGET`. Simplicity over isolation — a bot-per-empresa (one token per
server) remains an option if stronger isolation is ever wanted. **Zero code
change** — the send path is env-driven and the empresa label is already threaded.

Monitoring source per box: **remote via SSH** when the agent runs off-box (Mercamio:
MMAUTOML01 → `server232`) or **local** (`--server-alias local`) when co-located.

## Required scripts

- **Reuse**: `send_daily_report.py`, `alert_incidents.py`, `collect_etl_status.py`
  (now empresa-labeled).
- **New (Phase 2)**: `execute_action.py` — parse approval, gate against the
  per-empresa allowlist, dry-run or execute, append audit. Defaults to dry-run;
  refuses without a valid, in-window approval.

## Required credentials (never in repo)

Per server, in `.env` (gitignored): `TELEGRAM_BOT_TOKEN` (that empresa's bot),
`OS_TELEGRAM_TARGET` (shared group id), `OS_SERVER_ALIAS` (or "local"). SSH/exec:
`etl_monitor` (read-only) + `etl_runner` (execute), **non-root**, scoped to that
server only.

## Execution trigger (server-side, least privilege)

ETLs (which we build) are made triggerable **without root**: either user-level
systemd units that `etl_runner` starts (`systemctl --user start <job>`), or a
single narrow sudoers entry limited to the exact `systemctl start <job>.service`.
No root, no free-form command. Chosen per empresa at deploy, documented in
`operations-runbook.md`.

## Observability / logging

- Structured, redacted stderr lines (as today).
- **Audit ledger**: append-only `audit-ledger.jsonl` (already gitignored) — one
  line per approval / dry-run / execute / verify: `ts, empresa, task_id, action,
  command, outcome, trace_id`, all redacted.

## Failure modes

- Missing `empresa` / catalog / approval → fail closed (refuse).
- Approval malformed / expired / wrong-empresa / not on allowlist → refuse + log.
- Runner error → report failure, keep audit, do **not** mark success.
- Telegram unreachable → daily/alert already fail closed (retry next run).

## Rollback plan

- The multi-empresa label change is **additive and reversible** (revert 3 source
  files + the example).
- Execution ships **behind dry-run**; enabling real runs is a per-empresa config
  switch. Each allowlisted action defines its own rollback/verify.

## Tests

- Catalog: `empresa` required (fail closed); example loads with empresa.
- Reports: daily shows `Empresa:`; chat shows `Reporte empresa X`; existing
  header + redaction assertions still hold.
- Execution (Phase 2): approval parser (valid / malformed / expired /
  wrong-empresa); allowlist enforcement (named-only); dry-run renders command +
  rollback; audit line written + redacted; refuse-without-approval.

## Manual verification checklist

- [ ] Two `.env`/catalog sets produce two labeled reports in one Telegram group.
- [ ] An agent has no config path to another empresa's server.
- [ ] `execute_action.py` without approval → dry-run only.
- [ ] A non-allowlisted action → refused and audited.
- [ ] No secret in any report / alert / audit line.
