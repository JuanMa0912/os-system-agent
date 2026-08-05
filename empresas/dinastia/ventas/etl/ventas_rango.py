#!/usr/bin/env python3
"""
ETL — Dinastia "Ventas por línea" for a date range.

FAITHFUL 1:1 port of the Mercamio reference
``_reference/ventas/fruver_ventas_rango.py`` (PostgreSQL) to Dinastia. SAME fields,
SAME grain (one row per documento × categoria × línea), SAME filters — only:
  - source is MySQL ERP ``BD_BIABLE01`` via ``common.db.MySQLSource`` (pymysql);
  - destination is the config-selected GCP loader (``common.loader``);
  - Mercamio and Dinastia are BOTH Siesa/Biable ERPs, so tables/columns map almost
    verbatim: ``cmmovimiento_pdv``/``items`` -> ``CMMOVIMIENTO_PDV``/``ITEMS``.

Category/line filters (Mercamio hard-codes fruver: categoria='4', linea1='01') are
kept but adjustable via config ``source.filters`` (empty = all lines).

Runtime contract with the orchestrator/runner:
  - prints exactly one line ``RECORDS_LOADED: <n>`` on success (see RECORDS_MARKER);
  - exit 0 ok · 1 runtime error · 2 usage error · 3 schema-not-confirmed (scaffold).

Usage (on the Dinastia box):
  python etl/ventas_rango.py --date 20260721
  python etl/ventas_rango.py --start-date 20260701 --end-date 20260721
  python etl/ventas_rango.py --date 20260721 --dry-run   # validate wiring only
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

# Make `common` importable when run standalone (subprocess) or via the runner.
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from common.db import MySQLSource, build_source  # noqa: E402
from common.loader import GcpLoader, TargetSchema, WriteMode, build_loader  # noqa: E402
from common.utils import (  # noqa: E402
    RECORDS_MARKER,
    PipelineConfig,
    get_module_logger,
    parse_args_date_range,
)

LOG = get_module_logger("etl_ventas_rango")
DEFAULT_CONFIG = "/opt/dinastia-ventas/config/pipeline_config.yaml"

EMPRESA = "dinastia"  # literal empresa_bd stamped on every row (multi-empresa parity)

# The query is grounded in confirmed BD_BIABLE01 tables/columns (Siesa/Biable) and
# mirrors the proven Mercamio query, so it is ready to run. (Guard kept for parity
# with the framework template.)
SCHEMA_CONFIRMED = True
_SCHEMA_TODO_MESSAGE = "SCHEMA_CONFIRMED is False (scaffold guard). Set it True to run."

# ============================================================================
# TARGET SCHEMA — identical shape to Mercamio's ``ventas_fruver`` (same fields).
# Grain: one row per (empresa_bd, centro_operacion, sede, caja, fecha_dcto,
# id_tipdoc_fc, documento_fc, id_vend_cc, categoria, linea).
# fecha_dcto stays TEXT 'YYYYMMDD' exactly like the reference.
# ============================================================================
TARGET_SCHEMA = TargetSchema(
    table="ventas",
    columns={
        "empresa_bd": "string",
        "centro_operacion": "string",
        "sede": "string",
        "nombre_sede": "string",
        "caja": "string",
        "fecha_dcto": "string",            # YYYYMMDD (text, like Mercamio)
        "id_tipdoc_fc": "string",
        "documento_fc": "string",
        "hora_final_hora": "time",
        "venta_sin_impuesto": "numeric",
        "impuesto": "numeric",
        "venta_con_impuesto": "numeric",
        "total_bruto": "numeric",
        "id_vend_cc": "string",
        "vendedor": "string",
        "categoria": "string",
        "linea": "string",
        "fecha_carga": "timestamp",        # load timestamp (audit)
    },
    primary_key=(
        "empresa_bd", "centro_operacion", "sede", "caja", "fecha_dcto",
        "id_tipdoc_fc", "documento_fc", "id_vend_cc", "categoria", "linea",
    ),
    partition_field="fecha_dcto",
)

# ============================================================================
# SOURCE QUERY — 1:1 with Mercamio SQL_ORIGEN, mapped to BD_BIABLE01 (MySQL).
#
# pymysql paramstyle is 'format' (%s). Literal '%' is DOUBLED to '%%' so it is
# not read as a placeholder — the Z-exclusion ('Z%%') and the date masks.
# HORA_FINAL is char(4) 'HHMM'; we render it to a 'HH:MM:SS' text so the target
# ``time`` column casts cleanly (avoids the pymysql TIME->timedelta quirk).
# {extra_filters} is injected from config (categoria / linea1); empty = all lines.
#
# NOTE vs Mercamio: Mercamio joined items only on id_item; Dinastia's ITEMS has
# the (ID_ITEM, ID_EXT_ITM) key, so we also match ID_EXT_ITM = m.ID_ITMEXT to keep
# the join 1:1 (no fan-out that would inflate the SUMs). Same fields, same result.
# ============================================================================
SOURCE_SQL_TEMPLATE = """
SELECT
    '{empresa}'                     AS empresa_bd,
    m.ID_CO                         AS centro_operacion,
    m.ID_SUC                        AS sede,
    (SELECT TRIM(co.DESCRIPCION) FROM CENTRO_OPERACION co
      WHERE TRIM(co.CODIGO) = TRIM(m.ID_CO) LIMIT 1) AS nombre_sede,
    m.ID_CAJA                       AS caja,
    m.FECHA_DCTO                    AS fecha_dcto,
    m.ID_TIPDOC_FC                  AS id_tipdoc_fc,
    m.DOCUMENTO_FC                  AS documento_fc,

    MAX(DATE_FORMAT(STR_TO_DATE(LPAD(m.HORA_FINAL, 4, '0'), '%%H%%i'), '%%H:%%i:%%s')) AS hora_final_hora,

    SUM(m.VEN_NETAS)                AS venta_sin_impuesto,
    SUM(m.IMP_NETOS)                AS impuesto,
    SUM(m.VEN_NETAS + m.IMP_NETOS)  AS venta_con_impuesto,
    SUM(m.VLRTOT_BRU)               AS total_bruto,

    m.ID_VEND_CC                    AS id_vend_cc,
    MAX(m.VEND_CC_DESC)             AS vendedor,

    i.ID_TIPO                       AS categoria,
    TRIM(i.ID_LINEA1)               AS linea
FROM CMMOVIMIENTO_PDV m
JOIN ITEMS i
      ON i.ID_ITEM = m.ID_ITEM
     AND i.ID_EXT_ITM = m.ID_ITMEXT
WHERE m.FECHA_DCTO BETWEEN %s AND %s
  AND m.ID_TIPDOC_FC NOT LIKE 'Z%%'
{extra_filters}
GROUP BY
    m.ID_CO, m.ID_SUC, m.ID_CAJA, m.FECHA_DCTO, m.ID_TIPDOC_FC, m.DOCUMENTO_FC,
    m.ID_VEND_CC, i.ID_TIPO, TRIM(i.ID_LINEA1)
ORDER BY
    m.FECHA_DCTO, m.ID_TIPDOC_FC, m.DOCUMENTO_FC
"""


def build_source_sql(categoria: str, linea1: str) -> tuple[str, list]:
    """Return ``(sql, filter_params)``. The category/line filters mirror Mercamio's
    CATEGORIA / LINEA1 but are optional (empty string = do not filter)."""
    extra = ""
    params: list = []
    if categoria:
        extra += "  AND i.ID_TIPO = %s\n"
        params.append(categoria)
    if linea1:
        extra += "  AND TRIM(i.ID_LINEA1) = %s\n"
        params.append(linea1)
    sql = SOURCE_SQL_TEMPLATE.format(empresa=EMPRESA, extra_filters=extra)
    return sql, params


# ---------------------------------------------------------------------------
# Extract
# ---------------------------------------------------------------------------
def extract(source: MySQLSource, start_date: str, end_date: str,
            categoria: str = "", linea1: str = "",
            batch_size: int = 5000) -> Iterable[list[dict]]:
    """Stream sales-by-line rows for the date range from the ERP (read-only)."""
    if not SCHEMA_CONFIRMED:
        raise NotImplementedError(_SCHEMA_TODO_MESSAGE)
    sql, filter_params = build_source_sql(categoria, linea1)
    params = (start_date, end_date, *filter_params)
    LOG.info("Extracting %s..%s (categoria=%s, linea1=%s) from %s",
             start_date, end_date, categoria or "ALL", linea1 or "ALL", source.cfg.masked())
    yield from source.stream(sql, params, batch_size=batch_size)


# ---------------------------------------------------------------------------
# Transform  (mirrors Mercamio's load normalization: text cols -> str, keep
# numerics, add the load timestamp. NO derived cost/rentabilidad — same as ref.)
# ---------------------------------------------------------------------------
def transform(rows: Iterable[Mapping[str, Any]]) -> list[dict]:
    now = datetime.now()
    out: list[dict] = []
    for r in rows:
        out.append({
            "empresa_bd": _txt(r.get("empresa_bd")) or EMPRESA,
            "centro_operacion": _txt(r.get("centro_operacion")),
            "sede": _txt(r.get("sede")),
            "nombre_sede": _txt(r.get("nombre_sede")),
            "caja": _txt(r.get("caja")),
            "fecha_dcto": _txt(r.get("fecha_dcto")),
            "id_tipdoc_fc": _txt(r.get("id_tipdoc_fc")),
            "documento_fc": _txt(r.get("documento_fc")),
            "hora_final_hora": r.get("hora_final_hora"),  # 'HH:MM:SS' text or None
            "venta_sin_impuesto": _num(r.get("venta_sin_impuesto")),
            "impuesto": _num(r.get("impuesto")),
            "venta_con_impuesto": _num(r.get("venta_con_impuesto")),
            "total_bruto": _num(r.get("total_bruto")),
            "id_vend_cc": _txt(r.get("id_vend_cc")),
            "vendedor": _txt(r.get("vendedor")),
            "categoria": _txt(r.get("categoria")),
            "linea": _txt(r.get("linea")),
            "fecha_carga": now,
        })
    return out


def _num(value: Any) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _txt(value: Any) -> str:
    return "" if value is None else str(value).strip()


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------
def load(loader: GcpLoader, rows: list[dict], mode: WriteMode) -> int:
    """Ensure the target exists, then write the rows via the GCP loader."""
    if not rows:
        return 0
    loader.ensure_target(TARGET_SCHEMA)
    return loader.load(rows, TARGET_SCHEMA, mode=mode)


# ---------------------------------------------------------------------------
# Config / orchestration for a single ETL run
# ---------------------------------------------------------------------------
def _resolve_config_path() -> str:
    return os.environ.get("DINASTIA_ETL_CONFIG", DEFAULT_CONFIG)


def _write_mode(config: PipelineConfig) -> WriteMode:
    target_type = config.get("target.type", "")
    raw = config.get(f"target.{target_type}.write_mode", "upsert") if target_type else "upsert"
    try:
        return WriteMode(raw)
    except ValueError:
        LOG.warning("Unknown write_mode=%r; defaulting to upsert", raw)
        return WriteMode.UPSERT


def run(start_date: str, end_date: str, dry_run: bool) -> int:
    config_path = _resolve_config_path()
    LOG.info("Config: %s", config_path)
    config = PipelineConfig(config_path)  # fails closed if a ${ENV} secret is missing

    source = build_source(config)          # validates source.mysql.* keys
    mode = _write_mode(config)
    categoria = str(config.get("source.filters.categoria", "") or "").strip()
    linea1 = str(config.get("source.filters.linea1", "") or "").strip()

    if dry_run:
        # Validate wiring only — do NOT touch the ERP or GCP.
        LOG.info("[DRY-RUN] source = %s", source.cfg.masked())
        LOG.info("[DRY-RUN] target = %s (write_mode=%s)", config.get("target.type"), mode.value)
        LOG.info("[DRY-RUN] target table = %s, pk=%s",
                 TARGET_SCHEMA.table, ",".join(TARGET_SCHEMA.primary_key))
        LOG.info("[DRY-RUN] range = %s..%s | categoria=%s linea1=%s",
                 start_date, end_date, categoria or "ALL", linea1 or "ALL")
        print(f"{RECORDS_MARKER} 0")
        return 0

    if not SCHEMA_CONFIRMED:
        LOG.error(_SCHEMA_TODO_MESSAGE)
        return 3

    loader = build_loader(config)          # validates target.type
    try:
        total = 0
        for batch in extract(source, start_date, end_date, categoria, linea1):
            total += load(loader, transform(batch), mode)
        LOG.info("Loaded range %s..%s -> %s", start_date, end_date, TARGET_SCHEMA.table)
        # Machine-readable marker parsed by scripts/etl_runner.py:
        print(f"{RECORDS_MARKER} {total}")
        return 0
    finally:
        loader.close()


def main() -> int:
    try:
        args = parse_args_date_range()
    except ValueError as exc:
        print(f"USAGE ERROR: {exc}", file=sys.stderr)
        return 2
    try:
        return run(args.start_date, args.end_date, args.dry_run)
    except NotImplementedError as exc:
        LOG.error(str(exc))
        return 3
    except Exception as exc:  # noqa: BLE001
        LOG.error("ETL failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
