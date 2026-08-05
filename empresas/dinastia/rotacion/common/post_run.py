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


# Columnas de medida candidatas, en orden de preferencia. Se autodetecta la primera
# que exista en la tabla; asi los tres pipelines usan el mismo codigo pese a tener
# nombres distintos (ventas/rotacion: venta_sin_impuesto · margen: ven_totales).
_MEASURE_CANDIDATES = ("venta_sin_impuesto", "ven_totales", "venta_con_impuesto",
                       "vlrtot_bru", "total_bruto")

# Dias hacia atras para calcular el volumen "normal" y umbral por debajo del cual
# se avisa. 0.6 = si el dia cargo menos del 60% de la mediana reciente, algo falta.
_VOLUME_LOOKBACK_DAYS = 56  # 8 semanas -> ~8 muestras por dia de la semana
_LOW_VOLUME_RATIO = 0.6


def _yesterday_yyyymmdd(config) -> str:
    """Ayer en la zona del pipeline (o del sistema si el config no la trae)."""
    from datetime import datetime, timedelta
    try:
        now = datetime.now(config.timezone)
    except Exception:  # noqa: BLE001
        now = datetime.now()
    return (now.date() - timedelta(days=1)).strftime("%Y%m%d")


def _days_in_range(start_date: str, end_date: str) -> int:
    """Días inclusivos entre dos YYYYMMDD (mínimo 1).

    El volumen normal se mide por DÍA, así que un rango de 8 días (weekly) debe
    compararse contra 8 veces la mediana diaria, no contra una.
    """
    from datetime import datetime
    try:
        a = datetime.strptime(start_date, "%Y%m%d").date()
        b = datetime.strptime(end_date, "%Y%m%d").date()
        return max(1, (b - a).days + 1)
    except Exception:  # noqa: BLE001
        return 1


def _quality_stats(config, table: str, partition_field: str,
                   start_date: str, end_date: str) -> dict:
    """Senales que el conteo de filas NO ve. Falla-suave: devuelve {} si algo sale mal.

    Tres cosas, cada una nacida de un fallo real en produccion:

    - ``measure_sum``: filas > 0 con la medida en CERO. El 2026-08-03 rotacion cargo
      10.365 filas con la venta en 0 (el ERP aun no tenia el dia) y el health-check
      la dio por buena, porque solo miraba que hubiera filas.
    - ``median_rows``: carga PARCIAL. El 2026-08-01 ventas cargo 8.381 de las 14.066
      filas reales — 60% del dia — y tambien paso como ✅. Comparar contra la mediana
      reciente lo delata.
    - ``yesterday_rows``: responder "¿cargo ayer?" sin que el operador tenga que
      calcularlo mirando una fecha.
    """
    conn = _pg_connect(config)
    if conn is None:
        return {}
    out: dict = {}
    try:
        cur = conn.cursor()
        tbl = table.split(".")[-1]
        cur.execute("SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = %s AND column_name = %s LIMIT 1",
                    (tbl, partition_field))
        r = cur.fetchone()
        is_date = "date" in ((r[0] if r else "") or "") or "timestamp" in ((r[0] if r else "") or "")
        col = f'"{partition_field}"'
        # El filtro por rango cambia segun la columna sea date/timestamp o texto YYYYMMDD.
        where = f"{col} BETWEEN %s AND %s" if is_date else f"{col}::text BETWEEN %s AND %s"
        rng = (_iso(start_date), _iso(end_date)) if is_date else (start_date, end_date)

        # -- medida: ¿hay filas pero sin valor? --------------------------------
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = %s AND column_name = ANY(%s)",
                    (tbl, list(_MEASURE_CANDIDATES)))
        found = {row[0] for row in cur.fetchall()}
        measure = next((c for c in _MEASURE_CANDIDATES if c in found), None)
        out["measure_col"] = measure
        if measure:
            cur.execute(f'SELECT COALESCE(SUM("{measure}"), 0) FROM {table} WHERE {where}', rng)
            out["measure_sum"] = float(cur.fetchone()[0] or 0)

        # -- volumen normal: mediana del MISMO DIA DE LA SEMANA ------------------
        # Medido en Dinastia (jun-jul 2026), el volumen depende muchisimo del dia:
        # Sab 9.113 · Jue 7.020 · Mar 6.572 · Mie 6.011 · Fri 5.574 · Dom 5.382 · Lun 4.444.
        # Contra una mediana global (7.720) un domingo normal da 70% y un lunes 58%:
        # falsas alarmas garantizadas. Comparado contra su propio dia de la semana,
        # el margen es real y el umbral no dispara solo.
        #
        # Solo para rangos de UN dia: en un weekly (8 dias) la suma se promedia y
        # deja de decir nada util. El daily es el que corre a diario y el que debe
        # avisar el mismo dia.
        if _days_in_range(start_date, end_date) == 1:
            if is_date:
                sub = (f'SELECT {col} d, COUNT(*) n FROM {table} '
                       f'WHERE {col} >= (%s::date - %s) AND {col} < %s::date '
                       f'AND EXTRACT(DOW FROM {col}) = EXTRACT(DOW FROM %s::date) GROUP BY 1')
            else:
                sub = (f'SELECT {col} d, COUNT(*) n FROM {table} '
                       f"WHERE {col}::text >= to_char(%s::date - %s, 'YYYYMMDD') "
                       f'AND {col}::text < %s '
                       f"AND EXTRACT(DOW FROM to_date({col}::text, 'YYYYMMDD')) "
                       f'= EXTRACT(DOW FROM %s::date) GROUP BY 1')
            params = (_iso(end_date), _VOLUME_LOOKBACK_DAYS,
                      _iso(start_date) if is_date else start_date, _iso(start_date))
            cur.execute(f"SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY n) FROM ({sub}) s",
                        params)
            med = cur.fetchone()[0]
            out["median_rows"] = float(med) if med is not None else None

        # -- ¿cargo ayer? -------------------------------------------------------
        yday = _yesterday_yyyymmdd(config)
        out["yesterday"] = yday
        cur.execute(f'SELECT COUNT(*) FROM {table} WHERE {col}::text = %s',
                    (_iso(yday) if is_date else yday,))
        out["yesterday_rows"] = cur.fetchone()[0]

        cur.close()
        return out
    except Exception:  # noqa: BLE001 — el health-check nunca rompe la corrida
        return out
    finally:
        conn.close()


def _money(v) -> str:
    """1469196965.0 -> '1.469.196.965' (separador de miles con punto)."""
    try:
        return f"{int(round(float(v))):,}".replace(",", ".")
    except Exception:  # noqa: BLE001
        return str(v)


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

    # --- señales que el conteo de filas NO ve --------------------------------
    q = _quality_stats(config, table, partition_field, start_date, end_date)
    measure_col, measure_sum = q.get("measure_col"), q.get("measure_sum")
    yday, yrows = q.get("yesterday"), q.get("yesterday_rows")

    # Hay filas, pero la medida esta en cero (rotacion, 2026-08-03).
    if ok and rows_range and measure_sum == 0:
        warnings.append(f"{rows_range} filas pero {measure_col} = 0 "
                        "(¿el ERP aún no cerró el día?)")

    # Muy por debajo del volumen normal: carga parcial (ventas, 2026-08-01).
    expected = ratio = None
    if ok and rows_range and q.get("median_rows"):
        expected = q["median_rows"] * _days_in_range(start_date, end_date)
        ratio = (rows_range / expected) if expected else None
        if ratio is not None and ratio < _LOW_VOLUME_RATIO:
            warnings.append(f"VOLUMEN BAJO: {rows_range} filas vs ~{int(expected)} "
                            f"normales ({ratio:.0%}) — ¿carga parcial?")

    # ¿Cargo ayer? Se avisa solo si el rango deberia haberlo cubierto.
    if ok and yrows == 0 and yday and start_date <= yday <= end_date:
        warnings.append(f"AYER ({yday}) quedó SIN datos")

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
    # Responder "¿cargó ayer?" sin que nadie tenga que deducirlo de una fecha.
    if yday and yrows is not None:
        lines.append(f"Ayer ({yday}): {yrows} filas  "
                     f"{'✅ cargado' if yrows else '❌ NO cargado'}")
    if ratio is not None:
        lines.append(f"Volumen: {rows_range} vs ~{int(expected)} normal ({ratio:.0%})  "
                     f"{'✅' if ratio >= _LOW_VOLUME_RATIO else '⚠️'}")
    if measure_col and measure_sum is not None:
        lines.append(f"{measure_col} del rango: {_money(measure_sum)}  "
                     f"{'✅' if measure_sum else '⚠️ EN CERO'}")
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
