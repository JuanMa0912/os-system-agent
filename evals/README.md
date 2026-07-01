# evals/

Golden cases that pin the agent's **judgment**, not just its plumbing. This is
the LLMOps side of the project: the severity classifier and the secret
redactor are the two decisions where a wrong call is expensive (a missed
CRITICAL, or a leaked token), so they get versioned, executable test cases.

## Layout

- `cases/severity_cases.yaml` — freshness delay → expected `Severity`.
- `cases/redaction_cases.yaml` — input text → substring that must be gone after redaction.

These files are consumed directly by `tests/test_evals.py`, so every case runs
in CI. Add a case here before changing the corresponding logic.

## How to add a case

1. Add an entry to the relevant YAML file with a unique `name`.
2. Run `pytest tests/test_evals.py`.
3. If it fails, decide whether the logic or the expectation is wrong — and fix
   the right one.
