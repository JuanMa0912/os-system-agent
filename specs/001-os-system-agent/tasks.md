# Tasks — 001 OS_SYSTEM_AGENT

## T001 — Create project skeleton

Risk: low  
Approval required: no

- Create folders.
- Add CLAUDE.md.
- Add requirements docs.
- Add config examples.

Verification:

- Repository tree exists.

## T002 — Install OpenClaw in WSL2

Risk: medium  
Approval required: yes before installing packages

Verification:

- `openclaw gateway status`
- dashboard local only

**Status: DONE.** OpenClaw `2026.6.11` on WSL2 Ubuntu 24.04 (Node 24). Gateway
bound to `127.0.0.1:18789`, systemd user service, Tailscale off, no channels.
Model provider: Ollama Cloud (`ollama-cloud/gpt-oss:120b`, free tier — local
model not viable: no GPU / ~8 GB RAM). See `docs/openclaw-phase1-runbook.md`.

## T003 — Configure Telegram test alert

Risk: low  
Approval required: no, if token already supplied securely

Verification:

- Test alert received.

**Status: DONE.** Bot added via `openclaw channels add`, running in polling mode.
Operator paired and locked down: `channels.telegram.allowFrom` + auto-set
`commands.ownerAllowFrom` = single operator id. Round-trip verified (operator DM
→ gpt-oss reply). `security audit --deep` still 0 critical. Known accepted warn:
`probe_failed` (deep-probe operator scope, not a vuln). See
`docs/openclaw-phase1-runbook.md` §8.

## T004 — Configure SSH alias server232

Risk: medium  
Approval required: yes

Verification:

- `ssh server232 hostname`
- no password prompt
- non-root user

## T005 — Build read-only healthcheck

Risk: low  
Approval required: no after SSH is configured

Verification:

- outputs hostname/date/uptime/disk/service status

**Status: PARTIAL.** Building block done: `ssh_client.run_read_only()` runs an
allowlist-verified command over SSH (BatchMode, fails closed via
`assert_read_only`). Wiring the healthcheck script to a live server lands with
M4 (real SSH access + `etl_monitor` user).

## T006 — Build ETL freshness collector

Risk: low  
Approval required: no

Verification:

- mocked folder/log tests pass
- real read-only check returns status

**Status: DONE (dry-run).** `catalog.py` (fail-closed job-catalog loader),
`monitors/freshness.py` (pure `evaluate_freshness` → severity), and
`scripts/collect_etl_status.py` / `check_etl_freshness.py` (dry-run by default).
**Live read-only collection implemented** via `monitors/systemd.py`:
`--live` runs `systemctl show <unit>` over the read-only SSH allowlist, parses
`Result`/`ExecMainExitTimestamp`, and classifies freshness. Verified end-to-end
against the real ETL server (`etl_monitor` user, key auth).

## T007 — Build daily report

Risk: low  
Approval required: no

Verification:

- report generated locally
- secrets redacted

**Status: DONE.** `reports/daily.py` renders the §13 daily report and incident
alert; all evidence passes through `redact()`; CRITICAL marks "Requires approval:
yes". Verified by tests + `collect_etl_status.py` dry-run.

## T008 — Build approval parser

Risk: medium  
Approval required: no for parser; yes for real execution

Verification:

- rejects vague approvals
- accepts exact approval format
- logs trace ID

## T009 — Add controlled execution allowlist

Risk: high  
Approval required: yes

Verification:

- dry-run first
- only allowlisted command executes
- audit ledger captures result

## T010 — Run OpenClaw security audit

Risk: low  
Approval required: no

Verification:

- no critical findings before enabling channels

**Status: DONE.** `openclaw security audit --deep` = **0 critical**. Hardening:
web/browser tools denied, insecure Control UI auth off, memory-search off,
unused skills disabled. Accepted warns: `weak_tier` (cost trade-off),
`trusted_proxies` (local-only), `probe_failed` (clears when the command owner is
set in T003). Sandbox `off` (no Docker WSL integration) — re-enable before
Phase 2 / channels. See `docs/openclaw-phase1-runbook.md`.
