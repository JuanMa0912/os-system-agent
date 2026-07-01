# Operations Runbook — OS_SYSTEM_AGENT

## Daily workflow

1. Run server health check.
2. Collect ETL status.
3. Validate freshness.
4. Generate report.
5. Send report to Telegram.
6. Store audit entry.

## Failure workflow

1. Detect failure.
2. Gather evidence.
3. Classify severity.
4. Send alert.
5. Recommend next action.
6. Ask for approval if execution is needed.
7. Execute only allowlisted approved action.
8. Verify result.
9. Send final report.

## Standard evidence

- server timestamp
- job name
- expected schedule
- latest log lines
- latest file timestamp
- row count or max date when available
- command used for verification
