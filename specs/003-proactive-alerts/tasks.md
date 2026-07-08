# Tasks — 003 Proactive incident alerts

## T301 — Shared notify module

Risk: low · Approval: no

Refactor the send path out of the daily script into `os_system_agent/notify.py`
(`split_message`, `send_via_openclaw`, `default_sender`, `send_chunked`) so the
push and the alerts share one, tested implementation.

**Status: DONE.** `send_daily_report.py` re-exports from `notify`; its tests
still pass unchanged.

## T302 — Alerting logic (alert-on-change)

Risk: low · Approval: no

`os_system_agent/alerting.py`: `incident_statuses`, `diff_incidents` (new /
escalated / recovered vs. previous state), `render_alert` (compact, redacted),
`server_down_status` (synthetic CRITICAL). Pure, no I/O.

**Status: DONE.** Unit-tested (new incident, unchanged, escalation, recovery,
render).

## T303 — Orchestrator script

Risk: low · Approval: no

`scripts/alert_incidents.py`: connectivity probe (`hostname`) → server-down
incident if unreachable; else live collect; load state file; diff; send on
change; save state. Dry-run default; fail-closed on missing target (state not
advanced). State in `.alert-state.json` (gitignored).

**Status: DONE.** Orchestration unit-tested with a mocked collector + injected
sender (sends-once-then-silent, recovery, dry-run, fail-closed). 110 tests total.

## T304 — Deploy on MMAUTOML01 (box, guided)

Risk: low · Approval: no (read-only monitoring + notifications)

Install `config/systemd/os-system-agent-alerts.{service,timer}.example` as USER
units (venv python, cwd=repo, env target). Timer every ~2h. Verify: force a
one-shot run and confirm behavior (alert on incident, silence when unchanged).

**Status: PENDING (box).**

## Verification checklist (box)

- [ ] Dry-run once: `python scripts/alert_incidents.py --catalog config/alert-rules.yml`
      (prints "no change" when healthy, or the alert text if something is off).
- [ ] Install the timer; `systemctl --user list-timers | grep alerts`.
- [ ] Optional real test: temporarily tighten a job's threshold (or stop the
      server) to force an incident, confirm one alert arrives, and that a second
      run stays silent. Revert the threshold.
