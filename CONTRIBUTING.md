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

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate  |  Unix: source .venv/bin/activate
pip install -e ".[dev]"
```

## Before opening a PR

```bash
ruff check .
ruff format --check .
pytest
```

## Hard rules

- No secrets anywhere (use fake values in tests and examples).
- Anything destructive or production-writing stays approval-gated.
- Phase 1 is monitoring-only: do not add code that mutates a server without an
  approval gate and an audit trail.
