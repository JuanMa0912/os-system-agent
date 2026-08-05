#!/usr/bin/env python3
"""
ETL Rotación de Ítems v2 — 3 empresas (mercamio, mtodo, bogota)
================================================================
Origen:  192.168.35.217  (mercamio / mtodo / bogota)
Destino: 192.168.35.232  BD=produXdia  tabla=rotacion_base_item_dia_sede

Cambios respecto a versiones anteriores
----------------------------------------
1. FULL OUTER JOIN ventas ↔ inventario:
   Incluye ítems con inventario activo aunque no tuvieran venta ese día.

2. Foto diaria de inventario:
   can_exis_fin se toma del lapso disponible más reciente (MAX lapso_doc
   <= mes de consulta). Como cmresumen_inventario actualiza diariamente,
   la foto capturada en el INSERT diario queda protegida: en modo rolling
   el inventario NO se sobreescribe (UPSERT_VENTAS_SQL).

3. Sin duplicados:
   Todos los CTEs agregan con GROUP BY antes del JOIN. La PK de la tabla
   destino garantiza unicidad por (empresa, fecha_dia, sede, bodega,
   item, id_ext_itm).

4. Categoría correcta:
   Usa criterios_itm_4 (id_cricla4 + id_tipo = id_catego) en lugar de la
   tabla genérica categorias.

5. id_empresa desde la conexión:
   El parámetro "empresa" viene de Python según la base de datos origen,
   no de un campo inexistente en cmresumen_inventario.

── Modos ───────────────────────────────────────────────────────────────────────
  daily    (timer 7am):
      Carga el día de ayer. UPSERT completo (ventas + inventario).
      Es la carga principal: captura la foto del inventario del día.

  rolling  (timer 1am días 1, 11, 21):
      Reprocesa los últimos N días (default 15).
      UPSERT SOLO ventas — inventario histórico intacto.

  backfill (manual):
      Carga un rango de fechas. UPSERT completo.
      Itera día a día para respetar el lapso de inventario de cada mes.

── Ejemplos ────────────────────────────────────────────────────────────────────
  python etl_rotacion_v2.py --mode daily
  python etl_rotacion_v2.py --mode rolling --rolling-days 15
  python etl_rotacion_v2.py --mode backfill --date-start 20260101 --date-end 20260430
  python etl_rotacion_v2.py --mode backfill --date-start 20260101 --date-end 20260430 --recreate-table
  python etl_rotacion_v2.py --mode daily --empresas mercamio --dry-run
  python etl_rotacion_v2.py --check-only
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

import psycopg2
import psycopg2.extras
from psycopg2.extensions import connection as PgConn

# ── Logger ────────────────────────────────────────────────────────────────────
LOGGER = logging.getLogger("etl_rotacion_v2")

# ── Constantes ────────────────────────────────────────────────────────────────
TARGET_TABLE         = "public.rotacion_base_item_dia_sede"
DEFAULT_ROLLING_DAYS = 15
LOG_FILE_PREFIX      = "etl_rotacion_v2"
FILE_LOG_RETENTION   = 31
BATCH_SIZE           = 2_000

COMPANIES: List[str] = ["mercamio", "mtodo", "bogota"]

# Nombre legible por empresa (inyectado en nombre_empresa)
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
# PK: (empresa, fecha_dia, sede, bodega, item, id_ext_itm)
# · fecha_dia como DATE nativo — más eficiente para filtros en BI.
# · inv_foto_bloqueada: TRUE una vez que el INSERT diario captura el inventario.
#   En rolling (UPSERT_VENTAS_SQL) este campo nunca se toca, preservando la foto.

DDL_DROP = f"DROP TABLE IF EXISTS {TARGET_TABLE};"

DDL_CREATE = f"""
CREATE TABLE {TARGET_TABLE} (
    -- Llave primaria
    empresa                 VARCHAR(20)    NOT NULL,
    fecha_dia               DATE           NOT NULL,
    sede                    VARCHAR(10)    NOT NULL,
    bodega                  VARCHAR(5)     NOT NULL DEFAULT '',
    item                    VARCHAR(10)    NOT NULL,
    id_ext_itm              VARCHAR(3)     NOT NULL DEFAULT '',

    -- Descriptores
    nombre_empresa          VARCHAR(40),
    nombre_sede             VARCHAR(120),
    nombre_bodega           VARCHAR(40),

    -- Maestro ítem
    descripcion             VARCHAR(255),
    unidad                  VARCHAR(3),

    -- Línea
    linea_nivel_3_codigo    VARCHAR(10),
    linea_nivel_1_codigo    VARCHAR(10),
    nombre_linea_nivel_1    VARCHAR(40),

    -- Categoría (criterios_itm_4)
    categoria_codigo        VARCHAR(4),
    nombre_categoria        VARCHAR(40),

    -- Tipo inventario
    tipo_inv                VARCHAR(1),

    -- Ventas del día
    venta_sin_impuesto_dia  NUMERIC(18,2)  DEFAULT 0,
    unidades_vendidas_dia   NUMERIC(18,4)  DEFAULT 0,

    -- Inventario (foto del día — protegida en reprocesos rolling)
    inventario_cierre       NUMERIC(18,4)  DEFAULT 0,
    valor_inventario        NUMERIC(18,2)  DEFAULT 0,
    lapso_inventario        VARCHAR(6),     -- YYYYMM origen del snapshot

    -- Fechas de referencia
    fecha_ultima_venta      DATE,
    fecha_ultima_entrada    DATE,

    -- Indicadores de rotación (calculados en ETL para facilitar BI)
    dias_sin_venta          INTEGER,        -- días desde última venta al día de consulta
    quiebre_inventario      BOOLEAN DEFAULT FALSE,  -- venta > 0 pero inventario <= 0
    item_sin_rotacion       BOOLEAN DEFAULT FALSE,  -- inventario > 0 pero venta = 0

    -- Auditoría
    inv_foto_bloqueada      BOOLEAN        DEFAULT FALSE,
    fecha_carga             TIMESTAMP      DEFAULT now(),
    fecha_actualizacion     TIMESTAMP      DEFAULT now(),

    PRIMARY KEY (empresa, fecha_dia, sede, bodega, item, id_ext_itm)
);
"""

DDL_CREATE_IF_NOT_EXISTS = DDL_CREATE.replace(
    f"CREATE TABLE {TARGET_TABLE}",
    f"CREATE TABLE IF NOT EXISTS {TARGET_TABLE}",
)

# Migraciones incrementales (para tablas preexistentes)
DDL_MIGRATIONS: List[str] = [
    f"ALTER TABLE {TARGET_TABLE} ADD COLUMN IF NOT EXISTS nombre_empresa VARCHAR(40);",
    f"ALTER TABLE {TARGET_TABLE} ADD COLUMN IF NOT EXISTS lapso_inventario VARCHAR(6);",
    f"ALTER TABLE {TARGET_TABLE} ADD COLUMN IF NOT EXISTS tipo_inv VARCHAR(1);",
    f"ALTER TABLE {TARGET_TABLE} ADD COLUMN IF NOT EXISTS categoria_codigo VARCHAR(4);",
    f"ALTER TABLE {TARGET_TABLE} ADD COLUMN IF NOT EXISTS nombre_categoria VARCHAR(40);",
    f"ALTER TABLE {TARGET_TABLE} ADD COLUMN IF NOT EXISTS dias_sin_venta INTEGER;",
    f"ALTER TABLE {TARGET_TABLE} ADD COLUMN IF NOT EXISTS quiebre_inventario BOOLEAN DEFAULT FALSE;",
    f"ALTER TABLE {TARGET_TABLE} ADD COLUMN IF NOT EXISTS item_sin_rotacion BOOLEAN DEFAULT FALSE;",
    f"ALTER TABLE {TARGET_TABLE} ADD COLUMN IF NOT EXISTS inv_foto_bloqueada BOOLEAN DEFAULT FALSE;",
    f"ALTER TABLE {TARGET_TABLE} ADD COLUMN IF NOT EXISTS linea_nivel_3_codigo VARCHAR(10);",
    # Renombrar columnas obsoletas si existen (migración suave)
    """DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema='public' AND table_name='rotacion_base_item_dia_sede'
              AND column_name='fecha_cierre_inventario'
        ) AND NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema='public' AND table_name='rotacion_base_item_dia_sede'
              AND column_name='lapso_inventario'
        ) THEN
            ALTER TABLE public.rotacion_base_item_dia_sede
                RENAME COLUMN fecha_cierre_inventario TO lapso_inventario;
        END IF;
    END $$;""",
]

# ── ÍNDICES recomendados ──────────────────────────────────────────────────────
DDL_INDICES: List[str] = [
    f"CREATE INDEX IF NOT EXISTS idx_rot_fecha_empresa ON {TARGET_TABLE} (fecha_dia, empresa);",
    f"CREATE INDEX IF NOT EXISTS idx_rot_sede_fecha    ON {TARGET_TABLE} (sede, fecha_dia);",
    f"CREATE INDEX IF NOT EXISTS idx_rot_item_fecha    ON {TARGET_TABLE} (item, fecha_dia);",
    f"CREATE INDEX IF NOT EXISTS idx_rot_linea1_fecha  ON {TARGET_TABLE} (linea_nivel_1_codigo, fecha_dia);",
    f"CREATE INDEX IF NOT EXISTS idx_rot_cat_fecha     ON {TARGET_TABLE} (categoria_codigo, fecha_dia);",
    f"CREATE INDEX IF NOT EXISTS idx_rot_quiebre       ON {TARGET_TABLE} (fecha_dia, quiebre_inventario) WHERE quiebre_inventario = TRUE;",
    f"CREATE INDEX IF NOT EXISTS idx_rot_sin_rotar     ON {TARGET_TABLE} (fecha_dia, item_sin_rotacion)  WHERE item_sin_rotacion  = TRUE;",
]

# ── SQL de extracción (origen) ────────────────────────────────────────────────
#
# Parámetros posicionales:
#   %s  1: empresa (text)
#   %s  2: fecha_consulta YYYYMMDD (text)  — fecha del día a procesar
#   %s  3: lapso_consulta YYYYMM   (text)  — mes del día a procesar
#
# Lógica clave:
#   · ventas_dia     → cmmovimiento_pdv filtrado por fecha_consulta y bodegas %01
#   · inventario_max → MAX(lapso_doc) disponible por (co,local,item,ext)
#                      limitado a <= lapso_consulta (maneja fin de mes sin datos)
#   · inventario_foto→ can_exis_fin del lapso más reciente disponible
#   · llaves         → UNION de (sede,bodega,item,ext) de ventas e inventario
#                      GARANTIZA que items solo con inventario también aparecen
#   · JOIN final     → todos los maestros: items, lineas, criterios_itm_4,
#                      centro_operacion, bodegas, empresas (no existe en src,
#                      se inyecta desde Python con el parámetro empresa)
#
SOURCE_SQL = """
WITH
-- ─── VENTAS DEL DÍA ──────────────────────────────────────────────────────────
-- Fuente: cmmovimiento_pdv (transacciones de caja / PDV)
-- Agrupado por (co, local, item, ext, fecha) para eliminar duplicados
ventas_dia AS (
    SELECT
        BTRIM(mp.id_co)                            AS sede,
        BTRIM(mp.id_local)                         AS bodega,
        BTRIM(mp.id_item)                          AS item,
        COALESCE(BTRIM(mp.id_itmext), '')          AS id_ext_itm,
        SUM(COALESCE(mp.ven_netas, 0))             AS venta_sin_impuesto_dia,
        SUM(COALESCE(mp.cantidad,  0))             AS unidades_vendidas_dia
    FROM public.cmmovimiento_pdv mp
    WHERE BTRIM(mp.fecha_dcto)          = %s           -- fecha_consulta YYYYMMDD
      AND RIGHT(BTRIM(mp.id_local), 2)  = '01'         -- bodegas principales
      -- Excluir notas Z (cierres de caja / ajustes) por docto_acumulacion
      AND (
            mp.docto_acumulacion IS NULL
            OR BTRIM(mp.docto_acumulacion) = ''
            OR BTRIM(mp.docto_acumulacion) NOT LIKE 'Z%%'
          )
    GROUP BY
        BTRIM(mp.id_co),
        BTRIM(mp.id_local),
        BTRIM(mp.id_item),
        COALESCE(BTRIM(mp.id_itmext), '')
),
-- ─── LAPSO MÁS RECIENTE POR ITEM/BODEGA ──────────────────────────────────────
-- Resuelve el lapso disponible más reciente <= mes de consulta.
-- Crítico en los primeros días de un mes nuevo: si aún no hay datos
-- para el lapso actual, toma el lapso del mes anterior.
inventario_max_lapso AS (
    SELECT
        BTRIM(ri.id_co)                            AS sede,
        BTRIM(ri.id_local)                         AS bodega,
        BTRIM(ri.id_item)                          AS item,
        COALESCE(BTRIM(ri.id_ext_itm), '')         AS id_ext_itm,
        MAX(BTRIM(ri.lapso_doc))                   AS max_lapso
    FROM public.cmresumen_inventario ri
    WHERE RIGHT(BTRIM(ri.id_local), 2) = '01'
      AND BTRIM(ri.lapso_doc) <= %s                 -- lapso_consulta YYYYMM
    GROUP BY
        BTRIM(ri.id_co),
        BTRIM(ri.id_local),
        BTRIM(ri.id_item),
        COALESCE(BTRIM(ri.id_ext_itm), '')
),
-- ─── FOTO DE INVENTARIO DEL DÍA ──────────────────────────────────────────────
-- can_exis_fin = saldo corriente del período (se actualiza diariamente en el ERP)
-- Al correr el ETL de hoy se captura la foto actual → se almacena con fecha=hoy
-- y queda protegida por inv_foto_bloqueada = TRUE en la tabla destino.
inventario_foto AS (
    SELECT
        ml.sede,
        ml.bodega,
        ml.item,
        ml.id_ext_itm,
        ml.max_lapso                               AS lapso_inventario,
        SUM(COALESCE(ri.can_exis_fin,  0))         AS inventario_cierre,
        -- Valor inventario: existencia final × costo promedio unitario
        SUM(
            COALESCE(ri.can_exis_fin, 0)
            * COALESCE(NULLIF(ri.costo_uni, 0), ri.ult_costo, 0)
        )                                          AS valor_inventario,
        -- Fechas: solo se toman valores con año >= 1900 para evitar
        -- que '00000000' o fechas inválidas rompan TO_DATE en Python.
        MAX(NULLIF(BTRIM(ri.fecha_ultent), ''))
            FILTER (WHERE BTRIM(ri.fecha_ultent) ~ '^[12][0-9]{7}$')
                                                   AS fecha_ultima_entrada,
        MAX(NULLIF(BTRIM(ri.fecha_ultvta), ''))
            FILTER (WHERE BTRIM(ri.fecha_ultvta) ~ '^[12][0-9]{7}$')
                                                   AS fecha_ultima_venta_inv
    FROM inventario_max_lapso ml
    JOIN public.cmresumen_inventario ri
        ON  BTRIM(ri.id_co)                     = ml.sede
        AND BTRIM(ri.id_local)                  = ml.bodega
        AND BTRIM(ri.id_item)                   = ml.item
        AND COALESCE(BTRIM(ri.id_ext_itm), '')  = ml.id_ext_itm
        AND BTRIM(ri.lapso_doc)                 = ml.max_lapso
    GROUP BY
        ml.sede, ml.bodega, ml.item, ml.id_ext_itm, ml.max_lapso
),
-- ─── LLAVES: UNIÓN VENTAS + INVENTARIO ───────────────────────────────────────
-- FULL OUTER JOIN implícito via UNION:
--   · Items con venta (tengan o no inventario)
--   · Items SIN venta pero CON inventario activo (can_exis_fin > 0)
-- Esto garantiza que el tablero muestre todos los SKUs relevantes,
-- no solo los que vendieron ese día.
llaves AS (
    SELECT sede, bodega, item, id_ext_itm FROM ventas_dia
    UNION
    SELECT sede, bodega, item, id_ext_itm
    FROM inventario_foto
    WHERE inventario_cierre > 0 OR valor_inventario > 0
),
-- ─── MAESTRO DE ÍTEMS ENRIQUECIDO ────────────────────────────────────────────
-- Join único sobre items: evita repetirescan en el SELECT principal
maestro AS (
    SELECT
        BTRIM(i.id_item)                           AS item,
        COALESCE(BTRIM(i.id_ext_itm), '')          AS id_ext_itm,
        BTRIM(i.id_tipo)                           AS tipo_inv,
        BTRIM(COALESCE(i.descripcion, ''))         AS descripcion,
        BTRIM(COALESCE(
            NULLIF(i.unimed_inv_1, ''),
            NULLIF(i.unimed_com,   ''),
            ''
        ))                                         AS unidad,
        -- Línea nivel 3 (la más específica asignada al ítem)
        BTRIM(COALESCE(i.id_linea, ''))            AS linea_nivel_3_codigo,
        -- Línea nivel 1 (agrupador macro)
        BTRIM(COALESCE(i.id_linea1, ''))           AS linea_nivel_1_codigo,
        -- Categoría 4 del ítem (criterio clasificatorio nivel 4)
        BTRIM(COALESCE(i.id_cricla4, ''))          AS categoria_codigo
    FROM public.items i
)
-- ─── QUERY PRINCIPAL ─────────────────────────────────────────────────────────
SELECT
    -- Empresa (viene del parámetro Python — no existe en origen de inventario)
    %s::text                                       AS empresa,
    -- Fecha del día de consulta (convertida a DATE para tipo nativo)
    TO_DATE(%s, 'YYYYMMDD')                        AS fecha_dia,

    -- Llave: orden exacto del INSERT (sede, bodega, item, id_ext_itm, nombres...)
    k.sede,
    k.bodega,
    k.item,
    k.id_ext_itm,
    -- Nombre de empresa legible (parámetro Python: 'Mercamio', 'Método', 'Bogotá')
    %s::text                                       AS nombre_empresa,
    BTRIM(COALESCE(co.descripcion, ''))            AS nombre_sede,
    BTRIM(COALESCE(b.cmlocal_descripcion, ''))     AS nombre_bodega,
    COALESCE(m.descripcion, '')                    AS descripcion,
    COALESCE(m.unidad, '')                         AS unidad,
    COALESCE(m.tipo_inv, '')                       AS tipo_inv,

    -- Líneas
    COALESCE(m.linea_nivel_3_codigo, '')           AS linea_nivel_3_codigo,
    COALESCE(m.linea_nivel_1_codigo, '')           AS linea_nivel_1_codigo,
    BTRIM(COALESCE(l1.cmlineas_descripcion, ''))   AS nombre_linea_nivel_1,

    -- Categoría 4 (criterios_itm_4, join por id_cricla4 + id_catego = id_tipo)
    COALESCE(m.categoria_codigo, '')               AS categoria_codigo,
    BTRIM(COALESCE(c4.cmcricla_descripcion, ''))   AS nombre_categoria,

    -- Ventas del día
    COALESCE(v.venta_sin_impuesto_dia, 0)          AS venta_sin_impuesto_dia,
    COALESCE(v.unidades_vendidas_dia,  0)          AS unidades_vendidas_dia,

    -- Inventario (foto del día — protegida en reprocesos)
    COALESCE(inv.inventario_cierre, 0)             AS inventario_cierre,
    COALESCE(inv.valor_inventario,  0)             AS valor_inventario,
    COALESCE(inv.lapso_inventario,  '')            AS lapso_inventario,

    -- Fecha última venta:
    --   · Si vendió hoy → fecha de hoy
    --   · Si tiene histórico en inventario → usa esa fecha
    --   · Null si no se conoce
    -- Fecha última venta:
    --   · Si vendió hoy → fecha de hoy
    --   · Si tiene histórico válido en inventario → usa esa fecha
    --   · fecha válida = empieza con 1 o 2 y tiene 8 dígitos (YYYYMMDD)
    CASE
        WHEN COALESCE(v.unidades_vendidas_dia, 0) > 0
            THEN TO_DATE(%s, 'YYYYMMDD')
        WHEN inv.fecha_ultima_venta_inv IS NOT NULL
             AND inv.fecha_ultima_venta_inv ~ '^[12][0-9]{7}$'
            THEN TO_DATE(inv.fecha_ultima_venta_inv, 'YYYYMMDD')
        ELSE NULL
    END                                            AS fecha_ultima_venta,

    -- Fecha último ingreso (mercancía recibida)
    CASE
        WHEN inv.fecha_ultima_entrada IS NOT NULL
             AND inv.fecha_ultima_entrada ~ '^[12][0-9]{7}$'
            THEN TO_DATE(inv.fecha_ultima_entrada, 'YYYYMMDD')
        ELSE NULL
    END                                            AS fecha_ultima_entrada,

    -- Indicadores de rotación calculados en ETL
    -- dias_sin_venta: diferencia entre fecha_consulta y última venta.
    --   · Si vendió hoy        → 0
    --   · Si última venta <= fecha_consulta (histórico válido) → diferencia en días
    --   · Si última venta > fecha_consulta (backfill: inventario más nuevo que el día)
    --     → NULL para evitar valores negativos que confundan el dashboard
    CASE
        WHEN COALESCE(v.unidades_vendidas_dia, 0) > 0
            THEN 0
        WHEN inv.fecha_ultima_venta_inv IS NOT NULL
             AND inv.fecha_ultima_venta_inv ~ '^[12][0-9]{7}$'
             AND TO_DATE(inv.fecha_ultima_venta_inv, 'YYYYMMDD')
                 <= TO_DATE(%s, 'YYYYMMDD')
            THEN (TO_DATE(%s, 'YYYYMMDD')
                  - TO_DATE(inv.fecha_ultima_venta_inv, 'YYYYMMDD'))
        ELSE NULL
    END                                            AS dias_sin_venta,

    -- quiebre_inventario: vendió pero no hay existencia
    (
        COALESCE(v.unidades_vendidas_dia, 0)  > 0
        AND COALESCE(inv.inventario_cierre, 0) <= 0
    )                                              AS quiebre_inventario,

    -- item_sin_rotacion: hay stock pero no se vendió hoy
    (
        COALESCE(v.unidades_vendidas_dia, 0)  = 0
        AND COALESCE(inv.inventario_cierre, 0) > 0
    )                                              AS item_sin_rotacion,

    -- inv_foto_bloqueada: siempre FALSE al insertar.
    -- En modo daily se activa a TRUE con LOCK_INV_SQL tras el commit.
    -- En modo backfill queda FALSE para permitir re-backfill si es necesario.
    FALSE                                          AS inv_foto_bloqueada

FROM llaves k
-- Centro de operación (sede)
JOIN public.centro_operacion co
    ON co.codigo = k.sede
-- Bodega
LEFT JOIN public.bodegas b
    ON BTRIM(b.id_local) = k.bodega
-- Maestro ítem
LEFT JOIN maestro m
    ON m.item       = k.item
   AND m.id_ext_itm = k.id_ext_itm
-- Línea nivel 1 (join doble: código + tipo_inv como discriminador)
LEFT JOIN public.lineas l1
    ON BTRIM(l1.id_linea) = m.linea_nivel_1_codigo
   AND BTRIM(l1.id_tipo)  = m.tipo_inv
-- Categoría 4 (join doble: id_cricla4 + id_catego = tipo_inv)
LEFT JOIN public.criterios_itm_4 c4
    ON BTRIM(c4.id_cricla4) = m.categoria_codigo
   AND BTRIM(c4.id_catego)  = m.tipo_inv
-- Ventas del día (LEFT JOIN: NULL si no vendió)
LEFT JOIN ventas_dia v
    ON v.sede       = k.sede
   AND v.bodega     = k.bodega
   AND v.item       = k.item
   AND v.id_ext_itm = k.id_ext_itm
-- Inventario del día (LEFT JOIN: NULL si no hay inventario)
LEFT JOIN inventario_foto inv
    ON inv.sede       = k.sede
   AND inv.bodega     = k.bodega
   AND inv.item       = k.item
   AND inv.id_ext_itm = k.id_ext_itm

ORDER BY k.sede, k.bodega, k.item, k.id_ext_itm;
"""

# ── UPSERT COMPLETO (daily + backfill) ───────────────────────────────────────
# Actualiza TODOS los campos incluido el inventario.
# Se usa cuando la carga es nueva (primer INSERT del día) o en backfill.
# Al final del proceso daily se activa inv_foto_bloqueada = TRUE.

UPSERT_FULL_SQL = f"""
INSERT INTO {TARGET_TABLE} (
    empresa, fecha_dia, sede, bodega, item, id_ext_itm,
    nombre_empresa, nombre_sede, nombre_bodega,
    descripcion, unidad, tipo_inv,
    linea_nivel_3_codigo, linea_nivel_1_codigo, nombre_linea_nivel_1,
    categoria_codigo, nombre_categoria,
    venta_sin_impuesto_dia, unidades_vendidas_dia,
    inventario_cierre, valor_inventario, lapso_inventario,
    fecha_ultima_venta, fecha_ultima_entrada,
    dias_sin_venta, quiebre_inventario, item_sin_rotacion,
    inv_foto_bloqueada
) VALUES %s
ON CONFLICT (empresa, fecha_dia, sede, bodega, item, id_ext_itm)
DO UPDATE SET
    nombre_empresa         = EXCLUDED.nombre_empresa,
    nombre_sede            = EXCLUDED.nombre_sede,
    nombre_bodega          = EXCLUDED.nombre_bodega,
    descripcion            = EXCLUDED.descripcion,
    unidad                 = EXCLUDED.unidad,
    tipo_inv               = EXCLUDED.tipo_inv,
    linea_nivel_3_codigo   = EXCLUDED.linea_nivel_3_codigo,
    linea_nivel_1_codigo   = EXCLUDED.linea_nivel_1_codigo,
    nombre_linea_nivel_1   = EXCLUDED.nombre_linea_nivel_1,
    categoria_codigo       = EXCLUDED.categoria_codigo,
    nombre_categoria       = EXCLUDED.nombre_categoria,
    venta_sin_impuesto_dia = EXCLUDED.venta_sin_impuesto_dia,
    unidades_vendidas_dia  = EXCLUDED.unidades_vendidas_dia,
    inventario_cierre      = EXCLUDED.inventario_cierre,
    valor_inventario       = EXCLUDED.valor_inventario,
    lapso_inventario       = EXCLUDED.lapso_inventario,
    fecha_ultima_venta     = EXCLUDED.fecha_ultima_venta,
    fecha_ultima_entrada   = EXCLUDED.fecha_ultima_entrada,
    dias_sin_venta         = EXCLUDED.dias_sin_venta,
    quiebre_inventario     = EXCLUDED.quiebre_inventario,
    item_sin_rotacion      = EXCLUDED.item_sin_rotacion,
    inv_foto_bloqueada     = EXCLUDED.inv_foto_bloqueada,
    fecha_actualizacion    = now()
"""

# ── UPSERT PARCIAL (rolling) ──────────────────────────────────────────────────
# Actualiza SOLO ventas e indicadores derivados de venta.
# inventario_cierre, valor_inventario, lapso_inventario, fecha_ultima_entrada
# y inv_foto_bloqueada NO se tocan → preserva la foto histórica del día.

UPSERT_VENTAS_SQL = f"""
INSERT INTO {TARGET_TABLE} (
    empresa, fecha_dia, sede, bodega, item, id_ext_itm,
    nombre_empresa, nombre_sede, nombre_bodega,
    descripcion, unidad, tipo_inv,
    linea_nivel_3_codigo, linea_nivel_1_codigo, nombre_linea_nivel_1,
    categoria_codigo, nombre_categoria,
    venta_sin_impuesto_dia, unidades_vendidas_dia,
    inventario_cierre, valor_inventario, lapso_inventario,
    fecha_ultima_venta, fecha_ultima_entrada,
    dias_sin_venta, quiebre_inventario, item_sin_rotacion,
    inv_foto_bloqueada
) VALUES %s
ON CONFLICT (empresa, fecha_dia, sede, bodega, item, id_ext_itm)
DO UPDATE SET
    nombre_empresa         = EXCLUDED.nombre_empresa,
    nombre_sede            = EXCLUDED.nombre_sede,
    nombre_bodega          = EXCLUDED.nombre_bodega,
    descripcion            = EXCLUDED.descripcion,
    unidad                 = EXCLUDED.unidad,
    tipo_inv               = EXCLUDED.tipo_inv,
    linea_nivel_3_codigo   = EXCLUDED.linea_nivel_3_codigo,
    linea_nivel_1_codigo   = EXCLUDED.linea_nivel_1_codigo,
    nombre_linea_nivel_1   = EXCLUDED.nombre_linea_nivel_1,
    categoria_codigo       = EXCLUDED.categoria_codigo,
    nombre_categoria       = EXCLUDED.nombre_categoria,
    venta_sin_impuesto_dia = EXCLUDED.venta_sin_impuesto_dia,
    unidades_vendidas_dia  = EXCLUDED.unidades_vendidas_dia,
    -- ▼▼▼ INVENTARIO NO SE TOCA EN MODO ROLLING ▼▼▼
    -- inventario_cierre, valor_inventario, lapso_inventario,
    -- fecha_ultima_entrada e inv_foto_bloqueada permanecen intactos
    fecha_ultima_venta     = EXCLUDED.fecha_ultima_venta,
    dias_sin_venta         = EXCLUDED.dias_sin_venta,
    quiebre_inventario     = EXCLUDED.quiebre_inventario,
    item_sin_rotacion      = EXCLUDED.item_sin_rotacion,
    fecha_actualizacion    = now()
"""

# Bloquear foto de inventario del día tras INSERT diario exitoso
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
    """Carga config/rotacion.env si existe, sin sobreescribir vars ya definidas."""
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
             app_name: str = "etl_rotacion_v2") -> PgConn:
    return psycopg2.connect(
        host=host,
        port=int(port),
        dbname=dbname,
        user=user,
        password=password,
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
    return _connect(
        host=_env(cfg["host"]),
        port=_env(cfg["port"], "5432"),
        dbname=_env(cfg["db"]),
        user=_env(cfg["user"]),
        password=_env(cfg["pw"]),
        timeout=_env_int("PGCONNECT_TIMEOUT", 15),
    )


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
            for migration in DDL_MIGRATIONS:
                cur.execute(migration)
            for idx in DDL_INDICES:
                cur.execute(idx)
            LOGGER.info("Tabla y migraciones verificadas OK")
    tgt.commit()


# ── Extracción ────────────────────────────────────────────────────────────────

def extract_day(src: PgConn, empresa: str, day: date) -> List[tuple]:
    """
    Extrae todas las filas de (ventas ∪ inventario) para un día específico.
    Parámetros del SOURCE_SQL (en orden):
      1: fecha YYYYMMDD    → filtro WHERE fecha_dcto = ?         (ventas_dia)
      2: lapso YYYYMM      → filtro lapso_doc <= ?               (inventario_max_lapso)
      3: empresa           → %s::text AS empresa                 (SELECT)
      4: fecha YYYYMMDD    → TO_DATE AS fecha_dia                (SELECT)
      5: nombre_empresa    → %s::text AS nombre_empresa          (SELECT)
      6: fecha YYYYMMDD    → TO_DATE en CASE fecha_ultima_venta  (SELECT)
      7: fecha YYYYMMDD    → TO_DATE <= check en dias_sin_venta  (SELECT)
      8: fecha YYYYMMDD    → TO_DATE cálculo dias_sin_venta      (SELECT)
    """
    fecha_str  = fmt_date(day)
    lapso_str  = fmt_lapso(day)
    nombre_emp = COMPANY_NAMES.get(empresa, empresa)
    params = (
        fecha_str,    # %s 1 → filtro WHERE fecha_dcto = ?
        lapso_str,    # %s 2 → filtro lapso_doc <= ?
        empresa,      # %s 3 → empresa en SELECT
        fecha_str,    # %s 4 → TO_DATE fecha_dia
        nombre_emp,   # %s 5 → nombre_empresa en SELECT  ← NUEVO
        fecha_str,    # %s 6 → TO_DATE en CASE fecha_ultima_venta
        fecha_str,    # %s 7 → TO_DATE <= check dias_sin_venta  ← NUEVO
        fecha_str,    # %s 8 → TO_DATE cálculo dias_sin_venta
    )
    with src.cursor() as cur:
        cur.execute(SOURCE_SQL, params)
        rows = cur.fetchall()
    LOGGER.info(
        "empresa=%-10s  fecha=%s  lapso=%s  filas=%d",
        empresa, fecha_str, lapso_str, len(rows),
    )
    return rows


# ── Carga destino ─────────────────────────────────────────────────────────────

def load_rows(tgt: PgConn, rows: List[tuple], upsert_sql: str) -> int:
    """Inserta/actualiza filas en lotes. Retorna cantidad cargada."""
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
    """Bloquea el inventario del día para evitar que reprocesos lo sobreescriban."""
    with tgt.cursor() as cur:
        cur.execute(LOCK_INV_SQL, (empresa, day))
        locked = cur.rowcount
    tgt.commit()
    return locked


# ── Modos de carga ────────────────────────────────────────────────────────────

def process_daily(empresa: str,
                  src: PgConn,
                  tgt: PgConn,
                  dry_run: bool) -> int:
    """
    Modo daily: carga el día de ayer con UPSERT COMPLETO (ventas + inventario).
    Al terminar, bloquea el inventario para que rolling no lo sobreescriba.
    """
    yesterday = date.today() - timedelta(days=1)
    LOGGER.info("empresa=%-10s  modo=daily  fecha=%s", empresa, fmt_date(yesterday))

    if dry_run:
        LOGGER.info("[DRY-RUN] procesaría empresa=%s fecha=%s", empresa, fmt_date(yesterday))
        return 0

    rows = extract_day(src, empresa, yesterday)
    loaded = load_rows(tgt, rows, UPSERT_FULL_SQL)
    locked = lock_inventory_snapshot(tgt, empresa, yesterday)
    LOGGER.info(
        "OK  empresa=%-10s  modo=daily  fecha=%s  cargadas=%d  inv_bloqueadas=%d",
        empresa, fmt_date(yesterday), loaded, locked,
    )
    return loaded


def process_rolling(empresa: str,
                    src: PgConn,
                    tgt: PgConn,
                    rolling_days: int,
                    dry_run: bool) -> int:
    """
    Modo rolling: reprocesa los últimos N días.
    UPSERT PARCIAL → solo ventas. El inventario histórico permanece intacto.
    Útil para corregir ajustes de facturación sin corromper snapshots.
    """
    yesterday  = date.today() - timedelta(days=1)
    date_start = yesterday - timedelta(days=rolling_days - 1)

    LOGGER.info(
        "empresa=%-10s  modo=rolling  rango=%s..%s  (inventario NO se actualiza)",
        empresa, fmt_date(date_start), fmt_date(yesterday),
    )

    if dry_run:
        LOGGER.info(
            "[DRY-RUN] procesaría empresa=%s rango=%s..%s",
            empresa, fmt_date(date_start), fmt_date(yesterday),
        )
        return 0

    total = 0
    for day in date_range(date_start, yesterday):
        rows  = extract_day(src, empresa, day)
        total += load_rows(tgt, rows, UPSERT_VENTAS_SQL)

    LOGGER.info("OK  empresa=%-10s  modo=rolling  total_cargadas=%d", empresa, total)
    return total


def process_backfill(empresa: str,
                     src: PgConn,
                     tgt: PgConn,
                     date_start: date,
                     date_end: date,
                     dry_run: bool) -> int:
    """
    Modo backfill: carga histórico día a día con UPSERT COMPLETO.
    Cada día usa su propio lapso de inventario (maneja cruce de mes
    correctamente porque inventario_max_lapso usa MAX <= lapso_consulta).
    """
    LOGGER.info(
        "empresa=%-10s  modo=backfill  rango=%s..%s",
        empresa, fmt_date(date_start), fmt_date(date_end),
    )

    if dry_run:
        LOGGER.info(
            "[DRY-RUN] procesaría empresa=%s rango=%s..%s",
            empresa, fmt_date(date_start), fmt_date(date_end),
        )
        return 0

    total = 0
    for day in date_range(date_start, date_end):
        rows   = extract_day(src, empresa, day)
        loaded = load_rows(tgt, rows, UPSERT_FULL_SQL)
        # No bloqueamos en backfill: permite re-backfill si es necesario.
        total += loaded

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
        description="ETL Rotación de Ítems v2 — 3 empresas",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--mode",
        choices=("daily", "rolling", "backfill"),
        default="daily",
        help="Modo de carga: daily (default), rolling, backfill",
    )
    p.add_argument(
        "--rolling-days", type=int, default=DEFAULT_ROLLING_DAYS, metavar="N",
        help=f"Días a reprocesar en modo rolling (default: {DEFAULT_ROLLING_DAYS})",
    )
    p.add_argument(
        "--date-start", default=None, metavar="YYYYMMDD",
        help="Inicio del rango para modo backfill",
    )
    p.add_argument(
        "--date-end", default=None, metavar="YYYYMMDD",
        help="Fin del rango para modo backfill (default: ayer)",
    )
    p.add_argument(
        "--empresas",
        nargs="+",
        default=list(COMPANIES),
        choices=list(COMPANIES),
        metavar="EMPRESA",
        help="Empresas a procesar: mercamio mtodo bogota (default: todas)",
    )
    p.add_argument(
        "--recreate-table",
        action="store_true",
        help="⚠ BORRA y recrea la tabla destino — pierde datos existentes",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Muestra qué procesaría sin cargar nada",
    )
    p.add_argument(
        "--check-only",
        action="store_true",
        help="Solo verifica conexiones (origen + destino) y termina",
    )
    p.add_argument(
        "--log-dir", default=None, metavar="PATH",
        help="Directorio de logs (default: ETL_LOG_DIR env o ./logs)",
    )
    p.add_argument(
        "--log-retention-days", type=int, default=FILE_LOG_RETENTION, metavar="N",
        help=f"Días a conservar archivos de log (default: {FILE_LOG_RETENTION})",
    )
    return p.parse_args(argv)


# ── Entry point ───────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> int:
    yesterday = date.today() - timedelta(days=1)

    # Validar args de backfill
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
            LOGGER.error("Fecha inválida en backfill: %s", exc)
            return 1
        if date_start > date_end:
            LOGGER.error("date_start (%s) > date_end (%s)",
                         fmt_date(date_start), fmt_date(date_end))
            return 1

    LOGGER.info(
        "=== ETL rotacion_v2 inicio  modo=%s  empresas=%s ===",
        args.mode, ",".join(args.empresas),
    )

    # Conexión destino
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

    # Preparar tabla destino
    if not args.dry_run:
        try:
            ensure_target_table(tgt, recreate=args.recreate_table)
        except psycopg2.Error as exc:
            LOGGER.error("Error preparando tabla destino: %s", exc)
            tgt.close()
            return 1

    total_loaded = 0
    errors       = 0

    for empresa in args.empresas:
        try:
            src = get_source_conn(empresa)
        except psycopg2.Error as exc:
            LOGGER.error("empresa=%-10s  no se pudo conectar: %s", empresa, exc)
            errors += 1
            continue

        try:
            if args.mode == "daily":
                n = process_daily(empresa, src, tgt, args.dry_run)
            elif args.mode == "rolling":
                n = process_rolling(empresa, src, tgt,
                                    args.rolling_days, args.dry_run)
            else:  # backfill
                n = process_backfill(empresa, src, tgt,
                                     date_start, date_end, args.dry_run)
            total_loaded += n

        except psycopg2.Error as exc:
            LOGGER.error("empresa=%-10s  error inesperado: %s", empresa, exc)
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

    LOGGER.info(
        "=== ETL rotacion_v2 fin  total_cargadas=%d  errores=%d ===",
        total_loaded, errors,
    )
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
