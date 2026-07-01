# Security Runbook — OS_SYSTEM_AGENT

## Golden rules

- Do not expose OpenClaw Gateway publicly.
- Do not connect personal WhatsApp.
- Do not install unreviewed skills/plugins.
- Do not store secrets in repo.
- Do not run production writes without approval.
- Do not run the agent as root.
- Do not let group chats trigger exec tools without allowlist + mention gating + sandbox.

## OpenClaw hardening

1. Bind Gateway to localhost or private network.
2. Enable auth.
3. Enable sandboxing.
4. Configure channel allowlists.
5. Configure group mention gating.
6. Restrict tools for channel sessions.
7. Run `openclaw security audit`.
8. Fix critical findings before continuing.
9. Keep plugin/skill allowlist strict.
10. Pin plugin versions where supported.

## SSH hardening

- Dedicated key.
- Dedicated user.
- No root login.
- No password auth for agent.
- Minimal read permissions.
- Separate execution user if needed.
- Log all commands.

## Secret handling

- `.env` must be gitignored.
- `.env.example` contains placeholders only.
- Redact tokens from logs.
- Rotate token if exposed.
- Never paste private keys into chat.

## Incident response

If suspicious behavior occurs:

1. Stop OpenClaw Gateway.
2. Disable channels.
3. Revoke Telegram/WhatsApp tokens if applicable.
4. Disable SSH key.
5. Preserve logs.
6. Review audit ledger.
7. Run security audit.
8. Re-enable only after root cause is understood.
