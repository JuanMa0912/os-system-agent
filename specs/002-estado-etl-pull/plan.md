# Plan — 002 estado_etl (read-only ETL status on demand)

## Decision (approved)

Expose one **read-only** capability, `estado_etl`, two ways:

1. **Natural language** — the agent (gpt-oss) may call the tool when the owner
   asks "how are the ETLs?" over Telegram.
2. **Deterministic** — a `/estado` slash command dispatches straight to the same
   tool, bypassing the model.

The tool is **parameterless** and runs only the fixed read-only collector, so
neither path can execute anything else (spec 002 security boundaries).

## Architecture

```text
Telegram (owner only)
   │  "/estado"  or  "¿cómo están los ETL?"
   ▼
OpenClaw Gateway (localhost)  ── owner allowlist + pairing (M3)
   │
   ├── /estado  ─ command-dispatch: tool ─┐   (model bypassed)
   └── NL query ─ gpt-oss calls tool ──────┤   (model in loop, read-only tool)
                                           ▼
                          MCP server  (stdio, local, our code)
                          tool: estado_etl  (parameterless)
                                           │
                                           ▼
                   os_system_agent.collector.collect_statuses(live=True)
                                           │  run_read_only("server232", "systemctl show …")
                                           ▼
                                       server232  (etl_monitor, read-only)
```

The MCP server is **our** code (`os_system_agent.mcp_server`), run over
stdio by OpenClaw. It reuses the exact collector the daily push uses, so there
is one monitoring implementation.

## Data flow

1. Owner triggers `/estado` (or asks in NL) on Telegram.
2. Gateway (owner-gated) routes to the `estado_etl` MCP tool.
3. The tool loads the catalog, runs `collect_statuses(live=True)` → read-only
   `systemctl show` over SSH as `etl_monitor`, renders the redacted daily report.
4. Report text is returned to the gateway → delivered to the owner.

## Interfaces

- **MCP tool** `estado_etl() -> str` — no arguments; returns the rendered report.
- **Server module** `os_system_agent.mcp_server` — `current_report(...)`
  (testable core) + `main()` that runs FastMCP over stdio.
- **Config via env** (set in the `mcp add` server definition, not committed):
  - `OS_ETL_CATALOG` — path to the real catalog (default `config/alert-rules.yml`).
  - `OS_SERVER_ALIAS` — SSH alias (default `server232`).

## Required scripts / files

- `src/os_system_agent/mcp_server.py` — the MCP server (new).
- `~/.openclaw/workspace/skills/estado-etl/SKILL.md` — the `/estado` skill (on
  the box; a committed template lives in `config/skills/estado-etl.SKILL.md`).
- New dependency: `mcp` (official Python SDK) for a correct stdio server.

## Required credentials (no secrets in repo)

- Reuses the existing `etl_monitor` SSH key + `server232` alias (M4). No new
  credentials. The owner Telegram id stays in gateway config on the box only.

## Observability / logging

- The MCP tool logs one structured line per invocation (timestamp, overall
  severity, job count) to stderr — captured by the gateway. **No secrets, no raw
  evidence** in the log line (the report itself is already redacted).
- `openclaw mcp status` / `mcp probe` show transport + tool health.

## Failure modes

- **Catalog missing/invalid** → `load_catalog` fails closed; tool returns a short
  safe error ("status unavailable: catalog error"), no stack trace.
- **SSH unreachable / timeout** → collector surfaces it; tool returns a safe
  "server unreachable" message; no path/secret leak.
- **Abuse / rapid repeats** → a short in-process TTL cache (~20s) returns the
  last report instead of hammering SSH; owner-only channel bounds this anyway.
- **MCP server crash** → gateway reports the tool as unavailable; `/estado`
  returns an error; the daily push (independent systemd timer) is unaffected.

## Rollback plan

- `openclaw mcp unset <server>` removes the MCP server.
- Delete `~/.openclaw/workspace/skills/estado-etl/`.
- `systemctl --user restart openclaw-gateway`.
- No repo revert needed (new files are additive); daily push keeps working.

## Tests

- Unit: `current_report(...)` returns the rendered report given a fake read-only
  runner + a temp catalog (no real SSH); TTL cache returns cached within window.
- Unit: tool is parameterless (signature check) and returns a `str`.
- Manual (on box): see checklist.

## Manual verification checklist

- [ ] `uv sync` pulls `mcp`; `uv run python -m os_system_agent.mcp_server`
      starts (stdio; Ctrl-C to stop).
- [ ] `openclaw mcp add` the server (stdio, cwd=repo, env=catalog+alias); it
      **probes OK** before saving.
- [ ] `openclaw mcp probe` lists exactly the `estado_etl` tool; `mcp tools`
      filter excludes anything else.
- [ ] Note the tool's fully-qualified name; set it as `command-tool` in SKILL.md.
- [ ] `openclaw skills check` shows `estado-etl` ready.
- [ ] Owner `/estado` on Telegram → current report arrives.
- [ ] Owner "¿cómo están los ETL?" → agent calls the tool, answers from it.
- [ ] Non-owner is not served; an injection attempt runs nothing but the tool.
- [ ] `openclaw security audit --deep` still **0 critical**.

## Open verification point (resolve on the box)

Whether `command-dispatch: tool` can target an **MCP** tool directly. If yes →
fully deterministic `/estado`. If not → the `/estado` skill body instructs the
model to call `estado_etl` (still owner-gated + read-only; slightly less
deterministic). NL invocation works regardless. Decide from `skills`/`mcp`
output; the fallback does not change the security posture (tool stays read-only).
