# OS_SYSTEM_AGENT

[![CI](https://github.com/JuanMa0912/os-system-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/JuanMa0912/os-system-agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

A controlled **OpenClaw + Claude Code** agent that monitors ETL/pipeline jobs on
a remote server — so an operator stops logging into production for routine
checks — while keeping every action **safe, auditable, and reversible**.

> **Phase 1 is monitoring-only.** Nothing writes to production or runs a
> privileged action without explicit human approval.

## Why this exists

ETL operations usually mean SSH-ing into a server to check "did the job run?
did the data land?". This project puts a disciplined agent in front of that:
it observes, diagnoses, reports, and *asks before it acts*.

## Methodology

- **Spec-driven development** — every feature starts as `spec → plan → tasks`
  under [`specs/`](specs/) before any code (see [CONTRIBUTING.md](CONTRIBUTING.md)).
- **AgentOps** — approval gates, command allowlists, sandboxing, least-privilege
  SSH, severity model, and an audit trail for every action.
- **LLMOps** — the agent's judgment (alert severity, secret redaction) is pinned
  with versioned golden cases under [`evals/`](evals/), executed in CI.

## Repository layout

```text
CLAUDE.md              Operating rules for the engineering agent
README.md              This file
pyproject.toml         Packaging, ruff, pytest, mypy config
config/                Example configs (no secrets): OpenClaw, SSH, alert rules
docs/                  Requirements + security/operations runbooks + roadmap
specs/                 Spec-driven features (spec / plan / tasks)
src/os_system_agent/   Python package (config, redaction, severity, ssh safety, monitors, reports, notifications)
scripts/               Operational entry points (Phase 1 skeletons)
tests/                 Unit tests + eval runner
evals/                 Versioned golden cases (severity, redaction)
.github/               CI (ruff + pytest + gitleaks), issue/PR templates
```

## Quickstart (development)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate  |  Unix: source .venv/bin/activate
pip install -e ".[dev]"

ruff check .
pytest
```

Then to configure a real deployment:

1. Read [`CLAUDE.md`](CLAUDE.md) and [`docs/REQUIREMENTS_OS_SYSTEM_AGENT.md`](docs/REQUIREMENTS_OS_SYSTEM_AGENT.md).
2. Copy `.env.example` to `.env` (kept out of version control).
3. Configure the SSH alias from [`config/server232.ssh_config.example`](config/server232.ssh_config.example).
4. Install and configure OpenClaw locally (localhost / private network only).
5. Start with **read-only monitoring only**.

## Safety model

- No secrets in code, commits, config, logs, reports, or tests — enforced in CI
  by [gitleaks](https://github.com/gitleaks/gitleaks).
- Destructive / production-write actions are refused unless explicitly approved
  in-session (see [`docs/security-runbook.md`](docs/security-runbook.md)).
- Read-only monitoring must be stable before any execution is enabled.

See [SECURITY.md](SECURITY.md) to report a vulnerability.

## Roadmap

Phase 1 local monitor → Phase 2 approved execution → Phase 3 observability →
Phase 4 hardened deployment → Phase 5 intelligent operations. Details in
[`docs/phase-roadmap.md`](docs/phase-roadmap.md).

## License

[MIT](LICENSE) © 2026 Juan Manuel Velasquez
