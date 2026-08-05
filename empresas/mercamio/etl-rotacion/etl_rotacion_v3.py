#!/usr/bin/env python3
"""
ETL Rotación de Ítems v3 — 3 empresas (mercamio, mtodo, bogota)
================================================================
Origen:  192.168.35.217  (mercamio / mtodo / bogota)
Destino: 192.168.35.232  BD=produXdia  tabla=public.rotacion_base_item_dia_sede

Cambios respecto a v2
----------------------
· Consulta origen completamente reescrita según especificación del usuario.
· Categoría desde tabla `categorias` (id_tipo = '4') — no criterios_itm_4.
· Inventario usa can_disponible (no can_exis_fin).
· Sin id_ext_itm en PK — ítem se identifica por (empresa, fecha_dia, sede,
  bodega_local, id_item).
· Dos campos de última venta para análisis de patrones:
    - ultima_venta_pdv         → MAX(fecha_dcto) en cmmovimiento_pdv, ventana
                                 1-enero del año en curso hasta el día procesado.
    - ultima_venta_inventario  → cmresumen_inventario.fecha_ultvta.
· dias_sin_venta NO se almacena — lo calcula el portal/BI dinámicamente.
· Lapso inventario: MAX(lapso_doc) <= lapso_consulta — si no hay datos del mes
  actual toma el mes anterior.

── Modos ───────────────────────────────────────────────────────────────────────
  daily    (timer 7am):
      Carga el día de ayer con UPSERT completo (ventas + inventario).
      Al terminar bloquea el snapshot con inv_foto_bloqueada = TRUE.

  rolling  (timer 1am días 1, 11, 21):
      Reprocesa los últimos N días solo ventas. Inventario histórico intacto.

  backfill (manual):
      Carga un rango histórico día a día con UPSERT completo.

── Ejemplos ────────────────────────────────────────────────────────────────────
  python etl_rotacion_v3.py --mode daily
  python etl_rotacion_v3.py --mode rolling --rolling-days 15
  python etl_rotacion_v3.py --mode backfill --date-start 20260101 --date-end 20260426
  python etl_rotacion_v3.py --mode backfill --date-start 20260101 --date-end 20260426 --recreate-table
  python etl_rotacion_v3.py --mode daily --empresas mercamio --dry-run
  python etl_rotacion_v3.py --check-only
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence

import psycopg2
import psycopg2.extras
from psycopg2.extensions import connection as PgConn

# ── Logger ────────────────────────────────────────────────────────────────────
LOGGER = logging.getLogger("etl_rotacion_v3")

# ── Constantes ────────────────────────────────────────────────────────────────
TARGET_TABLE         = "public.rotacion_base_item_dia_sede"
DEFAULT_ROLLING_DAYS  = 15
LOG_FILE_PREFIX       = "etl_rotacion_v3"
FILE_LOG_RETENTION    = 31
BATCH_SIZE            = 2_000
# Pausa entre días en backfill (segundos) — reduce carga en servidor origen
BACKFILL_SLEEP_SECS   = 2

COMPANIES: List[str] = ["mercamio", "mtodo", "bogota"]

COMPANY_NAMES: Dict[str, str] = {
    "mercamio": "Mercamio",
    "mtodo":    "Método",
    "bogota":   "Bogotá",
}

# Variables de entorno por empresa
COMPANY_ENV: Dict[str, Dict[str, str]] = {
    "mercamio": dict(
        host="SRC_MERCAMIO_PGHOST", port="SRC_MERCAMIO_PGPORT",
        db="SRC_MERCAMIO_PGDATABASE", user="SRC_MERCAMIO_PGUSER",
        pw="SRC_MERCAMIO_PGPASSWORD",
    ),
    "mtodo": dict(
        host="SRC_MTODO_PGHOST", port="SRC_MTODO_PGPORT",
        db="SRC_MTODO_PGDATABASE", user="SRC_MTODO_PGUSER",
        pw="SRC_MTODO_PGPASSWORD",
    ),
    "bogota": dict(
        host="SRC_BOGOTA_PGHOST", port="SRC_BOGOTA_PGPORT",
        db="SRC_BOGOTA_PGDATABASE", user="SRC_BOGOTA_PGUSER",
        pw="SRC_BOGOTA_PGPASSWORD",
    ),
}

# ── DDL destino ───────────────────────────────────────────────────────────────
DDL_DROP = f"DROP TABLE IF EXISTS {TARGET_TABLE};"

DDL_CREATE = f"""
CREATE TABLE {TARGET_TABLE} (
    -- Llave primaria
    empresa                     VARCHAR(20)     NOT NULL,
    fecha_dia                   DATE            NOT NULL,
    sede                        VARCHAR(10)     NOT NULL,
    bodega_local                VARCHAR(10)     NOT NULL DEFAULT '',
    id_item                     VARCHAR(10)     NOT NULL,

    -- Descriptor sede
    nombre_sede                 VARCHAR(120),

    -- Maestro ítem
    nombre_item                 VARCHAR(255),
    id_unidad                   VARCHAR(10),

    -- Categoría (tabla categorias, id_tipo = '4')
    id_categoria                VARCHAR(10),
    nombre_categoria            VARCHAR(100),

    -- Línea
    id_linea_nivel_1            VARCHAR(10),
    nombre_linea_nivel_1        VARCHAR(100),

    -- Ventas del día
    cantidad_vendida            NUMERIC(18,4)   DEFAULT 0,
    venta_sin_impuesto          NUMERIC(18,2)   DEFAULT 0,
    total_costo                 NUMERIC(18,2)   DEFAULT 0,

    -- Última venta (dos fuentes para análisis de patrones)
    ultima_venta_pdv            DATE,           -- MAX fecha en PDV, ventana año en curso
    ultima_venta_inventario     DATE,           -- cmresumen_inventario.fecha_ultvta
    estado_ultima_venta_item    VARCHAR(25),    -- 'CON VENTA EN EL AÑO' / 'SIN VENTA EN EL AÑO'

    -- Inventario (foto del día — protegida en rolling con inv_foto_bloqueada)
    lapso_inventario            VARCHAR(6),     -- YYYYMM del snapshot
    can_disponible_foto         NUMERIC(18,4)   DEFAULT 0,
    fecha_ultima_compra         DATE,
    fecha_ultima_entrada        DATE,
    costo_uni_inventario        NUMERIC(18,4)   DEFAULT 0,
    fecha_foto_inventario       DATE,

    -- Control de snapshot
    inv_foto_bloqueada          BOOLEAN         DEFAULT FALSE,
    fecha_carga                 TIMESTAMP       DEFAULT now(),
    fecha_actualizacion         TIMESTAMP       DEFAULT now(),

    PRIMARY KEY (empresa, fecha_dia, sede, bodega_local, id_item)
);
"""

DDL_CREATE_IF_NOT_EXISTS = DDL_CREATE.replace(
    f"CREATE TABLE {TARGET_TABLE}",
    f"CREATE TABLE IF NOT EXISTS {TARGET_TABLE}",
)

# ── ÍNDICES ───────────────────────────────────────────────────────────────────
DDL_INDICES: List[str] = [
    f"CREATE INDEX IF NOT EXISTS idx_rot_v3_fecha_empresa    ON {TARGET_TABLE} (fecha_dia, empresa);",
    f"CREATE INDEX IF NOT EXISTS idx_rot_v3_sede_fecha       ON {TARGET_TABLE} (sede, fecha_dia);",
    f"CREATE INDEX IF NOT EXISTS idx_rot_v3_item_fecha       ON {TARGET_TABLE} (id_item, fecha_dia);",
    f"CREATE INDEX IF NOT EXISTS idx_rot_v3_linea1_fecha     ON {TARGET_TABLE} (id_linea_nivel_1, fecha_dia);",
    f"CREATE INDEX IF NOT EXISTS idx_rot_v3_categoria_fecha  ON {TARGET_TABLE} (id_categoria, fecha_dia);",
    f"CREATE INDEX IF NOT EXISTS idx_rot_v3_sin_venta_anio   ON {TARGET_TABLE} (fecha_dia, estado_ultima_venta_item);",
]

# ── SQL DE EXTRACCIÓN ─────────────────────────────────────────────────────────
#
# Parámetros posicionales (%s en orden de aparición):
#   1  lapso_str       → inventario_max_lapso: MAX lapso_doc <= ?
#   2  empresa         → inventario_foto: etiqueta de empresa
#   3  year_start_str  → mov_base_anio: fecha_dcto >= ? (1-ene del año)
#   4  fecha_str       → mov_base_anio: fecha_dcto <= ? (día procesado)
#   5  fecha_str       → ventas_dia:    fecha_dcto  = ? (día procesado)
#   6  fecha_str       → SELECT:        TO_DATE AS fecha_dia
#   7  fecha_str       → SELECT:        fecha_foto_inventario
#
SOURCE_SQL = """
WITH
-- ─── MAESTRO DE ÍTEMS CATEGORÍA 4 ────────────────────────────────────────────
-- Solo ítems de id_tipo = '4'. Obtiene categoría (tabla categorias)
-- y línea nivel 1 (tabla lineas, join doble por id_linea1 + id_tipo).
items_cat4 AS MATERIALIZED (
    SELECT
        BTRIM(i.id_item)                                            AS id_item,
        BTRIM(i.descripcion)                                        AS nombre_item,
        BTRIM(COALESCE(
            NULLIF(i.unimed_inv_1, ''),
            NULLIF(i.unimed_com,   ''),
            ''
        ))                                                          AS unidad_inventario,
        BTRIM(i.id_tipo)                                            AS id_categoria,
        BTRIM(COALESCE(c.cmtipinv_descripcion, ''))                 AS nombre_categoria,
        BTRIM(COALESCE(i.id_linea1, ''))                            AS id_linea_nivel_1,
        BTRIM(COALESCE(l1.cmlineas_descripcion, ''))                AS nombre_linea_nivel_1,
        -- Costo fallback para kits e ítems con costo_uni = 0 en inventario
        COALESCE(NULLIF(i.costo_act_acum, 0), NULLIF(i.ultimo_costo_ed, 0), 0)
                                                                    AS costo_item_maestro
    FROM public.items i
    LEFT JOIN public.categorias c
        ON BTRIM(c.id_tipo) = BTRIM(i.id_tipo)
    LEFT JOIN public.lineas l1
        ON BTRIM(l1.id_linea) = BTRIM(i.id_linea1)
       AND BTRIM(l1.id_tipo)  = BTRIM(i.id_tipo)
    WHERE BTRIM(i.id_tipo) = '4'
),
-- ─── LAPSO MÁS RECIENTE DE INVENTARIO ────────────────────────────────────────
-- MAX(lapso_doc) <= lapso_consulta por (sede, bodega, ítem).
-- Resuelve el caso de primeros días del mes: si aún no hay datos del lapso
-- actual, toma el lapso del mes anterior.
inventario_max_lapso AS (
    SELECT
        BTRIM(ri.id_co)                             AS sede,
        BTRIM(ri.id_local)                          AS bodega_local,
        BTRIM(ri.id_item)                           AS id_item,
        MAX(BTRIM(ri.lapso_doc))                    AS max_lapso
    FROM public.cmresumen_inventario ri
    INNER JOIN items_cat4 i
        ON i.id_item = BTRIM(ri.id_item)
    WHERE RIGHT(BTRIM(ri.id_local), 2) = '01'
      AND BTRIM(ri.lapso_doc)          >= %s        -- year_start_lapso YYYYMM (enero del año)
      AND BTRIM(ri.lapso_doc)          <= %s        -- lapso_str YYYYMM
    GROUP BY
        BTRIM(ri.id_co),
        BTRIM(ri.id_local),
        BTRIM(ri.id_item)
),
-- ─── FOTO DE INVENTARIO DEL DÍA ──────────────────────────────────────────────
-- can_disponible = stock disponible (excluye reservas).
-- Dos fechas de referencia del inventario:
--   · fecha_ultima_venta_inventario → cmresumen_inventario.fecha_ultvta
--   · fecha_ultima_compra           → cmresumen_inventario.fecha_ultcom
--   · fecha_ultima_entrada          → cmresumen_inventario.fecha_ultent
-- Fechas: FILTER elimina valores '00000000' u otros no válidos.
inventario_foto AS MATERIALIZED (
    SELECT
        %s::text                                    AS empresa,   -- empresa param
        ml.sede,
        ml.bodega_local,
        ml.id_item,
        ml.max_lapso                                AS lapso_inventario,
        SUM(COALESCE(ri.can_disponible, 0))         AS can_disponible_foto,
        MAX(NULLIF(BTRIM(ri.fecha_ultcom), ''))
            FILTER (WHERE BTRIM(ri.fecha_ultcom) ~ '^[12][0-9]{7}$')
                                                    AS fecha_ultima_compra,
        MAX(NULLIF(BTRIM(ri.fecha_ultent), ''))
            FILTER (WHERE BTRIM(ri.fecha_ultent) ~ '^[12][0-9]{7}$')
                                                    AS fecha_ultima_entrada,
        MAX(NULLIF(BTRIM(ri.fecha_ultvta), ''))
            FILTER (WHERE BTRIM(ri.fecha_ultvta) ~ '^[12][0-9]{7}$')
                                                    AS fecha_ultima_venta_inventario,
        MAX(COALESCE(ri.costo_uni, 0))              AS costo_uni_inventario
    FROM inventario_max_lapso ml
    JOIN public.cmresumen_inventario ri
        ON  BTRIM(ri.id_co)    = ml.sede
        AND BTRIM(ri.id_local) = ml.bodega_local
        AND BTRIM(ri.id_item)  = ml.id_item
        AND BTRIM(ri.lapso_doc)= ml.max_lapso
    GROUP BY
        ml.sede, ml.bodega_local, ml.id_item, ml.max_lapso
),
-- ─── MOVIMIENTOS DEL AÑO EN CURSO ────────────────────────────────────────────
-- Ventana: 1-enero del año procesado → día procesado.
-- Sirve para:
--   a) ventas_dia    → filtro al día exacto procesado
--   b) ultima_venta_pdv → MAX(fecha_dcto) por (sede, ítem)
-- Filtros: solo bodegas principales (RIGHT = '01'), excluye notas Z.
mov_base_anio AS MATERIALIZED (
    SELECT
        BTRIM(mp.id_co)                             AS sede,
        BTRIM(mp.fecha_dcto)                        AS fecha_dcto,
        BTRIM(mp.id_local)                          AS bodega_local,
        BTRIM(mp.id_item)                           AS id_item,
        BTRIM(COALESCE(mp.id_unidad, ''))           AS id_unidad,
        COALESCE(mp.cantidad,  0)                   AS cantidad,
        COALESCE(mp.vlrtot_bru, 0)                  AS ven_netas,
        COALESCE(mp.tot_costo, 0)                   AS tot_costo
    FROM public.cmmovimiento_pdv mp
    INNER JOIN items_cat4 i
        ON i.id_item = BTRIM(mp.id_item)
    WHERE BTRIM(mp.fecha_dcto) >= %s               -- year_start_str YYYYMMDD
      AND BTRIM(mp.fecha_dcto) <= %s               -- fecha_str YYYYMMDD
      AND RIGHT(BTRIM(mp.id_local), 2) = '01'
      AND COALESCE(BTRIM(mp.docto_acumulacion), '') NOT LIKE 'Z%%'
),
-- ─── VENTAS DEL DÍA PROCESADO ────────────────────────────────────────────────
ventas_dia AS (
    SELECT
        sede,
        bodega_local,
        id_item,
        id_unidad,
        SUM(cantidad)  AS cantidad_vendida,
        SUM(ven_netas) AS venta_sin_impuesto,
        SUM(tot_costo) AS total_costo
    FROM mov_base_anio
    WHERE fecha_dcto = %s                          -- fecha_str YYYYMMDD
    GROUP BY sede, bodega_local, id_item, id_unidad
),
-- ─── ÚLTIMA VENTA EN PDV (ventana año) ───────────────────────────────────────
-- MAX(fecha_dcto) desde el 1-ene hasta el día procesado.
-- Ítems que no aparecen aquí → ultima_venta_pdv = NULL → 'SIN VENTA EN EL AÑO'
ultima_venta_pdv AS (
    SELECT
        sede,
        id_item,
        MAX(fecha_dcto) AS ultima_venta_item_sede_pdv
    FROM mov_base_anio
    GROUP BY sede, id_item
)
-- ─── QUERY FINAL ─────────────────────────────────────────────────────────────
SELECT
    inv.empresa,
    inv.sede,
    BTRIM(COALESCE(co.descripcion, ''))             AS nombre_sede,
    TO_DATE(%s, 'YYYYMMDD')                         AS fecha_dia,       -- fecha_str
    inv.bodega_local,
    inv.id_item,
    COALESCE(i.nombre_item, '')                     AS nombre_item,
    -- Unidad: preferir la vendida, fallback maestro, fallback literal
    COALESCE(
        NULLIF(v.id_unidad, ''),
        NULLIF(i.unidad_inventario, ''),
        'SIN_VENTA'
    )                                               AS id_unidad,
    COALESCE(i.id_categoria, '')                    AS id_categoria,
    COALESCE(i.nombre_categoria, '')                AS nombre_categoria,
    COALESCE(i.id_linea_nivel_1, '')                AS id_linea_nivel_1,
    COALESCE(i.nombre_linea_nivel_1, '')            AS nombre_linea_nivel_1,
    COALESCE(v.cantidad_vendida,   0)               AS cantidad_vendida,
    COALESCE(v.venta_sin_impuesto, 0)               AS venta_sin_impuesto,
    -- Fix kits: ERP registra tot_costo=0 en movimientos → calcular desde costo unitario
    CASE
        WHEN COALESCE(v.total_costo, 0) = 0 AND COALESCE(v.cantidad_vendida, 0) > 0
        THEN COALESCE(v.cantidad_vendida, 0) *
             COALESCE(NULLIF(inv.costo_uni_inventario, 0), i.costo_item_maestro, 0)
        ELSE COALESCE(v.total_costo, 0)
    END                                             AS total_costo,

    -- Última venta PDV (ventana año en curso)
    CASE
        WHEN uv.ultima_venta_item_sede_pdv IS NOT NULL
            THEN TO_DATE(uv.ultima_venta_item_sede_pdv, 'YYYYMMDD')
        ELSE NULL
    END                                             AS ultima_venta_pdv,

    -- Última venta desde cmresumen_inventario.fecha_ultvta
    CASE
        WHEN inv.fecha_ultima_venta_inventario IS NOT NULL
             AND inv.fecha_ultima_venta_inventario ~ '^[12][0-9]{7}$'
            THEN TO_DATE(inv.fecha_ultima_venta_inventario, 'YYYYMMDD')
        ELSE NULL
    END                                             AS ultima_venta_inventario,

    -- Estado venta año
    CASE
        WHEN uv.ultima_venta_item_sede_pdv IS NOT NULL
            THEN 'CON VENTA EN EL AÑO'
        ELSE 'SIN VENTA EN EL AÑO'
    END                                             AS estado_ultima_venta_item,

    -- Snapshot de inventario
    inv.lapso_inventario,
    inv.can_disponible_foto,

    CASE
        WHEN inv.fecha_ultima_compra IS NOT NULL
             AND inv.fecha_ultima_compra ~ '^[12][0-9]{7}$'
            THEN TO_DATE(inv.fecha_ultima_compra, 'YYYYMMDD')
        ELSE NULL
    END                                             AS fecha_ultima_compra,

    CASE
        WHEN inv.fecha_ultima_entrada IS NOT NULL
             AND inv.fecha_ultima_entrada ~ '^[12][0-9]{7}$'
            THEN TO_DATE(inv.fecha_ultima_entrada, 'YYYYMMDD')
        ELSE NULL
    END                                             AS fecha_ultima_entrada,

    -- Costo inventario con fallback al maestro para kits y costo_uni = 0
    COALESCE(NULLIF(inv.costo_uni_inventario, 0), i.costo_item_maestro, 0)
                                                    AS costo_uni_inventario,
    TO_DATE(%s, 'YYYYMMDD')                         AS fecha_foto_inventario, -- fecha_str
    FALSE                                           AS inv_foto_bloqueada

FROM inventario_foto inv
INNER JOIN items_cat4 i
    ON i.id_item = inv.id_item
LEFT JOIN ventas_dia v
    ON  v.sede        = inv.sede
    AND v.bodega_local = inv.bodega_local
    AND v.id_item     = inv.id_item
LEFT JOIN ultima_venta_pdv uv
    ON  uv.sede    = inv.sede
    AND uv.id_item = inv.id_item
LEFT JOIN public.centro_operacion co
    ON BTRIM(co.codigo) = inv.sede
-- Excluir filas vacías: sin ventas, sin inventario disponible y sin valor de inventario.
-- Si can_disponible=0 → valor (can_disp × costo_uni) = 0 siempre.
WHERE (
    COALESCE(v.cantidad_vendida,   0) <> 0
    OR COALESCE(v.venta_sin_impuesto, 0) <> 0
    OR COALESCE(inv.can_disponible_foto, 0) <> 0
    OR COALESCE(inv.can_disponible_foto, 0) * COALESCE(inv.costo_uni_inventario, 0) <> 0
)
ORDER BY inv.sede, inv.bodega_local, inv.id_item;
"""

# ── UPSERT COMPLETO (daily + backfill) ───────────────────────────────────────
# Columnas en el MISMO orden que el SELECT de SOURCE_SQL.
UPSERT_FULL_SQL = f"""
INSERT INTO {TARGET_TABLE} (
    empresa, sede, nombre_sede, fecha_dia, bodega_local, id_item,
    nombre_item, id_unidad,
    id_categoria, nombre_categoria,
    id_linea_nivel_1, nombre_linea_nivel_1,
    cantidad_vendida, venta_sin_impuesto, total_costo,
    ultima_venta_pdv, ultima_venta_inventario, estado_ultima_venta_item,
    lapso_inventario, can_disponible_foto,
    fecha_ultima_compra, fecha_ultima_entrada,
    costo_uni_inventario, fecha_foto_inventario,
    inv_foto_bloqueada
) VALUES %s
ON CONFLICT (empresa, fecha_dia, sede, bodega_local, id_item)
DO UPDATE SET
    nombre_sede                 = EXCLUDED.nombre_sede,
    nombre_item                 = EXCLUDED.nombre_item,
    id_unidad                   = EXCLUDED.id_unidad,
    id_categoria                = EXCLUDED.id_categoria,
    nombre_categoria            = EXCLUDED.nombre_categoria,
    id_linea_nivel_1            = EXCLUDED.id_linea_nivel_1,
    nombre_linea_nivel_1        = EXCLUDED.nombre_linea_nivel_1,
    cantidad_vendida            = EXCLUDED.cantidad_vendida,
    venta_sin_impuesto          = EXCLUDED.venta_sin_impuesto,
    total_costo                 = EXCLUDED.total_costo,
    ultima_venta_pdv            = EXCLUDED.ultima_venta_pdv,
    ultima_venta_inventario     = EXCLUDED.ultima_venta_inventario,
    estado_ultima_venta_item    = EXCLUDED.estado_ultima_venta_item,
    -- Inventario: solo actualizar si NO está bloqueado (daily ya guardó la foto real)
    lapso_inventario            = CASE WHEN {TARGET_TABLE}.inv_foto_bloqueada THEN {TARGET_TABLE}.lapso_inventario            ELSE EXCLUDED.lapso_inventario            END,
    can_disponible_foto         = CASE WHEN {TARGET_TABLE}.inv_foto_bloqueada THEN {TARGET_TABLE}.can_disponible_foto         ELSE EXCLUDED.can_disponible_foto         END,
    fecha_ultima_compra         = CASE WHEN {TARGET_TABLE}.inv_foto_bloqueada THEN {TARGET_TABLE}.fecha_ultima_compra         ELSE EXCLUDED.fecha_ultima_compra         END,
    fecha_ultima_entrada        = CASE WHEN {TARGET_TABLE}.inv_foto_bloqueada THEN {TARGET_TABLE}.fecha_ultima_entrada        ELSE EXCLUDED.fecha_ultima_entrada        END,
    costo_uni_inventario        = CASE WHEN {TARGET_TABLE}.inv_foto_bloqueada THEN {TARGET_TABLE}.costo_uni_inventario        ELSE EXCLUDED.costo_uni_inventario        END,
    fecha_foto_inventario       = CASE WHEN {TARGET_TABLE}.inv_foto_bloqueada THEN {TARGET_TABLE}.fecha_foto_inventario       ELSE EXCLUDED.fecha_foto_inventario       END,
    inv_foto_bloqueada          = {TARGET_TABLE}.inv_foto_bloqueada,
    fecha_actualizacion         = now()
"""

# ── UPSERT PARCIAL (rolling) ──────────────────────────────────────────────────
# Solo actualiza ventas y campos derivados. El snapshot de inventario NO se toca.
UPSERT_VENTAS_SQL = f"""
INSERT INTO {TARGET_TABLE} (
    empresa, sede, nombre_sede, fecha_dia, bodega_local, id_item,
    nombre_item, id_unidad,
    id_categoria, nombre_categoria,
    id_linea_nivel_1, nombre_linea_nivel_1,
    cantidad_vendida, venta_sin_impuesto, total_costo,
    ultima_venta_pdv, ultima_venta_inventario, estado_ultima_venta_item,
    lapso_inventario, can_disponible_foto,
    fecha_ultima_compra, fecha_ultima_entrada,
    costo_uni_inventario, fecha_foto_inventario,
    inv_foto_bloqueada
) VALUES %s
ON CONFLICT (empresa, fecha_dia, sede, bodega_local, id_item)
DO UPDATE SET
    nombre_sede                 = EXCLUDED.nombre_sede,
    nombre_item                 = EXCLUDED.nombre_item,
    id_unidad                   = EXCLUDED.id_unidad,
    id_categoria                = EXCLUDED.id_categoria,
    nombre_categoria            = EXCLUDED.nombre_categoria,
    id_linea_nivel_1            = EXCLUDED.id_linea_nivel_1,
    nombre_linea_nivel_1        = EXCLUDED.nombre_linea_nivel_1,
    cantidad_vendida            = EXCLUDED.cantidad_vendida,
    venta_sin_impuesto          = EXCLUDED.venta_sin_impuesto,
    total_costo                 = EXCLUDED.total_costo,
    ultima_venta_pdv            = EXCLUDED.ultima_venta_pdv,
    ultima_venta_inventario     = EXCLUDED.ultima_venta_inventario,
    estado_ultima_venta_item    = EXCLUDED.estado_ultima_venta_item,
    -- ▼ INVENTARIO NO SE TOCA EN ROLLING ▼
    -- lapso_inventario, can_disponible_foto, fecha_ultima_compra,
    -- fecha_ultima_entrada, costo_uni_inventario, fecha_foto_inventario,
    -- inv_foto_bloqueada  →  permanecen intactos
    fecha_actualizacion         = now()
"""

# ── SOURCE SQL BACKFILL (optimizado) ─────────────────────────────────────────
#
# Versión del SOURCE_SQL para backfill histórico.
# Diferencia clave: mov_base_anio solo escanea el DÍA exacto procesado,
# NO la ventana año-hasta-hoy. Esto lo convierte de O(n²) a O(n).
#
# Consecuencia aceptable: ultima_venta_pdv en registros históricos refleja
# si el ítem vendió ESE día específico (no el acumulado del año).
# El timer diario (SOURCE_SQL) sí usa la ventana completa del año.
#
# Parámetros posicionales:
#   %s 1  lapso_str  → inventario_max_lapso: MAX lapso_doc <= ?
#   %s 2  empresa    → inventario_foto: etiqueta empresa
#   %s 3  fecha_str  → mov_base_anio:   fecha_dcto = ? (solo el día)
#   %s 4  fecha_str  → ventas_dia:      fecha_dcto = ?
#   %s 5  fecha_str  → SELECT TO_DATE fecha_dia
#   %s 6  fecha_str  → SELECT fecha_foto_inventario
#
SOURCE_SQL_BACKFILL = """
WITH
items_cat4 AS MATERIALIZED (
    SELECT
        BTRIM(i.id_item)                                            AS id_item,
        BTRIM(i.descripcion)                                        AS nombre_item,
        BTRIM(COALESCE(
            NULLIF(i.unimed_inv_1, ''),
            NULLIF(i.unimed_com,   ''),
            ''
        ))                                                          AS unidad_inventario,
        BTRIM(i.id_tipo)                                            AS id_categoria,
        BTRIM(COALESCE(c.cmtipinv_descripcion, ''))                 AS nombre_categoria,
        BTRIM(COALESCE(i.id_linea1, ''))                            AS id_linea_nivel_1,
        BTRIM(COALESCE(l1.cmlineas_descripcion, ''))                AS nombre_linea_nivel_1,
        -- Costo fallback para kits e ítems con costo_uni = 0 en inventario
        COALESCE(NULLIF(i.costo_act_acum, 0), NULLIF(i.ultimo_costo_ed, 0), 0)
                                                                    AS costo_item_maestro
    FROM public.items i
    LEFT JOIN public.categorias c
        ON BTRIM(c.id_tipo) = BTRIM(i.id_tipo)
    LEFT JOIN public.lineas l1
        ON BTRIM(l1.id_linea) = BTRIM(i.id_linea1)
       AND BTRIM(l1.id_tipo)  = BTRIM(i.id_tipo)
    WHERE BTRIM(i.id_tipo) = '4'
),
inventario_max_lapso AS (
    SELECT
        BTRIM(ri.id_co)                             AS sede,
        BTRIM(ri.id_local)                          AS bodega_local,
        BTRIM(ri.id_item)                           AS id_item,
        MAX(BTRIM(ri.lapso_doc))                    AS max_lapso
    FROM public.cmresumen_inventario ri
    INNER JOIN items_cat4 i ON i.id_item = BTRIM(ri.id_item)
    WHERE RIGHT(BTRIM(ri.id_local), 2) = '01'
      AND BTRIM(ri.lapso_doc)          >= %s   -- year_start_lapso YYYYMM
      AND BTRIM(ri.lapso_doc)          <= %s   -- lapso_str YYYYMM
    GROUP BY BTRIM(ri.id_co), BTRIM(ri.id_local), BTRIM(ri.id_item)
),
inventario_foto AS MATERIALIZED (
    SELECT
        %s::text                                    AS empresa,
        ml.sede,
        ml.bodega_local,
        ml.id_item,
        ml.max_lapso                                AS lapso_inventario,
        SUM(COALESCE(ri.can_disponible, 0))         AS can_disponible_foto,
        MAX(NULLIF(BTRIM(ri.fecha_ultcom), ''))
            FILTER (WHERE BTRIM(ri.fecha_ultcom) ~ '^[12][0-9]{7}$')
                                                    AS fecha_ultima_compra,
        MAX(NULLIF(BTRIM(ri.fecha_ultent), ''))
            FILTER (WHERE BTRIM(ri.fecha_ultent) ~ '^[12][0-9]{7}$')
                                                    AS fecha_ultima_entrada,
        MAX(NULLIF(BTRIM(ri.fecha_ultvta), ''))
            FILTER (WHERE BTRIM(ri.fecha_ultvta) ~ '^[12][0-9]{7}$')
                                                    AS fecha_ultima_venta_inventario,
        MAX(COALESCE(ri.costo_uni, 0))              AS costo_uni_inventario
    FROM inventario_max_lapso ml
    JOIN public.cmresumen_inventario ri
        ON  BTRIM(ri.id_co)     = ml.sede
        AND BTRIM(ri.id_local)  = ml.bodega_local
        AND BTRIM(ri.id_item)   = ml.id_item
        AND BTRIM(ri.lapso_doc) = ml.max_lapso
    GROUP BY ml.sede, ml.bodega_local, ml.id_item, ml.max_lapso
),
-- BACKFILL: solo el día exacto — sin ventana año acumulada
mov_base_anio AS MATERIALIZED (
    SELECT
        BTRIM(mp.id_co)                             AS sede,
        BTRIM(mp.fecha_dcto)                        AS fecha_dcto,
        BTRIM(mp.id_local)                          AS bodega_local,
        BTRIM(mp.id_item)                           AS id_item,
        BTRIM(COALESCE(mp.id_unidad, ''))           AS id_unidad,
        COALESCE(mp.cantidad,  0)                   AS cantidad,
        COALESCE(mp.vlrtot_bru, 0)                  AS ven_netas,
        COALESCE(mp.tot_costo, 0)                   AS tot_costo
    FROM public.cmmovimiento_pdv mp
    INNER JOIN items_cat4 i ON i.id_item = BTRIM(mp.id_item)
    WHERE BTRIM(mp.fecha_dcto)             = %s     -- solo el día procesado
      AND RIGHT(BTRIM(mp.id_local), 2)    = '01'
      AND COALESCE(BTRIM(mp.docto_acumulacion), '') NOT LIKE 'Z%%'
),
ventas_dia AS (
    SELECT
        sede, bodega_local, id_item, id_unidad,
        SUM(cantidad)  AS cantidad_vendida,
        SUM(ven_netas) AS venta_sin_impuesto,
        SUM(tot_costo) AS total_costo
    FROM mov_base_anio
    WHERE fecha_dcto = %s
    GROUP BY sede, bodega_local, id_item, id_unidad
),
ultima_venta_pdv AS (
    SELECT sede, id_item, MAX(fecha_dcto) AS ultima_venta_item_sede_pdv
    FROM mov_base_anio
    GROUP BY sede, id_item
)
SELECT
    inv.empresa,
    inv.sede,
    BTRIM(COALESCE(co.descripcion, ''))             AS nombre_sede,
    TO_DATE(%s, 'YYYYMMDD')                         AS fecha_dia,
    inv.bodega_local,
    inv.id_item,
    COALESCE(i.nombre_item, '')                     AS nombre_item,
    COALESCE(NULLIF(v.id_unidad, ''), NULLIF(i.unidad_inventario, ''), 'SIN_VENTA')
                                                    AS id_unidad,
    COALESCE(i.id_categoria, '')                    AS id_categoria,
    COALESCE(i.nombre_categoria, '')                AS nombre_categoria,
    COALESCE(i.id_linea_nivel_1, '')                AS id_linea_nivel_1,
    COALESCE(i.nombre_linea_nivel_1, '')            AS nombre_linea_nivel_1,
    COALESCE(v.cantidad_vendida,   0)               AS cantidad_vendida,
    COALESCE(v.venta_sin_impuesto, 0)               AS venta_sin_impuesto,
    -- Fix kits: ERP registra tot_costo=0 en movimientos → calcular desde costo unitario
    CASE
        WHEN COALESCE(v.total_costo, 0) = 0 AND COALESCE(v.cantidad_vendida, 0) > 0
        THEN COALESCE(v.cantidad_vendida, 0) *
             COALESCE(NULLIF(inv.costo_uni_inventario, 0), i.costo_item_maestro, 0)
        ELSE COALESCE(v.total_costo, 0)
    END                                             AS total_costo,
    CASE
        WHEN uv.ultima_venta_item_sede_pdv IS NOT NULL
            THEN TO_DATE(uv.ultima_venta_item_sede_pdv, 'YYYYMMDD')
        ELSE NULL
    END                                             AS ultima_venta_pdv,
    CASE
        WHEN inv.fecha_ultima_venta_inventario IS NOT NULL
             AND inv.fecha_ultima_venta_inventario ~ '^[12][0-9]{7}$'
            THEN TO_DATE(inv.fecha_ultima_venta_inventario, 'YYYYMMDD')
        ELSE NULL
    END                                             AS ultima_venta_inventario,
    CASE
        WHEN uv.ultima_venta_item_sede_pdv IS NOT NULL THEN 'CON VENTA EN EL AÑO'
        ELSE 'SIN VENTA EN EL AÑO'
    END                                             AS estado_ultima_venta_item,
    inv.lapso_inventario,
    inv.can_disponible_foto,
    CASE WHEN inv.fecha_ultima_compra IS NOT NULL
              AND inv.fecha_ultima_compra ~ '^[12][0-9]{7}$'
         THEN TO_DATE(inv.fecha_ultima_compra,  'YYYYMMDD') ELSE NULL END
                                                    AS fecha_ultima_compra,
    CASE WHEN inv.fecha_ultima_entrada IS NOT NULL
              AND inv.fecha_ultima_entrada ~ '^[12][0-9]{7}$'
         THEN TO_DATE(inv.fecha_ultima_entrada, 'YYYYMMDD') ELSE NULL END
                                                    AS fecha_ultima_entrada,
    COALESCE(NULLIF(inv.costo_uni_inventario, 0), i.costo_item_maestro, 0)
                                                    AS costo_uni_inventario,
    TO_DATE(%s, 'YYYYMMDD')                         AS fecha_foto_inventario,
    FALSE                                           AS inv_foto_bloqueada
FROM inventario_foto inv
INNER JOIN items_cat4 i    ON i.id_item  = inv.id_item
LEFT JOIN  ventas_dia v
    ON  v.sede        = inv.sede
    AND v.bodega_local = inv.bodega_local
    AND v.id_item     = inv.id_item
LEFT JOIN  ultima_venta_pdv uv
    ON  uv.sede    = inv.sede
    AND uv.id_item = inv.id_item
LEFT JOIN  public.centro_operacion co ON BTRIM(co.codigo) = inv.sede
-- Excluir filas vacías: sin ventas, sin inventario disponible y sin valor de inventario.
-- Si can_disponible=0 → valor (can_disp × costo_uni) = 0 siempre.
WHERE (
    COALESCE(v.cantidad_vendida,   0) <> 0
    OR COALESCE(v.venta_sin_impuesto, 0) <> 0
    OR COALESCE(inv.can_disponible_foto, 0) <> 0
    OR COALESCE(inv.can_disponible_foto, 0) * COALESCE(inv.costo_uni_inventario, 0) <> 0
)
ORDER BY inv.sede, inv.bodega_local, inv.id_item;
"""

# ── LOCK snapshot de inventario ───────────────────────────────────────────────
LOCK_INV_SQL = f"""
UPDATE {TARGET_TABLE}
SET    inv_foto_bloqueada = TRUE
WHERE  empresa   = %s
  AND  fecha_dia = %s
  AND  inv_foto_bloqueada = FALSE;
"""

# ── Helpers de fecha ──────────────────────────────────────────────────────────

def fmt_date(d: date) -> str:
    return d.strftime("%Y%m%d")

def fmt_lapso(d: date) -> str:
    return d.strftime("%Y%m")

def fmt_year_start(d: date) -> str:
    """1 de enero del año del día procesado."""
    return f"{d.year}0101"

def date_range(start: date, end: date) -> Iterator[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


# ── Env / Conexiones ──────────────────────────────────────────────────────────

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()

def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key, "").strip()
    return int(raw) if raw else default

def load_env_file() -> None:
    env_path = Path(__file__).parent / "config" / "rotacion.env"
    if not env_path.exists():
        return
    with open(env_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

def _connect(host: str, port: str, dbname: str, user: str,
             password: str, timeout: int = 15,
             app_name: str = "etl_rotacion_v3") -> PgConn:
    return psycopg2.connect(
        host=host, port=int(port), dbname=dbname,
        user=user, password=password,
        connect_timeout=timeout,
        options=f"-c client_encoding=UTF8 -c application_name={app_name}",
    )

def get_target_conn() -> PgConn:
    return _connect(
        host=_env("TARGET_PGHOST"),
        port=_env("TARGET_PGPORT", "5432"),
        dbname=_env("TARGET_PGDATABASE"),
        user=_env("TARGET_PGUSER"),
        password=_env("TARGET_PGPASSWORD"),
        timeout=_env_int("PGCONNECT_TIMEOUT", 15),
    )

def get_source_conn(empresa: str) -> PgConn:
    cfg = COMPANY_ENV[empresa]
    conn = _connect(
        host=_env(cfg["host"]),
        port=_env(cfg["port"], "5432"),
        dbname=_env(cfg["db"]),
        user=_env(cfg["user"]),
        password=_env(cfg["pw"]),
        timeout=_env_int("PGCONNECT_TIMEOUT", 15),
    )
    # Servidor 217 es BD replicadora (no ERP directo) — podemos usar más memoria
    with conn.cursor() as cur:
        cur.execute("SET work_mem = '256MB';")
    conn.commit()
    return conn


# ── Tabla destino ─────────────────────────────────────────────────────────────

def ensure_target_table(tgt: PgConn, recreate: bool = False) -> None:
    with tgt.cursor() as cur:
        if recreate:
            LOGGER.warning("--recreate-table: eliminando tabla existente …")
            cur.execute(DDL_DROP)
            cur.execute(DDL_CREATE)
            LOGGER.info("Tabla recreada OK")
        else:
            cur.execute(DDL_CREATE_IF_NOT_EXISTS)
            for idx in DDL_INDICES:
                cur.execute(idx)
            LOGGER.info("Tabla verificada OK")
    tgt.commit()


# ── Extracción ────────────────────────────────────────────────────────────────

def extract_day(src: PgConn, empresa: str, day: date,
                backfill: bool = False) -> List[tuple]:
    """
    Extrae ventas + inventario para un día específico.

    backfill=False  →  SOURCE_SQL (ventana año completa — para daily/rolling)
      %s 1  lapso_str       → inventario_max_lapso
      %s 2  empresa         → inventario_foto
      %s 3  year_start_str  → mov_base_anio fecha >= ?
      %s 4  fecha_str       → mov_base_anio fecha <= ?
      %s 5  fecha_str       → ventas_dia
      %s 6  fecha_str       → fecha_dia SELECT
      %s 7  fecha_str       → fecha_foto_inventario SELECT

    backfill=True   →  SOURCE_SQL_BACKFILL (solo el día — O(n) vs O(n²))
      %s 1  lapso_str  → inventario_max_lapso
      %s 2  empresa    → inventario_foto
      %s 3  fecha_str  → mov_base_anio = día exacto
      %s 4  fecha_str  → ventas_dia
      %s 5  fecha_str  → fecha_dia SELECT
      %s 6  fecha_str  → fecha_foto_inventario SELECT
    """
    fecha_str      = fmt_date(day)
    lapso_str      = fmt_lapso(day)

    year_start_lapso = f"{day.year}01"   # ej: '202601'

    if backfill:
        params = (
            year_start_lapso, # %s 1 — inventario_max_lapso >= enero del año
            lapso_str,        # %s 2 — inventario_max_lapso <= lapso actual
            empresa,          # %s 3 — empresa en inventario_foto
            fecha_str,        # %s 4 — mov_base_anio solo el día
            fecha_str,        # %s 5 — ventas_dia
            fecha_str,        # %s 6 — fecha_dia
            fecha_str,        # %s 7 — fecha_foto_inventario
        )
        sql = SOURCE_SQL_BACKFILL
        label = "backfill-fast"
    else:
        year_start_str = fmt_year_start(day)
        params = (
            year_start_lapso, # %s 1 — inventario_max_lapso >= enero del año
            lapso_str,        # %s 2 — inventario_max_lapso <= lapso actual
            empresa,          # %s 3 — empresa en inventario_foto
            year_start_str,   # %s 4 — mov_base_anio fecha >= ?
            fecha_str,        # %s 5 — mov_base_anio fecha <= ?
            fecha_str,        # %s 6 — ventas_dia
            fecha_str,        # %s 7 — fecha_dia
            fecha_str,        # %s 8 — fecha_foto_inventario
        )
        sql = SOURCE_SQL
        label = "ventana-año"

    with src.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    LOGGER.info(
        "empresa=%-10s  fecha=%s  lapso=%s  modo=%s  filas=%d",
        empresa, fecha_str, lapso_str, label, len(rows),
    )
    return rows


# ── Carga destino ─────────────────────────────────────────────────────────────

def load_rows(tgt: PgConn, rows: List[tuple], upsert_sql: str) -> int:
    if not rows:
        return 0
    total = 0
    with tgt.cursor() as cur:
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i: i + BATCH_SIZE]
            psycopg2.extras.execute_values(cur, upsert_sql, batch,
                                           page_size=BATCH_SIZE)
            total += len(batch)
    tgt.commit()
    return total

def lock_inventory_snapshot(tgt: PgConn, empresa: str, day: date) -> int:
    with tgt.cursor() as cur:
        cur.execute(LOCK_INV_SQL, (empresa, day))
        locked = cur.rowcount
    tgt.commit()
    return locked


# ── Modos de carga ────────────────────────────────────────────────────────────

def process_daily(empresa: str, src: PgConn, tgt: PgConn, dry_run: bool) -> int:
    yesterday = date.today() - timedelta(days=1)
    LOGGER.info("empresa=%-10s  modo=daily  fecha=%s", empresa, fmt_date(yesterday))
    if dry_run:
        LOGGER.info("[DRY-RUN] empresa=%s  fecha=%s", empresa, fmt_date(yesterday))
        return 0
    rows   = extract_day(src, empresa, yesterday, backfill=True)
    loaded = load_rows(tgt, rows, UPSERT_FULL_SQL)
    locked = lock_inventory_snapshot(tgt, empresa, yesterday)
    LOGGER.info(
        "OK  empresa=%-10s  modo=daily  fecha=%s  cargadas=%d  inv_bloqueadas=%d",
        empresa, fmt_date(yesterday), loaded, locked,
    )
    return loaded


def process_rolling(empresa: str, src: PgConn, tgt: PgConn,
                    rolling_days: int, dry_run: bool) -> int:
    yesterday  = date.today() - timedelta(days=1)
    date_start = yesterday - timedelta(days=rolling_days - 1)
    LOGGER.info(
        "empresa=%-10s  modo=rolling  rango=%s..%s",
        empresa, fmt_date(date_start), fmt_date(yesterday),
    )
    if dry_run:
        LOGGER.info("[DRY-RUN] empresa=%s  rango=%s..%s",
                    empresa, fmt_date(date_start), fmt_date(yesterday))
        return 0
    total = 0
    for day in date_range(date_start, yesterday):
        rows   = extract_day(src, empresa, day, backfill=True)
        total += load_rows(tgt, rows, UPSERT_VENTAS_SQL)
    LOGGER.info("OK  empresa=%-10s  modo=rolling  total_cargadas=%d", empresa, total)
    return total


def extract_inventory_month(src: PgConn, empresa: str,
                            lapso_str: str, year_start_lapso: str) -> Dict[tuple, tuple]:
    """
    Extrae el snapshot de inventario para un mes completo.
    Retorna dict keyed by (sede, bodega_local, id_item) → tupla con todos
    los campos de inventario y maestro.
    Se llama UNA VEZ por mes — no por día. Dramáticamente más eficiente.
    """
    INVENTORY_SQL = """
WITH
items_cat4 AS MATERIALIZED (
    SELECT
        BTRIM(i.id_item)                                            AS id_item,
        BTRIM(i.descripcion)                                        AS nombre_item,
        BTRIM(COALESCE(NULLIF(i.unimed_inv_1,''),NULLIF(i.unimed_com,''),''))
                                                                    AS unidad_inventario,
        BTRIM(i.id_tipo)                                            AS id_categoria,
        BTRIM(COALESCE(c.cmtipinv_descripcion,''))                  AS nombre_categoria,
        BTRIM(COALESCE(i.id_linea1,''))                             AS id_linea_nivel_1,
        BTRIM(COALESCE(l1.cmlineas_descripcion,''))                 AS nombre_linea_nivel_1,
        COALESCE(NULLIF(i.costo_act_acum,0), NULLIF(i.ultimo_costo_ed,0), 0)
                                                                    AS costo_item_maestro
    FROM public.items i
    LEFT JOIN public.categorias c  ON BTRIM(c.id_tipo) = BTRIM(i.id_tipo)
    LEFT JOIN public.lineas l1
        ON BTRIM(l1.id_linea) = BTRIM(i.id_linea1)
       AND BTRIM(l1.id_tipo)  = BTRIM(i.id_tipo)
    WHERE BTRIM(i.id_tipo) = '4'
),
inventario_max_lapso AS (
    SELECT
        BTRIM(ri.id_co)     AS sede,
        BTRIM(ri.id_local)  AS bodega_local,
        BTRIM(ri.id_item)   AS id_item,
        MAX(BTRIM(ri.lapso_doc)) AS max_lapso
    FROM public.cmresumen_inventario ri
    INNER JOIN items_cat4 i ON i.id_item = BTRIM(ri.id_item)
    WHERE RIGHT(BTRIM(ri.id_local),2) = '01'
      AND BTRIM(ri.lapso_doc) >= %s
      AND BTRIM(ri.lapso_doc) <= %s
    GROUP BY BTRIM(ri.id_co), BTRIM(ri.id_local), BTRIM(ri.id_item)
),
inventario_foto AS MATERIALIZED (
    SELECT
        %s::text            AS empresa,
        ml.sede, ml.bodega_local, ml.id_item,
        ml.max_lapso        AS lapso_inventario,
        SUM(COALESCE(ri.can_disponible,0))  AS can_disponible_foto,
        MAX(NULLIF(BTRIM(ri.fecha_ultcom),''))
            FILTER (WHERE BTRIM(ri.fecha_ultcom) ~ '^[12][0-9]{7}$') AS fecha_ultima_compra,
        MAX(NULLIF(BTRIM(ri.fecha_ultent),''))
            FILTER (WHERE BTRIM(ri.fecha_ultent) ~ '^[12][0-9]{7}$') AS fecha_ultima_entrada,
        MAX(NULLIF(BTRIM(ri.fecha_ultvta),''))
            FILTER (WHERE BTRIM(ri.fecha_ultvta) ~ '^[12][0-9]{7}$') AS fecha_ultima_venta_inv,
        MAX(COALESCE(ri.costo_uni,0))       AS costo_uni_inventario
    FROM inventario_max_lapso ml
    JOIN public.cmresumen_inventario ri
        ON  BTRIM(ri.id_co)     = ml.sede
        AND BTRIM(ri.id_local)  = ml.bodega_local
        AND BTRIM(ri.id_item)   = ml.id_item
        AND BTRIM(ri.lapso_doc) = ml.max_lapso
    GROUP BY ml.sede, ml.bodega_local, ml.id_item, ml.max_lapso
)
SELECT
    inv.empresa,
    inv.sede,
    BTRIM(COALESCE(co.descripcion,''))  AS nombre_sede,
    inv.bodega_local,
    inv.id_item,
    COALESCE(i.nombre_item,'')          AS nombre_item,
    COALESCE(NULLIF(i.unidad_inventario,''),'SIN_VENTA') AS unidad_inventario,
    COALESCE(i.id_categoria,'')         AS id_categoria,
    COALESCE(i.nombre_categoria,'')     AS nombre_categoria,
    COALESCE(i.id_linea_nivel_1,'')     AS id_linea_nivel_1,
    COALESCE(i.nombre_linea_nivel_1,'') AS nombre_linea_nivel_1,
    inv.lapso_inventario,
    inv.can_disponible_foto,
    CASE WHEN inv.fecha_ultima_compra ~ '^[12][0-9]{7}$'
         THEN TO_DATE(inv.fecha_ultima_compra,'YYYYMMDD') ELSE NULL END  AS fecha_ultima_compra,
    CASE WHEN inv.fecha_ultima_entrada ~ '^[12][0-9]{7}$'
         THEN TO_DATE(inv.fecha_ultima_entrada,'YYYYMMDD') ELSE NULL END AS fecha_ultima_entrada,
    CASE WHEN inv.fecha_ultima_venta_inv ~ '^[12][0-9]{7}$'
         THEN TO_DATE(inv.fecha_ultima_venta_inv,'YYYYMMDD') ELSE NULL END AS ultima_venta_inventario,
    COALESCE(NULLIF(inv.costo_uni_inventario, 0), i.costo_item_maestro, 0)
                                                AS costo_uni_inventario
FROM inventario_foto inv
INNER JOIN items_cat4 i ON i.id_item = inv.id_item
LEFT JOIN public.centro_operacion co ON BTRIM(co.codigo) = inv.sede
WHERE (inv.can_disponible_foto <> 0 OR inv.costo_uni_inventario <> 0
       OR i.costo_item_maestro <> 0);
"""
    with src.cursor() as cur:
        cur.execute(INVENTORY_SQL, (year_start_lapso, lapso_str, empresa))
        rows = cur.fetchall()
    # key = (sede, bodega_local, id_item)
    return {(r[1], r[3], r[4]): r for r in rows}


def extract_sales_day(src: PgConn, day: date) -> Dict[tuple, tuple]:
    """
    Extrae solo las ventas del día. Consulta liviana — un solo día.
    Retorna dict keyed by (sede, bodega_local, id_item).
    """
    SALES_DAY_SQL = """
SELECT
    BTRIM(mp.id_co)                         AS sede,
    BTRIM(mp.id_local)                      AS bodega_local,
    BTRIM(mp.id_item)                       AS id_item,
    BTRIM(COALESCE(mp.id_unidad,''))        AS id_unidad,
    SUM(COALESCE(mp.cantidad,  0))          AS cantidad_vendida,
    SUM(COALESCE(mp.vlrtot_bru,0))          AS venta_sin_impuesto,
    SUM(COALESCE(mp.tot_costo, 0))          AS total_costo
FROM public.cmmovimiento_pdv mp
INNER JOIN public.items i
    ON BTRIM(i.id_item) = BTRIM(mp.id_item)
   AND BTRIM(i.id_tipo) = '4'
WHERE BTRIM(mp.fecha_dcto) = %s
  AND RIGHT(BTRIM(mp.id_local),2) = '01'
  AND COALESCE(BTRIM(mp.docto_acumulacion),'') NOT LIKE 'Z%%'
GROUP BY BTRIM(mp.id_co), BTRIM(mp.id_local),
         BTRIM(mp.id_item), BTRIM(COALESCE(mp.id_unidad,''));
"""
    with src.cursor() as cur:
        cur.execute(SALES_DAY_SQL, (fmt_date(day),))
        rows = cur.fetchall()
    return {(r[0], r[1], r[2]): r for r in rows}


def merge_day(inventory: Dict[tuple, tuple], sales: Dict[tuple, tuple],
              empresa: str, day: date) -> List[tuple]:
    """
    Combina inventario (cache mensual) con ventas (día específico) en Python.
    Retorna la lista de tuplas lista para UPSERT — mismo orden que el INSERT.
    """
    rows = []
    for key, inv in inventory.items():
        # inv: empresa,sede,nombre_sede,bodega_local,id_item,nombre_item,
        #      unidad_inv,id_cat,nombre_cat,id_linea,nombre_linea,
        #      lapso_inv,can_disp,f_compra,f_entrada,ult_vta_inv,costo_uni
        (emp, sede, nombre_sede, bodega_local, id_item,
         nombre_item, unidad_inv, id_cat, nombre_cat,
         id_linea, nombre_linea, lapso_inv, can_disp,
         f_compra, f_entrada, ult_vta_inv, costo_uni) = inv

        sale = sales.get(key)
        if sale:
            id_unidad         = sale[3] or unidad_inv
            cantidad_vendida  = sale[4]
            venta_sin_imp     = sale[5]
            total_costo       = sale[6]
            # Fix kits: tot_costo=0 en movimientos → calcular desde costo unitario
            if float(total_costo or 0) == 0 and float(cantidad_vendida or 0) > 0:
                total_costo = float(cantidad_vendida or 0) * float(costo_uni or 0)
            ultima_venta_pdv  = day
            estado            = 'CON VENTA EN EL AÑO'
        else:
            id_unidad         = unidad_inv
            cantidad_vendida  = 0
            venta_sin_imp     = 0
            total_costo       = 0
            ultima_venta_pdv  = None
            estado            = 'SIN VENTA EN EL AÑO'

        # Filtro: excluir filas sin ventas, sin inventario disponible y sin valor de inventario.
        # Nota: si can_disp=0 entonces valor_inv=0 siempre (0 × costo_uni = 0).
        valor_inv = float(can_disp or 0) * float(costo_uni or 0)
        if (cantidad_vendida == 0 and venta_sin_imp == 0
                and float(can_disp or 0) == 0 and valor_inv == 0):
            continue

        rows.append((
            emp, sede, nombre_sede, day, bodega_local, id_item,
            nombre_item, id_unidad, id_cat, nombre_cat,
            id_linea, nombre_linea,
            cantidad_vendida, venta_sin_imp, total_costo,
            ultima_venta_pdv, ult_vta_inv, estado,
            lapso_inv, can_disp,
            f_compra, f_entrada,
            costo_uni, day,   # fecha_foto_inventario
            False,            # inv_foto_bloqueada
        ))
    return rows


def process_backfill(empresa: str, src: PgConn, tgt: PgConn,
                     date_start: date, date_end: date, dry_run: bool) -> int:
    """
    Backfill optimizado: inventario cargado UNA VEZ por mes,
    ventas consultadas día a día (consulta liviana).
    Reduce el tiempo de horas a minutos.
    """
    LOGGER.info(
        "empresa=%-10s  modo=backfill  rango=%s..%s",
        empresa, fmt_date(date_start), fmt_date(date_end),
    )
    if dry_run:
        LOGGER.info("[DRY-RUN] empresa=%s  rango=%s..%s",
                    empresa, fmt_date(date_start), fmt_date(date_end))
        return 0

    total            = 0
    current_lapso    = None
    inventory_cache: Dict[tuple, tuple] = {}

    for day in date_range(date_start, date_end):
        lapso            = fmt_lapso(day)
        year_start_lapso = f"{day.year}01"

        # Recargar inventario solo cuando cambia el mes
        if lapso != current_lapso:
            LOGGER.info(
                "empresa=%-10s  cargando inventario lapso=%s …", empresa, lapso
            )
            inventory_cache = extract_inventory_month(
                src, empresa, lapso, year_start_lapso
            )
            current_lapso = lapso
            LOGGER.info(
                "empresa=%-10s  inventario lapso=%s  ítems_con_stock=%d",
                empresa, lapso, len(inventory_cache),
            )

        # Ventas del día (rápido)
        sales = extract_sales_day(src, day)

        # Merge en Python
        rows   = merge_day(inventory_cache, sales, empresa, day)
        loaded = load_rows(tgt, rows, UPSERT_FULL_SQL)
        total += loaded
        LOGGER.info(
            "empresa=%-10s  fecha=%s  ventas_items=%d  cargadas=%d",
            empresa, fmt_date(day), len(sales), loaded,
        )

    LOGGER.info("OK  empresa=%-10s  modo=backfill  total_cargadas=%d", empresa, total)
    return total


# ── Logging ───────────────────────────────────────────────────────────────────

def resolve_log_dir(log_dir_arg: Optional[str]) -> Path:
    raw = log_dir_arg or _env("ETL_LOG_DIR") or "logs"
    p = Path(raw)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent / p
    p.mkdir(parents=True, exist_ok=True)
    return p

def configure_logging(args: argparse.Namespace) -> None:
    log_dir  = resolve_log_dir(args.log_dir)
    log_file = log_dir / f"{LOG_FILE_PREFIX}_{date.today():%Y%m%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
        force=True,
    )

def cleanup_old_logs(log_dir: Path, retention_days: int) -> None:
    cutoff = date.today() - timedelta(days=retention_days)
    for f in log_dir.glob(f"{LOG_FILE_PREFIX}_*.log"):
        try:
            ds = f.stem.split("_")[-1]
            if len(ds) == 8:
                d = date(int(ds[:4]), int(ds[4:6]), int(ds[6:8]))
                if d < cutoff:
                    f.unlink()
        except (ValueError, OSError):
            pass


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="ETL Rotación de Ítems v3",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--mode", choices=("daily", "rolling", "backfill"),
                   default="daily")
    p.add_argument("--rolling-days", type=int, default=DEFAULT_ROLLING_DAYS)
    p.add_argument("--date-start", default=None, metavar="YYYYMMDD")
    p.add_argument("--date-end",   default=None, metavar="YYYYMMDD")
    p.add_argument("--empresas", nargs="+", default=list(COMPANIES),
                   choices=list(COMPANIES), metavar="EMPRESA")
    p.add_argument("--recreate-table", action="store_true",
                   help="⚠ BORRA y recrea la tabla destino")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--check-only", action="store_true")
    p.add_argument("--log-dir", default=None, metavar="PATH")
    p.add_argument("--log-retention-days", type=int, default=FILE_LOG_RETENTION)
    return p.parse_args(argv)


# ── Entry point ───────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> int:
    yesterday = date.today() - timedelta(days=1)

    date_start: Optional[date] = None
    date_end:   Optional[date] = None
    if args.mode == "backfill":
        if not args.date_start:
            LOGGER.error("--mode backfill requiere --date-start YYYYMMDD")
            return 1
        try:
            date_start = date(int(args.date_start[:4]),
                              int(args.date_start[4:6]),
                              int(args.date_start[6:8]))
            date_end = (date(int(args.date_end[:4]),
                             int(args.date_end[4:6]),
                             int(args.date_end[6:8]))
                        if args.date_end else yesterday)
        except (ValueError, TypeError) as exc:
            LOGGER.error("Fecha inválida: %s", exc)
            return 1
        if date_start > date_end:
            LOGGER.error("date_start > date_end")
            return 1

    LOGGER.info("=== ETL rotacion_v3 inicio  modo=%s  empresas=%s ===",
                args.mode, ",".join(args.empresas))

    try:
        tgt = get_target_conn()
    except psycopg2.Error as exc:
        LOGGER.error("No se pudo conectar a BD destino (232): %s", exc)
        return 1

    if args.check_only:
        with tgt.cursor() as cur:
            cur.execute("SELECT current_database(), current_user, inet_server_addr();")
            db, usr, addr = cur.fetchone()
            LOGGER.info("Destino OK  db=%s  user=%s  addr=%s", db, usr, addr)
        tgt.close()
        for empresa in args.empresas:
            try:
                src = get_source_conn(empresa)
                with src.cursor() as cur:
                    cur.execute("SELECT current_database(), current_user, inet_server_addr();")
                    db, usr, addr = cur.fetchone()
                    LOGGER.info("Origen OK  empresa=%-10s  db=%s  user=%s  addr=%s",
                                empresa, db, usr, addr)
                src.close()
            except psycopg2.Error as exc:
                LOGGER.error("Origen FALLO  empresa=%s: %s", empresa, exc)
        return 0

    if not args.dry_run:
        try:
            ensure_target_table(tgt, recreate=args.recreate_table)
        except psycopg2.Error as exc:
            LOGGER.error("Error preparando tabla: %s", exc)
            tgt.close()
            return 1

    total_loaded = 0
    errors       = 0

    for empresa in args.empresas:
        try:
            src = get_source_conn(empresa)
        except psycopg2.Error as exc:
            LOGGER.error("empresa=%-10s  conexión fallida: %s", empresa, exc)
            errors += 1
            continue

        try:
            if args.mode == "daily":
                n = process_daily(empresa, src, tgt, args.dry_run)
            elif args.mode == "rolling":
                n = process_rolling(empresa, src, tgt,
                                    args.rolling_days, args.dry_run)
            else:
                n = process_backfill(empresa, src, tgt,
                                     date_start, date_end, args.dry_run)
            total_loaded += n

        except psycopg2.Error as exc:
            LOGGER.error("empresa=%-10s  error: %s", empresa, exc)
            try:
                tgt.rollback()
            except Exception:
                pass
            errors += 1
        finally:
            try:
                src.close()
            except Exception:
                pass

    try:
        tgt.close()
    except Exception:
        pass

    LOGGER.info("=== ETL rotacion_v3 fin  total_cargadas=%d  errores=%d ===",
                total_loaded, errors)
    return 0 if errors == 0 else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    load_env_file()
    args = parse_args(argv)
    configure_logging(args)
    cleanup_old_logs(resolve_log_dir(args.log_dir), args.log_retention_days)
    try:
        return run(args)
    except (psycopg2.Error, ValueError, OSError) as exc:
        LOGGER.error("Error fatal: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
