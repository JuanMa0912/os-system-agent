#!/usr/bin/env python3
"""
ETL — Dinastia "Rotacion" (base item x dia x sede) para un rango.

Port FIEL de la referencia Mercamio ``_reference/rotacion/etl_rotacion_v3.py``
(PostgreSQL, psycopg2) a Dinastia. Se porta la variante **SOURCE_SQL_BACKFILL**
(un solo dia, O(n)) — MISMAS columnas de salida y MISMA logica que la referencia:
inventario disponible (foto del lapso mas reciente) + ventas del dia + ultima
venta PDV (ventana del dia, por el modo backfill) por item x sede x bodega.

Solo cambia:
  - fuente MySQL ERP ``BD_BIABLE01`` via ``common.db.MySQLSource`` (pymysql);
  - destino = loader Cloud SQL de config (``common.loader``, ``replace_by_date``);
  - los CODIGOS de Dinastia:
      * categoria de item:  Mercamio id_tipo='4'  ->  Dinastia id_tipo='1'
      * exclusion de sedes: Mercamio ``sede <> 'PPT'``  ->  Dinastia
        ``sede NOT IN ('', 'U01', 'XXX', '003')`` (retail = 001/002; el resto
        es SIN CO / admin / cierre / camion).
  - filtro bodega principal ``RIGHT(TRIM(id_local),2) = '01'``: se conserva.
  - port PG->MySQL: sin MATERIALIZED; BTRIM->TRIM; TO_DATE->STR_TO_DATE;
    ``~ regex`` -> ``REGEXP``; ``MAX(x) FILTER (WHERE c)`` -> ``MAX(CASE WHEN c
    THEN x END)``; sin ``::text`` (empresa se inyecta como literal); ``%``
    literales doblados a ``%%`` para pymysql. items_cat agrupa por id_item con
    MIN() para no duplicar renglones cuando ITEMS tiene extensiones (ID_EXT_ITM).

Grano de salida: un renglon por (empresa, fecha_dia, sede, bodega_local, id_item).
Idempotencia: ``replace_by_date`` borra la particion (empresa, fecha_dia) y
reinserta — equivale al DELETE-dia + reinsercion. Re-correr NO duplica.

Se OMITE de v1 la maquinaria rolling / lock / inv_foto_bloqueada de la
referencia (solo relevante para el modo daily con ventana-anio y bloqueo de foto).

Contrato con el orquestador: imprime ``RECORDS_LOADED: <n>`` al terminar.
  exit 0 ok . 1 error . 2 uso . 3 schema-no-confirmado.

Uso (en el box de Dinastia):
  python etl/rotacion_rango.py --date 20260721
  python etl/rotacion_rango.py --start-date 20260701 --end-date 20260721
  python etl/rotacion_rango.py --date 20260721 --dry-run
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

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

LOG = get_module_logger("etl_rotacion_rango")
DEFAULT_CONFIG = "/opt/dinastia-rotacion/config/pipeline_config.yaml"

EMPRESA = "dinastia"
SCHEMA_CONFIRMED = True
_SCHEMA_TODO_MESSAGE = "SCHEMA_CONFIRMED is False (scaffold guard). Set it True to run."

# Filtro/codigo Dinastia (ajustable por config source.filters). Default =
# lo descubierto el 2026-07-22.
DEFAULT_CATEGORIA = "1"           # ITEMS.ID_TIPO 'mercado' (Mercamio usaba '4')

BATCH_SIZE = 2000                 # como la referencia (BATCH_SIZE = 2_000)

# ============================================================================
# TARGET SCHEMA — mismas columnas que ``rotacion_base_item_dia_sede`` de la
# referencia (DDL_CREATE), en el MISMO orden, MENOS ``inv_foto_bloqueada`` (no
# aplica en v1) y ``fecha_actualizacion`` (idempotencia por replace_by_date).
# Se agrega ``fecha_carga`` como timestamp de carga.
# ============================================================================
TARGET_SCHEMA = TargetSchema(
    table="rotacion_base_item_dia_sede",
    columns={
        "empresa": "string",
        "fecha_dia": "date",
        "sede": "string",
        "bodega_local": "string",
        "id_item": "string",
        "nombre_sede": "string",
        "nombre_item": "string",
        "id_unidad": "string",
        "id_categoria": "string",
        "nombre_categoria": "string",
        "id_linea_nivel_1": "string",
        "nombre_linea_nivel_1": "string",
        "id_linea_nivel_2": "string",
        "nombre_linea_nivel_2": "string",
        "cantidad_vendida": "numeric",
        "venta_sin_impuesto": "numeric",
        "total_costo": "numeric",
        "ultima_venta_pdv": "date",
        "ultima_venta_inventario": "date",
        "estado_ultima_venta_item": "string",
        "lapso_inventario": "string",
        "can_disponible_foto": "numeric",
        "fecha_ultima_compra": "date",
        "fecha_ultima_entrada": "date",
        "costo_uni_inventario": "numeric",
        "fecha_foto_inventario": "date",
        "fecha_carga": "timestamp",
    },
    primary_key=("empresa", "fecha_dia", "sede", "bodega_local", "id_item"),
    partition_field="fecha_dia",
)

# ============================================================================
# SOURCE QUERY — 1:1 con SOURCE_SQL_BACKFILL de la referencia, portada a
# BD_BIABLE01 (MySQL). ``{empresa}`` (etiqueta) y ``{categoria}`` (ITEMS.ID_TIPO)
# se inyectan por .format(). Las fechas/lapsos van como %s.
#
# Parametros posicionales (%s en orden de aparicion en el SQL) — 6 en total:
#   %s 1  year_start_lapso 'YYYYMM'  -> inventario_max_lapso  LAPSO_DOC >= ?
#   %s 2  lapso_str        'YYYYMM'  -> inventario_max_lapso  LAPSO_DOC <= ?
#   %s 3  fecha_str        'YYYYMMDD'-> mov_base_anio         FECHA_DCTO = ? (dia)
#   %s 4  fecha_str        'YYYYMMDD'-> ventas_dia            fecha_dcto = ?
#   %s 5  fecha_str        'YYYYMMDD'-> SELECT fecha_dia (STR_TO_DATE)
#   %s 6  fecha_str        'YYYYMMDD'-> SELECT fecha_foto_inventario (STR_TO_DATE)
#
# (La empresa era el param 3 de la referencia backfill; aqui se inyecta por
#  .format como literal, por eso se elimina de la lista de %s.)
#
# NOTA de escape: ``.format()`` procesa las llaves, por eso el cuantificador
# regex ``{7}`` va escrito ``{{7}}`` en la plantilla (rinde a ``{7}``). Los
# ``%%`` de las mascaras STR_TO_DATE / del ``LIKE 'Z%%'`` son literales pymysql
# (rinden a ``%`` en el servidor); ``.format()`` no los toca.
# ============================================================================
SOURCE_SQL_TEMPLATE = """
WITH
items_cat AS (
    SELECT STRAIGHT_JOIN
        TRIM(i.ID_ITEM)                                             AS id_item,
        MIN(TRIM(i.DESCRIPCION))                                    AS nombre_item,
        MIN(TRIM(COALESCE(
            NULLIF(i.UNIMED_INV_1, ''),
            NULLIF(i.UNIMED_COM,   ''),
            ''
        )))                                                         AS unidad_inventario,
        MIN(TRIM(i.ID_TIPO))                                        AS id_categoria,
        MIN(TRIM(COALESCE(c.CMTIPINV_DESCRIPCION, '')))             AS nombre_categoria,
        MIN(TRIM(COALESCE(i.ID_LINEA1, '')))                        AS id_linea_nivel_1,
        MIN(TRIM(COALESCE(l1.CMLINEAS_DESCRIPCION, '')))            AS nombre_linea_nivel_1,
        MIN(TRIM(COALESCE(i.ID_LINEA2, '')))                        AS id_linea_nivel_2,
        MIN(TRIM(COALESCE(l2.CMLINEAS_DESCRIPCION, '')))            AS nombre_linea_nivel_2,
        -- Costo fallback para kits e items con costo_uni = 0 en inventario
        MIN(COALESCE(NULLIF(i.COSTO_ACT_ACUM, 0), NULLIF(i.ULTIMO_COSTO_ED, 0), 0))
                                                                    AS costo_item_maestro
    FROM ITEMS i
    LEFT JOIN CATEGORIAS c
        ON TRIM(c.ID_TIPO) = TRIM(i.ID_TIPO)
    LEFT JOIN LINEAS l1
        ON TRIM(l1.ID_LINEA) = TRIM(i.ID_LINEA1)
       AND TRIM(l1.ID_TIPO)  = TRIM(i.ID_TIPO)
    LEFT JOIN LINEAS l2
        ON TRIM(l2.ID_LINEA) = TRIM(i.ID_LINEA2)
       AND TRIM(l2.ID_TIPO)  = TRIM(i.ID_TIPO)
    WHERE TRIM(i.ID_TIPO) = '{categoria}'
    -- Un renglon por id_item (ITEMS puede tener extensiones ID_EXT_ITM): MIN()
    -- colapsa a nivel item para no multiplicar filas de inventario/ventas.
    GROUP BY TRIM(i.ID_ITEM)
),
inventario_max_lapso AS (
    SELECT
        TRIM(ri.ID_CO)                              AS sede,
        TRIM(ri.ID_LOCAL)                           AS bodega_local,
        TRIM(ri.ID_ITEM)                            AS id_item,
        MAX(TRIM(ri.LAPSO_DOC))                     AS max_lapso
    FROM CMRESUMEN_INVENTARIO ri
    INNER JOIN items_cat i ON i.id_item = TRIM(ri.ID_ITEM)
    WHERE RIGHT(TRIM(ri.ID_LOCAL), 2) = '01'
      AND TRIM(ri.LAPSO_DOC)          >= %s   -- year_start_lapso YYYYMM
      AND TRIM(ri.LAPSO_DOC)          <= %s   -- lapso_str YYYYMM
    GROUP BY TRIM(ri.ID_CO), TRIM(ri.ID_LOCAL), TRIM(ri.ID_ITEM)
),
inventario_foto AS (
    SELECT
        '{empresa}'                                 AS empresa,
        ml.sede,
        ml.bodega_local,
        ml.id_item,
        ml.max_lapso                                AS lapso_inventario,
        SUM(COALESCE(ri.CAN_DISPONIBLE, 0))         AS can_disponible_foto,
        MAX(CASE WHEN TRIM(ri.FECHA_ULTCOM) REGEXP '^[12][0-9]{{7}}$'
                 THEN NULLIF(TRIM(ri.FECHA_ULTCOM), '') END)   AS fecha_ultima_compra,
        MAX(CASE WHEN TRIM(ri.FECHA_ULTENT) REGEXP '^[12][0-9]{{7}}$'
                 THEN NULLIF(TRIM(ri.FECHA_ULTENT), '') END)   AS fecha_ultima_entrada,
        MAX(CASE WHEN TRIM(ri.FECHA_ULTVTA) REGEXP '^[12][0-9]{{7}}$'
                 THEN NULLIF(TRIM(ri.FECHA_ULTVTA), '') END)   AS fecha_ultima_venta_inventario,
        MAX(COALESCE(ri.COSTO_UNI, 0))              AS costo_uni_inventario
    FROM inventario_max_lapso ml
    JOIN CMRESUMEN_INVENTARIO ri
        ON  TRIM(ri.ID_CO)     = ml.sede
        AND TRIM(ri.ID_LOCAL)  = ml.bodega_local
        AND TRIM(ri.ID_ITEM)   = ml.id_item
        AND TRIM(ri.LAPSO_DOC) = ml.max_lapso
    GROUP BY ml.sede, ml.bodega_local, ml.id_item, ml.max_lapso
),
-- BACKFILL: solo el dia exacto — sin ventana anio acumulada.
mov_base_anio AS (
    SELECT
        TRIM(mp.ID_CO)                              AS sede,
        TRIM(mp.FECHA_DCTO)                         AS fecha_dcto,
        TRIM(mp.ID_LOCAL)                           AS bodega_local,
        TRIM(mp.ID_ITEM)                            AS id_item,
        TRIM(COALESCE(mp.ID_UNIDAD, ''))            AS id_unidad,
        COALESCE(mp.CANTIDAD,   0)                  AS cantidad,
        COALESCE(mp.VLRTOT_BRU, 0)                  AS ven_netas,
        COALESCE(mp.TOT_COSTO,  0)                  AS tot_costo
    FROM CMMOVIMIENTO_PDV mp
    INNER JOIN items_cat i ON i.id_item = TRIM(mp.ID_ITEM)
    WHERE mp.FECHA_DCTO                   = %s     -- solo el dia procesado (usa IDX_FECHA)
      AND RIGHT(TRIM(mp.ID_LOCAL), 2)    = '01'
      AND COALESCE(TRIM(mp.DOCTO_ACUMULACION), '') NOT LIKE 'Z%%'
),
ventas_dia AS (
    SELECT
        sede, bodega_local, id_item,
        MAX(id_unidad) AS id_unidad,        -- unidad representativa (grano = item x sede)
        SUM(cantidad)  AS cantidad_vendida,
        SUM(ven_netas) AS venta_sin_impuesto,
        SUM(tot_costo) AS total_costo
    FROM mov_base_anio
    WHERE fecha_dcto = %s
    GROUP BY sede, bodega_local, id_item
),
ultima_venta_pdv AS (
    SELECT sede, id_item, MAX(fecha_dcto) AS ultima_venta_item_sede_pdv
    FROM mov_base_anio
    GROUP BY sede, id_item
)
SELECT
    inv.empresa                                     AS empresa,
    STR_TO_DATE(%s, '%%Y%%m%%d')                    AS fecha_dia,
    inv.sede                                        AS sede,
    inv.bodega_local                                AS bodega_local,
    inv.id_item                                     AS id_item,
    TRIM(COALESCE(co.DESCRIPCION, ''))              AS nombre_sede,
    COALESCE(i.nombre_item, '')                     AS nombre_item,
    -- Unidad: preferir la vendida, fallback maestro, fallback literal
    COALESCE(NULLIF(v.id_unidad, ''), NULLIF(i.unidad_inventario, ''), 'SIN_VENTA')
                                                    AS id_unidad,
    COALESCE(i.id_categoria, '')                    AS id_categoria,
    COALESCE(i.nombre_categoria, '')                AS nombre_categoria,
    COALESCE(i.id_linea_nivel_1, '')                AS id_linea_nivel_1,
    COALESCE(i.nombre_linea_nivel_1, '')            AS nombre_linea_nivel_1,
    COALESCE(i.id_linea_nivel_2, '')                AS id_linea_nivel_2,
    COALESCE(i.nombre_linea_nivel_2, '')            AS nombre_linea_nivel_2,
    COALESCE(v.cantidad_vendida,   0)               AS cantidad_vendida,
    COALESCE(v.venta_sin_impuesto, 0)               AS venta_sin_impuesto,
    -- Fix kits: ERP registra tot_costo=0 en movimientos -> calcular desde costo unitario
    CASE
        WHEN COALESCE(v.total_costo, 0) = 0 AND COALESCE(v.cantidad_vendida, 0) > 0
        THEN COALESCE(v.cantidad_vendida, 0) *
             COALESCE(NULLIF(inv.costo_uni_inventario, 0), i.costo_item_maestro, 0)
        ELSE COALESCE(v.total_costo, 0)
    END                                             AS total_costo,
    -- Ultima venta PDV (ventana del dia en modo backfill)
    CASE
        WHEN uv.ultima_venta_item_sede_pdv IS NOT NULL
            THEN STR_TO_DATE(uv.ultima_venta_item_sede_pdv, '%%Y%%m%%d')
        ELSE NULL
    END                                             AS ultima_venta_pdv,
    -- Ultima venta desde CMRESUMEN_INVENTARIO.FECHA_ULTVTA
    CASE
        WHEN inv.fecha_ultima_venta_inventario IS NOT NULL
             AND inv.fecha_ultima_venta_inventario REGEXP '^[12][0-9]{{7}}$'
            THEN STR_TO_DATE(inv.fecha_ultima_venta_inventario, '%%Y%%m%%d')
        ELSE NULL
    END                                             AS ultima_venta_inventario,
    -- Estado venta
    CASE
        WHEN uv.ultima_venta_item_sede_pdv IS NOT NULL THEN 'CON VENTA EN EL AÑO'
        ELSE 'SIN VENTA EN EL AÑO'
    END                                             AS estado_ultima_venta_item,
    -- Snapshot de inventario
    inv.lapso_inventario                            AS lapso_inventario,
    inv.can_disponible_foto                         AS can_disponible_foto,
    CASE WHEN inv.fecha_ultima_compra IS NOT NULL
              AND inv.fecha_ultima_compra REGEXP '^[12][0-9]{{7}}$'
         THEN STR_TO_DATE(inv.fecha_ultima_compra,  '%%Y%%m%%d') ELSE NULL END
                                                    AS fecha_ultima_compra,
    CASE WHEN inv.fecha_ultima_entrada IS NOT NULL
              AND inv.fecha_ultima_entrada REGEXP '^[12][0-9]{{7}}$'
         THEN STR_TO_DATE(inv.fecha_ultima_entrada, '%%Y%%m%%d') ELSE NULL END
                                                    AS fecha_ultima_entrada,
    -- Costo inventario con fallback al maestro para kits y costo_uni = 0
    COALESCE(NULLIF(inv.costo_uni_inventario, 0), i.costo_item_maestro, 0)
                                                    AS costo_uni_inventario,
    STR_TO_DATE(%s, '%%Y%%m%%d')                    AS fecha_foto_inventario
FROM inventario_foto inv
INNER JOIN items_cat i    ON i.id_item  = inv.id_item
LEFT JOIN  ventas_dia v
    ON  v.sede         = inv.sede
    AND v.bodega_local = inv.bodega_local
    AND v.id_item      = inv.id_item
LEFT JOIN  ultima_venta_pdv uv
    ON  uv.sede    = inv.sede
    AND uv.id_item = inv.id_item
LEFT JOIN  CENTRO_OPERACION co ON TRIM(co.CODIGO) = inv.sede
-- Excluir filas vacias: sin ventas, sin inventario Y sin costo unitario.
-- Items con costo_uni > 0 pero can_disponible = 0 SI se incluyen (stock a cero).
WHERE (
    COALESCE(v.cantidad_vendida,   0) <> 0
    OR COALESCE(v.venta_sin_impuesto, 0) <> 0
    OR COALESCE(inv.can_disponible_foto, 0) <> 0
    OR COALESCE(NULLIF(inv.costo_uni_inventario, 0), i.costo_item_maestro, 0) <> 0
)
  AND inv.sede NOT IN ('', 'U01', 'XXX', '003')   -- retail Dinastia = 001/002
ORDER BY inv.sede, inv.bodega_local, inv.id_item
"""


def build_source_sql(categoria: str) -> str:
    """Render the rotacion SQL with Dinastia codes injected (dates stay as %s).

    ``empresa`` is the module label (constant) and ``categoria`` is ITEMS.ID_TIPO
    (from config; default ``DEFAULT_CATEGORIA``). Both are literals — the 6 date/
    lapso params remain ``%s`` positional placeholders (see :func:`day_params`).
    """
    return SOURCE_SQL_TEMPLATE.format(empresa=EMPRESA, categoria=categoria)


def day_params(fecha_str: str) -> tuple[str, str, str, str, str, str]:
    """Positional %s params for ONE day, in SQL order (backfill/per-day variant):

      (year_start_lapso, lapso_str, fecha_str, fecha_str, fecha_str, fecha_str)

    - year_start_lapso = 'YYYY01'  (enero del anio del dia)  -> inventario >= ?
    - lapso_str        = 'YYYYMM'  (mes del dia)             -> inventario <= ?
    - fecha_str        = 'YYYYMMDD'(el dia)                  -> mov/ventas/select
    """
    day = datetime.strptime(fecha_str, "%Y%m%d").date()
    lapso_str = day.strftime("%Y%m")
    year_start_lapso = f"{day.year}01"
    return (year_start_lapso, lapso_str, fecha_str, fecha_str, fecha_str, fecha_str)


def _iter_days(start_date: str, end_date: str) -> Iterator[str]:
    """Yield each day in ``[start_date .. end_date]`` as canonical 'YYYYMMDD'."""
    start = datetime.strptime(start_date, "%Y%m%d").date()
    end = datetime.strptime(end_date, "%Y%m%d").date()
    current: date = start
    while current <= end:
        yield current.strftime("%Y%m%d")
        current += timedelta(days=1)


# ---------------------------------------------------------------------------
# Extract  (per-day; rotacion es POR DIA)
# ---------------------------------------------------------------------------
def extract(source: MySQLSource, fecha_str: str, categoria: str,
            batch_size: int = BATCH_SIZE) -> Iterable[list[dict]]:
    if not SCHEMA_CONFIRMED:
        raise NotImplementedError(_SCHEMA_TODO_MESSAGE)
    sql = build_source_sql(categoria)
    params = day_params(fecha_str)
    LOG.info("Extracting rotacion dia=%s (categoria=%s) from %s",
             fecha_str, categoria, source.cfg.masked())
    yield from source.stream(sql, params, batch_size=batch_size)


# ---------------------------------------------------------------------------
# Transform  (pass-through de fechas/strings + fecha_carga; sin derivar nada)
# ---------------------------------------------------------------------------
_TEXT_COLS = (
    "empresa", "sede", "bodega_local", "id_item", "nombre_sede", "nombre_item",
    "id_unidad", "id_categoria", "nombre_categoria",
    "id_linea_nivel_1", "nombre_linea_nivel_1",
    "id_linea_nivel_2", "nombre_linea_nivel_2",
    "estado_ultima_venta_item", "lapso_inventario",
)
_NUM_COLS = (
    "cantidad_vendida", "venta_sin_impuesto", "total_costo",
    "can_disponible_foto", "costo_uni_inventario",
)
# pymysql devuelve objetos date para los STR_TO_DATE -> se pasan tal cual.
_DATE_COLS = (
    "fecha_dia", "ultima_venta_pdv", "ultima_venta_inventario",
    "fecha_ultima_compra", "fecha_ultima_entrada", "fecha_foto_inventario",
)


def transform(rows: Iterable[Mapping[str, Any]]) -> list[dict]:
    now = datetime.now()
    out: list[dict] = []
    for r in rows:
        row = {c: _txt(r.get(c)) for c in _TEXT_COLS}
        row.update({c: _num(r.get(c)) for c in _NUM_COLS})
        row.update({c: r.get(c) for c in _DATE_COLS})  # date objects pass-through
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
        for fecha_str in _iter_days(start_date, end_date):
            day_total = 0
            for batch in extract(source, fecha_str, categoria):
                day_total += load(loader, transform(batch), mode)
            LOG.info("Loaded dia %s -> %s (%d filas)", fecha_str, TARGET_SCHEMA.table, day_total)
            total += day_total
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
