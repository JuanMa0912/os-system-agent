# Tasks — 004 Multi-empresa agent + approved ETL execution

Ordered so **read-only, low-risk** work lands first (CLAUDE.md §1.6). Execution
(T5–T10) is Phase 2 and stays behind dry-run + approval until explicitly enabled.

---

## T1 — Empresa dimension in catalog + reports  ·  risk: low  ·  approval: no

**Objective:** Reports carry the company: `Reporte empresa <X>`. Catalog declares
its empresa and fails closed if missing.

**Files:**
- `src/os_system_agent/catalog.py` — required top-level `empresa`; `EtlJob.empresa`.
- `src/os_system_agent/reports/daily.py` — `empresa` param; daily `Empresa:` line;
  chat `Reporte empresa <X>` line.
- `src/os_system_agent/collector.py` — thread `empresa` into both build funcs.
- `config/alert-rules.example.yml` — add top-level `empresa: NombreEmpresa`.
- `tests/test_catalog.py`, `tests/test_daily_report.py`, `tests/test_chat_report.py`,
  `tests/test_collector.py`.

**Commands:** `python -m pytest -q` · `ruff check .`

**Verify:** example loads with `empresa`; a catalog without `empresa` raises
`CatalogError`; chat report contains `Reporte empresa <X>`; existing
`OS_SYSTEM_AGENT · ETL …` + redaction assertions still pass.

**Status:** ✅ DONE. `empresa` is required top-level (fail closed) and denormalized
onto each `EtlJob`; daily shows `Empresa:`, chat leads with `Reporte empresa <X>`
(old header line preserved, so no existing assertion broke). Test fixtures in
`test_send_report`/`test_mcp_estado` gained `empresa`. Verified: `pytest` 116 pass,
`ruff` clean, `mypy` clean, and a dry-run against `config/alert-rules.yml`
(Mercamio) renders `Reporte empresa Mercamio`.

---

## T2 — Label incident alerts with empresa  ·  risk: low  ·  approval: no

**Objective:** `alert_incidents.py` / `render_alert` also lead with the empresa so
a 3pm CRITICAL says which company.

**Files:** `src/os_system_agent/alerting.py`, `scripts/alert_incidents.py`,
`tests/test_alerting.py`, `tests/test_alert_incidents.py`.

**Verify:** an alert message contains `empresa <X>`; no-change → still silent.

**Status:** ✅ DONE. `render_alert` header now reads
`OS_SYSTEM_AGENT · empresa <X> — cambios ETL …` (old substring preserved).
`_current_incidents` returns empresa (from the catalog); `main` passes it through.

---

## T3 — Local command runner (co-located monitoring)  ·  risk: low  ·  approval: no

**Objective:** Since each agent runs ON its empresa's server, add a local runner
(reusing the read-only allowlist) so monitoring needs no SSH-to-self.

**Files:** `src/os_system_agent/ssh_client.py`, `tests/test_ssh_allowlist.py`.

**Verify:** `collect_statuses` works against localhost with the allowlist enforced;
SSH path unchanged when an alias is set.

**Status:** ✅ DONE. Implemented as a **dispatch inside `run_read_only`**: alias
`local`/`localhost` runs `run_read_only_local` (shlex-split, no shell, allowlist
enforced first); any other alias is unchanged SSH. **Zero change** to `collector.py`
or the scripts — co-located mode is activated purely with `--server-alias local`.

---

## T4 — Mercamio deployment config  ·  risk: low  ·  approval: no

**Objective:** Stand up Mercamio's instance so the ETLs can be registered as they
are built.

**Files (gitignored / on-server, NOT committed):** `config/alert-rules.yml`
(`empresa: Mercamio` + jobs), `.env` (bot_mercamio token + group id), systemd
units from the `config/systemd/*.example`.

**Verify:** `python scripts/collect_etl_status.py` (dry-run) prints
`Reporte empresa Mercamio`.

**Status:** 🟡 Repo-side ready. Mercamio's gitignored `config/alert-rules.yml`
scaffold created, and the full deploy/update guide is written in
`docs/deploy-multiempresa.md` (parameterized per empresa — same steps for Dinastia).
On-server steps (bot, OpenClaw channel, systemd install) are done per box at deploy.

---

## T5 — Per-empresa execution allowlist (format + loader)  ·  risk: med  ·  approval: no

**Objective:** Define `config/exec-allowlist.example.yml`: named actions
(e.g. `rerun daily_sales`) → exact command + risk + rollback + verify. Loader
fails closed. **No execution yet** — just the typed, validated allowlist.

**Files:** `config/exec-allowlist.example.yml`, `src/os_system_agent/execution/allowlist.py`,
tests.

---

## T6 — Approval parser  ·  risk: med  ·  approval: no

**Objective:** Parse `APPROVE os_system_agent <task_id> <action> <empresa> <window>`.
Reject malformed / expired / empresa-mismatch. Pure, deterministic, tested.

**Files:** `src/os_system_agent/execution/approval.py`, tests (valid + every
rejection path).

---

## T7 — Audit ledger  ·  risk: med  ·  approval: no

**Objective:** Append-only `audit-ledger.jsonl` writer: one redacted line per
approval / dry-run / execute / verify.

**Files:** `src/os_system_agent/execution/audit.py`, tests (line shape + redaction).

---

## T8 — execute_action.py (dry-run only)  ·  risk: high  ·  approval: yes

**Objective:** Wire T5–T7 into a CLI that, given an approval, shows the dry-run
(command, impact, rollback, risk) and writes audit — **still no real execution**.

**Files:** `scripts/execute_action.py`, tests.

**Verify:** without a valid approval → dry-run only, non-zero on send-without-approval.

---

## T9 — Server-side etl_runner + trigger  ·  risk: high  ·  approval: yes (per empresa)

**Objective:** On each server, create non-root `etl_runner` and the narrow trigger
(user systemd unit or single-command sudoers) for each allowlisted action.
Document in `operations-runbook.md`.

---

## T10 — Enable real execution per empresa  ·  risk: high  ·  approval: yes (per empresa)

**Objective:** Flip the per-empresa switch so an approved, allowlisted action runs
as `etl_runner`, then verifies + reports + audits. One empresa at a time.

**Verify (AC-4):** dry-run → execute → verify → report → audit line written.
