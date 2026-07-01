# Spec — 001 OS_SYSTEM_AGENT

## Problem

ETL/pipeline operations currently require manual server entry to check whether jobs ran, whether files were generated, and whether data reached the expected destination. This creates delays, human dependency, and low visibility.

## Goal

Create a local, controlled AI-assisted operations layer that monitors ETL execution on `server232`, reports status, and requests approval before any execution/remediation.

## Users

- Primary operator: Juan / engineering operator.
- OS_SYSTEM_AGENT: AI-assisted monitoring and operations layer.
- OpenClaw Gateway: message/control gateway.
- server232: remote ETL server.
- Telegram/WhatsApp: notification and instruction channels.

## User journeys

### Journey 1 — Daily ETL report

The operator receives a daily message showing which jobs ran, which failed, and what evidence supports the conclusion.

### Journey 2 — Failure alert

If an ETL job did not run or data was not uploaded, the agent sends a CRITICAL alert with evidence and recommended action.

### Journey 3 — Approved rerun

The operator approves an exact rerun command. The agent performs a dry-run, executes the allowlisted action, verifies the result, and reports back.

## Functional requirements

- Monitor server availability.
- Monitor ETL files/logs/services.
- Validate data freshness.
- Send alerts.
- Generate daily report.
- Require approval for execution.

## Non-functional requirements

- Secure by default.
- Least privilege.
- Auditable.
- Reversible.
- WSL2-compatible.
- Migration-ready to Linux VM.

## Out of scope

- Full autonomous remediation.
- Open public bot access.
- Personal WhatsApp automation.
- Unreviewed third-party skills.

## Acceptance criteria

- Local OpenClaw Gateway runs.
- SSH read-only check to `server232` works.
- At least one ETL job status can be collected.
- Telegram test alert works.
- Daily report generated.
- Risky command is refused without approval.
