# Plan — 001 OS_SYSTEM_AGENT

## Architecture

Phase 1 runs locally in WSL2.

Components:

1. OpenClaw Gateway.
2. Claude Code workspace.
3. Python monitoring package.
4. SSH read-only checks to `server232`.
5. Telegram notification module.
6. Local status store.
7. Audit ledger.

## Data flow

```text
server232 logs/files/services/db
        ↓ SSH read-only
collector scripts
        ↓
status normalization
        ↓
rules engine
        ↓
report + alerts
        ↓
Telegram / operator
```

## Security design

- WSL2 local-only gateway.
- Channel allowlists.
- Sandbox for OpenClaw tools.
- SSH least privilege.
- No secrets in repo.
- Approval-gated execution.

## Implementation steps

1. Create project skeleton.
2. Install OpenClaw.
3. Configure OpenClaw local gateway.
4. Configure Telegram bot.
5. Configure SSH alias.
6. Implement `healthcheck_server232.sh`.
7. Implement `collect_etl_status.py`.
8. Implement report generator.
9. Implement alert sender.
10. Add approval parser.
11. Add tests.
12. Run security audit.

## Validation

- Local tests pass.
- SSH health check returns expected data.
- Telegram alert sends.
- Report generated.
- Risky command blocked.
- Audit log generated.

## Rollback

- Disable OpenClaw channel.
- Stop Gateway.
- Remove Telegram token.
- Disable SSH key.
- Revert repo changes.
