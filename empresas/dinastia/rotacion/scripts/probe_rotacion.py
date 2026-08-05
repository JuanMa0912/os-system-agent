#!/usr/bin/env python3
"""
probe_rotacion — READ-ONLY smoke test of the rotacion source query.

Runs the SAME per-day query the ETL uses against the ERP for ONE day and prints
row count + a sample + totals. Touches NOTHING in GCP and writes NOTHING back to
MySQL (SELECT only). Validate on the Dinastia box before wiring the GCP load.

It uses the exact same day-params path as ``rotacion_rango.run()`` for a single
day (``day_params`` + ``build_source_sql``).

Usage:
  python scripts/probe_rotacion.py --date 20260721
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from common.db import build_source                              # noqa: E402
from common.utils import PipelineConfig, parse_args_date_range  # noqa: E402
from etl.rotacion_rango import (                                # noqa: E402
    DEFAULT_CATEGORIA, build_source_sql, day_params,
)

DEFAULT_CONFIG = "/opt/dinastia-rotacion/config/pipeline_config.yaml"
SAMPLE = 15


def main() -> int:
    args = parse_args_date_range()
    fecha_str = args.start_date  # single day (probe processes one day)
    config = PipelineConfig(os.environ.get("DINASTIA_ETL_CONFIG", DEFAULT_CONFIG))
    source = build_source(config)
    categoria = str(config.get("source.filters.categoria", DEFAULT_CATEGORIA) or DEFAULT_CATEGORIA).strip()

    sql = build_source_sql(categoria)
    params = day_params(fecha_str)

    print(f"[probe] source = {source.cfg.masked()}")
    print(f"[probe] dia = {fecha_str} | categoria={categoria}  (READ-ONLY)\n")

    seen = 0
    tot_ven = tot_costo = 0.0
    sample: list[dict] = []
    for batch in source.stream(sql, params, batch_size=5000):
        for r in batch:
            seen += 1
            tot_ven += float(r.get("venta_sin_impuesto") or 0)
            tot_costo += float(r.get("total_costo") or 0)
            if len(sample) < SAMPLE:
                sample.append(r)

    if not seen:
        print("[probe] 0 filas — revisa fecha/filtros.")
        return 0

    print(f"{'sede':<5} {'bod':<5} {'id_item':<10} {'nombre_item':<24} "
          f"{'cant_vend':>10} {'can_disp':>10} {'ult_vta_pdv':>12}")
    for r in sample:
        print(f"{str(r.get('sede')):<5} {str(r.get('bodega_local')):<5} "
              f"{str(r.get('id_item')):<10} {str(r.get('nombre_item'))[:24]:<24} "
              f"{float(r.get('cantidad_vendida') or 0):>10.2f} "
              f"{float(r.get('can_disponible_foto') or 0):>10.2f} "
              f"{str(r.get('ultima_venta_pdv') or ''):>12}")

    print(f"\n[probe] filas (item x sede x bodega): {seen}")
    print(f"[probe] TOTAL venta_sin_impuesto = {tot_ven:,.2f} | "
          f"total_costo = {tot_costo:,.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
