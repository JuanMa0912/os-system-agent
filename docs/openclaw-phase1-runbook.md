# OpenClaw Phase 1 Runbook (local gateway)

How the local OpenClaw gateway was installed, wired to a model provider, and
hardened for **Phase 1 (monitoring-only, single trusted operator, no channels
yet)**. Reproducible and secret-free — replace placeholders with your own
values and never commit tokens/keys.

> Scope: a WSL2 Ubuntu host (no GPU, ~8 GB RAM). OpenClaw CLI `2026.6.x`,
> Node 24. Everything stays on `localhost`.

## 1. Install

```bash
# OpenClaw CLI (official installer)
curl -fsSL https://openclaw.ai/install.sh | bash
openclaw --version
```

## 2. Model provider — why cloud, why gpt-oss

The host has **no GPU and ~8 GB RAM**, so a local model is not viable for a
tool-calling agent (OpenClaw's docs discourage small/quantized models for
agentic use). We use a **cloud** model instead; the gateway still runs locally.

Provider: **Ollama Cloud** (`ollama-cloud`), native API `https://ollama.com`
(never the `/v1` OpenAI-compatible URL — it breaks tool calling).

- Get an API key at <https://ollama.com/settings/keys> (stored in the agent
  auth store, not in plaintext config).
- Some models require a paid subscription (kimi, deepseek, …). The **free-tier
  `gpt-oss` models work and have solid tool calling**.

```bash
openclaw onboard --auth-choice ollama-cloud   # QuickStart, decline channels
openclaw models set ollama-cloud/gpt-oss:120b # free tier, good tool-calling
```

Quick check that a model is reachable on your tier (returns a reply, not a
`subscription`/`not found` error):

```bash
curl -s -H "Authorization: Bearer $OLLAMA_API_KEY" https://ollama.com/api/chat \
  -d '{"model":"gpt-oss:120b","messages":[{"role":"user","content":"hi"}],"stream":false}'
```

## 3. Onboarding choices (Phase 1)

- Setup mode: **QuickStart** (local gateway, bound to `127.0.0.1`).
- Channels: **skip** — no channel is enabled until the security audit is clean.
- Web search provider, skills, hooks: **skip** (least privilege).

## 4. Hardening (least privilege)

```bash
# Disable the QuickStart insecure Control UI auth flag (we are localhost-only)
openclaw config set gateway.controlUi.allowInsecureAuth false

# Deny web + browser tools — the monitoring agent never browses, and these are
# the main prompt-injection vector for a smaller model.
openclaw config set tools.deny '["group:web","browser"]'

# Memory search uses an embedding provider we do not need in Phase 1
openclaw config set agents.defaults.memorySearch.enabled false

# Rotate the gateway token if it was ever exposed
openclaw doctor --generate-gateway-token

systemctl --user restart openclaw-gateway
```

Also let `openclaw doctor --fix` tighten file permissions and disable unused
skills.

## 5. Security audit — target and accepted risks

```bash
openclaw security audit --deep   # goal: 0 critical
```

Result: **0 critical**. The three remaining warnings are accepted for Phase 1:

| Warning | Decision |
| --- | --- |
| `models.weak_tier` (gpt-oss below GPT-5/Claude 4.5) | **Accepted** cost trade-off. Revisit before the agent can *execute* (Phase 2+). |
| `gateway.trusted_proxies_missing` | **Non-issue** — Control UI is local-only, no reverse proxy. |
| `gateway.probe_failed` (`operator.read`) | **Accepted** — the deep audit's own probe lacks operator scope over the local gateway; not a vulnerability. (Configuring the command owner did **not** clear it.) |

## 6. Sandbox decision

`agents.defaults.sandbox.mode` is **`off`**. Docker Desktop had no WSL
integration on this host (the `docker` binary was only a shim), and it is heavy
for an 8 GB box. The `models.small_params` finding is already resolved by
denying web/browser tools, so the sandbox is not required to reach 0 critical.

**Re-enable a sandbox before Phase 2 (autonomous execution) or before exposing
any channel**, via Docker WSL integration or a lighter alternative.

## 7. State summary

- Gateway: `ws://127.0.0.1:18789`, systemd user service, Tailscale exposure off.
- Channels: none. Skills/hooks: minimal.
- Model: `ollama-cloud/gpt-oss:120b`.

## 8. Telegram channel (M3)

The first channel. Added security-first: paired and allowlisted to a single
operator before it is trusted. Telegram defaults to **pairing**, so unknown
senders are gated until approved.

```bash
openclaw channels add              # pick Telegram, paste the bot token (@BotFather)
systemctl --user restart openclaw-gateway
openclaw channels status           # expect: telegram ... running, mode:polling
```

Capture the operator's Telegram id via pairing (no manual lookup): DM the bot
once, then:

```bash
openclaw pairing list --channel telegram
openclaw pairing approve telegram <CODE>   # also sets command owner if it was empty
```

Lock it to that single operator:

```bash
openclaw config set channels.telegram.allowFrom '["<telegram_user_id>"]'
# commands.ownerAllowFrom is set automatically by the first pairing approve
systemctl --user restart openclaw-gateway
```

Verify: the operator DMs the bot and gets a model reply; other senders are gated
by pairing + allowlist. The Telegram bot token lives in config (`token:config`)
— migrate to a SecretRef (`openclaw secrets`) as later hardening.

## 9. Daily report push (M5)

The push closes the loop: collect live status → render the daily report →
deliver it over the Telegram channel, on a schedule, with no human in the loop.

The delivery path was verified by hand first (outbound send works):

```bash
openclaw message send --channel telegram --target <chat_id> --message "test"
# -> "Sent via telegram. Message ID: ..." and it arrives in the chat
```

`scripts/send_daily_report.py` wraps the collector and the send. It is
dry-run/fail-closed by default (CLAUDE.md §14): it only prints unless `--send`
is given, and refuses to send without a target.

```bash
# dry-run: print the report, deliver nothing
uv run python scripts/send_daily_report.py --live --catalog config/alert-rules.yml

# real push (what the timer runs)
uv run python scripts/send_daily_report.py --live --send \
    --channel telegram --target <chat_id> --catalog config/alert-rules.yml
```

The recipient id is never committed — pass it with `--target` or the
`OS_TELEGRAM_TARGET` env var. `--only-incidents` delivers only when something is
WARNING or worse (for a second, more frequent alert-only timer that stays quiet
on healthy days).

Schedule it with a **systemd user timer** (so it can reach the local gateway):
copy `config/systemd/os-system-agent-daily.{service,timer}.example` into
`~/.config/systemd/user/`, fill in the paths + `OS_TELEGRAM_TARGET`, then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now os-system-agent-daily.timer
loginctl enable-linger "$USER"          # run even when not logged in
systemctl --user start os-system-agent-daily.service   # one-shot smoke test
systemctl --user list-timers | grep os-system-agent    # confirm next run
```

The service is `Type=oneshot` with a 180s `TimeoutStartSec`, so a stuck run
never hangs the box. It stays **read-only** — it reads status and sends a
message; it never touches the ETL server's state. Approval-gated execution is
Phase 2.

## 10. Proactive incident alerts (spec 003)

The daily push is a digest; this is the "something broke" signal. A second, more
frequent timer runs `scripts/alert_incidents.py`, which live-checks status and
sends a Telegram alert **only when the incident set changes** — a new/escalated
incident, a recovery, or the server going unreachable — and stays silent
otherwise (no alert fatigue). It is **deterministic** (no model), so it cannot
run away.

```bash
# dry-run once (prints "no change" when healthy, or the alert text if not)
python scripts/alert_incidents.py --catalog config/alert-rules.yml

# what the timer runs (state in .alert-state.json, gitignored)
python scripts/alert_incidents.py --send --channel telegram \
    --target <chat_id> --catalog config/alert-rules.yml
```

Install `config/systemd/os-system-agent-alerts.{service,timer}.example` as USER
units (same pattern as the daily push; timer ~every 2h). It reuses the
`etl_monitor` read-only SSH path and one batched `systemctl show`.

## Note — interactive pull (`/estado`) is parked

The read-only `estado_etl` MCP tool works, but making it **conversational
through the free model** proved unreliable (`command-dispatch` can't reach MCP
tools; `gpt-oss` times out / refuses / asks for params / can run away). See
`specs/002-estado-etl-pull/tasks.md`. A reliable `/estado` needs a deterministic
OpenClaw **plugin** (no model) or a **paid model**. Parked; the push + alerts
cover Phase 1 monitoring.

## Next

- **Deterministic `/estado` plugin** — the interactive pull, done without the
  model (`registerCommand()`), so it is instant and cannot run away.
- **Phase 2** — approval-gated ETL reruns (`APPROVE` format + dry-run + audit
  ledger). Re-enable the OpenClaw sandbox before this.
