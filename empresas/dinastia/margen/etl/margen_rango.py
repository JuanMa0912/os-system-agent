#!/usr/bin/env python3
"""
ETL — Dinastia "Margen" (movimiento unificado con costo/margen) para un rango.

Port FIEL 1:1 de la referencia Mercamio ``_reference/margen/cargar_margen.py``
(PostgreSQL) a Dinastia. MISMOS campos, MISMO grano (un renglón por línea de
movimiento POS), MISMA lógica (CTE de costo de kits, exclusión ``Z%``, impoconsumo
de licores, fallback de costo). Solo cambia:
  - fuente MySQL ERP ``BD_BIABLE01`` vía ``common.db.MySQLSource`` (pymysql);
  - destino = loader Cloud SQL de config (``common.loader``, ``replace_by_date``);
  - los CÓDIGOS de Dinastia (ver memoria dinastia-etl-mapeos-mercamio):
      · categoría "mercado":  Mercamio '3','4'  ->  Dinastia '1'
      · línea impoconsumo:    Mercamio '33'      ->  Dinastia '50' (LICORES)
  - port PG->MySQL: sin MATERIALIZED, TRIM en joins a LINEAS (char(6) vs char(7)),
    join a ITEMS por (ID_ITEM, ID_EXT_ITM) para no duplicar renglones.

Idempotencia: ``replace_by_date`` borra la partición (empresa, fecha_dcto) y
reinserta — equivale al "DELETE día + COPY" del original. Re-correr NO duplica.

Contrato con el orquestador: imprime ``RECORDS_LOADED: <n>`` al terminar.
  exit 0 ok · 1 error · 2 uso · 3 schema-no-confirmado.

Uso (en el box de Dinastia):
  python etl/margen_rango.py --date 20260721
  python etl/margen_rango.py --start-date 20260701 --end-date 20260721
  python etl/margen_rango.py --date 20260721 --dry-run
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

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

LOG = get_module_logger("etl_margen_rango")
DEFAULT_CONFIG = "/opt/dinastia-margen/config/pipeline_config.yaml"

EMPRESA = "dinastia"
SCHEMA_CONFIRMED = True
_SCHEMA_TODO_MESSAGE = "SCHEMA_CONFIRMED is False (scaffold guard). Set it True to run."

# Filtros/códigos Dinastia (ajustables por config source.filters). Defaults =
# lo descubierto el 2026-07-22.
DEFAULT_CATEGORIA = "1"           # ITEMS.ID_TIPO 'mercado' (Mercamio usaba '3','4')
DEFAULT_LINEA_IMPOCONSUMO = "50,51"  # LICORES(50) + CIGARRILLOS(51); coma-separado (Mercamio usaba '33')

# ============================================================================
# TARGET SCHEMA — mismos campos que ``margen_final`` de Mercamio (sin el serial).
# Grano fino: un renglón por línea de movimiento. Sin PK de negocio: la
# idempotencia es por reemplazo del día (replace_by_date sobre fecha_dcto).
# ============================================================================
TARGET_SCHEMA = TargetSchema(
    table="margen",
    columns={
        "empresa": "string",
        "id_empresa": "string",
        "fecha_dcto": "string",         # YYYYMMDD (text)
        "id_co": "string",
        "nombre_sede": "string",
        "id_caja": "string",
        "hora_final": "string",         # 'HHMM' crudo (como el original)
        "id_item": "string",
        "item_descripcion": "string",
        "id_tipo": "string",
        "id_linea1": "string",
        "nombre_linea1": "string",
        "id_linea2": "string",
        "nombre_linea2": "string",
        "id_linea": "string",
        "nombre_linea": "string",
        "id_unidad": "string",
        "cantidad": "numeric",
        "precio_uni": "numeric",
        "dscto_netos": "numeric",
        "vlrtot_bru": "numeric",
        "vlrimpcon1": "numeric",
        "ven_totales": "numeric",
        "precio_unitario": "numeric",
        "tot_costo": "numeric",
        "costo_unitario": "numeric",
        "documento_fc": "string",
        "id_tipdoc_fc": "string",
        "vend_cc": "string",
        "vend_cc_desc": "string",
        "documento_docfc": "string",
        "id_terc": "string",
        "nombre_terc": "string",
        "fecha_carga": "timestamp",
    },
    primary_key=(),                      # sin PK de negocio (day-replace)
    partition_field="fecha_dcto",
)

# ============================================================================
# SOURCE QUERY — 1:1 con SQL_TEMPLATE de Mercamio, portada a BD_BIABLE01 (MySQL).
#
# {empresa}/{id_empresa}/{categoria}/{linea_impoconsumo_in} se inyectan por .format
# (constantes de config). El rango de fechas va como %s (2 placeholders). Literal
# '%' doblado a '%%' para pymysql (solo en 'Z%%').
# ============================================================================
SOURCE_SQL_TEMPLATE = """
WITH mv AS (
    SELECT *
    FROM CMMOVIMIENTO_PDV
    WHERE FECHA_DCTO BETWEEN %s AND %s
      AND ID_TIPDOC_FC NOT LIKE 'Z%%'
),
costo_kit AS (
    SELECT
        vk.ID_COD_ITEM_P AS id_kit,
        SUM(COALESCE(ic.ULTIMO_COSTO_ED, 0)
            * COALESCE(vk.CANTIDAD, 0)
            * COALESCE(vk.FACTOR, 1))                    AS costo_unitario_kit
    FROM V_KITS vk
    JOIN ITEMS ic ON ic.ID_ITEM = vk.ID_COD_ITEM_C
    GROUP BY vk.ID_COD_ITEM_P
)
SELECT STRAIGHT_JOIN
    '{empresa}'                          AS empresa,
    '{id_empresa}'                       AS id_empresa,
    m.FECHA_DCTO                         AS fecha_dcto,
    m.ID_CO                              AS id_co,
    (SELECT TRIM(co.DESCRIPCION) FROM CENTRO_OPERACION co
      WHERE TRIM(co.CODIGO) = TRIM(m.ID_CO) LIMIT 1) AS nombre_sede,
    m.ID_CAJA                            AS id_caja,
    m.HORA_FINAL                         AS hora_final,
    m.ID_ITEM                            AS id_item,
    i.DESCRIPCION                        AS item_descripcion,
    i.ID_TIPO                            AS id_tipo,
    i.ID_LINEA1                          AS id_linea1,
    l1.CMLINEAS_DESCRIPCION              AS nombre_linea1,
    i.ID_LINEA2                          AS id_linea2,
    l2.CMLINEAS_DESCRIPCION              AS nombre_linea2,
    i.ID_LINEA                           AS id_linea,
    l3.CMLINEAS_DESCRIPCION              AS nombre_linea,
    m.ID_UNIDAD                          AS id_unidad,
    m.CANTIDAD                           AS cantidad,
    m.PRECIO_UNI                         AS precio_uni,
    m.DSCTO_NETOS                        AS dscto_netos,
    -- Impoconsumo: para las líneas afectas (LICORES 50 + CIGARRILLOS 51) el bruto
    -- incluye el impoconsumo. ven_totales usa el bruto ORIGINAL, así no se duplica.
    CASE
        WHEN TRIM(i.ID_LINEA1) IN ({linea_impoconsumo_in})
        THEN m.VLRTOT_BRU + COALESCE(m.VLRIMPCON1, 0)
        ELSE m.VLRTOT_BRU
    END                                  AS vlrtot_bru,
    m.VLRIMPCON1                         AS vlrimpcon1,
    (m.VLRTOT_BRU + m.VLRIMPCON1)        AS ven_totales,
    ROUND((m.VLRTOT_BRU + m.VLRIMPCON1) / NULLIF(m.CANTIDAD, 0), 2) AS precio_unitario,
    -- Fix kits: si el ERP registra tot_costo=0, calcular desde costo unitario.
    CASE
        WHEN m.TOT_COSTO > 0 THEN m.TOT_COSTO
        ELSE ROUND(COALESCE(ck.costo_unitario_kit, i.ULTIMO_COSTO_ED, 0) * m.CANTIDAD, 2)
    END                                  AS tot_costo,
    CASE
        WHEN m.TOT_COSTO > 0 THEN ROUND(m.TOT_COSTO / NULLIF(m.CANTIDAD, 0), 2)
        ELSE ROUND(COALESCE(ck.costo_unitario_kit, i.ULTIMO_COSTO_ED, 0), 2)
    END                                  AS costo_unitario,
    m.DOCUMENTO_FC                       AS documento_fc,
    m.ID_TIPDOC_FC                       AS id_tipdoc_fc,
    m.VEND_CC                            AS vend_cc,
    m.VEND_CC_DESC                       AS vend_cc_desc,
    m.DOCUMENTO_DOCFC                    AS documento_docfc,
    m.ID_TERC                            AS id_terc,
    t.DESCRIPCION                        AS nombre_terc
FROM mv m
JOIN ITEMS i
      ON i.ID_ITEM = m.ID_ITEM
     AND i.ID_EXT_ITM = m.ID_ITMEXT
JOIN LINEAS l1 ON TRIM(l1.ID_LINEA) = TRIM(i.ID_LINEA1) AND l1.ID_TIPO = i.ID_TIPO
JOIN LINEAS l2 ON TRIM(l2.ID_LINEA) = TRIM(i.ID_LINEA2) AND l2.ID_TIPO = i.ID_TIPO
JOIN LINEAS l3 ON TRIM(l3.ID_LINEA) = TRIM(i.ID_LINEA)  AND l3.ID_TIPO = i.ID_TIPO
LEFT JOIN costo_kit ck ON ck.id_kit = i.ID_ITEM
LEFT JOIN TERCEROS t   ON t.CODIGO = m.ID_TERC AND t.SUCURSAL = m.ID_SUC
WHERE i.ID_TIPO = '{categoria}'
"""


def build_source_sql(categoria: str, linea_impoconsumo: str, id_empresa: str) -> str:
    """Render the margen SQL with Dinastia codes injected (dates stay as %s).

    ``linea_impoconsumo`` acepta VARIAS líneas separadas por coma (ej. "50,51" =
    LICORES + CIGARRILLOS); se rinde como lista SQL ``IN ('50','51')``. Vacío = la
    condición nunca hace match (nadie recibe el ajuste de impoconsumo).
    """
    lineas = [x.strip() for x in str(linea_impoconsumo).split(",") if x.strip()]
    linea_in = ", ".join("'{}'".format(x) for x in lineas) if lineas else "''"
    return SOURCE_SQL_TEMPLATE.format(
        empresa=EMPRESA,
        id_empresa=id_empresa,
        categoria=categoria,
        linea_impoconsumo_in=linea_in,
    )


# ---------------------------------------------------------------------------
# Extract
# ---------------------------------------------------------------------------
def extract(source: MySQLSource, start_date: str, end_date: str,
            categoria: str, linea_impoconsumo: str, id_empresa: str,
            batch_size: int = 5000) -> Iterable[list[dict]]:
    if not SCHEMA_CONFIRMED:
        raise NotImplementedError(_SCHEMA_TODO_MESSAGE)
    sql = build_source_sql(categoria, linea_impoconsumo, id_empresa)
    LOG.info("Extracting %s..%s (categoria=%s, impoconsumo_linea=%s) from %s",
             start_date, end_date, categoria, linea_impoconsumo, source.cfg.masked())
    yield from source.stream(sql, (start_date, end_date), batch_size=batch_size)


# ---------------------------------------------------------------------------
# Transform  (pass-through + timestamp; sin derivar nada — igual que el original)
# ---------------------------------------------------------------------------
_TEXT_COLS = (
    "empresa", "id_empresa", "fecha_dcto", "id_co", "nombre_sede", "id_caja", "hora_final",
    "id_item", "item_descripcion", "id_tipo", "id_linea1", "nombre_linea1",
    "id_linea2", "nombre_linea2", "id_linea", "nombre_linea", "id_unidad",
    "documento_fc", "id_tipdoc_fc", "vend_cc", "vend_cc_desc", "documento_docfc",
    "id_terc", "nombre_terc",
)
_NUM_COLS = (
    "cantidad", "precio_uni", "dscto_netos", "vlrtot_bru", "vlrimpcon1",
    "ven_totales", "precio_unitario", "tot_costo", "costo_unitario",
)


def transform(rows: Iterable[Mapping[str, Any]]) -> list[dict]:
    now = datetime.now()
    out: list[dict] = []
    for r in rows:
        row = {c: _txt(r.get(c)) for c in _TEXT_COLS}
        row.update({c: _num(r.get(c)) for c in _NUM_COLS})
        row["fecha_carga"] = now
        out.append(row)
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
    if not rows:
        return 0
    loader.ensure_target(TARGET_SCHEMA)
    return loader.load(rows, TARGET_SCHEMA, mode=mode)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _resolve_config_path() -> str:
    return os.environ.get("DINASTIA_ETL_CONFIG", DEFAULT_CONFIG)


def _write_mode(config: PipelineConfig) -> WriteMode:
    target_type = config.get("target.type", "")
    raw = config.get(f"target.{target_type}.write_mode", "replace_by_date") if target_type else "replace_by_date"
    try:
        return WriteMode(raw)
    except ValueError:
        LOG.warning("Unknown write_mode=%r; defaulting to replace_by_date", raw)
        return WriteMode.REPLACE_BY_DATE


def run(start_date: str, end_date: str, dry_run: bool) -> int:
    config = PipelineConfig(_resolve_config_path())
    source = build_source(config)
    mode = _write_mode(config)
    categoria = str(config.get("source.filters.categoria", DEFAULT_CATEGORIA) or DEFAULT_CATEGORIA).strip()
    linea_impo = str(config.get("source.filters.linea_impoconsumo", DEFAULT_LINEA_IMPOCONSUMO) or "").strip()
    id_empresa = str(config.get("source.filters.id_empresa", "") or "").strip()

    if dry_run:
        LOG.info("[DRY-RUN] source=%s target=%s (%s) table=%s range=%s..%s cat=%s",
                 source.cfg.masked(), config.get("target.type"), mode.value,
                 TARGET_SCHEMA.table, start_date, end_date, categoria)
        print(f"{RECORDS_MARKER} 0")
        return 0

    if not SCHEMA_CONFIRMED:
        LOG.error(_SCHEMA_TODO_MESSAGE)
        return 3

    loader = build_loader(config)
    try:
        total = 0
        for batch in extract(source, start_date, end_date, categoria, linea_impo, id_empresa):
            total += load(loader, transform(batch), mode)
        LOG.info("Loaded range %s..%s -> %s", start_date, end_date, TARGET_SCHEMA.table)
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
