# Dinastia ventas pipeline — "Rentabilidad por línea"

Daily + monthly ETL that reads sales from the Dinastia ERP
(**MySQL 8.0**, `BD_BIABLE01`, Siesa/Biable @ `192.168.30.1`) and loads
**profitability-by-product-line** into **GCP**. Adapted from the Mercamio
PostgreSQL reference (`../_reference/ventas/`) — see [`ANALYSIS.md`](./ANALYSIS.md).

This pipeline is also the **framework template** for the other Dinastia pipelines
(`rotacion`, `margen`): they reuse `common/` and copy `etl/ventas_rango.py`.

> **Status: scaffold.** The source SQL is a **stub** and the GCP loader is
> **abstract** (BigQuery / Cloud SQL stubs). It will not run a real load until the
> ERP schema and GCP target are confirmed — see [`SCHEMA_NEEDS.md`](./SCHEMA_NEEDS.md).
> The guard `SCHEMA_CONFIRMED = False` in `etl/ventas_rango.py` prevents a
> half-guessed query from ever hitting production (a real run exits `3`).

---

## Layout

```
ventas/
├── common/                    # shared framework (reused by rotacion + margen)
│   ├── db.py                  #   MySQL source (pymysql), read-only, streaming
│   ├── loader.py              #   abstract GCP loader + BigQuery/CloudSQL stubs
│   └── utils.py               #   env-expanded config, logging, dates, JSON reports
├── etl/
│   └── ventas_rango.py        # the line-profitability ETL (SQL stubbed, flow wired)
├── scripts/
│   ├── pipeline_orchestrator.py  # daily/monthly, parallel+sequential, retries, report
│   └── etl_runner.py             # subprocess runner w/ retries + timeout
├── systemd/                   # non-root .service + .timer templates (daily, monthly)
├── config.example.yaml        # every config key; secrets via ${ENV} only
├── requirements.txt           # pymysql, pyyaml, tzdata, GCP client (loose pins)
├── ANALYSIS.md  SCHEMA_NEEDS.md  README.md
```

`common/` intentionally has **no ventas-specific logic**. To share it with
`rotacion`/`margen`, hoist it to `dinastia-etl/common/` and add the repo root to
`PYTHONPATH` (or install it as an editable package).

---

## 1. Configure

Copy the template and keep it free of secrets — every `${VAR}` is expanded from the
environment at load time, and a missing required var makes config loading **fail
closed**:

```bash
cp config.example.yaml config.local.yaml   # edit non-secret values (hosts, schedule)
```

Provide secrets via the environment (systemd `EnvironmentFile=` on the box; never
committed). Required env vars:

| Env var | Used for |
|---------|----------|
| `DINASTIA_MYSQL_USER` | ERP read-only account |
| `DINASTIA_MYSQL_PASSWORD` | ERP password |
| `GCP_PROJECT` | if `target.type: bigquery` |
| `GOOGLE_APPLICATION_CREDENTIALS` | BigQuery service-account json path |
| `GCP_CLOUDSQL_*` | if `target.type: cloudsql_postgres` |

Point tools at the config with `--config` or the `DINASTIA_ETL_CONFIG` env var.

---

## 2. Install (uv)

The project standardizes on **uv** (never `pip`/`venv` directly):

```bash
uv venv                 # create .venv
uv pip install -r requirements.txt
# (or, if a pyproject is added later: `uv sync`)
```

Requires Python **3.11+**. `PyMySQL` is pure-Python (no C toolchain needed).
Install the GCP client for your chosen target only (see `requirements.txt`).

---

## 3. Run (manually)

```bash
# validate wiring WITHOUT touching the ERP or GCP (safe on the scaffold):
uv run python etl/ventas_rango.py --date 20260701 --dry-run

# a single day / a range (once SCHEMA_CONFIRMED = True):
uv run python etl/ventas_rango.py --date 20260701
uv run python etl/ventas_rango.py --start-date 20260701 --end-date 20260722

# the orchestrator (what systemd runs):
uv run python scripts/pipeline_orchestrator.py --mode daily   --config config.local.yaml
uv run python scripts/pipeline_orchestrator.py --mode monthly --config config.local.yaml
```

**Exit codes** — ETL: `0` ok · `1` runtime error · `2` usage error · `3` schema not
confirmed (scaffold). Orchestrator: `0` ok · `1` an ETL failed · `2` fatal.

The ETL prints one machine-readable line `RECORDS_LOADED: <n>` that the runner
parses into the JSON report (`logs/report_*.json`).

---

## 4. Deploy on Dinastia's Debian 12 box (`servidorUAID`)

Runs as a **dedicated non-root user** (`dinastia`). Adjust the paths/User in the
`systemd/*` templates if your layout differs.

```bash
# 1. Code + venv under /opt/dinastia-ventas
sudo mkdir -p /opt/dinastia-ventas /etc/dinastia-ventas
sudo useradd -r -s /usr/sbin/nologin dinastia
sudo rsync -a ./ /opt/dinastia-ventas/         # ship this folder (excl. .venv)
cd /opt/dinastia-ventas
sudo -u dinastia uv venv && sudo -u dinastia uv pip install -r requirements.txt

# 2. Config (non-secret) + secrets file (root-owned, 0600)
sudo cp config.example.yaml config/pipeline_config.yaml   # edit values
sudo install -m 600 /dev/null /etc/dinastia-ventas/dinastia-ventas.env
sudo tee -a /etc/dinastia-ventas/dinastia-ventas.env >/dev/null <<'ENV'
DINASTIA_MYSQL_USER=...        # the read-only ERP account
DINASTIA_MYSQL_PASSWORD=...
GCP_PROJECT=...                # or GCP_CLOUDSQL_* for Cloud SQL
GOOGLE_APPLICATION_CREDENTIALS=/etc/dinastia-ventas/sa.json
ENV
sudo chown -R dinastia:dinastia /opt/dinastia-ventas
sudo chmod 750 /opt/dinastia-ventas

# 3. systemd units (edit ExecStart python path if not using /opt/.../.venv)
sudo cp systemd/dinastia-ventas-*.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dinastia-ventas-daily.timer dinastia-ventas-monthly.timer
```

Schedule (matches Mercamio): **daily 07:00**, **monthly validation 14:00** (the
monthly service runs daily but only does work on `execution.validation_weeks`,
default weeks 1 & 3).

### Monitor

```bash
systemctl list-timers 'dinastia-ventas-*'
journalctl -u dinastia-ventas-daily.service -n 100 --no-pager
sudo systemctl start dinastia-ventas-daily.service      # run now, off-schedule
ls -lt /opt/dinastia-ventas/logs/report_*.json | head   # latest run report
```

> When the pipeline is a live systemd service on `servidorUAID`, register it in the
> `os-system-agent` catalog under `empresa: Dinastia` so the monitor tracks it.

---

## 5. Finish the real build (remove the scaffold guard)

1. Answer everything in [`SCHEMA_NEEDS.md`](./SCHEMA_NEEDS.md) (ERP columns, joins,
   the rentabilidad/cost definition, and the GCP target).
2. Fill `SOURCE_SQL` and finalize `TARGET_SCHEMA` in `etl/ventas_rango.py`.
3. Implement the chosen loader in `common/loader.py` (BigQuery **or** Cloud SQL).
4. Set `SCHEMA_CONFIRMED = True`.
5. Validate: `--dry-run`, then a one-day load to a **non-prod** GCP target; sanity-
   check `RECORDS_LOADED` and totals against a known Biable report.

## Security notes

- Source is **read-only**: sessions open `SET SESSION TRANSACTION READ ONLY`
  (`source.mysql.enforce_read_only`); the ERP account should be SELECT-only anyway.
- **No secrets in the repo**: config is `*.example` + `${ENV}`; secrets live only in
  the root-owned `EnvironmentFile`.
- systemd units run **non-root** with `NoNewPrivileges`, `ProtectSystem=strict`,
  `ProtectHome`, `PrivateTmp`, and a single writable path (`logs/`).
