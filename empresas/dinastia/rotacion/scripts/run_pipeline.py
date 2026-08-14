#!/usr/bin/env python3
"""
run_pipeline — Dinastia ROTACION: runner daily/weekly/monthly + rango + refresh.

Calcula el rango (por modo o explícito) y corre el ETL EN PROCESO (carga idempotente
por día vía replace_by_date). Al terminar (si NO es dry-run): **refresca las vistas /
funciones configuradas** (`target.cloudsql_postgres.refresh_views`) y manda un reporte
por Telegram (bot cortana). Ver common/post_run.py.

  --mode daily|weekly|monthly           ayer | últimos 8 días | mes-a-la-fecha
  --start-date YYYYMMDD --end-date …    rango explícito (backfill) — TAMBIÉN refresca
  --refresh-only                        NO carga; solo refresca las vistas/funciones
  --dry-run                             valida sin cargar (no refresca ni reporta)

Exit codes (los del ETL): 0 ok · 1 error · 2 uso · 3 esquema sin confirmar.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from common.post_run import refresh_materialized_views, report_run  # noqa: E402
from common.utils import (  # noqa: E402
    PipelineConfig,
    get_module_logger,
    get_month_range_yyyymmdd,
    get_yesterday_yyyymmdd,
    validate_yyyymmdd,
)
from etl.rotacion_rango import TARGET_SCHEMA, _resolve_config_path, run  # noqa: E402

PIPELINE = "rotacion"
WEEKLY_DAYS = 8
LOG = get_module_logger(f"run_pipeline_{PIPELINE}")


# Dias que el daily arrastra hacia atras ademas de ayer. NO es paranoia: la sede
# 002 postea al ERP con 2-3 dias de retraso (medido el 2026-08-14 — el 11 y el 12
# entraron a GCP con la 001 solamente, $625M sin cargar). Cargando solo D-1 esos
# dias quedan a medias y nadie los vuelve a mirar hasta el weekly del sabado, o
# sea hasta 7 dias despues. Con la ventana, el daily del dia siguiente los recoge
# solo. Sobre-escribir un dia ya completo es inofensivo: la fuente es la misma.
DAILY_LOOKBACK_DEFAULT = 4


def _range_for_mode(mode: str, config) -> tuple[str, str]:
    tz = config.timezone
    if mode == "daily":
        day = get_yesterday_yyyymmdd(tz)
        try:
            back = int(config.get("source.daily_lookback_days", DAILY_LOOKBACK_DEFAULT))
        except (TypeError, ValueError):
            back = DAILY_LOOKBACK_DEFAULT
        back = max(0, back)
        if not back:
            return day, day
        start = datetime.strptime(day, "%Y%m%d").date() - timedelta(days=back)
        return start.strftime("%Y%m%d"), day
    if mode == "monthly":
        return get_month_range_yyyymmdd(tz)
    # weekly: ultimos WEEKLY_DAYS dias hasta ayer
    yesterday = datetime.now(tz).date() - timedelta(days=1)
    start = yesterday - timedelta(days=WEEKLY_DAYS - 1)
    return start.strftime("%Y%m%d"), yesterday.strftime("%Y%m%d")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=f"Dinastia {PIPELINE} — runner (modo/rango) con refresco de vistas."
    )
    parser.add_argument("--mode", choices=["daily", "weekly", "monthly"], default=None,
                        help="daily=ayer, weekly=8 días, monthly=mes-a-la-fecha")
    parser.add_argument("--start-date", dest="start_date", default=None,
                        help="rango explícito (con --end-date); backfill que TAMBIÉN refresca")
    parser.add_argument("--end-date", dest="end_date", default=None)
    parser.add_argument("--refresh-only", action="store_true",
                        help="NO carga; solo refresca las vistas/funciones configuradas")
    parser.add_argument("--dry-run", action="store_true",
                        help="valida el cableado sin ejecutar ni cargar")
    args = parser.parse_args()

    config = PipelineConfig(_resolve_config_path())
    table = config.get("target.cloudsql_postgres.table", TARGET_SCHEMA.table)

    # --refresh-only: solo refresca (útil tras un backfill con el ETL directo).
    if args.refresh_only:
        LOG.info("[refresh-only] refrescando vistas/funciones de %s ...", table)
        refresh_materialized_views(config, LOG)
        return 0

    # Rango: explícito (--start-date/--end-date) o por --mode.
    if args.start_date or args.end_date:
        if not (args.start_date and args.end_date):
            print("ERROR: --start-date y --end-date van juntos.", file=sys.stderr)
            return 2
        start_date = validate_yyyymmdd(args.start_date)
        end_date = validate_yyyymmdd(args.end_date)
        label = "range"
    elif args.mode:
        start_date, end_date = _range_for_mode(args.mode, config)
        label = args.mode
    else:
        print("ERROR: pasa --mode daily|weekly|monthly, o --start-date/--end-date, "
              "o --refresh-only.", file=sys.stderr)
        return 2

    print(f"[run_pipeline] {PIPELINE} mode={label} range={start_date}..{end_date} "
          f"dry_run={args.dry_run}", file=sys.stderr)

    rc = run(start_date, end_date, args.dry_run)

    # Post-proceso solo en corrida real. Nunca rompe la corrida (fallan-suave).
    if not args.dry_run:
        refresh_result = []
        try:
            refresh_result = refresh_materialized_views(
                config, LOG, start_date=start_date, end_date=end_date)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("refresco de vistas falló: %s", exc)
        try:
            report_run(config, table=table, partition_field=TARGET_SCHEMA.partition_field,
                       mode=label, start_date=start_date, end_date=end_date,
                       ok=(rc == 0), refresh_result=refresh_result, logger=LOG)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("reporte Telegram falló: %s", exc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
