"""Hooks post-carga: refrescar vistas materializadas + reporte Telegram (cortana).

Ambos son OPCIONALES y **fallan-suave**: si falta config o credenciales, no hacen
nada (nunca rompen la corrida del ETL).

- ``refresh_materialized_views``: refresca la lista ``target.cloudsql_postgres.
  refresh_views`` del config (vacia por defecto -> no-op). Auto-detecta si el
  nombre es una FUNCION de refresco o una VISTA materializada:
    * Funcion que acepta 2 argumentos y hay rango cargado -> ``SELECT f(desde, hasta)``
      (INCREMENTAL: solo recalcula el rango). El dia que BI cree una funcion con
      rango, el daily la usa sola sin cambiar codigo.
    * Funcion de 0 argumentos -> ``SELECT f()`` (rebuild completo, como hasta hoy).
    * Vista materializada -> ``REFRESH MATERIALIZED VIEW``.
  Devuelve una lista de resultados por vista para que el reporte diga si refresco.
- ``report_run``: manda un HEALTH-CHECK al bot **cortana DIRECTO** (api.telegram.org),
  sin OpenClaw ni grupo. Incluye: estado de carga, filas cargadas del rango,
  FRESCURA (la fecha esperada si llego a GCP), estado de refresco por vista, y una
  linea de ⚠️ ACCION cuando algo falta. Objetivo: que la alerta avise sola de datos
  faltantes/atrasados (evitar el "falta informacion" por telefono).
"""
from __future__ import annotations

import json
import os
import urllib.request

try:
    import psycopg2
except ImportError:  # el reporte/refresh dependen del driver PG; sin el, no-op
    psycopg2 = None  # type: ignore

_TELEGRAM_API = "https://api.telegram.org"


def _cp(config) -> dict:
    """Sub-config de Cloud SQL Postgres (con ${ENV} ya expandido)."""
    cp = config.get("target.cloudsql_postgres", {})
    return cp if isinstance(cp, dict) else {}


def _pg_connect(config):
    """Conexión psycopg2 al destino, o None si falta driver/host."""
    if psycopg2 is None:
        return None
    cp = _cp(config)
    host = cp.get("host")
    if not host:
        return None
    return psycopg2.connect(
        host=host,
        port=int(cp.get("port", 5432) or 5432),
        dbname=cp.get("database"),
        user=cp.get("user"),
        password=cp.get("password"),
        sslmode=cp.get("sslmode", "require") or "require",
        connect_timeout=15,
    )


def _iso(d):
    """YYYYMMDD -> 'YYYY-MM-DD' (para params tipo date). Deja igual si no aplica."""
    if d and len(d) == 8 and str(d).isdigit():
        return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
    return d


def _norm_yyyymmdd(v):
    """Normaliza un valor de fecha (date/int/text) a 'YYYYMMDD' o None."""
    if v is None:
        return None
    if hasattr(v, "strftime"):
        return v.strftime("%Y%m%d")
    s = str(v).strip()
    return s.replace("-", "")[:8] if "-" in s else s[:8]


def refresh_materialized_views(config, logger=None, *, start_date=None,
                               end_date=None) -> list[dict]:
    """Refresca las vistas/funciones de ``refresh_views``.

    Devuelve ``[{"view", "status", "detail"}]`` para que el reporte informe si
    refresco. ``status`` in {incremental, full, matview, failed, skipped}.
    """
    views = config.get("target.cloudsql_postgres.refresh_views", []) or []
    if not views:
        return []
    conn = _pg_connect(config)
    if conn is None:
        # sin driver/host no podemos refrescar: lo decimos (transparencia en el reporte)
        return [{"view": v, "status": "skipped", "detail": "sin conexión PG"}
                for v in views]

    d_from, d_to = _iso(start_date), _iso(end_date)
    results: list[dict] = []
    try:
        conn.autocommit = True
        cur = conn.cursor()
        for view in views:
            name = view.split(".")[-1]
            try:
                cur.execute("SELECT pronargs FROM pg_proc WHERE proname = %s", (name,))
                arities = {r[0] for r in cur.fetchall()}
                if arities:  # es una FUNCION de refresco
                    if 2 in arities and d_from and d_to:
                        cur.execute(f"SELECT {view}(%s, %s)", (d_from, d_to))
                        results.append({"view": view, "status": "incremental",
                                        "detail": f"{d_from}..{d_to}"})
                        if logger:
                            logger.info("Refresco INCREMENTAL: %s(%s, %s)", view, d_from, d_to)
                    else:
                        cur.execute(f"SELECT {view}()")
                        results.append({"view": view, "status": "full", "detail": ""})
                        if logger:
                            logger.info("Refresco completo: %s()", view)
                else:  # es una VISTA materializada
                    try:
                        cur.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view}")
                    except Exception:
                        cur.execute(f"REFRESH MATERIALIZED VIEW {view}")
                    results.append({"view": view, "status": "matview", "detail": ""})
                    if logger:
                        logger.info("Vista materializada refrescada: %s", view)
            except Exception as exc:  # noqa: BLE001
                results.append({"view": view, "status": "failed",
                                "detail": str(exc).splitlines()[0][:200]})
                if logger:
                    logger.warning("No se pudo refrescar la vista %s: %s", view, exc)
        cur.close()
    finally:
        conn.close()
    return results


def _range_stats(config, table: str, partition_field: str, start_date: str,
                 end_date: str):
    """(filas_en_rango, ultima_fecha_yyyymmdd, filas_totales). (None,None,None) si falla.

    Type-agnostic: detecta si la columna de partición es date/timestamp o
    entero/texto YYYYMMDD y arma el filtro acorde.
    """
    conn = _pg_connect(config)
    if conn is None:
        return None, None, None
    try:
        cur = conn.cursor()
        cur.execute("SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = %s AND column_name = %s LIMIT 1",
                    (table.split(".")[-1], partition_field))
        r = cur.fetchone()
        dtype = (r[0] if r else "") or ""
        is_date = "date" in dtype or "timestamp" in dtype
        col = f'"{partition_field}"'
        if is_date:
            cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} BETWEEN %s AND %s",
                        (_iso(start_date), _iso(end_date)))
            rows_range = cur.fetchone()[0]
            cur.execute(f"SELECT to_char(MAX({col}), 'YYYYMMDD'), COUNT(*) FROM {table}")
        else:  # entero/texto YYYYMMDD
            cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}::text BETWEEN %s AND %s",
                        (start_date, end_date))
            rows_range = cur.fetchone()[0]
            cur.execute(f"SELECT MAX({col})::text, COUNT(*) FROM {table}")
        last, total = cur.fetchone()
        cur.close()
        return rows_range, _norm_yyyymmdd(last), total
    except Exception:  # noqa: BLE001
        return None, None, None
    finally:
        conn.close()


def _telegram_send(text: str, logger=None) -> bool:
    """POST directo a la API de Telegram (bot cortana). No-op si falta token/chat."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    target = os.environ.get("OS_TELEGRAM_TARGET")
    if not (token and target):
        if logger:
            logger.info("Reporte omitido: falta TELEGRAM_BOT_TOKEN u OS_TELEGRAM_TARGET.")
        return False
    payload = json.dumps({"chat_id": target, "text": text}).encode("utf-8")
    request = urllib.request.Request(
        f"{_TELEGRAM_API}/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=20)  # noqa: S310 (host fijo https)
        return True
    except Exception as exc:  # noqa: BLE001 — el reporte NUNCA debe romper la corrida
        if logger:
            logger.warning("No se pudo enviar el reporte Telegram: %s", exc)
        return False


_REFRESH_ICON = {"incremental": "✅", "full": "✅", "matview": "✅",
                 "skipped": "⏭️", "failed": "❌"}


def report_run(config, *, table: str, partition_field: str, mode: str,
               start_date: str, end_date: str, ok: bool,
               refresh_result=None, logger=None) -> None:
    """Arma y envía el HEALTH-CHECK de la corrida por Telegram (cortana directo).

    Señales (deterministas, sin LLM): carga OK/ERROR, filas del rango (volumen),
    frescura (la fecha esperada llegó a GCP), y estado de refresco por vista. Si
    algo falta, agrega una línea ⚠️ ACCIÓN para que se note de una.
    """
    empresa = config.get("report.empresa", "Dinastia")
    refresh_result = refresh_result or []
    rows_range, last, total = _range_stats(config, table, partition_field,
                                            start_date, end_date)

    # --- señales de alerta ---------------------------------------------------
    warnings: list[str] = []
    if not ok:
        warnings.append("la carga NO terminó bien — revisar journalctl")
    if ok and rows_range == 0:
        warnings.append(f"SIN datos para {start_date}..{end_date} (¿el ERP aún no cargó?)")
    stale = bool(ok and last and last < end_date)
    if stale:
        warnings.append(f"ATRASO: última en GCP {last} < esperada {end_date}")
    failed_refresh = [r for r in refresh_result if r.get("status") == "failed"]
    if failed_refresh:
        warnings.append("una vista NO se refrescó (datos podrían verse viejos en BI)")

    # --- severidad en el encabezado -----------------------------------------
    if not ok or failed_refresh:
        head = "❌"
    elif warnings:
        head = "⚠️"
    else:
        head = "✅"

    # --- cuerpo --------------------------------------------------------------
    fresh_mark = ""
    if last:
        fresh_mark = f"  ⚠️(atrasada, <{end_date})" if stale else "  ✅ al día"
    lines = [
        f"{head} [{empresa}] ETL {table}",
        f"Carga: {'OK' if ok else 'ERROR'}  |  Modo: {mode}  |  Rango: {start_date}..{end_date}",
        f"Filas cargadas (rango): {rows_range if rows_range is not None else '¿?'}",
        f"Última fecha en GCP: {last or '¿?'}{fresh_mark}",
    ]
    if refresh_result:
        for r in refresh_result:
            icon = _REFRESH_ICON.get(r.get("status"), "•")
            det = f": {r['detail']}" if r.get("detail") else ""
            lines.append(f"Refresco {icon} {r['view']} ({r.get('status')}{det})")
    else:
        lines.append("Refresco: (sin vista configurada)")
    lines.append(f"Filas totales en tabla: {total if total is not None else '¿?'}")
    if warnings:
        lines.append("⚠️ ACCIÓN: " + " · ".join(warnings))

    _telegram_send("\n".join(lines), logger=logger)
