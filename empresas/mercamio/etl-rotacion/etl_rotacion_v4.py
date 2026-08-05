#!/usr/bin/env python3
"""
ETL Rotación de Ítems v4 — 3 empresas (mercamio, mtodo, bogota)
================================================================
Origen:  192.168.35.217  (mercamio / mtodo / bogota)
Destino: 192.168.35.232  BD=produXdia  tabla=public.rotacion_v4

Mejoras respecto a v3
----------------------
· can_disponible_foto  → cmresumen_inventario (diario), reconstrucción por
                         movimientos en backfill histórico.
· total_costo          → SUM(costot) de movimientos RV (ind_es=2) del día.
                         Resuelve definitivamente el problema de kits con costo 0.
· ultima_venta_pdv     → MAX(fecha_fc) donde doc_inv_tipo=RV, ind_es=2
                         (cmmovimiento_inventario — más preciso que PDV).
· ultimo_ingreso       → MAX(fecha_fc) donde doc_inv_tipo=EA, ind_es=1 [NUEVO].
· costo_uni_inventario → cmresumen → último EA → items.costo_act_acum → items.ultimo_costo_ed.
· Tabla destino separada (rotacion_v4) para validación en paralelo con v3.

── Modos ───────────────────────────────────────────────────────────────────────
  daily    (timer 7am):   Carga ayer con UPSERT completo + bloquea snapshot.
  rolling  (días 1/11/21): Reprocesa últimos N días solo ventas.
  backfill (manual):      Carga rango histórico día a día.

── Ejemplos ────────────────────────────────────────────────────────────────────
  python etl_rotacion_v4.py --mode daily
  python etl_rotacion_v4.py --mode rolling --rolling-days 15
  python etl_rotacion_v4.py --mode backfill --date-start 20260101 --date-end 20260511
  python etl_rotacion_v4.py --mode daily --empresas mercamio --dry-run
  python etl_rotacion_v4.py --check-only
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
LOGGER = logging.getLogger("etl_rotacion_v4")

# ── Constantes ────────────────────────────────────────────────────────────────
TARGET_TABLE          = "public.rotacion_v4"
DEFAULT_ROLLING_DAYS  = 15
LOG_FILE_PREFIX       = "etl_rotacion_v4"
FILE_LOG_RETENTION    = 31
BATCH_SIZE            = 2_000

COMPANIES: List[str] = ["mercamio", "mtodo", "bogota"]

COMPANY_NAMES: Dict[str, str] = {
    "mercamio": "Mercamio",
    "mtodo":    "Método",
    "bogota":   "Bogotá",
}

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

    -- Categoría
    id_categoria                VARCHAR(10),
    nombre_categoria            VARCHAR(100),

    -- Línea
    id_linea_nivel_1            VARCHAR(10),
    nombre_linea_nivel_1        VARCHAR(100),

    -- Ventas del día (fuente: cmmovimiento_pdv)
    cantidad_vendida            NUMERIC(18,4)   DEFAULT 0,
    venta_sin_impuesto          NUMERIC(18,2)   DEFAULT 0,

    -- Costo ventas (fuente: cmmovimiento_inventario RV — resuelve kits)
    total_costo                 NUMERIC(18,2)   DEFAULT 0,

    -- Última venta (fuente: cmmovimiento_inventario RV MAX fecha_fc)
    ultima_venta_pdv            DATE,
    ultima_venta_inventario     DATE,           -- cmresumen.fecha_ultvta
    estado_ultima_venta_item    VARCHAR(25),

    -- Último ingreso [NUEVO] (fuente: cmmovimiento_inventario EA MAX fecha_fc)
    ultimo_ingreso              DATE,

    -- Inventario (foto del día — cmresumen_inventario)
    lapso_inventario            VARCHAR(6),
    can_disponible_foto         NUMERIC(18,4)   DEFAULT 0,
    fecha_ultima_compra         DATE,
    fecha_ultima_entrada        DATE,

    -- Costo unitario: cmresumen → último EA → items maestro
    costo_uni_inventario        NUMERIC(18,4)   DEFAULT 0,
    fecha_foto_inventario       DATE,

    -- Control snapshot
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

DDL_INDICES: List[str] = [
    f"CREATE INDEX IF NOT EXISTS idx_rot_v4_fecha_empresa   ON {TARGET_TABLE} (fecha_dia, empresa);",
    f"CREATE INDEX IF NOT EXISTS idx_rot_v4_sede_fecha      ON {TARGET_TABLE} (sede, fecha_dia);",
    f"CREATE INDEX IF NOT EXISTS idx_rot_v4_item_fecha      ON {TARGET_TABLE} (id_item, fecha_dia);",
    f"CREATE INDEX IF NOT EXISTS idx_rot_v4_linea1_fecha    ON {TARGET_TABLE} (id_linea_nivel_1, fecha_dia);",
    f"CREATE INDEX IF NOT EXISTS idx_rot_v4_categoria_fecha ON {TARGET_TABLE} (id_categoria, fecha_dia);",
    f"CREATE INDEX IF NOT EXISTS idx_rot_v4_sin_venta       ON {TARGET_TABLE} (fecha_dia, estado_ultima_venta_item);",
]

# ── UPSERT COMPLETO ───────────────────────────────────────────────────────────
UPSERT_FULL_SQL = f"""
INSERT INTO {TARGET_TABLE} (
    empresa, sede, nombre_sede, fecha_dia, bodega_local, id_item,
    nombre_item, id_unidad,
    id_categoria, nombre_categoria,
    id_linea_nivel_1, nombre_linea_nivel_1,
    cantidad_vendida, venta_sin_impuesto, total_costo,
    ultima_venta_pdv, ultima_venta_inventario, estado_ultima_venta_item,
    ultimo_ingreso,
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
    ultimo_ingreso              = EXCLUDED.ultimo_ingreso,
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
UPSERT_VENTAS_SQL = f"""
INSERT INTO {TARGET_TABLE} (
    empresa, sede, nombre_sede, fecha_dia, bodega_local, id_item,
    nombre_item, id_unidad,
    id_categoria, nombre_categoria,
    id_linea_nivel_1, nombre_linea_nivel_1,
    cantidad_vendida, venta_sin_impuesto, total_costo,
    ultima_venta_pdv, ultima_venta_inventario, estado_ultima_venta_item,
    ultimo_ingreso,
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
    ultimo_ingreso              = EXCLUDED.ultimo_ingreso,
    -- Inventario NO se toca en rolling
    fecha_actualizacion         = now()
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
    return f"{d.year}0101"

def month_end(d: date) -> date:
    """Último día del mes de d."""
    if d.month == 12:
        return date(d.year, 12, 31)
    return date(d.year, d.month + 1, 1) - timedelta(days=1)

def str_to_date(s: Optional[str]) -> Optional[date]:
    """Convierte YYYYMMDD a date. Retorna None si inválido."""
    if not s or len(s) < 8:
        return None
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except (ValueError, TypeError):
        return None

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
             app_name: str = "etl_rotacion_v4") -> PgConn:
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


# ── Extracción: inventario mensual (cmresumen_inventario) ─────────────────────

def extract_inventory_month(src: PgConn, empresa: str,
                             lapso_str: str,
                             year_start_lapso: str) -> Dict[tuple, tuple]:
    """
    Extrae snapshot de inventario para un mes completo desde cmresumen_inventario.
    Se ejecuta UNA VEZ por mes.
    Retorna dict keyed by (sede, bodega_local, id_item).
    Columnas retornadas (17):
      0  empresa
      1  sede
      2  nombre_sede
      3  bodega_local
      4  id_item
      5  nombre_item
      6  unidad_inventario
      7  id_categoria
      8  nombre_categoria
      9  id_linea_nivel_1
      10 nombre_linea_nivel_1
      11 lapso_inventario
      12 can_disponible_foto
      13 fecha_ultima_compra   (date | None)
      14 fecha_ultima_entrada  (date | None)
      15 ultima_venta_inv      (date | None)
      16 costo_uni_inventario  (con fallback a items maestro)
    """
    SQL = """
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
        MAX(COALESCE(ri.costo_uni,0))       AS costo_uni_inv
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
         THEN TO_DATE(inv.fecha_ultima_venta_inv,'YYYYMMDD') ELSE NULL END AS ultima_venta_inv,
    -- Costo: cmresumen → fallback maestro de ítems
    COALESCE(NULLIF(inv.costo_uni_inv,0), i.costo_item_maestro, 0) AS costo_uni_inventario
FROM inventario_foto inv
INNER JOIN items_cat4 i ON i.id_item = inv.id_item
LEFT JOIN public.centro_operacion co ON BTRIM(co.codigo) = inv.sede
WHERE (inv.can_disponible_foto <> 0
    OR inv.costo_uni_inv <> 0
    OR i.costo_item_maestro <> 0);
"""
    with src.cursor() as cur:
        cur.execute(SQL, (year_start_lapso, lapso_str, empresa))
        rows = cur.fetchall()
    return {(r[1], r[3], r[4]): r for r in rows}


# ── Extracción: movimientos mensuales (cmmovimiento_inventario) ───────────────

def extract_movements_month(src: PgConn,
                             lapso_str: str,
                             year_start_str: str) -> Dict[tuple, tuple]:
    """
    Extrae movimientos del año hasta fin del mes actual desde cmmovimiento_inventario.
    Se ejecuta UNA VEZ por mes.
    Retorna dict keyed by (sede, bodega_local, id_item):
      0  sede
      1  bodega_local
      2  id_item
      3  ultima_venta_mov   (str YYYYMMDD | None) — MAX RV salida en el año
      4  ultimo_ingreso     (str YYYYMMDD | None) — MAX EA entrada en el año
      5  ultimo_costo_ea    (numeric | None)      — costo_uni del último EA
    """
    # Último día del mes
    year  = int(lapso_str[:4])
    month = int(lapso_str[4:])
    mes_fin = fmt_date(month_end(date(year, month, 1)))

    SQL = """
WITH
items_filter AS MATERIALIZED (
    SELECT BTRIM(id_item) AS id_item
    FROM public.items
    WHERE BTRIM(id_tipo) = '4'
),
movs AS MATERIALIZED (
    SELECT
        BTRIM(m.id_co_mov)    AS sede,
        BTRIM(m.id_local)     AS bodega_local,
        BTRIM(m.id_item)      AS id_item,
        BTRIM(m.fecha_fc)     AS fecha_fc,
        BTRIM(m.doc_inv_tipo) AS doc_tipo,
        BTRIM(m.ind_es)       AS ind_es,
        m.costo_uni
    FROM public.cmmovimiento_inventario m
    INNER JOIN items_filter f ON f.id_item = BTRIM(m.id_item)
    WHERE BTRIM(m.fecha_fc) >= %s
      AND BTRIM(m.fecha_fc) <= %s
      AND RIGHT(BTRIM(m.id_local),2) = '01'
),
resumen AS (
    SELECT
        sede, bodega_local, id_item,
        MAX(CASE WHEN doc_tipo='RV' AND ind_es='2' THEN fecha_fc END) AS ultima_venta_mov,
        MAX(CASE WHEN doc_tipo='EA' AND ind_es='1' THEN fecha_fc END) AS ultimo_ingreso
    FROM movs
    GROUP BY sede, bodega_local, id_item
),
ultimo_ea AS (
    SELECT DISTINCT ON (sede, bodega_local, id_item)
        sede, bodega_local, id_item, costo_uni AS ultimo_costo_ea
    FROM movs
    WHERE doc_tipo = 'EA' AND ind_es = '1'
    ORDER BY sede, bodega_local, id_item, fecha_fc DESC
)
SELECT
    r.sede,
    r.bodega_local,
    r.id_item,
    r.ultima_venta_mov,
    r.ultimo_ingreso,
    e.ultimo_costo_ea
FROM resumen r
LEFT JOIN ultimo_ea e USING (sede, bodega_local, id_item);
"""
    with src.cursor() as cur:
        cur.execute(SQL, (year_start_str, mes_fin))
        rows = cur.fetchall()
    return {(r[0], r[1], r[2]): r for r in rows}


# ── Extracción: ventas del día (cmmovimiento_pdv) ────────────────────────────

def extract_sales_day(src: PgConn, day: date) -> Dict[tuple, tuple]:
    """
    Ventas del día desde cmmovimiento_pdv.
    Retorna dict keyed by (sede, bodega_local, id_item).
    Columnas: sede, bodega_local, id_item, id_unidad,
              cantidad_vendida, venta_sin_impuesto
    """
    SQL = """
SELECT
    BTRIM(mp.id_co)                         AS sede,
    BTRIM(mp.id_local)                      AS bodega_local,
    BTRIM(mp.id_item)                       AS id_item,
    BTRIM(COALESCE(mp.id_unidad,''))        AS id_unidad,
    SUM(COALESCE(mp.cantidad,  0))          AS cantidad_vendida,
    SUM(COALESCE(mp.vlrtot_bru,0))          AS venta_sin_impuesto
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
        cur.execute(SQL, (fmt_date(day),))
        rows = cur.fetchall()
    return {(r[0], r[1], r[2]): r for r in rows}


# ── Extracción: costo ventas del día (cmmovimiento_inventario RV) ─────────────

def extract_cost_day(src: PgConn, day: date) -> Dict[tuple, tuple]:
    """
    Costo de ventas del día desde cmmovimiento_inventario (doc_tipo=RV, ind_es=2).
    Resuelve el problema de kits con tot_costo=0 en cmmovimiento_pdv.
    Retorna dict keyed by (sede, bodega_local, id_item).
    Columnas: sede, bodega_local, id_item, total_costo_rv
    """
    SQL = """
SELECT
    BTRIM(m.id_co_mov)  AS sede,
    BTRIM(m.id_local)   AS bodega_local,
    BTRIM(m.id_item)    AS id_item,
    SUM(m.costot)       AS total_costo_rv
FROM public.cmmovimiento_inventario m
INNER JOIN public.items i
    ON BTRIM(i.id_item) = BTRIM(m.id_item)
   AND BTRIM(i.id_tipo) = '4'
WHERE BTRIM(m.fecha_fc)     = %s
  AND BTRIM(m.doc_inv_tipo) = 'RV'
  AND BTRIM(m.ind_es)       = '2'
  AND RIGHT(BTRIM(m.id_local),2) = '01'
GROUP BY BTRIM(m.id_co_mov), BTRIM(m.id_local), BTRIM(m.id_item);
"""
    with src.cursor() as cur:
        cur.execute(SQL, (fmt_date(day),))
        rows = cur.fetchall()
    return {(r[0], r[1], r[2]): r for r in rows}


# ── Merge diario ──────────────────────────────────────────────────────────────

def merge_day(inventory:  Dict[tuple, tuple],
              movements:  Dict[tuple, tuple],
              sales:      Dict[tuple, tuple],
              costs:      Dict[tuple, tuple],
              empresa:    str,
              day:        date) -> List[tuple]:
    """
    Combina los 4 caches/queries en una lista de tuplas lista para UPSERT.
    Orden de columnas = orden del INSERT en UPSERT_FULL_SQL.
    """
    rows = []
    for key, inv in inventory.items():
        # Desempaquetar inventario (17 cols)
        (emp, sede, nombre_sede, bodega_local, id_item,
         nombre_item, unidad_inv, id_cat, nombre_cat,
         id_linea, nombre_linea, lapso_inv, can_disp,
         f_compra, f_entrada, ult_vta_inv, costo_uni_inv) = inv

        # Movimientos del mes
        mov = movements.get(key)
        ultima_venta_mov_str  = mov[3] if mov else None
        ultimo_ingreso_str    = mov[4] if mov else None
        ultimo_costo_ea       = float(mov[5] or 0) if mov and mov[5] else 0.0

        # Ventas del día
        sale = sales.get(key)
        if sale:
            id_unidad        = sale[3] or unidad_inv
            cantidad_vendida = sale[4]
            venta_sin_imp    = sale[5]
        else:
            id_unidad        = unidad_inv
            cantidad_vendida = 0
            venta_sin_imp    = 0

        # Costo de ventas del día (RV cmmovimiento_inventario)
        cost = costs.get(key)
        total_costo = float(cost[3] or 0) if cost else 0.0
        # Fallback si RV no tiene costo (p.ej. movimiento sin costot)
        if total_costo == 0 and float(cantidad_vendida or 0) > 0:
            costo_uni_calc = float(costo_uni_inv or 0) or ultimo_costo_ea
            total_costo = float(cantidad_vendida) * costo_uni_calc

        # Costo unitario final: cmresumen → último EA → ya aplicado en inv cache
        costo_uni_final = float(costo_uni_inv or 0)
        if costo_uni_final == 0 and ultimo_costo_ea > 0:
            costo_uni_final = ultimo_costo_ea

        # Conversión fechas de movimientos
        ultima_venta_pdv = str_to_date(ultima_venta_mov_str)
        ultimo_ingreso   = str_to_date(ultimo_ingreso_str)
        estado = 'CON VENTA EN EL AÑO' if ultima_venta_pdv else 'SIN VENTA EN EL AÑO'

        # Filtro: excluir solo si no hay ventas, no hay inventario Y costo_uni = 0
        # Ítems con costo > 0 pero can_disponible = 0 SÍ se escriben para reflejar
        # que el stock bajó a cero (ej. salida por RI después del backfill).
        if (cantidad_vendida == 0 and venta_sin_imp == 0
                and float(can_disp or 0) == 0 and costo_uni_final == 0):
            continue

        rows.append((
            emp, sede, nombre_sede, day, bodega_local, id_item,
            nombre_item, id_unidad, id_cat, nombre_cat,
            id_linea, nombre_linea,
            cantidad_vendida, venta_sin_imp, total_costo,
            ultima_venta_pdv, ult_vta_inv, estado,
            ultimo_ingreso,          # NUEVO
            lapso_inv, can_disp,
            f_compra, f_entrada,
            costo_uni_final, day,   # fecha_foto_inventario
            False,                  # inv_foto_bloqueada
        ))
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

def process_backfill(empresa: str, src: PgConn, tgt: PgConn,
                     date_start: date, date_end: date,
                     dry_run: bool) -> int:
    """
    Backfill optimizado v4:
    - Inventario cargado UNA VEZ por mes (cmresumen_inventario)
    - Movimientos cargados UNA VEZ por mes (cmmovimiento_inventario)
    - Ventas y costos consultados por día (queries livianas)
    """
    LOGGER.info(
        "empresa=%-10s  modo=backfill  rango=%s..%s",
        empresa, fmt_date(date_start), fmt_date(date_end),
    )
    if dry_run:
        LOGGER.info("[DRY-RUN] empresa=%s  rango=%s..%s",
                    empresa, fmt_date(date_start), fmt_date(date_end))
        return 0

    total           = 0
    current_lapso   = None
    inventory_cache: Dict[tuple, tuple] = {}
    movements_cache: Dict[tuple, tuple] = {}

    for day in date_range(date_start, date_end):
        lapso            = fmt_lapso(day)
        year_start_lapso = f"{day.year}01"
        year_start_str   = fmt_year_start(day)

        # Recargar caches solo cuando cambia el mes
        if lapso != current_lapso:
            LOGGER.info("empresa=%-10s  cargando inventario  lapso=%s …", empresa, lapso)
            inventory_cache = extract_inventory_month(
                src, empresa, lapso, year_start_lapso
            )
            LOGGER.info(
                "empresa=%-10s  inventario lapso=%s  ítems=%d",
                empresa, lapso, len(inventory_cache),
            )

            LOGGER.info("empresa=%-10s  cargando movimientos lapso=%s …", empresa, lapso)
            movements_cache = extract_movements_month(
                src, lapso, year_start_str
            )
            LOGGER.info(
                "empresa=%-10s  movimientos lapso=%s  ítems=%d",
                empresa, lapso, len(movements_cache),
            )
            current_lapso = lapso

        # Ventas y costos del día (queries livianas)
        sales = extract_sales_day(src, day)
        costs = extract_cost_day(src, day)

        # Merge en Python
        rows   = merge_day(inventory_cache, movements_cache,
                           sales, costs, empresa, day)
        loaded = load_rows(tgt, rows, UPSERT_FULL_SQL)
        total += loaded
        LOGGER.info(
            "empresa=%-10s  fecha=%s  ventas=%d  costos_rv=%d  cargadas=%d",
            empresa, fmt_date(day), len(sales), len(costs), loaded,
        )

    LOGGER.info("OK  empresa=%-10s  modo=backfill  total_cargadas=%d", empresa, total)
    return total


def process_daily(empresa: str, src: PgConn, tgt: PgConn,
                  dry_run: bool) -> int:
    yesterday = date.today() - timedelta(days=1)
    LOGGER.info("empresa=%-10s  modo=daily  fecha=%s", empresa, fmt_date(yesterday))
    if dry_run:
        LOGGER.info("[DRY-RUN] empresa=%s  fecha=%s", empresa, fmt_date(yesterday))
        return 0

    lapso            = fmt_lapso(yesterday)
    year_start_lapso = f"{yesterday.year}01"
    year_start_str   = fmt_year_start(yesterday)

    inventory = extract_inventory_month(src, empresa, lapso, year_start_lapso)
    movements = extract_movements_month(src, lapso, year_start_str)
    sales     = extract_sales_day(src, yesterday)
    costs     = extract_cost_day(src, yesterday)

    rows   = merge_day(inventory, movements, sales, costs, empresa, yesterday)
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

    total         = 0
    current_lapso = None
    inventory_cache: Dict[tuple, tuple] = {}
    movements_cache: Dict[tuple, tuple] = {}

    for day in date_range(date_start, yesterday):
        lapso          = fmt_lapso(day)
        year_start_str = fmt_year_start(day)
        year_start_lp  = f"{day.year}01"

        if lapso != current_lapso:
            inventory_cache = extract_inventory_month(src, empresa, lapso, year_start_lp)
            movements_cache = extract_movements_month(src, lapso, year_start_str)
            current_lapso = lapso

        sales = extract_sales_day(src, day)
        costs = extract_cost_day(src, day)
        rows  = merge_day(inventory_cache, movements_cache,
                          sales, costs, empresa, day)
        total += load_rows(tgt, rows, UPSERT_VENTAS_SQL)

    LOGGER.info("OK  empresa=%-10s  modo=rolling  total_cargadas=%d", empresa, total)
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
        description="ETL Rotación de Ítems v4",
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

    LOGGER.info("=== ETL rotacion_v4 inicio  modo=%s  empresas=%s ===",
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

    LOGGER.info("=== ETL rotacion_v4 fin  total_cargadas=%d  errores=%d ===",
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
