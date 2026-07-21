# Spec — 004 Multi-empresa agent + approved ETL execution

## Problem

Today the system monitors a single server (`server232`) **read-only** and reports
to Telegram. Two things change now:

1. **Multi-empresa.** We will operate the same agent across several companies
   (Mercamio, Dinastia, …). Each company has its own ETL server that **we own
   and build the ETLs on** — not an opaque third party.
2. **Execution.** We go beyond read-only: the agent must be able to **run / re-run
   the ETLs it monitors** and report the outcome — *without* giving up the safety
   model (approval, allowlist, dry-run, audit, non-root).

This opens **Phase 2** (controlled execution), which 001–003 explicitly deferred.

## Design decisions (confirmed — hard constraints)

- **Topology: one agent per empresa, co-located on that empresa's server.** Each
  agent only ever touches its **own** server. No box holds SSH keys or config for
  any other empresa (compromise of one ≠ access to another). Monitoring is **local**
  (no cross-company SSH).
- **Reporting: per-empresa messages to a shared Telegram chat**, each labeled
  `Reporte empresa <X>` (e.g. `Reporte empresa Mercamio`, `Reporte empresa Dinastia`).
  A single combined/aggregated report is explicitly **not** the model.
- **Execution is included**, but only as **named, allowlisted actions** gated by an
  explicit human approval — never free-form remote commands.

## Actors

- **Human operator (Juan)** — single report recipient **and the only approver** of
  executions.
- **OS_SYSTEM_AGENT (per empresa)** — a co-located instance that monitors and, on
  valid approval, executes local ETLs.
- **OpenClaw Gateway (per empresa)** — local, loopback-bound.
- **Company ETL server** — e.g. `server232` = Mercamio, `<dinastia_host>` = Dinastia.
  Hosts the ETLs; `etl_monitor` (read-only) for checks, `etl_runner` (**non-root**)
  for approved execution.
- **Telegram channel** — shared transport; every message labeled by empresa.

## User journeys

1. **Per-empresa daily report.** Each empresa's agent sends `Reporte empresa <X>`
   with its jobs' status (start/finish/freshness/paths).
2. **"Revisar rutas" (path/freshness check).** The agent verifies expected
   output/log paths exist and are fresh at the configured locations.
3. **Incident alert.** Per empresa, change-based (reuse 003), labeled by empresa.
4. **Approved execution.** The operator sends an `APPROVE …` message naming an
   exact action, empresa and time window. The agent validates it against the
   **per-empresa execution allowlist**, runs a **dry-run**, executes as
   `etl_runner`, verifies the result, reports back, and writes an **audit entry**.
5. **Refused execution.** A request that is malformed, expired, wrong-empresa, or
   not on the allowlist is **refused and logged** — nothing runs.

## Functional requirements

### Monitoring / report (multi-empresa)

- **FR-1** Each agent knows its `empresa` (config) and labels every report/alert
  `empresa <X>`.
- **FR-2** Monitor local ETL status (systemd + paths) with **no** cross-empresa
  access.
- **FR-3** *Revisar rutas*: verify expected output/log paths exist and are fresh
  per job.
- **FR-4** Per-empresa daily report + change-based incident alerts (reuse 002/003)
  to the shared Telegram chat.

### Execution (Phase 2, approval-gated)

- **FR-5** A **per-empresa execution allowlist**: exact, named actions
  (e.g. `rerun daily_sales`) each mapped to an exact command. Anything not on it is
  refused.
- **FR-6** **Approval parser**: accept only
  `APPROVE os_system_agent <task_id> <action> <empresa> <time_window>`; reject
  malformed, expired, or empresa-mismatched approvals.
- **FR-7** **Dry-run first**: show command, expected impact, rollback/verify, and
  risk level before executing.
- **FR-8** Execute as **non-root** `etl_runner`; never root, never `sudo` beyond a
  single narrow allowlisted trigger.
- **FR-9** **Audit ledger**: every approval, dry-run, execution and verification is
  appended (append-only, redacted).
- **FR-10** **Post-exec verification + report**: after running, re-check status and
  report success/failure (with evidence, redacted).

## Non-functional requirements

- **NFR-1 Isolation** — no agent holds credentials/paths for another empresa.
- **NFR-2 Least privilege** — reads as `etl_monitor`; execution as `etl_runner`
  with the narrowest possible trigger (a user-level systemd unit, or a
  single-command sudoers entry — decided in `plan.md`).
- **NFR-3 Fail closed** — missing config/approval/allowlist → refuse. Execution
  defaults to **dry-run** without an explicit, valid, in-window approval.
- **NFR-4 Auditable + reversible** — every execution has an audit entry and a
  documented rollback/verify step.
- **NFR-5 No secrets** in reports, alerts, audit, approvals, or logs.
- **NFR-6 Deterministic safety layer** — the
  approval → allowlist → dry-run → execute path is pure code; no model decides
  whether to run.

## Security boundaries

- **Execution is the highest-risk surface.** It is OFF unless: valid approval
  **AND** on the per-empresa allowlist **AND** within the time window.
- **The approval message is not a shell.** It *selects a named allowlisted action*;
  it never carries a raw command.
- If executions are triggered via Telegram, that channel requires **owner-only
  allowlist + mention gating + OpenClaw sandbox** (CLAUDE.md §8/§9).
- **No cross-empresa reach** — each `~/.ssh`, each `.env`, each catalog is
  single-empresa.
- `etl_runner` **cannot become root**; the ETL trigger is a fixed command surface.
- Same redaction as 001–003 everywhere (reports, alerts, audit).

## Acceptance criteria

- **AC-1** Two empresas configured → each sends its own `Reporte empresa <X>` to
  the same Telegram, correctly labeled.
- **AC-2** A *revisar rutas* check flags a missing/stale expected output as
  WARNING/CRITICAL per thresholds.
- **AC-3** An agent cannot read or execute anything on another empresa's server
  (no config path to it exists).
- **AC-4** An approved, allowlisted action runs the full chain: dry-run shown →
  executed as `etl_runner` → verified → reported → audit entry written.
- **AC-5** A malformed / expired / wrong-empresa / non-allowlisted execution
  request is refused, **nothing runs**, and the refusal is logged.
- **AC-6** An execution attempt without a valid approval defaults to **dry-run
  only**.
- **AC-7** No secret appears in any report, alert, audit line, or log.

## Out of scope

- Central multi-empresa aggregation (a single combined report) — rejected in favor
  of per-empresa agents.
- Cross-empresa dashboards / SLA history (Phase 3).
- Autonomous execution **without** human approval.
- Arbitrary / free-form remote command execution (only **named** allowlisted
  actions).
- WhatsApp; per-job custom channels.
