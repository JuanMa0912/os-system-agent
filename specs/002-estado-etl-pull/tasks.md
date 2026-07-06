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

Files: `config/skills/estado-etl.SKILL.md`.

- `user-invocable: true`, `command-dispatch: tool`, `command-tool: <mcp tool>`,
  owner-only; body: "call `estado_etl`, return it verbatim; monitor only, never
  execute/rerun anything." `command-tool` filled from the box's probe output.

## T204 — Register + wire on MMAUTOML01 (box, guided)

Risk: medium
Approval required: yes (touches OpenClaw config)

- `git pull`; `uv sync` (pulls `mcp`).
- `openclaw mcp add` the stdio server (cwd=repo, env `OS_ETL_CATALOG` +
  `OS_SERVER_ALIAS`); confirm it probes OK.
- `openclaw mcp tools` include-filter → only `estado_etl`.
- Copy the skill template to `~/.openclaw/workspace/skills/estado-etl/SKILL.md`;
  set `command-tool` to the probed tool name.
- `systemctl --user restart openclaw-gateway`.

Verification: `openclaw mcp probe`, `openclaw skills check`.

## T205 — End-to-end + re-audit (box, guided)

Risk: low
Approval required: no

- Owner `/estado` on Telegram → current report arrives.
- Owner NL "¿cómo están los ETL?" → agent answers from the tool.
- Injection attempt → nothing but the tool runs.
- `openclaw security audit --deep` → **0 critical**.

Verification: screenshots/paste of the three interactions + audit summary.
Update this file with completion notes and the memory file.
