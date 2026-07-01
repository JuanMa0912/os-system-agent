# Contributing

This project is **spec-driven**. Code follows specs, not the other way around.

## Workflow

1. **Specify** — open or update `specs/<id>-<feature>/spec.md` (problem, actors,
   journeys, acceptance criteria, security boundaries).
2. **Plan** — `plan.md` (architecture, data flow, failure modes, rollback).
3. **Tasks** — `tasks.md` (small, testable units with a risk level).
4. **Implement** — only clearly-defined tasks; minimal, reversible changes.
5. **Verify** — tests + manual checks; update docs.

See `CLAUDE.md` for the full operating rules.

## Local setup

This project uses [uv](https://docs.astral.sh/uv/). The committed `uv.lock`
pins exact versions so the env is identical on every machine.

```bash
# install uv once: https://docs.astral.sh/uv/getting-started/installation/
uv sync
```

## Before opening a PR

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

## Hard rules

- No secrets anywhere (use fake values in tests and examples).
- Anything destructive or production-writing stays approval-gated.
- Phase 1 is monitoring-only: do not add code that mutates a server without an
  approval gate and an audit trail.
