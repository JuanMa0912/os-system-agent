#!/usr/bin/env python3
"""
ETL rotacion incremental — 3 empresas (mercamio, mtodo, bogota).

Extrae ventas diarias (cmmovimiento_pdv) e inventario del mes actual
(cmresumen_inventario) desde 192.168.35.217 y carga en
rotacion_base_item_dia_sede en 192.168.35.232 (BD produXdia).

── Modos de operacion ──────────────────────────────────────────────────────────

  daily (default / timer 7am):
    Carga el dia de ayer para las 3 empresas.
    UPSERT completo: actualiza ventas + inventario (foto del momento).

  rolling (timer 1am en dias 1, 11, 21):
    Reprocesa los ultimos N dias (default: 15).
    UPSERT parcial: actualiza solo ventas. NO toca inventario.

  backfill (uso manual):
    Carga un rango de fechas --date-start / --date-end.
    UPSERT completo: actualiza ventas + inventario.
    Agrupa por mes para usar el lapso correcto de inventario en cada mes.

── Ejemplos ────────────────────────────────────────────────────────────────────

  # Carga del dia (timer 7am)
  python etl_rotacion_incremental_3bd.py --mode daily

  # Reproceso ultimos 15 dias (timer 1am)
  python etl_rotacion_incremental_3bd.py --mode rolling --rolling-days 15

  # Backfill de un rango historico
  python etl_rotacion_incremental_3bd.py --mode backfill --date-start 20260101 --date-end 20260331

  # Recrear tabla desde cero (BORRA datos existentes)
  python etl_rotacion_incremental_3bd.py --mode backfill --date-start 20260101 --date-end 20260423 --recreate-table

  # Ver que haria sin cargar
  python etl_rotacion_incremental_3bd.py --mode daily --dry-run

  # Verificar conexiones
  python etl_rotacion_incremental_3bd.py --check-only
"""

from __future__ import annotations

import argparse
import calendar
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import psycopg2
import psycopg2.extras

# ── Logger ────────────────────────────────────────────────────────────────────
LOGGER = logging.getLogger("etl_rotacion")

# ── Constantes ────────────────────────────────────────────────────────────────
TARGET_TABLE          = "public.rotacion_base_item_dia_sede"
DEFAULT_ROLLING_DAYS  = 15
LOG_FILE_PREFIX       = "etl_rotacion_incremental_3bd"
FILE_LOG_RETENTION    = 31
BATCH_SIZE            = 2_000

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
# Nota: para instalar sobre tabla antigua con PK distinto usar --recreate-table

DDL_CREATE = f"""
CREATE TABLE {TARGET_TABLE} (
    empresa                varchar(20)    NOT NULL,
    fecha_dia              date           NOT NULL,
    sede                   varchar(20)    NOT NULL,
    bodega                 varchar(5)     NOT NULL DEFAULT '',
    item                   varchar(30)    NOT NULL,
    id_ext_itm             varchar(3)     NOT NULL DEFAULT '',
    nombre_sede            varchar(120),
    nombre_bodega          varchar(40),
    linea                  varchar(20),
    descripcion            varchar(255),
    linea_nivel_1_codigo   varchar(20),
    venta_sin_impuesto_dia numeric(18,2)  DEFAULT 0,
    unidades_vendidas_dia  numeric(18,4)  DEFAULT 0,
    inventario_cierre      numeric(18,4)  DEFAULT 0,
    valor_inventario       numeric(18,2)  DEFAULT 0,
    unidad                 varchar(20),
    fecha_ultima_venta     date,
    fecha_ultima_entrada   date,
    categoria              varchar(1),
    nombre_categoria       varchar(40),
    linea01                varchar(6),
    nombre_linea01         varchar(40),
    fecha_carga            timestamp      DEFAULT now(),
    fecha_actualizacion    timestamp      DEFAULT now(),
    PRIMARY KEY (empresa, fecha_dia, sede, bodega, item, id_ext_itm)
);
"""

DDL_CREATE_IF_NOT_EXISTS = f"""
CREATE TABLE IF NOT EXISTS {TARGET_TABLE} (
    empresa                varchar(20)    NOT NULL,
    fecha_dia              date           NOT NULL,
    sede                   varchar(20)    NOT NULL,
    bodega                 varchar(5)     NOT NULL DEFAULT '',
    item                   varchar(30)    NOT NULL,
    id_ext_itm             varchar(3)     NOT NULL DEFAULT '',
    nombre_sede            varchar(120),
    nombre_bodega          varchar(40),
    linea                  varchar(20),
    descripcion            varchar(255),
    linea_nivel_1_codigo   varchar(20),
    venta_sin_impuesto_dia numeric(18,2)  DEFAULT 0,
    unidades_vendidas_dia  numeric(18,4)  DEFAULT 0,
    inventario_cierre      numeric(18,4)  DEFAULT 0,
    valor_inventario       numeric(18,2)  DEFAULT 0,
    unidad                 varchar(20),
    fecha_ultima_venta     date,
    fecha_ultima_entrada   date,
    categoria              varchar(1),
    nombre_categoria       varchar(40),
    linea01                varchar(6),
    nombre_linea01         varchar(40),
    fecha_carga            timestamp      DEFAULT now(),
    fecha_actualizacion    timestamp      DEFAULT now(),
    PRIMARY KEY (empresa, fecha_dia, sede, bodega, item, id_ext_itm)
);
"""

DDL_DROP = f"DROP TABLE IF EXISTS {TARGET_TABLE};"

# ── SQL origen ────────────────────────────────────────────────────────────────
# Parametros: empresa, fecha_inicio (YYYYMMDD), fecha_fin (YYYYMMDD),
#             lapso_inicio (YYYYMM), lapso_fin (YYYYMM)
#
# Para modo daily:   fecha_inicio = fecha_fin = ayer, lapso = mes de ayer
# Para modo rolling: fecha_inicio = hoy-N, fecha_fin = ayer, lapso = mes actual
# Para modo backfill: se llama una vez por mes, lapso = ese mes

SOURCE_SQL = """
WITH ventas_dia AS (
    SELECT
        %s::text                                    AS empresa,
        BTRIM(mp.id_co)                             AS sede,
        BTRIM(mp.id_local)                          AS bodega,
        BTRIM(mp.id_item)                           AS item,
        COALESCE(BTRIM(mp.id_itmext), '')           AS id_ext_itm,
        BTRIM(mp.fecha_dcto)                        AS fecha_dcto,
        SUM(COALESCE(mp.ven_netas, 0))              AS venta_sin_impuesto_dia,
        SUM(COALESCE(mp.cantidad, 0))               AS unidades_vendidas_dia
    FROM public.cmmovimiento_pdv mp
    WHERE BTRIM(mp.fecha_dcto) BETWEEN %s AND %s
      AND RIGHT(BTRIM(mp.id_local), 2) = '01'
    GROUP BY
        BTRIM(mp.id_co),
        BTRIM(mp.id_local),
        BTRIM(mp.id_item),
        COALESCE(BTRIM(mp.id_itmext), ''),
        BTRIM(mp.fecha_dcto)
),
inventario_actual AS (
    SELECT
        BTRIM(ri.id_co)                             AS sede,
        BTRIM(ri.id_local)                          AS bodega,
        BTRIM(ri.id_item)                           AS item,
        COALESCE(BTRIM(ri.id_ext_itm), '')          AS id_ext_itm,
        SUM(COALESCE(ri.can_exis_fin, 0))           AS inventario_cierre,
        SUM(COALESCE(ri.vlr_cost_fin, 0))           AS valor_inventario,
        MAX(NULLIF(BTRIM(ri.fecha_ultvta), ''))     AS fecha_ultima_venta,
        MAX(NULLIF(BTRIM(ri.fecha_ultent), ''))     AS fecha_ultima_entrada
    FROM public.cmresumen_inventario ri
    WHERE BTRIM(ri.lapso_doc) BETWEEN %s AND %s
      AND RIGHT(BTRIM(ri.id_local), 2) = '01'
    GROUP BY
        BTRIM(ri.id_co),
        BTRIM(ri.id_local),
        BTRIM(ri.id_item),
        COALESCE(BTRIM(ri.id_ext_itm), '')
)
SELECT
    v.empresa,
    TO_DATE(v.fecha_dcto, 'YYYYMMDD')                          AS fecha_dia,
    v.sede,
    v.bodega,
    v.item,
    v.id_ext_itm,
    BTRIM(COALESCE(co.descripcion, ''))                        AS nombre_sede,
    BTRIM(COALESCE(b.cmlocal_descripcion, ''))                 AS nombre_bodega,
    BTRIM(COALESCE(i.id_linea, ''))                            AS linea,
    BTRIM(COALESCE(i.descripcion, ''))                         AS descripcion,
    BTRIM(COALESCE(i.id_linea1, ''))                           AS linea_nivel_1_codigo,
    v.venta_sin_impuesto_dia,
    v.unidades_vendidas_dia,
    COALESCE(inv.inventario_cierre, 0)                         AS inventario_cierre,
    COALESCE(inv.valor_inventario, 0)                          AS valor_inventario,
    BTRIM(COALESCE(NULLIF(i.unimed_inv_1, ''), i.unimed_com, '')) AS unidad,
    CASE WHEN inv.fecha_ultima_venta IS NOT NULL
         THEN TO_DATE(inv.fecha_ultima_venta, 'YYYYMMDD')
         ELSE NULL
    END                                                        AS fecha_ultima_venta,
    CASE WHEN inv.fecha_ultima_entrada IS NOT NULL
         THEN TO_DATE(inv.fecha_ultima_entrada, 'YYYYMMDD')
         ELSE NULL
    END                                                        AS fecha_ultima_entrada,
    BTRIM(COALESCE(i.id_tipo, ''))                             AS categoria,
    BTRIM(COALESCE(cat.cmtipinv_descripcion, ''))              AS nombre_categoria,
    BTRIM(COALESCE(i.id_linea1, ''))                           AS linea01,
    BTRIM(COALESCE(l1.cmlineas_descripcion, ''))               AS nombre_linea01
FROM ventas_dia v
LEFT JOIN public.items i
       ON v.item       = BTRIM(i.id_item)
      AND v.id_ext_itm = COALESCE(BTRIM(i.id_ext_itm), '')
LEFT JOIN inventario_actual inv
       ON v.sede       = inv.sede
      AND v.bodega     = inv.bodega
      AND v.item       = inv.item
      AND v.id_ext_itm = inv.id_ext_itm
LEFT JOIN public.centro_operacion co
       ON v.sede = BTRIM(co.codigo)
LEFT JOIN public.bodegas b
       ON v.bodega = BTRIM(b.id_local)
LEFT JOIN public.categorias cat
       ON BTRIM(i.id_tipo) = BTRIM(cat.id_tipo)
LEFT JOIN public.lineas l1
       ON BTRIM(i.id_linea1) = BTRIM(l1.id_linea)
      AND BTRIM(i.id_tipo)   = BTRIM(l1.id_tipo)
ORDER BY v.fecha_dcto, v.sede, v.item
"""

# ── UPSERT completo (daily + backfill): actualiza ventas + inventario ─────────
UPSERT_FULL_SQL = f"""
INSERT INTO {TARGET_TABLE} (
    empresa, fecha_dia, sede, bodega, item, id_ext_itm,
    nombre_sede, nombre_bodega, linea, descripcion, linea_nivel_1_codigo,
    venta_sin_impuesto_dia, unidades_vendidas_dia,
    inventario_cierre, valor_inventario, unidad,
    fecha_ultima_venta, fecha_ultima_entrada,
    categoria, nombre_categoria, linea01, nombre_linea01
) VALUES %s
ON CONFLICT (empresa, fecha_dia, sede, bodega, item, id_ext_itm) DO UPDATE SET
    nombre_sede            = EXCLUDED.nombre_sede,
    nombre_bodega          = EXCLUDED.nombre_bodega,
    linea                  = EXCLUDED.linea,
    descripcion            = EXCLUDED.descripcion,
    linea_nivel_1_codigo   = EXCLUDED.linea_nivel_1_codigo,
    venta_sin_impuesto_dia = EXCLUDED.venta_sin_impuesto_dia,
    unidades_vendidas_dia  = EXCLUDED.unidades_vendidas_dia,
    inventario_cierre      = EXCLUDED.inventario_cierre,
    valor_inventario       = EXCLUDED.valor_inventario,
    unidad                 = EXCLUDED.unidad,
    fecha_ultima_venta     = EXCLUDED.fecha_ultima_venta,
    fecha_ultima_entrada   = EXCLUDED.fecha_ultima_entrada,
    categoria              = EXCLUDED.categoria,
    nombre_categoria       = EXCLUDED.nombre_categoria,
    linea01                = EXCLUDED.linea01,
    nombre_linea01         = EXCLUDED.nombre_linea01,
    fecha_actualizacion    = now()
"""

# ── UPSERT parcial (rolling): actualiza solo ventas, preserva inventario ──────
UPSERT_VENTAS_SQL = f"""
INSERT INTO {TARGET_TABLE} (
    empresa, fecha_dia, sede, bodega, item, id_ext_itm,
    nombre_sede, nombre_bodega, linea, descripcion, linea_nivel_1_codigo,
    venta_sin_impuesto_dia, unidades_vendidas_dia,
    inventario_cierre, valor_inventario, unidad,
    fecha_ultima_venta, fecha_ultima_entrada,
    categoria, nombre_categoria, linea01, nombre_linea01
) VALUES %s
ON CONFLICT (empresa, fecha_dia, sede, bodega, item, id_ext_itm) DO UPDATE SET
    nombre_sede            = EXCLUDED.nombre_sede,
    nombre_bodega          = EXCLUDED.nombre_bodega,
    linea                  = EXCLUDED.linea,
    descripcion            = EXCLUDED.descripcion,
    linea_nivel_1_codigo   = EXCLUDED.linea_nivel_1_codigo,
    venta_sin_impuesto_dia = EXCLUDED.venta_sin_impuesto_dia,
    unidades_vendidas_dia  = EXCLUDED.unidades_vendidas_dia,
    -- inventario_cierre NO se toca en modo rolling
    -- valor_inventario  NO se toca en modo rolling
    unidad                 = EXCLUDED.unidad,
    fecha_ultima_venta     = EXCLUDED.fecha_ultima_venta,
    fecha_ultima_entrada   = EXCLUDED.fecha_ultima_entrada,
    categoria              = EXCLUDED.categoria,
    nombre_categoria       = EXCLUDED.nombre_categoria,
    linea01                = EXCLUDED.linea01,
    nombre_linea01         = EXCLUDED.nombre_linea01,
    fecha_actualizacion    = now()
"""

# ── Helpers ───────────────────────────────────────────────────────────────────

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def fmt_date(d: date) -> str:
    return d.strftime("%Y%m%d")


def fmt_lapso(d: date) -> str:
    return d.strftime("%Y%m")


def month_bounds(d: date) -> Tuple[date, date]:
    """Primer y ultimo dia del mes de d."""
    last = calendar.monthrange(d.year, d.month)[1]
    return date(d.year, d.month, 1), date(d.year, d.month, last)


def months_in_range(start: date, end: date) -> List[Tuple[str, date, date]]:
    """
    Devuelve lista de (lapso, fecha_inicio, fecha_fin) por mes
    dentro del rango [start, end], capped a start/end.
    """
    result = []
    cur = date(start.year, start.month, 1)
    while cur <= end:
        lapso = fmt_lapso(cur)
        _, last_day = month_bounds(cur)
        ms = max(cur, start)
        me = min(last_day, end)
        result.append((lapso, ms, me))
        # avanzar al siguiente mes
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)
    return result

# ── Conexiones ────────────────────────────────────────────────────────────────

def _connect(host: str, port: str, db: str, user: str, pw: str,
             timeout: int = 15) -> psycopg2.extensions.connection:
    return psycopg2.connect(
        host=host, port=int(port), dbname=db,
        user=user, password=pw,
        connect_timeout=timeout,
        options="-c client_encoding=UTF8",
    )


def get_target_conn() -> psycopg2.extensions.connection:
    return _connect(
        _env("TARGET_PGHOST"), _env("TARGET_PGPORT", "5432"),
        _env("TARGET_PGDATABASE"), _env("TARGET_PGUSER"), _env("TARGET_PGPASSWORD"),
        int(_env("PGCONNECT_TIMEOUT", "15")),
    )


def get_source_conn(empresa: str) -> psycopg2.extensions.connection:
    cfg = COMPANY_ENV[empresa]
    return _connect(
        _env(cfg["host"]), _env(cfg["port"], "5432"),
        _env(cfg["db"]), _env(cfg["user"]), _env(cfg["pw"]),
        int(_env("PGCONNECT_TIMEOUT", "15")),
    )

# ── Tabla destino ─────────────────────────────────────────────────────────────

def ensure_target_table(conn: psycopg2.extensions.connection,
                        recreate: bool = False) -> None:
    with conn.cursor() as cur:
        if recreate:
            LOGGER.warning("--recreate-table: borrando tabla existente...")
            cur.execute(DDL_DROP)
            cur.execute(DDL_CREATE)
            LOGGER.info("Tabla recreada OK")
        else:
            cur.execute(DDL_CREATE_IF_NOT_EXISTS)
            LOGGER.info("Tabla verificada/creada OK")
    conn.commit()

# ── Extraccion ────────────────────────────────────────────────────────────────

def fetch_rows(src_conn: psycopg2.extensions.connection,
               empresa: str,
               fecha_start: date,
               fecha_end: date,
               lapso_start: str,
               lapso_end: str) -> List[tuple]:
    """
    Extrae filas de ventas+inventario para el rango de fechas indicado.
    lapso_start/lapso_end: rango YYYYMM para filtrar cmresumen_inventario.
    """
    params = (
        empresa,
        fmt_date(fecha_start), fmt_date(fecha_end),  # ventas BETWEEN
        lapso_start, lapso_end,                        # inventario lapso BETWEEN
    )
    with src_conn.cursor() as cur:
        cur.execute(SOURCE_SQL, params)
        rows = cur.fetchall()
    LOGGER.info(
        "empresa=%s origen=%s..%s lapso=%s..%s filas=%d",
        empresa, fmt_date(fecha_start), fmt_date(fecha_end),
        lapso_start, lapso_end, len(rows),
    )
    return rows

# ── Carga destino ─────────────────────────────────────────────────────────────

def upsert_rows(tgt_conn: psycopg2.extensions.connection,
                rows: List[tuple],
                upsert_sql: str) -> int:
    """Carga filas en lotes. Retorna cantidad cargada."""
    if not rows:
        return 0
    total = 0
    with tgt_conn.cursor() as cur:
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i: i + BATCH_SIZE]
            psycopg2.extras.execute_values(cur, upsert_sql, batch,
                                           page_size=BATCH_SIZE)
            total += len(batch)
    tgt_conn.commit()
    return total

# ── Procesamiento por empresa ─────────────────────────────────────────────────

def process_daily(empresa: str,
                  src_conn: psycopg2.extensions.connection,
                  tgt_conn: psycopg2.extensions.connection,
                  dry_run: bool) -> int:
    """
    Modo daily: carga el dia de ayer con inventario actual (foto del momento).
    UPSERT completo: actualiza ventas + inventario.
    """
    yesterday = date.today() - timedelta(days=1)
    lapso = fmt_lapso(yesterday)

    LOGGER.info("empresa=%s modo=daily fecha=%s lapso=%s",
                empresa, fmt_date(yesterday), lapso)

    if dry_run:
        LOGGER.info("[DRY-RUN] empresa=%s procesaria fecha=%s",
                    empresa, fmt_date(yesterday))
        return 0

    rows = fetch_rows(src_conn, empresa, yesterday, yesterday, lapso, lapso)
    loaded = upsert_rows(tgt_conn, rows, UPSERT_FULL_SQL)
    LOGGER.info("OK empresa=%s modo=daily cargadas=%d", empresa, loaded)
    return loaded


def process_rolling(empresa: str,
                    src_conn: psycopg2.extensions.connection,
                    tgt_conn: psycopg2.extensions.connection,
                    rolling_days: int,
                    dry_run: bool) -> int:
    """
    Modo rolling: reprocesa ultimos N dias.
    UPSERT parcial: actualiza solo ventas, NO toca inventario existente.
    """
    yesterday  = date.today() - timedelta(days=1)
    date_start = date.today() - timedelta(days=rolling_days)
    lapso = fmt_lapso(yesterday)  # lapso actual (inventario no se usa de todas formas)

    LOGGER.info("empresa=%s modo=rolling rango=%s..%s (inventario NO se actualiza)",
                empresa, fmt_date(date_start), fmt_date(yesterday))

    if dry_run:
        LOGGER.info("[DRY-RUN] empresa=%s procesaria %s..%s",
                    empresa, fmt_date(date_start), fmt_date(yesterday))
        return 0

    rows = fetch_rows(src_conn, empresa, date_start, yesterday, lapso, lapso)
    loaded = upsert_rows(tgt_conn, rows, UPSERT_VENTAS_SQL)
    LOGGER.info("OK empresa=%s modo=rolling cargadas=%d", empresa, loaded)
    return loaded


def process_backfill(empresa: str,
                     src_conn: psycopg2.extensions.connection,
                     tgt_conn: psycopg2.extensions.connection,
                     date_start: date,
                     date_end: date,
                     dry_run: bool) -> int:
    """
    Modo backfill: carga rango completo agrupado por mes.
    Cada mes usa su propio lapso de inventario.
    UPSERT completo: actualiza ventas + inventario.
    """
    months = months_in_range(date_start, date_end)
    LOGGER.info("empresa=%s modo=backfill rango=%s..%s meses=%d",
                empresa, fmt_date(date_start), fmt_date(date_end), len(months))

    total = 0
    for lapso, ms, me in months:
        LOGGER.info("empresa=%s lapso=%s ventas=%s..%s",
                    empresa, lapso, fmt_date(ms), fmt_date(me))

        if dry_run:
            LOGGER.info("[DRY-RUN] empresa=%s lapso=%s procesaria %s..%s",
                        empresa, lapso, fmt_date(ms), fmt_date(me))
            continue

        rows = fetch_rows(src_conn, empresa, ms, me, lapso, lapso)
        loaded = upsert_rows(tgt_conn, rows, UPSERT_FULL_SQL)
        LOGGER.info("OK empresa=%s lapso=%s cargadas=%d", empresa, lapso, loaded)
        total += loaded

    return total

# ── Logging ───────────────────────────────────────────────────────────────────

def resolve_log_dir(log_dir_arg: Optional[str]) -> Path:
    d = log_dir_arg or _env("ETL_LOG_DIR") or "logs"
    p = Path(d)
    p.mkdir(parents=True, exist_ok=True)
    return p


def configure_logging(args: argparse.Namespace) -> None:
    today = date.today()
    log_dir = resolve_log_dir(args.log_dir)
    log_file = log_dir / f"{LOG_FILE_PREFIX}_{today:%Y%m%d}.log"
    fmt = "%(asctime)s | %(levelname)s | %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
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

# ── Env file ──────────────────────────────────────────────────────────────────

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

# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="ETL rotacion incremental — 3 empresas",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--mode", choices=("daily", "rolling", "backfill"), default="daily",
        help="Modo de carga (default: daily)",
    )
    p.add_argument(
        "--rolling-days", type=int, default=DEFAULT_ROLLING_DAYS, metavar="N",
        help="Dias a reprocesar en modo rolling (default: %(default)s)",
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
        "--empresas", nargs="+", default=list(COMPANY_ENV.keys()),
        choices=list(COMPANY_ENV.keys()), metavar="EMPRESA",
        help="Empresas a procesar (default: todas)",
    )
    p.add_argument(
        "--recreate-table", action="store_true",
        help="BORRA y recrea la tabla destino (pierde datos existentes)",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Muestra que se procesaria sin cargar nada",
    )
    p.add_argument(
        "--check-only", action="store_true",
        help="Solo verifica conexiones y termina",
    )
    p.add_argument(
        "--log-dir", default=None, metavar="PATH",
        help="Directorio de logs (default: ETL_LOG_DIR env o ./logs)",
    )
    p.add_argument(
        "--log-retention-days", type=int, default=FILE_LOG_RETENTION, metavar="N",
        help="Dias a conservar logs de archivo (default: %(default)s)",
    )
    return p.parse_args(argv)

# ── Run ───────────────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> int:
    yesterday = date.today() - timedelta(days=1)

    # Validar argumentos de backfill
    if args.mode == "backfill":
        if not args.date_start:
            LOGGER.error("--mode backfill requiere --date-start")
            return 1
        date_start = date(int(args.date_start[:4]),
                          int(args.date_start[4:6]),
                          int(args.date_start[6:8]))
        date_end = (date(int(args.date_end[:4]),
                         int(args.date_end[4:6]),
                         int(args.date_end[6:8]))
                    if args.date_end else yesterday)
        if date_start > date_end:
            LOGGER.error("date_start (%s) > date_end (%s)",
                         args.date_start, fmt_date(date_end))
            return 1
    else:
        date_start = date_end = None

    LOGGER.info("=== ETL rotacion inicio modo=%s empresas=%s ===",
                args.mode, ",".join(args.empresas))

    # Conexion destino
    try:
        tgt_conn = get_target_conn()
    except psycopg2.Error as exc:
        LOGGER.error("No se pudo conectar a BD destino: %s", exc)
        return 1

    if args.check_only:
        LOGGER.info("Conexion destino OK")
        for empresa in args.empresas:
            try:
                src = get_source_conn(empresa)
                src.close()
                LOGGER.info("Conexion origen empresa=%s OK", empresa)
            except psycopg2.Error as exc:
                LOGGER.error("Conexion origen empresa=%s FALLO: %s", empresa, exc)
        tgt_conn.close()
        return 0

    if not args.dry_run:
        try:
            ensure_target_table(tgt_conn, recreate=args.recreate_table)
        except psycopg2.Error as exc:
            LOGGER.error("Error preparando tabla destino: %s", exc)
            tgt_conn.close()
            return 1

    total_loaded = 0
    errors = 0

    for empresa in args.empresas:
        try:
            src_conn = get_source_conn(empresa)
        except psycopg2.Error as exc:
            LOGGER.error("empresa=%s no se pudo conectar: %s", empresa, exc)
            errors += 1
            continue

        try:
            if args.mode == "daily":
                n = process_daily(empresa, src_conn, tgt_conn, args.dry_run)
            elif args.mode == "rolling":
                n = process_rolling(empresa, src_conn, tgt_conn,
                                    args.rolling_days, args.dry_run)
            else:  # backfill
                n = process_backfill(empresa, src_conn, tgt_conn,
                                     date_start, date_end, args.dry_run)
            total_loaded += n

        except psycopg2.Error as exc:
            LOGGER.error("empresa=%s error: %s", empresa, exc)
            try:
                tgt_conn.rollback()
            except Exception:
                pass
            errors += 1
        finally:
            src_conn.close()

    tgt_conn.close()
    LOGGER.info("=== ETL finalizado total_cargado=%d errores=%d ===",
                total_loaded, errors)
    return 0 if errors == 0 else 1

# ── Entry point ───────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    load_env_file()
    args = parse_args(argv)
    configure_logging(args)
    cleanup_old_logs(resolve_log_dir(args.log_dir), args.log_retention_days)
    try:
        return run(args)
    except (psycopg2.Error, ValueError, OSError) as exc:
        LOGGER.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
