#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import logging
import os
import socket
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, DecimalException
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence
from uuid import UUID, uuid4

import psycopg2
from psycopg2.extensions import connection as PgConnection
from psycopg2.extras import execute_values


LOGGER = logging.getLogger("etl_rotacion_diaria_sede_3bd")

TARGET_TABLE = "public.rotacion_base_item_dia_sede"
LOAD_RUN_TABLE = "public.ventas_item_cargas"
LOAD_DAY_TABLE = "public.ventas_item_carga_dias"
LOGIN_LOG_TABLE = "public.app_user_login_logs"
SESSION_TABLE = "public.app_user_sessions"

DEFAULT_SOURCE_HOST = "192.168.35.217"
DEFAULT_TARGET_HOST = "192.168.35.232"
DEFAULT_TARGET_DATABASE = "produXdia"
DEFAULT_DB_LOG_RETENTION_DAYS = 31
DEFAULT_FILE_LOG_RETENTION_DAYS = 31
DEFAULT_LOG_DIR = "logs"
LOG_FILE_PREFIX = "etl_rotacion_diaria_sede_3bd"
SOURCE_LOAD_NAME = "rotacion_base_item_dia_sede"
LOAD_DAY_EMPRESA_PREFIX = "rotacion:"
REQUIRED_TARGET_TABLES = (
    TARGET_TABLE,
    LOAD_RUN_TABLE,
    LOAD_DAY_TABLE,
    LOGIN_LOG_TABLE,
    SESSION_TABLE,
)
CREATE_TARGET_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TARGET_TABLE} (
    empresa varchar(20) NOT NULL,
    fecha_consulta varchar(8) NOT NULL,
    sede varchar(20) NOT NULL,
    nombre_sede varchar(120),
    linea varchar(20),
    item varchar(30) NOT NULL,
    descripcion varchar(255),
    linea_n1_codigo varchar(20),
    venta_sin_impuesto numeric(18,2) DEFAULT 0,
    unidades_vendidas numeric(18,4) DEFAULT 0,
    fecha_cierre_inventario varchar(8),
    inv_cierre_dia_ayer numeric(18,4) DEFAULT 0,
    valor_inventario numeric(18,2) DEFAULT 0,
    unidad varchar(20),
    fecha_ultima_venta varchar(8),
    fecha_ultima_entrada varchar(8),
    bodega varchar(5),
    nombre_bodega varchar(40),
    categoria varchar(1),
    nombre_categoria varchar(40),
    linea01 varchar(6),
    nombre_linea01 varchar(40),
    fecha_carga timestamp DEFAULT now(),
    PRIMARY KEY (empresa, fecha_consulta, sede, item)
);
"""
TARGET_TABLE_COLUMN_MIGRATIONS = (
    f"ALTER TABLE {TARGET_TABLE} ADD COLUMN IF NOT EXISTS unidades_vendidas numeric(18,4) DEFAULT 0;",
    f"ALTER TABLE {TARGET_TABLE} ADD COLUMN IF NOT EXISTS bodega varchar(5);",
    f"ALTER TABLE {TARGET_TABLE} ADD COLUMN IF NOT EXISTS nombre_bodega varchar(40);",
    f"ALTER TABLE {TARGET_TABLE} ADD COLUMN IF NOT EXISTS categoria varchar(1);",
    f"ALTER TABLE {TARGET_TABLE} ADD COLUMN IF NOT EXISTS nombre_categoria varchar(40);",
    f"ALTER TABLE {TARGET_TABLE} ADD COLUMN IF NOT EXISTS linea01 varchar(6);",
    f"ALTER TABLE {TARGET_TABLE} ADD COLUMN IF NOT EXISTS nombre_linea01 varchar(40);",
    f"ALTER TABLE {TARGET_TABLE} ADD COLUMN IF NOT EXISTS fecha_ultima_venta varchar(8);",
    f"ALTER TABLE {TARGET_TABLE} ADD COLUMN IF NOT EXISTS fecha_ultima_entrada varchar(8);",
)


@dataclass(frozen=True)
class PgConfig:
    name: str
    host: str
    port: int
    dbname: str
    user: str
    password: str | None = None
    connect_timeout: int = 15

    def as_connect_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "dbname": self.dbname,
            "user": self.user,
            "connect_timeout": self.connect_timeout,
            "application_name": "etl_rotacion_diaria_sede_3bd",
        }
        if self.password:
            kwargs["password"] = self.password
        return kwargs


@dataclass(frozen=True)
class SourceConfig(PgConfig):
    empresa: str = ""


@dataclass(frozen=True)
class WorkItem:
    fecha_consulta: str
    empresa: str
    reason: str


Row = tuple[Any, ...]

EXTRACT_QUERY = """
WITH params AS (
    SELECT
        %(fecha_consulta)s::varchar(8) AS fecha_consulta,
        TO_CHAR(TO_DATE(%(fecha_consulta)s, 'YYYYMMDD') - INTERVAL '1 day', 'YYYYMMDD')::varchar(8) AS fecha_ayer,
        TO_CHAR(TO_DATE(%(fecha_consulta)s, 'YYYYMMDD') - INTERVAL '1 day', 'YYYYMM')::varchar(6) AS lapso_ayer
),
ventas AS (
    SELECT
        m.id_co,
        m.id_item,
        SUM(COALESCE(m.ven_netas, 0)) AS venta_neta,
        SUM(COALESCE(m.cantidad, 0)) AS unidades_vendidas
    FROM cmmovimiento_pdv m
    CROSS JOIN params p
    WHERE m.fecha_dcto = p.fecha_consulta
      AND (
            m.docto_acumulacion IS NULL
            OR BTRIM(m.docto_acumulacion) = ''
            OR BTRIM(m.docto_acumulacion) NOT LIKE 'Z%%'
          )
    GROUP BY m.id_co, m.id_item
),
inv_lapso AS (
    -- Pre-calcula en un solo escaneo el lapso mas reciente disponible
    -- por (id_co, id_local, id_item) hasta lapso_ayer inclusive.
    -- Evita la correlated subquery que escaneaba la tabla N veces.
    SELECT
        ri.id_co,
        ri.id_local,
        ri.id_item,
        MAX(ri.lapso_doc) AS max_lapso
    FROM cmresumen_inventario ri
    CROSS JOIN params p
    WHERE BTRIM(ri.id_local) = BTRIM(ri.id_co) || '01'
      AND ri.lapso_doc <= p.lapso_ayer
    GROUP BY ri.id_co, ri.id_local, ri.id_item
),
inventario AS (
    SELECT
        ri.id_co,
        ri.id_item,
        MAX(BTRIM(ri.id_local)) AS bodega,
        MAX(BTRIM(b.cmlocal_descripcion)) AS nombre_bodega,
        SUM(COALESCE(ri.can_disponible, 0)) AS inv_cierre_dia_ayer,
        SUM(COALESCE(ri.can_disponible, 0) * COALESCE(ri.costo_uni, 0)) AS valor_inventario,
        MAX(ri.fecha_ultent) AS fecha_ultima_entrada,
        MAX(NULLIF(BTRIM(ri.fecha_ultvta), '')) AS fecha_ultvta_resumen
    FROM cmresumen_inventario ri
    JOIN inv_lapso il
        ON  il.id_co     = ri.id_co
        AND il.id_local  = ri.id_local
        AND il.id_item   = ri.id_item
        AND il.max_lapso = ri.lapso_doc
    LEFT JOIN bodegas b
        ON b.id_local = ri.id_local
    GROUP BY ri.id_co, ri.id_item
),
llaves AS (
    SELECT id_co, id_item FROM ventas
    UNION
    SELECT id_co, id_item FROM inventario
)
SELECT
    %(empresa)s::varchar(20) AS empresa,
    p.fecha_consulta,
    co.codigo AS sede,
    BTRIM(co.descripcion) AS nombre_sede,
    i.id_linea AS linea,
    i.id_item AS item,
    BTRIM(i.descripcion) AS descripcion,
    i.id_linea1 AS linea_n1_codigo,
    COALESCE(v.venta_neta, 0) AS venta_sin_impuesto,
    COALESCE(v.unidades_vendidas, 0) AS unidades_vendidas,
    p.fecha_ayer AS fecha_cierre_inventario,
    COALESCE(inv.inv_cierre_dia_ayer, 0) AS inv_cierre_dia_ayer,
    COALESCE(inv.valor_inventario, 0) AS valor_inventario,
    BTRIM(COALESCE(NULLIF(i.unimed_inv_1, ''), i.unimed_com)) AS unidad,
    CASE
        WHEN COALESCE(v.venta_neta, 0) <> 0 OR COALESCE(v.unidades_vendidas, 0) <> 0
            THEN p.fecha_consulta
        WHEN inv.fecha_ultvta_resumen IS NOT NULL
            THEN inv.fecha_ultvta_resumen
        ELSE NULL
    END AS fecha_ultima_venta,
    inv.fecha_ultima_entrada,
    inv.bodega,
    inv.nombre_bodega,
    BTRIM(i.id_tipo) AS categoria,
    BTRIM(cat.cmtipinv_descripcion) AS nombre_categoria,
    BTRIM(i.id_linea1) AS linea01,
    BTRIM(l1.cmlineas_descripcion) AS nombre_linea01
FROM llaves k
JOIN centro_operacion co
    ON co.codigo = k.id_co
JOIN items i
    ON i.id_item = k.id_item
CROSS JOIN params p
LEFT JOIN categorias cat
    ON cat.id_tipo = i.id_tipo
LEFT JOIN lineas l1
    ON l1.id_linea = i.id_linea1
   AND l1.id_tipo = i.id_tipo
LEFT JOIN ventas v
    ON v.id_co = k.id_co
   AND v.id_item = k.id_item
LEFT JOIN inventario inv
    ON inv.id_co = k.id_co
   AND inv.id_item = k.id_item
WHERE
    COALESCE(v.venta_neta, 0) <> 0
    OR COALESCE(v.unidades_vendidas, 0) <> 0
    OR COALESCE(inv.inv_cierre_dia_ayer, 0) <> 0
    OR COALESCE(inv.valor_inventario, 0) <> 0
ORDER BY co.codigo, i.id_linea, i.id_item;
"""

INSERT_SQL = f"""
INSERT INTO {TARGET_TABLE} (
    empresa, fecha_consulta, sede, nombre_sede, linea, item, descripcion,
    linea_n1_codigo, venta_sin_impuesto, unidades_vendidas, fecha_cierre_inventario,
    inv_cierre_dia_ayer, valor_inventario, unidad, fecha_ultima_venta,
    fecha_ultima_entrada, bodega, nombre_bodega, categoria, nombre_categoria, linea01, nombre_linea01
) VALUES %s
ON CONFLICT (empresa, fecha_consulta, sede, item)
DO UPDATE SET
    nombre_sede = EXCLUDED.nombre_sede,
    linea = EXCLUDED.linea,
    descripcion = EXCLUDED.descripcion,
    linea_n1_codigo = EXCLUDED.linea_n1_codigo,
    venta_sin_impuesto = EXCLUDED.venta_sin_impuesto,
    unidades_vendidas = EXCLUDED.unidades_vendidas,
    fecha_cierre_inventario = EXCLUDED.fecha_cierre_inventario,
    inv_cierre_dia_ayer = EXCLUDED.inv_cierre_dia_ayer,
    valor_inventario = EXCLUDED.valor_inventario,
    unidad = EXCLUDED.unidad,
    fecha_ultima_venta = EXCLUDED.fecha_ultima_venta,
    fecha_ultima_entrada = EXCLUDED.fecha_ultima_entrada,
    bodega = EXCLUDED.bodega,
    nombre_bodega = EXCLUDED.nombre_bodega,
    categoria = EXCLUDED.categoria,
    nombre_categoria = EXCLUDED.nombre_categoria,
    linea01 = EXCLUDED.linea01,
    nombre_linea01 = EXCLUDED.nombre_linea01,
    fecha_carga = now();
"""

DELETE_DAY_SQL = f"""
DELETE FROM {TARGET_TABLE}
WHERE empresa = %s
  AND fecha_consulta = %s;
"""

SELECT_EXISTING_DAY_SQL = f"""
SELECT
    empresa, fecha_consulta, sede, nombre_sede, linea, item, descripcion,
    linea_n1_codigo, venta_sin_impuesto, unidades_vendidas, fecha_cierre_inventario,
    inv_cierre_dia_ayer, valor_inventario, unidad, fecha_ultima_venta,
    fecha_ultima_entrada, bodega, nombre_bodega, categoria, nombre_categoria, linea01, nombre_linea01
FROM {TARGET_TABLE}
WHERE empresa = %s
  AND fecha_consulta = %s;
"""

SELECT_PREVIOUS_DAY_SQL = f"""
SELECT MAX(fecha_consulta)
FROM {TARGET_TABLE}
WHERE empresa = %s
  AND fecha_consulta < %s;
"""

SELECT_DAY_LAST_SALE_SQL = f"""
SELECT sede, item, fecha_ultima_venta
FROM {TARGET_TABLE}
WHERE empresa = %s
  AND fecha_consulta = %s
  AND fecha_ultima_venta IS NOT NULL;
"""

INSERT_LOAD_RUN_SQL = f"""
INSERT INTO {LOAD_RUN_TABLE} (
    source_name, source_hash, source_rows, loaded_by, notes
) VALUES (
    %s, %s, %s, %s, %s
)
RETURNING id;
"""

UPDATE_LOAD_RUN_ROWS_SQL = f"""
UPDATE {LOAD_RUN_TABLE}
SET source_rows = %s
WHERE id = %s;
"""

UPSERT_LOAD_DAY_SQL = f"""
INSERT INTO {LOAD_DAY_TABLE} (
    empresa, fecha_dcto, source_load_id, source_rows, status, last_error, updated_at
) VALUES (
    %s, %s, %s, %s, %s, %s, now()
)
ON CONFLICT (empresa, fecha_dcto)
DO UPDATE SET
    source_load_id = EXCLUDED.source_load_id,
    source_rows = EXCLUDED.source_rows,
    status = EXCLUDED.status,
    last_error = EXCLUDED.last_error,
    updated_at = now();
"""

DELETE_OLD_LOAD_RUNS_SQL = f"""
DELETE FROM {LOAD_RUN_TABLE}
WHERE loaded_at < now() - (%s::integer * INTERVAL '1 day');
"""

DELETE_OLD_LOGIN_LOGS_SQL = f"""
DELETE FROM {LOGIN_LOG_TABLE}
WHERE logged_at < now() - (%s::integer * INTERVAL '1 day');
"""

DELETE_OLD_SESSIONS_SQL = f"""
DELETE FROM {SESSION_TABLE}
WHERE expires_at < now() - (%s::integer * INTERVAL '1 day')
   OR (
        revoked_at IS NOT NULL
        AND revoked_at < now() - (%s::integer * INTERVAL '1 day')
      );
"""

FIND_MISSING_SQL = f"""
WITH empresas AS (
    SELECT unnest(%s::text[]) AS empresa
),
dias AS (
    SELECT TO_CHAR(dia::date, 'YYYYMMDD') AS fecha_consulta
    FROM generate_series(
        TO_DATE(%s, 'YYYYMMDD'),
        TO_DATE(%s, 'YYYYMMDD'),
        INTERVAL '1 day'
    ) AS gs(dia)
)
SELECT d.fecha_consulta, e.empresa
FROM dias d
CROSS JOIN empresas e
WHERE NOT EXISTS (
    SELECT 1
    FROM {LOAD_DAY_TABLE} cd
    WHERE cd.empresa = '{LOAD_DAY_EMPRESA_PREFIX}' || e.empresa
      AND cd.fecha_dcto = d.fecha_consulta
      AND cd.status = 'done'
)
ORDER BY d.fecha_consulta, e.empresa;
"""


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "ETL de rotacion diaria por sede. Por defecto recarga la ventana "
            "movil de los ultimos 15 dias cerrados."
        )
    )
    parser.add_argument("--fecha-inicio", help="Fecha inicial YYYYMMDD. Si se usa sola, procesa un solo dia.")
    parser.add_argument("--fecha-fin", help="Fecha final YYYYMMDD. Si se usa sola, procesa un solo dia.")
    parser.add_argument(
        "--history-start",
        help=(
            "Fecha YYYYMMDD desde donde se revisa ventas_item_carga_dias para rellenar "
            "dias faltantes. Usalo en la ejecucion diaria despues del backfill."
        ),
    )
    parser.add_argument(
        "--rolling-days",
        type=positive_int,
        default=15,
        help="Dias cerrados que siempre se borran y recargan en modo automatico. Default: 15.",
    )
    parser.add_argument(
        "--procesar-hoy",
        action="store_true",
        help="Incluye el dia actual cuando no se informa --fecha-fin. Por defecto termina ayer.",
    )
    parser.add_argument("--empresas", nargs="*", default=[], help="Filtra empresas: mercamio mtodo bogota.")
    parser.add_argument("--dry-run", action="store_true", help="Muestra el plan sin borrar ni cargar datos.")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Verifica conexiones y tabla destino sin cargar informacion.",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Detiene todo el proceso ante el primer error de una empresa/dia.",
    )
    parser.add_argument(
        "--refresh-inventory",
        action="store_true",
        help=(
            "Recalcula el inventario al reprocesar todos los dias existentes. "
            "Este modo puede sobrescribir snapshots historicos. "
            "Sin este flag, solo se refresca automaticamente el ultimo dia cerrado."
        ),
    )
    parser.add_argument(
        "--db-log-retention-days",
        "--audit-retention-days",
        dest="db_log_retention_days",
        type=positive_int,
        default=env_int(
            "ETL_DB_LOG_RETENTION_DAYS",
            env_int("ETL_AUDIT_RETENTION_DAYS", DEFAULT_DB_LOG_RETENTION_DAYS),
        ),
        help=(
            "Dias que se conservan logs en tablas existentes de la base. "
            "Default: 31. Tambien se puede definir con ETL_DB_LOG_RETENTION_DAYS."
        ),
    )
    parser.add_argument(
        "--no-cleanup-db-logs",
        "--no-cleanup-audit",
        dest="no_cleanup_db_logs",
        action="store_true",
        help="No borra logs antiguos en tablas existentes de la base en esta ejecucion.",
    )
    parser.add_argument(
        "--log-dir",
        default=os.getenv("ETL_LOG_DIR", DEFAULT_LOG_DIR),
        help=(
            "Carpeta donde el ETL guarda logs diarios. Default: ./logs. "
            "Tambien se puede definir con ETL_LOG_DIR."
        ),
    )
    parser.add_argument(
        "--no-file-log",
        action="store_true",
        help="No escribe archivo .log local; solo muestra logs por consola.",
    )
    parser.add_argument(
        "--file-log-retention-days",
        type=positive_int,
        default=env_int("ETL_FILE_LOG_RETENTION_DAYS", DEFAULT_FILE_LOG_RETENTION_DAYS),
        help=(
            "Dias que se conservan archivos .log del ETL en --log-dir. "
            "Default: 31. Tambien se puede definir con ETL_FILE_LOG_RETENTION_DAYS."
        ),
    )
    parser.add_argument(
        "--no-cleanup-file-logs",
        action="store_true",
        help="No borra archivos .log antiguos del ETL en esta ejecucion.",
    )
    parser.add_argument("--fetch-size", type=positive_int, default=5000, help="Filas por lectura desde origen.")
    parser.add_argument("--insert-page-size", type=positive_int, default=5000, help="Filas por lote de insert.")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args(argv)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("Debe ser un entero positivo.")
    return parsed


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def resolve_log_dir(raw_log_dir: str) -> Path:
    log_dir = Path(raw_log_dir).expanduser()
    if not log_dir.is_absolute():
        log_dir = Path(__file__).resolve().parent / log_dir
    return log_dir


def configure_logging(args: argparse.Namespace) -> Path | None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    log_dir: Path | None = None

    if not args.no_file_log:
        log_dir = resolve_log_dir(args.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{LOG_FILE_PREFIX}_{date.today():%Y%m%d}.log"
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=handlers,
        force=True,
    )

    if log_dir is not None:
        LOGGER.info("Log local activo: %s", log_dir)
    return log_dir


def cleanup_file_logs(log_dir: Path, retention_days: int) -> int:
    if not log_dir.exists():
        return 0

    cutoff = datetime.now().timestamp() - (retention_days * 24 * 60 * 60)
    deleted = 0
    for log_file in log_dir.glob(f"{LOG_FILE_PREFIX}*.log"):
        if not log_file.is_file():
            continue
        try:
            if log_file.stat().st_mtime < cutoff:
                log_file.unlink()
                deleted += 1
        except OSError as exc:
            LOGGER.warning("No se pudo borrar log local %s: %s", log_file, exc)

    LOGGER.info(
        "Mantenimiento logs locales: archivos borrados=%s retencion_dias=%s carpeta=%s",
        deleted,
        retention_days,
        log_dir,
    )
    return deleted


def parse_yyyymmdd(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError(f"Fecha invalida '{value}'. Usa formato YYYYMMDD.") from exc


def format_yyyymmdd(value: date) -> str:
    return value.strftime("%Y%m%d")


def daterange(start: date, end: date) -> Iterator[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def source_env_prefix(empresa: str) -> str:
    return f"SRC_{empresa.upper()}_"


def load_sources() -> list[SourceConfig]:
    definitions = (
        ("mercamio", "mercamio", "mercamio"),
        ("mtodo", "mtodo", "mtodo"),
        ("bogota", "bogota", "bogota"),
    )
    timeout = env_int("PGCONNECT_TIMEOUT", 15)
    sources: list[SourceConfig] = []
    for empresa, dbname, user in definitions:
        prefix = source_env_prefix(empresa)
        sources.append(
            SourceConfig(
                name=f"source:{empresa}",
                empresa=empresa,
                host=os.getenv(f"{prefix}PGHOST", DEFAULT_SOURCE_HOST),
                port=env_int(f"{prefix}PGPORT", 5432),
                dbname=os.getenv(f"{prefix}PGDATABASE", dbname),
                user=os.getenv(f"{prefix}PGUSER", user),
                password=os.getenv(f"{prefix}PGPASSWORD") or os.getenv("SRC_DEFAULT_PGPASSWORD"),
                connect_timeout=timeout,
            )
        )
    return sources


def load_target_config() -> PgConfig:
    return PgConfig(
        name="target",
        host=os.getenv("TARGET_PGHOST", DEFAULT_TARGET_HOST),
        port=env_int("TARGET_PGPORT", 5432),
        dbname=os.getenv("TARGET_PGDATABASE", DEFAULT_TARGET_DATABASE),
        user=os.getenv("TARGET_PGUSER", "postgres"),
        password=os.getenv("TARGET_PGPASSWORD"),
        connect_timeout=env_int("PGCONNECT_TIMEOUT", 15),
    )


def connect_pg(config: PgConfig) -> PgConnection:
    return psycopg2.connect(**config.as_connect_kwargs())


def ensure_target_objects(conn: PgConnection, apply_migrations: bool = True) -> None:
    with conn:
        with conn.cursor() as cur:
            ensure_target_table(cur)
            verify_required_tables(cur)
            if apply_migrations:
                ensure_target_columns(cur)


def ensure_target_table(cur: Any) -> None:
    cur.execute(CREATE_TARGET_TABLE_SQL)


def verify_required_tables(cur: Any) -> None:
    missing: list[str] = []
    for table_name in REQUIRED_TARGET_TABLES:
        cur.execute("SELECT to_regclass(%s);", (table_name,))
        if cur.fetchone()[0] is None:
            missing.append(table_name)
    if missing:
        raise ValueError(
            "Faltan tablas esperadas en el esquema destino: "
            + ", ".join(missing)
            + ". Revisa que la base corresponda al dump producXdia.sql."
        )


def ensure_target_columns(cur: Any) -> None:
    for statement in TARGET_TABLE_COLUMN_MIGRATIONS:
        cur.execute(statement)
    migrate_fecha_ultima_compra_column(cur)


def column_exists(cur: Any, table_name: str, column_name: str) -> bool:
    schema_name, simple_table_name = table_name.split(".", 1)
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
          AND column_name = %s;
        """,
        (schema_name, simple_table_name, column_name),
    )
    return cur.fetchone() is not None


def migrate_fecha_ultima_compra_column(cur: Any) -> None:
    has_old_column = column_exists(cur, TARGET_TABLE, "fecha_ultima_compra")
    has_new_column = column_exists(cur, TARGET_TABLE, "fecha_ultima_venta")

    if not has_old_column:
        return

    if not has_new_column:
        cur.execute(
            f"ALTER TABLE {TARGET_TABLE} RENAME COLUMN fecha_ultima_compra TO fecha_ultima_venta;"
        )
        LOGGER.info("Migracion aplicada: fecha_ultima_compra renombrada a fecha_ultima_venta.")
        return

    LOGGER.warning(
        "La tabla destino tiene ambas columnas: fecha_ultima_compra y fecha_ultima_venta. "
        "Se omite la copia automatica para evitar un UPDATE masivo en cada arranque."
    )


def cleanup_database_logs(conn: PgConnection, retention_days: int) -> dict[str, int]:
    deleted: dict[str, int] = {}
    with conn:
        with conn.cursor() as cur:
            cur.execute(DELETE_OLD_LOGIN_LOGS_SQL, (retention_days,))
            deleted["app_user_login_logs"] = cur.rowcount

            cur.execute(DELETE_OLD_LOAD_RUNS_SQL, (retention_days,))
            deleted["ventas_item_cargas"] = cur.rowcount

            cur.execute(DELETE_OLD_SESSIONS_SQL, (retention_days, retention_days))
            deleted["app_user_sessions"] = cur.rowcount

    if any(count > 0 for count in deleted.values()):
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(f"ANALYZE {LOGIN_LOG_TABLE};")
                    cur.execute(f"ANALYZE {LOAD_RUN_TABLE};")
                    cur.execute(f"ANALYZE {LOAD_DAY_TABLE};")
                    cur.execute(f"ANALYZE {SESSION_TABLE};")
        except psycopg2.Error as exc:
            conn.rollback()
            LOGGER.warning("No se pudo ejecutar ANALYZE despues del mantenimiento: %s", exc)

    LOGGER.info(
        "Mantenimiento BD: login_logs=%s cargas=%s sesiones=%s retencion_dias=%s",
        deleted["app_user_login_logs"],
        deleted["ventas_item_cargas"],
        deleted["app_user_sessions"],
        retention_days,
    )
    return deleted


def target_table_has_rows(conn: PgConnection) -> bool:
    with conn.cursor() as cur:
        cur.execute(f"SELECT EXISTS (SELECT 1 FROM {TARGET_TABLE} LIMIT 1);")
        return bool(cur.fetchone()[0])


def maybe_enable_refresh_inventory(args: argparse.Namespace, conn: PgConnection) -> None:
    if args.refresh_inventory:
        return
    if target_table_has_rows(conn):
        return

    args.refresh_inventory = True
    LOGGER.warning(
        "La tabla destino esta vacia; se activa --refresh-inventory automaticamente "
        "para no dejar dias historicos con inventario nulo."
    )


def select_sources(all_sources: Sequence[SourceConfig], empresas: Sequence[str]) -> list[SourceConfig]:
    if not empresas:
        return list(all_sources)

    requested = set(empresas)
    known = {source.empresa for source in all_sources}
    unknown = sorted(requested - known)
    if unknown:
        raise ValueError(f"Empresas desconocidas: {', '.join(unknown)}")
    return [source for source in all_sources if source.empresa in requested]


def resolve_explicit_range(args: argparse.Namespace) -> tuple[date, date] | None:
    if not args.fecha_inicio and not args.fecha_fin:
        return None
    start_text = args.fecha_inicio or args.fecha_fin
    end_text = args.fecha_fin or args.fecha_inicio
    if start_text is None or end_text is None:
        raise ValueError("No se pudo resolver el rango de fechas.")

    start = parse_yyyymmdd(start_text)
    end = parse_yyyymmdd(end_text)
    if start > end:
        raise ValueError("--fecha-inicio no puede ser mayor que --fecha-fin.")
    return start, end


def resolve_automatic_range(args: argparse.Namespace) -> tuple[date, date]:
    end = date.today() if args.procesar_hoy else date.today() - timedelta(days=1)
    start = end - timedelta(days=args.rolling_days - 1)
    return start, end


def latest_closed_day() -> str:
    return format_yyyymmdd(date.today() - timedelta(days=1))


def should_refresh_inventory(args: argparse.Namespace, item: WorkItem) -> bool:
    return args.refresh_inventory or item.fecha_consulta == latest_closed_day()


def add_work_item(items: dict[tuple[str, str], WorkItem], fecha: str, empresa: str, reason: str) -> None:
    key = (fecha, empresa)
    previous = items.get(key)
    if previous is None:
        items[key] = WorkItem(fecha_consulta=fecha, empresa=empresa, reason=reason)
        return

    reasons = previous.reason.split("+")
    if reason not in reasons:
        items[key] = WorkItem(fecha_consulta=fecha, empresa=empresa, reason=f"{previous.reason}+{reason}")


def find_missing_items(
    conn: PgConnection,
    empresas: Sequence[str],
    start: date,
    end: date,
) -> list[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(FIND_MISSING_SQL, (list(empresas), format_yyyymmdd(start), format_yyyymmdd(end)))
        rows = [(row[0], row[1]) for row in cur.fetchall()]
    conn.commit()
    return rows


def build_work_plan(
    conn: PgConnection | None,
    args: argparse.Namespace,
    sources: Sequence[SourceConfig],
) -> list[WorkItem]:
    items: dict[tuple[str, str], WorkItem] = {}
    explicit_range = resolve_explicit_range(args)
    source_names = [source.empresa for source in sources]

    if explicit_range:
        start, end = explicit_range
        for day in daterange(start, end):
            fecha = format_yyyymmdd(day)
            for empresa in source_names:
                add_work_item(items, fecha, empresa, "explicit")
    else:
        start, end = resolve_automatic_range(args)
        for day in daterange(start, end):
            fecha = format_yyyymmdd(day)
            for empresa in source_names:
                add_work_item(items, fecha, empresa, "rolling")

    if args.history_start:
        if conn is None:
            raise ValueError("--history-start requiere conexion al destino para leer ventas_item_carga_dias.")
        history_start = parse_yyyymmdd(args.history_start)
        _, plan_end = explicit_range or resolve_automatic_range(args)
        if history_start > plan_end:
            raise ValueError("--history-start no puede ser mayor que la fecha final del plan.")

        missing = find_missing_items(conn, source_names, history_start, plan_end)
        for fecha, empresa in missing:
            add_work_item(items, fecha, empresa, "missing")

    return sorted(items.values(), key=lambda item: (item.fecha_consulta, item.empresa))


def clean_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def clean_text(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip()
    return value


def clean_date_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, date):
        return value.strftime("%Y%m%d")

    text = str(value).strip()
    if not text:
        return None
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return text.replace("-", "")
    return text


def normalize_rows(rows: Iterable[Row]) -> list[Row]:
    normalized: list[Row] = []
    for row in rows:
        (
            empresa,
            fecha_consulta,
            sede,
            nombre_sede,
            linea,
            item,
            descripcion,
            linea_n1_codigo,
            venta_sin_impuesto,
            unidades_vendidas,
            fecha_cierre_inventario,
            inv_cierre_dia_ayer,
            valor_inventario,
            unidad,
            fecha_ultima_venta,
            fecha_ultima_entrada,
            bodega,
            nombre_bodega,
            categoria,
            nombre_categoria,
            linea01,
            nombre_linea01,
        ) = row
        normalized.append(
            (
                clean_text(empresa),
                clean_text(fecha_consulta),
                clean_text(sede),
                clean_text(nombre_sede),
                clean_text(linea),
                clean_text(item),
                clean_text(descripcion),
                clean_text(linea_n1_codigo),
                clean_decimal(venta_sin_impuesto),
                clean_decimal(unidades_vendidas),
                clean_text(fecha_cierre_inventario),
                clean_decimal(inv_cierre_dia_ayer),
                clean_decimal(valor_inventario),
                clean_text(unidad),
                clean_date_text(fecha_ultima_venta),
                clean_date_text(fecha_ultima_entrada),
                clean_text(bodega),
                clean_text(nombre_bodega),
                clean_text(categoria),
                clean_text(nombre_categoria),
                clean_text(linea01),
                clean_text(nombre_linea01),
            )
        )
    return normalized


def iter_source_chunks(source: SourceConfig, fecha_consulta: str, fetch_size: int) -> Iterator[list[Row]]:
    params = {"fecha_consulta": fecha_consulta, "empresa": source.empresa}
    cursor_name = f"rotacion_{source.empresa}_{fecha_consulta}"
    with closing(connect_pg(source)) as conn:
        with conn:
            with conn.cursor(name=cursor_name) as cur:
                cur.itersize = fetch_size
                cur.execute(EXTRACT_QUERY, params)
                while True:
                    rows = cur.fetchmany(fetch_size)
                    if not rows:
                        break
                    yield normalize_rows(rows)


def insert_rows(cur: Any, rows: Sequence[Row], page_size: int) -> int:
    if not rows:
        return 0
    execute_values(cur, INSERT_SQL, rows, page_size=page_size)
    return len(rows)


def row_key(row: Row) -> tuple[str, str]:
    return str(row[2] or ""), str(row[5] or "")


def row_has_sale(row: Row) -> bool:
    return clean_decimal(row[8]) != 0 or clean_decimal(row[9]) != 0


def has_inventory_snapshot(row: Row) -> bool:
    return clean_decimal(row[11]) != 0 or clean_decimal(row[12]) != 0


def restore_inventory_snapshot(row: Row, snapshot: Row) -> Row:
    current = list(row)
    current[10] = snapshot[10]
    current[11] = snapshot[11]
    current[12] = snapshot[12]
    current[13] = snapshot[13]
    # current[14] se conserva desde el calculo actual porque ahora representa la ultima venta.
    current[15] = snapshot[15]
    current[16] = snapshot[16]
    current[17] = snapshot[17]
    return tuple(current)


def clear_inventory_snapshot(row: Row) -> Row:
    current = list(row)
    current[11] = Decimal("0")
    current[12] = Decimal("0")
    current[15] = None
    current[16] = None
    current[17] = None
    return tuple(current)


def fetch_existing_day_rows(cur: Any, empresa: str, fecha_consulta: str) -> list[Row]:
    cur.execute(SELECT_EXISTING_DAY_SQL, (empresa, fecha_consulta))
    return normalize_rows(cur.fetchall())


def fetch_previous_last_sale_rows(cur: Any, empresa: str, fecha_consulta: str) -> dict[tuple[str, str], str]:
    cur.execute(SELECT_PREVIOUS_DAY_SQL, (empresa, fecha_consulta))
    previous_day = cur.fetchone()[0]
    if not previous_day:
        return {}

    cur.execute(SELECT_DAY_LAST_SALE_SQL, (empresa, previous_day))
    rows = cur.fetchall()
    return {
        (clean_text(sede), clean_text(item)): clean_date_text(fecha_ultima_venta) or ""
        for sede, item, fecha_ultima_venta in rows
        if clean_date_text(fecha_ultima_venta)
    }


def fetch_source_last_sale_seed(
    source: SourceConfig,
    fecha_consulta: str,
    keys: Sequence[tuple[str, str]],
) -> dict[tuple[str, str], str]:
    if not keys:
        return {}

    unique_keys = sorted({(str(sede or "").strip(), str(item or "").strip()) for sede, item in keys})
    if not unique_keys:
        return {}

    seed_sql = """
    SELECT
        BTRIM(m.id_co) AS sede,
        BTRIM(m.id_item) AS item,
        MAX(m.fecha_dcto) AS fecha_ultima_venta
    FROM cmmovimiento_pdv m
    JOIN tmp_last_sale_keys k
        ON m.id_co = k.id_co
       AND m.id_item = k.id_item
    WHERE m.fecha_dcto < %s
      AND m.fecha_dcto >= %s
      AND (
            m.docto_acumulacion IS NULL
            OR BTRIM(m.docto_acumulacion) = ''
            OR BTRIM(m.docto_acumulacion) NOT LIKE 'Z%%'
          )
      AND (
            COALESCE(m.cantidad, 0) > 0
            OR COALESCE(m.ven_netas, 0) > 0
          )
    GROUP BY BTRIM(m.id_co), BTRIM(m.id_item);
    """

    fecha_limite = format_yyyymmdd(
        parse_yyyymmdd(fecha_consulta) - timedelta(days=90)
    )

    with closing(connect_pg(source)) as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TEMP TABLE tmp_last_sale_keys (
                        id_co char(3) NOT NULL,
                        id_item char(6) NOT NULL
                    ) ON COMMIT DROP;
                    """
                )
                execute_values(
                    cur,
                    "INSERT INTO tmp_last_sale_keys (id_co, id_item) VALUES %s",
                    unique_keys,
                    page_size=5000,
                )
                cur.execute(seed_sql, (fecha_consulta, fecha_limite))
                rows = cur.fetchall()

    return {
        (clean_text(sede), clean_text(item)): clean_date_text(fecha_ultima_venta) or ""
        for sede, item, fecha_ultima_venta in rows
        if clean_date_text(fecha_ultima_venta)
    }


def apply_last_sale_dates(
    rows: Sequence[Row],
    previous_last_sales: dict[tuple[str, str], str],
) -> list[Row]:
    updated_rows: list[Row] = []
    for row in rows:
        current = list(row)
        key = row_key(row)
        if not current[14]:
            current[14] = previous_last_sales.get(key)
        updated_rows.append(tuple(current))
    return updated_rows


def prepare_rows_for_reprocess(
    rows: Sequence[Row],
    preserved_rows: dict[tuple[str, str], Row],
    seen_keys: set[tuple[str, str]],
    use_source_inventory: bool,
    is_first_load: bool = False,
) -> list[Row]:
    if use_source_inventory:
        seen_keys.update(row_key(row) for row in rows)
        return list(rows)

    prepared: list[Row] = []
    for row in rows:
        key = row_key(row)
        seen_keys.add(key)
        snapshot = preserved_rows.get(key)
        if snapshot is not None:
            prepared.append(restore_inventory_snapshot(row, snapshot))
            continue

        # Primera carga de este dia: no habia snapshot previo en destino.
        # Se usa el inventario del origen tal cual para no dejar historicos en cero.
        if is_first_load:
            prepared.append(row)
            continue

        # Reprocesando un dia ya cargado sin snapshot para esta clave especifica:
        # se preserva solo la venta, sin sobreescribir inventario historico.
        if not row_has_sale(row):
            continue

        prepared.append(clear_inventory_snapshot(row))

    return prepared


def remaining_inventory_snapshot_rows(
    preserved_rows: dict[tuple[str, str], Row],
    seen_keys: set[tuple[str, str]],
) -> list[Row]:
    remaining: list[Row] = []
    for key, row in preserved_rows.items():
        if key in seen_keys or not has_inventory_snapshot(row):
            continue
        current = list(row)
        current[8] = Decimal("0")
        current[9] = Decimal("0")
        remaining.append(tuple(current))
    return remaining


def current_loaded_by() -> str:
    return f"{getpass.getuser()}@{socket.gethostname()}"


def build_load_notes(args: argparse.Namespace, sources: Sequence[SourceConfig]) -> str:
    empresas = ",".join(source.empresa for source in sources)
    details = [
        "script=etl_rotacion_diaria_sede_3bd_auto.py",
        f"empresas={empresas}",
        f"fecha_inicio={args.fecha_inicio or ''}",
        f"fecha_fin={args.fecha_fin or ''}",
        f"history_start={args.history_start or ''}",
        f"rolling_days={args.rolling_days}",
        f"refresh_inventory={args.refresh_inventory}",
        "historical_inventory=preserve_existing_else_zero",
    ]
    return "; ".join(details)


def create_load_run(
    conn: PgConnection,
    *,
    run_id: UUID,
    args: argparse.Namespace,
    sources: Sequence[SourceConfig],
) -> int:
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                INSERT_LOAD_RUN_SQL,
                (
                    SOURCE_LOAD_NAME,
                    str(run_id),
                    0,
                    current_loaded_by(),
                    build_load_notes(args, sources),
                ),
            )
            load_id = cur.fetchone()[0]
    return int(load_id)


def update_load_run_rows(conn: PgConnection, load_id: int, source_rows: int) -> None:
    with conn:
        with conn.cursor() as cur:
            cur.execute(UPDATE_LOAD_RUN_ROWS_SQL, (source_rows, load_id))


def tracking_empresa(empresa: str) -> str:
    return f"{LOAD_DAY_EMPRESA_PREFIX}{empresa}"


def upsert_load_day(
    cur: Any,
    *,
    source_load_id: int,
    item: WorkItem,
    source_rows: int,
    status: str,
    last_error: str | None,
) -> None:
    cur.execute(
        UPSERT_LOAD_DAY_SQL,
        (
            tracking_empresa(item.empresa),
            item.fecha_consulta,
            source_load_id,
            source_rows,
            status,
            last_error,
        ),
    )


def write_failure_day(
    conn: PgConnection,
    *,
    source_load_id: int,
    item: WorkItem,
    rows_extracted: int,
    rows_loaded: int,
    rows_deleted: int,
    error: BaseException,
) -> None:
    error_message = (
        f"{type(error).__name__}: {error}. "
        f"rows_extracted={rows_extracted}; rows_loaded={rows_loaded}; rows_deleted={rows_deleted}"
    )[:4000]
    try:
        with conn:
            with conn.cursor() as cur:
                upsert_load_day(
                    cur,
                    source_load_id=source_load_id,
                    item=item,
                    status="failed",
                    source_rows=rows_loaded,
                    last_error=error_message,
                )
    except psycopg2.Error as day_error:
        conn.rollback()
        LOGGER.warning("No se pudo registrar estado fallido: %s", day_error)


def reload_day_company(
    target_conn: PgConnection,
    *,
    source: SourceConfig,
    item: WorkItem,
    source_load_id: int,
    fetch_size: int,
    insert_page_size: int,
    refresh_inventory: bool,
) -> int | None:
    rows_extracted = 0
    rows_loaded = 0
    rows_deleted = 0
    preserved_count = 0
    preserved_only_count = 0

    try:
        LOGGER.info(
            "Procesando empresa=%s fecha=%s reason=%s refresh_inventory=%s",
            item.empresa,
            item.fecha_consulta,
            item.reason,
            refresh_inventory,
        )
        with target_conn:
            with target_conn.cursor() as cur:
                existing_rows = fetch_existing_day_rows(cur, item.empresa, item.fecha_consulta)
                is_first_load = len(existing_rows) == 0
                preserved_rows = {row_key(row): row for row in existing_rows}
                previous_last_sales = fetch_previous_last_sale_rows(cur, item.empresa, item.fecha_consulta)
                if refresh_inventory:
                    preserved_rows = {}
                preserved_count = len(preserved_rows)
                seen_keys: set[tuple[str, str]] = set()

                cur.execute(DELETE_DAY_SQL, (item.empresa, item.fecha_consulta))
                rows_deleted = cur.rowcount

                source_rows: list[Row] = []
                for chunk in iter_source_chunks(source, item.fecha_consulta, fetch_size):
                    rows_extracted += len(chunk)
                    source_rows.extend(chunk)

                # fecha_ultima_venta viene de fecha_ultvta (cmresumen_inventario)
                # para items con inventario, y de fecha_consulta para items con venta.
                # Solo se consulta cmmovimiento_pdv para los casos residuales:
                # items sin inventario activo, sin venta hoy y sin fecha previa en destino.
                missing_last_sale_keys = [
                    row_key(row)
                    for row in source_rows
                    if not row[14]
                    and not row_has_sale(row)
                    and row_key(row) not in previous_last_sales
                ]
                LOGGER.info(
                    "Busqueda fecha_ultima_venta en cmmovimiento_pdv: empresa=%s fecha=%s items=%s",
                    item.empresa,
                    item.fecha_consulta,
                    len(missing_last_sale_keys),
                )
                if missing_last_sale_keys:
                    previous_last_sales.update(
                        fetch_source_last_sale_seed(source, item.fecha_consulta, missing_last_sale_keys)
                    )

                source_rows = apply_last_sale_dates(source_rows, previous_last_sales)
                prepared_rows = prepare_rows_for_reprocess(
                    source_rows,
                    preserved_rows,
                    seen_keys,
                    use_source_inventory=refresh_inventory,
                    is_first_load=is_first_load,
                )
                rows_loaded += insert_rows(cur, prepared_rows, insert_page_size)

                preserved_only_rows = remaining_inventory_snapshot_rows(preserved_rows, seen_keys)
                preserved_only_count = len(preserved_only_rows)
                rows_loaded += insert_rows(cur, preserved_only_rows, insert_page_size)

                upsert_load_day(
                    cur,
                    source_load_id=source_load_id,
                    item=item,
                    source_rows=rows_loaded,
                    status="done",
                    last_error=None,
                )
    except (psycopg2.Error, ValueError, DecimalException) as exc:
        target_conn.rollback()
        write_failure_day(
            target_conn,
            source_load_id=source_load_id,
            item=item,
            rows_extracted=rows_extracted,
            rows_loaded=rows_loaded,
            rows_deleted=rows_deleted,
            error=exc,
        )
        LOGGER.exception(
            "Fallo empresa=%s fecha=%s reason=%s",
            item.empresa,
            item.fecha_consulta,
            item.reason,
        )
        return None

    LOGGER.info(
        "OK empresa=%s fecha=%s reason=%s borradas=%s extraidas=%s cargadas=%s snapshots_preservados=%s inventario_solo_preservado=%s",
        item.empresa,
        item.fecha_consulta,
        item.reason,
        rows_deleted,
        rows_extracted,
        rows_loaded,
        preserved_count,
        preserved_only_count,
    )
    return rows_loaded


def check_connections(target: PgConfig, sources: Sequence[SourceConfig]) -> None:
    with closing(connect_pg(target)) as conn:
        ensure_target_objects(conn, apply_migrations=False)
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_user;")
            dbname, user = cur.fetchone()
            LOGGER.info("Destino OK database=%s user=%s host=%s", dbname, user, target.host)
        conn.commit()

    for source in sources:
        with closing(connect_pg(source)) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database(), current_user;")
                dbname, user = cur.fetchone()
                LOGGER.info(
                    "Origen OK empresa=%s database=%s user=%s host=%s",
                    source.empresa,
                    dbname,
                    user,
                    source.host,
                )
            conn.commit()


def run(args: argparse.Namespace) -> int:
    all_sources = load_sources()
    sources = select_sources(all_sources, args.empresas)
    source_by_empresa = {source.empresa: source for source in sources}
    target = load_target_config()
    run_id = uuid4()

    LOGGER.info("run_id=%s", run_id)

    if args.check_only:
        check_connections(target, sources)
        LOGGER.info("Verificacion finalizada.")
        return 0

    if args.dry_run:
        if args.history_start:
            with closing(connect_pg(target)) as target_conn:
                ensure_target_objects(target_conn, apply_migrations=False)
                maybe_enable_refresh_inventory(args, target_conn)
                plan = build_work_plan(target_conn, args, sources)
        else:
            plan = build_work_plan(None, args, sources)

        LOGGER.info("Plan de trabajo: %s empresa/dia.", len(plan))
        for item in plan:
            LOGGER.info(
                "DRY-RUN empresa=%s fecha=%s reason=%s refresh_inventory=%s",
                item.empresa,
                item.fecha_consulta,
                item.reason,
                should_refresh_inventory(args, item),
            )
        return 0

    with closing(connect_pg(target)) as target_conn:
        ensure_target_objects(target_conn)
        maybe_enable_refresh_inventory(args, target_conn)
        if not args.no_cleanup_db_logs:
            cleanup_database_logs(target_conn, args.db_log_retention_days)
        plan = build_work_plan(target_conn, args, sources)

        if not plan:
            LOGGER.info("No hay dias por procesar.")
            return 0

        LOGGER.info("Plan de trabajo: %s empresa/dia.", len(plan))
        source_load_id = create_load_run(target_conn, run_id=run_id, args=args, sources=sources)
        LOGGER.info("Registro de carga creado en ventas_item_cargas id=%s", source_load_id)

        total_loaded = 0
        failures = 0
        for item in plan:
            source = source_by_empresa[item.empresa]
            loaded = reload_day_company(
                target_conn,
                source=source,
                item=item,
                source_load_id=source_load_id,
                fetch_size=args.fetch_size,
                insert_page_size=args.insert_page_size,
                refresh_inventory=should_refresh_inventory(args, item),
            )
            if loaded is None:
                failures += 1
                if args.stop_on_error:
                    break
            else:
                total_loaded += loaded

        update_load_run_rows(target_conn, source_load_id, total_loaded)

    if failures:
        LOGGER.error("Proceso finalizado con %s fallo(s). run_id=%s", failures, run_id)
        return 1

    LOGGER.info("Proceso finalizado sin errores. run_id=%s", run_id)
    return 0




def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        configure_logging(args)
        if not args.no_cleanup_file_logs:
            cleanup_file_logs(resolve_log_dir(args.log_dir), args.file_log_retention_days)
        return run(args)
    except (psycopg2.Error, ValueError, OSError) as exc:
        LOGGER.error("%s", exc)
        return 1


if __name__ == "__main__":    raise SystemExit(main())
