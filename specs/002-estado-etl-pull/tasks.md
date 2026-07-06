# Tasks — 002 estado_etl (read-only ETL status on demand)

## T201 — MCP server exposing the read-only `estado_etl` tool

Risk: low
Approval required: no (read-only; no OpenClaw config change yet)

Files: `src/os_system_agent/mcp_server.py`, `pyproject.toml` (add `mcp` dep).

- `current_report(*, catalog_path, alias, now, runner)` reuses `collect_statuses`
  (live) + `build_daily_report`; testable with a fake runner + temp catalog.
- `estado_etl()` MCP tool: parameterless, returns the report; short TTL cache;
  safe error text on failure (no stack trace/secret).
- `main()` runs FastMCP over stdio.

Verification: `uv run ruff check`, `uv run mypy`, `uv run pytest`; local
`python -m os_system_agent.mcp_server` starts.

## T202 — Tests for the tool core

Risk: low
Approval required: no

Files: `tests/test_mcp_estado.py`.

- report built from a fake runner matches `build_daily_report`.
- parameterless signature; returns `str`.
- TTL cache returns cached within the window, refreshes after.

## T203 — Skill template (`/estado`) committed to the repo

Risk: low
Approval required: no

Files: `config/skills/estado.SKILL.md`.

- `name: estado` (defines the `/estado` command), `user-invocable: true`,
  `command-dispatch: tool`, `command-tool: <mcp tool>`, owner-only; body: "call
  `estado_etl`, return it verbatim; monitor only, never execute/rerun anything."
  `command-tool` filled from the box's probe output.

## T204 — Register + wire on MMAUTOML01 (box, guided)

Risk: medium
Approval required: yes (touches OpenClaw config)

- `git pull`; `uv sync` (pulls `mcp`).
- `openclaw mcp add` the stdio server (cwd=repo, env `OS_ETL_CATALOG` +
  `OS_SERVER_ALIAS`); confirm it probes OK.
- `openclaw mcp tools` include-filter → only `estado_etl`.
- Copy the skill template to `~/.openclaw/workspace/skills/estado/SKILL.md`;
  set `command-tool` to the probed tool name.
- `systemctl --user restart openclaw-gateway`.

Verification: `openclaw mcp probe`, `openclaw skills check`.

**Status: DONE.** MCP server `os-system-agent` added (`--include estado_etl`,
stdio via the venv python, cwd=repo, env catalog+alias) — probes OK, `1 tools`.
Skill `estado-etl` installed to the workspace, shows `ready`; `command-tool`
= `estado_etl`.

## T205 — End-to-end + re-audit (box, guided)

Risk: low
Approval required: no

- Owner `/estado` on Telegram → current report arrives.
- Owner NL "¿cómo están los ETL?" → agent answers from the tool.
- Injection attempt → nothing but the tool runs.
- `openclaw security audit --deep` → **0 critical**.

**Status: DONE (validated on Telegram).** `/estado` + NL "¿cómo están los ETL?"
both returned the report via the tool. Scope-lock held: refused two math
questions, an integral question, and a "restart the sales ETL" request (stated
read-only, did not execute). `security audit --deep` still **0 critical** (same 2
accepted warns; adding the tool/skill introduced no new finding). Note: the weak
model (gpt-oss) is contained precisely because the only tool it has is read-only
and parameterless.

## T206 — Compact chat report format (token + readability)

Risk: low
Approval required: no

- `render_chat_report` (one line/job, humanized freshness, no `n/a`, no markdown
  table — Telegram doesn't render those); used by the daily push and the pull
  tool. Skill instructs the model to return it verbatim (no re-tabulation).

**Status: CODE DONE.** 10 new tests. Box: `git pull`, reinstall skill
(`--force`), `mcp reload` + gateway restart so the MCP server respawns with the
new format.
