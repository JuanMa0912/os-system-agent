# Security Policy

This project operates against real infrastructure, so security is a first-class
concern. See `docs/security-runbook.md` and `CLAUDE.md` for the full model.

## Reporting a vulnerability

**Do not open a public issue for security problems.**

Please use GitHub's private vulnerability reporting:
<https://github.com/JuanMa0912/os-system-agent/security/advisories/new>

Include a redacted description, impact, and reproduction steps. You will get an
acknowledgement as soon as possible.

## Non-negotiable rules

- No secrets in code, commits, config, logs, reports, or tests (fake values only).
- The OpenClaw Gateway is never exposed publicly (localhost / private tailnet only).
- No destructive or production-write operations without explicit, documented approval.
- The agent never runs as root and uses least-privilege SSH users.
- Third-party skills/plugins are treated as untrusted until pinned, audited, and approved.

## Scope

This repository is a Phase 1 **monitoring-only** starter. Any code that would
execute changes on a server must remain behind the approval parser and audit
ledger described in the specs.
