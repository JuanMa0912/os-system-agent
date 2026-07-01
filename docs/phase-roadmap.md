# Phase Roadmap

## Phase 1 — Local monitor

Goal: visibility without autonomous production writes.

Deliverables:

- WSL2 OpenClaw install.
- Telegram alerts.
- SSH read-only checks.
- ETL report.

## Phase 2 — Approved execution

Goal: allow controlled reruns with human approval.

Deliverables:

- approval parser
- command allowlist
- dry-run
- audit ledger

## Phase 3 — Observability

Goal: metrics, dashboards, history.

Deliverables:

- Prometheus/Grafana or equivalent
- alert routing
- SLA dashboard

## Phase 4 — Hardened deployment

Goal: always-on secure runtime.

Deliverables:

- Linux VM
- systemd service
- Tailscale/VPN
- firewall
- sandboxed agents

## Phase 5 — Intelligent operations

Goal: diagnostic recommendations and anomaly detection.

Deliverables:

- root cause summaries
- historical anomaly detection
- runbook generation
