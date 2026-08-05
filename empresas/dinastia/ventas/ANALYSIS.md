# ANALYSIS — How the Mercamio "ventas" pipeline works

Concrete walkthrough of the reference pipeline in
`dinastia-etl/_reference/ventas/`, which this Dinastia scaffold adapts. Every
claim below cites a reference file. Read this before touching the scaffold — the
Dinastia framework mirrors this architecture, swapping PostgreSQL→MySQL at the
source and a fixed Postgres destination→a pluggable GCP loader.

The reference is a **daily + monthly** loader that pulls point-of-sale sales from
three PostgreSQL source databases and upserts them into a consolidated Postgres
warehouse (`produXdia`), one script per product category, coordinated by an
orchestrator with retries, timeouts, structured logs, and JSON run reports.

---

## 1. Component map

| File | Role |
|------|------|
| `pipeline_orchestrator.py` | Entry point. Picks daily vs monthly, runs ETLs (parallel then sequential), aggregates results, writes the JSON report. |
| `etl_runner.py` | Runs one ETL **as a subprocess** with retry/backoff + hard timeout; parses the row count from its stdout. |
| `pipeline_utils.py` | `PipelineConfig` (YAML dot-notation), `PipelineLogger` (file+console), date helpers, log cleanup, `format_duration`. |
| `fruver_ventas_rango.py` | One category ETL: source SQL → pandas → upsert. The template the other five categories copy. |
| `README.md` | Ops guide: install, systemd timers, monitoring, troubleshooting. |

Runtime layout (from `README.md`): `config/pipeline_config.yaml`, `etl/*_ventas_rango.py`
(fruver, carnes, pollo_pesc, industria, asadero, cajas), `scripts/{orchestrator,runner,utils}.py`,
`logs/{orchestrator_YYYYMMDD.log, report_*.json}` under `/opt/ventas_pipeline`.

---

## 2. Orchestration (`pipeline_orchestrator.py`)

**Construction** (`PipelineOrchestrator.__init__`, lines 35-47): loads
`PipelineConfig`, builds a `PipelineLogger("orchestrator")`, and constructs one
`ETLRunner` wired from config: `paths.etl_dir`, `execution.max_retries` (default 3),
`execution.retry_delay_seconds` (60), `execution.etl_timeout` (1800s = 30 min).

**Two run modes:**

- `run_daily_load()` (lines 49-74): computes **yesterday** via
  `get_yesterday_yyyymmdd(tz)`, cleans old logs, then runs the ETLs for the single
  day `[yesterday, yesterday]`.
- `run_monthly_validation()` (lines 76-107): **gated** — calls
  `should_run_validation(tz, execution.validation_weeks)` (default `[1, 3]`) and
  aborts if today's week-of-month isn't listed. Otherwise reprocesses
  **month-to-date** via `get_month_range_yyyymmdd(tz)` → `[first_of_month, yesterday]`.
  Same ETLs, wider range. This is the idempotent "catch late-arriving data" pass.

**Two-phase execution:**

- `_run_parallel_etls` (lines 109-142): reads `etls.parallel`, sorts by
  `priority`, and submits each to a `ThreadPoolExecutor(max_workers=len(etls))`.
  Threads are fine because each ETL is really a blocking `subprocess.run` (the work
  happens in a child process, not under the GIL). Results collected via
  `as_completed`; a raised exception is logged but doesn't sink the batch.
- `_run_sequential_etls` (lines 144-164): reads `etls.sequential`, sorts by
  `priority`, runs them **one at a time**. The README (section 1) marks `cajas` as the
  heavy sequential ETL run after the five light parallel ones.

**Reporting** (`_generate_report` lines 174-218, `_save_json_report` 220-241):
logs a summary (duration, ok/failed counts, total records) and a per-ETL detail
line, then writes `logs/report_<runtype>_<timestamp>.json`.

**CLI / exit codes** (`main`, lines 244-283): `--mode {daily,monthly}` (required),
`--config` (default `/opt/ventas_pipeline/config/pipeline_config.yaml`). Exit **1**
if any ETL failed, **2** on a fatal orchestrator exception, **0** otherwise — the
signal a systemd/monitoring layer keys on.

---

## 3. Retries, timeouts, and the stdout row-count contract (`etl_runner.py`)

`ETLRunner.run_etl` (lines 72-144) loops `attempt` from 1..`max_retries`. Each
attempt calls `_execute_script` (146-240):

- **Command** (158-165): single day → `python3 <script> --date <YYYYMMDD>`; a range
  → `--start-date <a> --end-date <b>`. So every ETL must accept those flags.
- **Execution** (171-177): `subprocess.run(cmd, capture_output=True, text=True,
  timeout=self.timeout, cwd=self.etl_dir)`.
- **Outcome → status**: returncode 0 → `SUCCESS`; nonzero → `FAILED` (error =
  `Exit code N: <stderr>`); `TimeoutExpired` → `TIMEOUT`. On failure with attempts
  left, `time.sleep(retry_delay)` then retry (129-136).
- **Row count** (`_parse_records_from_output`, 242-254): greps stdout for the token
  `Upsert:` and parses the integer after it. **This is the contract**: an ETL
  reports how many rows it loaded by printing `... Upsert: <n>` — which
  `fruver_ventas_rango.py` does in its final `print` (line 246).

`ETLResult` (dataclass, 24-52) captures name/status/timings/records/attempts/error
and serializes via `to_dict()` for the JSON report.

> **Design note the scaffold keeps:** the orchestrator never imports the ETLs. It
> shells out. That process isolation means one ETL crashing (or leaking memory on a
> huge query) can't take down the others or the coordinator — worth preserving.

---

## 4. Per-category ETL flow (`fruver_ventas_rango.py`)

The canonical single-category ETL — source query → transform → load.

- **Connections (lines 13-30):** `DBS` lists **three** source PG databases
  (`mercamio`, `mtodo`, `bogota`, all @ `192.168.35.217`); `DEST_DB` is
  `produXdia` @ `192.168.35.232`, table `ventas_fruver`. WARNING: **credentials are
  hardcoded here** — the exact anti-pattern the Dinastia scaffold removes (env +
  `${VAR}` expansion, see `common/utils.py`).
- **Category filter (34-35):** `CATEGORIA="4"`, `LINEA1="01"` — this ETL only loads
  one `(id_tipo, id_linea1)` slice; each sibling script changes these constants.
- **Source SQL (`SQL_ORIGEN`, 42-80):** joins `cmmovimiento_pdv m` ↔ `items i` on
  `id_item`; sums `ven_netas`, `imp_netos`, `vlrtot_bru`; groups to the
  document/seller/line grain. Filters: `fecha_dcto BETWEEN %s AND %s`,
  `id_tipdoc_fc NOT LIKE 'Z%%'` (exclude credit notes — note the **doubled `%%`** so
  it isn't read as a bind placeholder), `id_tipo = %s`, `trim(id_linea1) = %s`.
- **Destination DDL + upsert (85-135):** `CREATE TABLE IF NOT EXISTS` with a
  composite PK, then `INSERT ... ON CONFLICT (pk) DO UPDATE` — idempotent: re-running
  a day overwrites, never duplicates.
- **CLI (`parse_args_date_range`, 153-177):** `--date` OR `--start-date/--end-date`;
  default = **yesterday** in `America/Bogota`; accepts `YYYYMMDD` or `YYYY-MM-DD`.
- **fetch/load (179-230):** `pd.read_sql` per company; `load_destino` normalizes text
  columns, `execute_values(..., page_size=2000)` inside one transaction with
  `rollback()` on error.
- **main (232-246):** loops the 3 DBs, `pd.concat`, upserts, and prints the
  `... Upsert: <n>` line the runner parses.

---

## 5. Config, logging, dates (`pipeline_utils.py`)

- **`PipelineConfig`** (15-47): loads YAML; `get("a.b.c", default)` walks nested
  dicts by dotted path; `timezone` returns a `ZoneInfo` (default `America/Bogota`).
- **`PipelineLogger`** (50-106): one logger per component; a **file** handler
  (`<component>_<YYYYMMDD>.log`, DEBUG) + a **console** handler (INFO); format/date
  format come from `logging.*` config.
- **Dates:** `get_yesterday_yyyymmdd`, `get_month_range_yyyymmdd` (first-of-month..
  yesterday), `should_run_validation` (week-of-month = `((day-1)//7)+1`),
  plus `cleanup_old_logs` (mtime-based retention) and `format_duration`.

**Config keys the code reads** (union of `.get()` calls + README section Configuracion):
`timezone`; `paths.{etl_dir,logs_dir}`; `logging.{format,date_format,retention_days}`;
`execution.{max_retries,retry_delay_seconds,etl_timeout,validation_weeks}`;
`etls.parallel[]` and `etls.sequential[]` (each `{name, script, priority}`).

---

## 6. Scheduling (systemd) & JSON reports (`README.md`)

- **Timers:** `ventas-pipeline-daily.timer` → `OnCalendar=*-*-* 07:00:00`;
  `ventas-pipeline-monthly.timer` → `14:00`. The monthly **service runs daily** but
  the orchestrator's `validation_weeks` gate makes it a no-op except weeks 1 & 3
  (README section 2). Both call `pipeline_orchestrator.py --mode {daily,monthly}`.
- **Service user:** runs as **root** by default; the README (section Seguridad) explicitly
  recommends a dedicated non-root user. **The Dinastia scaffold makes non-root the
  default** (`systemd/*.service` ship `User=dinastia` + hardening).
- **JSON report shape** (orchestrator + README section Metricas):
  ```json
  { "run_type": "DAILY", "timestamp": "...-05:00",
    "summary": {"total_etls":6,"successful":6,"failed":0,"total_records":12543},
    "etls": [{"name":"fruver","status":"success","duration_seconds":45.3,
              "records_processed":2341,"attempts":1}] }
  ```

---

## 7. What the two sibling references add (why the scaffold is env-driven)

The ventas ETL hardcodes secrets, but the other Dinastia reports' references show
the pattern the framework should generalize:

- **`_reference/rotacion/etl_rotacion_v3.py`** — **env-var config** (`COMPANY_ENV`
  maps per-company `SRC_*` / `TARGET_*` env var names; `_env`/`_env_int` readers;
  optional `config/rotacion.env`). Adds run **modes** (`daily` / `rolling` /
  `backfill`), `--dry-run`, `--check-only`, DDL **indices**, batched upsert
  (`BATCH_SIZE=2000`), and business fixes (kit cost fallback to
  `costo_act_acum`/`ultimo_costo_ed`).
- **`_reference/margen/cargar_margen.py`** — **`.env.etl` file** (no secrets in
  code; `ETL_ENV_FILE` override), an **idempotent day-replace** load
  (`DELETE (fecha,empresa)` + `COPY`), `--dry-run` that only counts source rows,
  and explicit exit codes `0/1/2`.

**Takeaways baked into the Dinastia `common/`:** secrets come from the environment
(never literals); config is fail-closed; every ETL supports `--dry-run`; loads are
idempotent by date; row counts are reported to the orchestrator via a single stdout
marker.

---

## 8. Adaptation summary (reference → Dinastia scaffold)

| Concern | Mercamio reference | Dinastia scaffold |
|---------|--------------------|-------------------|
| Source DB | PostgreSQL, `psycopg2`, `pd.read_sql` | **MySQL 8.0**, `pymysql`, streaming `SSDictCursor` (`common/db.py`) |
| Source grain | per document (`cmmovimiento_pdv`+`items`) | per **(fecha, centro_operacion, linea)** — line profitability (`etl/ventas_rango.py`) |
| Secrets | **hardcoded** in `fruver_ventas_rango.py` | env-only via `${VAR}` expansion, fail-closed (`common/utils.PipelineConfig`) |
| Destination | fixed Postgres `produXdia` upsert | **pluggable GCP loader** (BigQuery / Cloud SQL stubs) (`common/loader.py`) |
| Row-count marker | `Upsert:` | `RECORDS_LOADED:` (back-compat with `Upsert:`) (`common/utils.RECORDS_MARKER`) |
| Interpreter | hardcoded `python3` | current interpreter (uv/venv-friendly) (`scripts/etl_runner.py`) |
| systemd user | root (README suggests changing) | **non-root** `dinastia` + hardening, by default |
| Orchestrator/runner/utils | as-is | same architecture, imports shared `common/` |
