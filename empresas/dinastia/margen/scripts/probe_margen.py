#!/usr/bin/env python3
"""
probe_margen — READ-ONLY smoke test of the margen source query.

Runs the SAME query the ETL uses against the ERP for a date range and prints row
count + a sample + totals (ventas, costo, margen). Touches NOTHING in GCP and
writes NOTHING back to MySQL (SELECT only). Validate on the Dinastia box before
wiring the GCP load.

Usage:
  python scripts/probe_margen.py --date 20260721
  python scripts/probe_margen.py --start-date 20260701 --end-date 20260721
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
from etl.margen_rango import (                           # noqa: E402
    build_source_sql, DEFAULT_CATEGORIA, DEFAULT_LINEA_IMPOCONSUMO,
)

DEFAULT_CONFIG = "/opt/dinastia-margen/config/pipeline_config.yaml"
SAMPLE = 15


def main() -> int:
    args = parse_args_date_range()
    config = PipelineConfig(os.environ.get("DINASTIA_ETL_CONFIG", DEFAULT_CONFIG))
    source = build_source(config)
    categoria = str(config.get("source.filters.categoria", DEFAULT_CATEGORIA) or DEFAULT_CATEGORIA).strip()
    linea_impo = str(config.get("source.filters.linea_impoconsumo", DEFAULT_LINEA_IMPOCONSUMO) or "").strip()
    id_empresa = str(config.get("source.filters.id_empresa", "") or "").strip()
    sql = build_source_sql(categoria, linea_impo, id_empresa)

    print(f"[probe] source = {source.cfg.masked()}")
    print(f"[probe] range = {args.start_date}..{args.end_date} | "
          f"categoria={categoria} | impoconsumo_linea={linea_impo}  (READ-ONLY)\n")

    seen = 0
    tot_ven = tot_costo = 0.0
    sample: list[dict] = []
    for batch in source.stream(sql, (args.start_date, args.end_date), batch_size=5000):
        for r in batch:
            seen += 1
            tot_ven += float(r.get("ven_totales") or 0)
            tot_costo += float(r.get("tot_costo") or 0)
            if len(sample) < SAMPLE:
                sample.append(r)

    if not seen:
        print("[probe] 0 filas — revisa rango/filtros.")
        return 0

    print(f"{'fecha':<9} {'co':<4} {'lin1':<5} {'nombre_linea1':<22} "
          f"{'cant':>7} {'ven_tot':>13} {'costo':>13}")
    for r in sample:
        print(f"{str(r.get('fecha_dcto')):<9} {str(r.get('id_co')):<4} "
              f"{str(r.get('id_linea1')):<5} {str(r.get('nombre_linea1'))[:22]:<22} "
              f"{float(r.get('cantidad') or 0):>7.1f} "
              f"{float(r.get('ven_totales') or 0):>13,.2f} "
              f"{float(r.get('tot_costo') or 0):>13,.2f}")

    margen = tot_ven - tot_costo
    pct = (margen / tot_ven * 100.0) if tot_ven else 0.0
    print(f"\n[probe] filas (renglones): {seen}")
    print(f"[probe] TOTAL ven_totales = {tot_ven:,.2f} | tot_costo = {tot_costo:,.2f} | "
          f"margen = {margen:,.2f} ({pct:.2f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
