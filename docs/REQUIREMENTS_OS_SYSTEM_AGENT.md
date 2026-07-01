# Requirements — OS_SYSTEM_AGENT for OpenClaw ETL Operations

## 1. Purpose

Build a controlled agent operating layer that monitors and assists ETL/pipeline operations without requiring routine manual login to production servers.

Phase 1 runs locally on a Windows computer through WSL2. The system will install and configure OpenClaw, connect to `server232` through SSH, validate ETL execution and data upload status, generate reports, and notify the operator through Telegram. WhatsApp is optional for later phases.

---

## 2. Scope

### In scope — Phase 1

- Install OpenClaw in WSL2.
- Configure OpenClaw Gateway locally.
- Configure Claude Code project instructions.
- Create a spec-driven project structure.
- Create read-only SSH monitor for `server232`.
- Collect ETL status from logs/files/services/tables.
- Send Telegram alerts.
- Generate daily ETL status reports.
- Create security runbooks.
- Require manual approval for execution actions.

### Out of scope — Phase 1

- Fully autonomous production execution.
- Publicly exposed OpenClaw Gateway.
- Personal WhatsApp automation.
- Unreviewed ClawHub skills.
- Destructive remediation.
- Root-level server operations.
- Database write operations without approval.
- Replacing existing ETL scheduler before observation is stable.

---

## 3. Functional requirements

### FR-001 — Local OpenClaw gateway

The system shall run OpenClaw Gateway locally inside WSL2 or a controlled local runtime.

Acceptance criteria:

- `openclaw gateway status` returns healthy.
- Control UI is reachable only from localhost or approved private network.
- Gateway auth is enabled.
- Security audit is reviewed.

### FR-002 — Server232 read-only connection

The system shall connect to `server232` using an SSH alias and a non-root monitoring user.

Acceptance criteria:

- SSH key is outside repo.
- User cannot run arbitrary privileged commands.
- Connection timeout is configured.
- Read-only health check succeeds.

### FR-003 — ETL job catalog

The system shall maintain a catalog of monitored ETL jobs.

Each job must define:

- job id
- name
- server
- schedule
- expected input path
- expected output path
- log path
- success pattern
- failure pattern
- freshness rule
- destination validation query
- alert thresholds
- owner/contact

Acceptance criteria:

- At least one job can be configured.
- Missing config fails closed.
- Config can be validated locally.

### FR-004 — ETL freshness check

The system shall determine if expected files/tables/logs are fresh.

Acceptance criteria:

- Freshness rule can check file modification time.
- Freshness rule can check log success timestamp.
- Freshness rule can check destination row count or max date when DB access is configured.
- Late data produces WARNING or CRITICAL depending on threshold.

### FR-005 — ETL completion report

The system shall produce a human-readable daily report.

Acceptance criteria:

- Report includes job status, evidence, failures, and recommendations.
- Report redacts secrets.
- Report can be sent to Telegram.
- Report can be saved locally.

### FR-006 — Telegram alerting

The system shall send alerts to an approved Telegram chat.

Acceptance criteria:

- Bot token is read from environment/secret store.
- Chat ID is not hardcoded in committed code.
- Test alert can be sent.
- Alerts include severity, evidence, and recommended action.

### FR-007 — Approval-gated execution

The system shall not execute ETL reruns or server modifications without explicit approval.

Acceptance criteria:

- Approval parser requires exact action.
- Dry-run is shown before action.
- Execution is logged with timestamp, actor, command, and result.
- Unapproved risky command is refused.

### FR-008 — Audit ledger

The system shall maintain an audit log of agent actions.

Acceptance criteria:

- Each action has a trace ID.
- Command, server, actor, risk level, approval status, stdout/stderr summary, and result are captured.
- Secrets are redacted.

---

## 4. Non-functional requirements

### NFR-001 — Security

- Least privilege.
- Fail closed.
- No secrets in repo.
- Sandbox agent tool execution.
- Allowlist channels.
- Restrict group chats.
- Audit OpenClaw config.
- Pin and review skills/plugins.

### NFR-002 — Reliability

- Health checks must have timeouts.
- Agent must handle server unreachable state.
- Alerts must deduplicate repeated noise.
- Reports must still generate with partial data.

### NFR-003 — Observability

- Structured logs.
- Metrics-ready format.
- OpenTelemetry/Prometheus optional in Phase 1.
- Dashboard optional after stable data collection.

### NFR-004 — Maintainability

- Spec-driven docs.
- Small testable modules.
- Clear runbooks.
- Version-controlled config examples.
- No hardcoded environment details.

### NFR-005 — Portability

- Phase 1 works in WSL2.
- Later migration to Linux VM should require minimal changes.
- Avoid Windows-only code.

---

## 5. Required local tools

- Windows with WSL2.
- Ubuntu in WSL2.
- Git.
- Node.js version supported by current OpenClaw docs.
- OpenClaw CLI.
- Claude Code CLI.
- Python 3.11+.
- Docker Desktop or Docker Engine if sandbox backend is Docker.
- SSH client.
- Optional: Prometheus, Grafana, Alertmanager.
- Optional: Tailscale for private remote access.

---

## 6. Required secrets

Do not commit these:

```text
ANTHROPIC_API_KEY or model provider auth
OPENCLAW_GATEWAY_TOKEN
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
SERVER232_SSH_PRIVATE_KEY
DB connection strings
GCP credentials
WhatsApp/Meta tokens
```

Use `.env.example` only with placeholder values.

---

## 7. Server232 requirements

Minimum server-side requirements:

- SSH enabled.
- Dedicated non-root user `etl_monitor`.
- SSH key auth.
- Read access to ETL logs and relevant folders.
- Read-only database user if DB validation is needed.
- Optional controlled execution user `etl_runner`.
- No broad sudo.
- Log retention policy.

Recommended allowlisted checks:

- service/timer status
- recent logs
- file timestamps
- disk usage
- job output paths
- database max date/count validation

---

## 8. WhatsApp requirements for later phase

WhatsApp must not be enabled casually.

Before using WhatsApp:

- Use dedicated assistant number.
- Use allowlisted sender(s).
- Use allowlisted group(s).
- Require mention in groups.
- Confirm business/API policy constraints.
- Define approval syntax.
- Never accept broad natural-language approval for risky commands.

---

## 9. Security acceptance gate

The system cannot move from monitoring to execution until:

- Read-only monitoring has run successfully for at least 7 days or agreed test period.
- False positives are understood.
- Telegram alerts are stable.
- `openclaw security audit` has no critical issues.
- SSH user is least-privileged.
- Approval parser is tested.
- Rollback plan exists.
- Operator understands exactly what commands can run.

---

## 10. Initial milestones

### M1 — Project skeleton

- CLAUDE.md
- Requirements
- Spec/plan/tasks
- .env.example
- config examples

### M2 — OpenClaw local install

- Gateway running
- Dashboard reachable locally
- Security audit baseline

### M3 — Telegram notification proof

- Bot created
- Test message sent
- Message redaction verified

### M4 — Server232 read-only monitor

- SSH alias works
- Health check script works
- ETL folder/log checks work

### M5 — First daily report

- Local report generated
- Telegram report sent
- Evidence included

### M6 — Controlled execution design

- Approval format
- Command allowlist
- Dry-run
- Audit ledger

---

## 11. Definition of done

A feature is done only when:

- Spec updated.
- Plan updated.
- Tasks updated.
- Code implemented.
- Tests/verification run.
- Security impact reviewed.
- Documentation updated.
- Human-readable summary provided.
