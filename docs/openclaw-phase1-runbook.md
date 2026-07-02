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
| `gateway.probe_failed` (`operator.read`) | Resolves once a **command owner** is configured (Phase M3, with the Telegram user id). |

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

## Next

- **M3** — Telegram channel: create a bot with `@BotFather`, `openclaw channels
  add telegram`, set `commands.ownerAllowFrom`, allowlist + pairing, redacted
  test alert.
- **M4** — read-only SSH monitoring of the ETL server (`etl_monitor` user).
