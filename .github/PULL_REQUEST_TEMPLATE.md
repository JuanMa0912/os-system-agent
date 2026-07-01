<!-- Spec-driven: link the spec/plan/tasks this PR implements. -->

## What & why

Closes / relates to: <!-- spec id, e.g. specs/001-os-system-agent + task id T00X -->

Summary of the change and the problem it solves.

## Risk level

- [ ] READ_ONLY / low
- [ ] LOW_RISK
- [ ] MEDIUM_RISK
- [ ] HIGH_RISK

## Safety checklist

- [ ] No secrets in code, config, logs, or tests (fake values only).
- [ ] Risky/destructive operations remain approval-gated.
- [ ] Read-only guarantees preserved for Phase 1.
- [ ] Docs/specs/tasks updated to match behavior.

## Verification

- [ ] `ruff check .` and `ruff format --check .`
- [ ] `pytest`
- [ ] Manual verification (describe):
