#!/usr/bin/env python3
"""
probe_ventas — READ-ONLY smoke test of the ventas source query.

Runs the SAME query the ETL uses against the ERP for a date range and prints row
count + a sample + totals. Touches NOTHING in GCP and writes NOTHING back to MySQL
(SELECT only, read-only session). Use it on the Dinastia box to confirm the query
returns sane numbers BEFORE wiring the GCP load.

Usage:
  python scripts/probe_ventas.py --date 20260721
  python scripts/probe_ventas.py --start-date 20260701 --end-date 20260721
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from common.db import build_source                       # noqa: E402
from common.utils import PipelineConfig, parse_args_date_range  # noqa: E402
from etl.ventas_rango import build_source_sql            # noqa: E402

DEFAULT_CONFIG = "/opt/dinastia-ventas/config/pipeline_config.yaml"
SAMPLE = 15


def main() -> int:
    args = parse_args_date_range()
    config = PipelineConfig(os.environ.get("DINASTIA_ETL_CONFIG", DEFAULT_CONFIG))
    source = build_source(config)
    categoria = str(config.get("source.filters.categoria", "") or "").strip()
    linea1 = str(config.get("source.filters.linea1", "") or "").strip()
    sql, filter_params = build_source_sql(categoria, linea1)
    params = (args.start_date, args.end_date, *filter_params)

    print(f"[probe] source = {source.cfg.masked()}")
    print(f"[probe] range  = {args.start_date}..{args.end_date} | "
          f"categoria={categoria or 'ALL'} | linea1={linea1 or 'ALL'}  (READ-ONLY)\n")

    seen = 0
    tot_sin = tot_con = tot_bru = 0.0
    sample: list[dict] = []
    for batch in source.stream(sql, params, batch_size=5000):
        for r in batch:
            seen += 1
            tot_sin += float(r.get("venta_sin_impuesto") or 0)
            tot_con += float(r.get("venta_con_impuesto") or 0)
            tot_bru += float(r.get("total_bruto") or 0)
            if len(sample) < SAMPLE:
                sample.append(r)

    if not seen:
        print("[probe] 0 filas — revisa que el rango tenga ventas o los filtros.")
        return 0

    print(f"{'fecha':<9} {'co':<4} {'caja':<5} {'tipdoc':<7} {'doc':<8} "
          f"{'cat':<4} {'linea':<8} {'venta_s/imp':>14} {'hora'}")
    for r in sample:
        print(f"{str(r.get('fecha_dcto')):<9} {str(r.get('centro_operacion')):<4} "
              f"{str(r.get('caja')):<5} {str(r.get('id_tipdoc_fc')):<7} "
              f"{str(r.get('documento_fc')):<8} {str(r.get('categoria')):<4} "
              f"{str(r.get('linea')):<8} {float(r.get('venta_sin_impuesto') or 0):>14,.2f} "
              f"{r.get('hora_final_hora')}")

    print(f"\n[probe] filas (documento x linea): {seen}")
    print(f"[probe] TOTAL venta_sin_imp = {tot_sin:,.2f} | "
          f"venta_con_imp = {tot_con:,.2f} | total_bruto = {tot_bru:,.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
