# Spec — 003 Proactive incident alerts

## Problem

The daily push (spec/M5) is a digest at 08:30. If an ETL fails — or the server
goes down — at 3pm, the operator does not find out until the next morning. We
want an **out-of-band alert the moment something breaks**, delivered to Telegram,
**without alert fatigue** (no repeating the same alert every check).

This closes Phase 1: it turns a passive daily report into active monitoring. It
stays **read-only** — it detects and reports; it never executes anything.

## Actors

- **Human operator (Juan)** — the single alert recipient (owner allowlist).
- **OS_SYSTEM_AGENT** — the alert job (systemd timer + Python).
- **server232** — read-only source (`etl_monitor`, no sudo).
- **Telegram channel** — alert transport (`openclaw message send`).

## User journeys

1. **An ETL fails / stalls.** A job crosses its freshness threshold or exits
   non-zero → the operator gets ONE alert naming the job and the evidence.
2. **The server is unreachable.** SSH to `server232` fails → the operator gets
   ONE clear "servidor no responde" alert (not 7 confusing per-job criticals).
3. **Recovery.** A previously-alerting job (or the server) is healthy again →
   the operator gets a short "recuperado" notice.
4. **Steady state (healthy or unchanged).** Nothing changed since the last check
   → **no message** (silence is the norm).

## Functional requirements

- **FR-1** On a schedule (every 1-2h), live-check ETL status read-only.
- **FR-2** Alert only on **change**: a new incident, an **escalation**
  (WARNING→CRITICAL), or a **recovery**. Unchanged state → no message.
- **FR-3** Treat an unreachable server as a single, distinct CRITICAL alert.
- **FR-4** Alerts are compact and redacted (same style as the daily push).
- **FR-5** Deliver only to the configured operator/target.

## Non-functional requirements

- **NFR-1 No fatigue** — a persistent incident is alerted once, not every run.
- **NFR-2 Deterministic** — no model in the loop; pure Python + one SSH call +
  one `openclaw message send`. (This is why it cannot run away like the model
  pull did.)
- **NFR-3 Fail closed** — dry-run unless `--send`; refuse to send without a
  target; on a send/target failure, do **not** advance state (so the alert
  retries next run).
- **NFR-4 Cheap** — one batched `systemctl show` (all units in one SSH call).

## Security boundaries

- **Read-only.** Same allowlisted SSH path as the monitor; no execution, no
  state change on the server.
- **Owner-only delivery.** Target id comes from env/timer on the box, never
  committed. No new inbound exposure.
- **No secrets** in alerts or logs (evidence goes through `redact`).

## Acceptance criteria

- **AC-1** A newly-broken/stale job produces exactly one alert; a second check
  with the same state produces none.
- **AC-2** An escalation (WARNING→CRITICAL) produces a fresh alert.
- **AC-3** A recovery produces a "recuperado" notice and clears that job's state.
- **AC-4** Server unreachable → one "servidor no responde" CRITICAL alert.
- **AC-5** `--send` without a target returns non-zero and sends nothing, without
  advancing state.
- **AC-6** No secret appears in any alert or log line.

## Out of scope

- Any execution / rerun (Phase 2, approval-gated).
- Alert routing / escalation policies, on-call schedules (Phase 3).
- Per-job custom alert channels; WhatsApp.
