#!/usr/bin/env python3
"""dinastia_ops — operar los 3 ETLs de Dinastia a mano, sin perder datos.

Nace del incidente del 2026-09-21: el ERP estuvo 14 dias sin entregar datos, las
corridas cargaron cero sin fallar, y cuando el dato reaparecio ninguna ventana
automatica llegaba ya tan atras (daily=D-4..D-1, weekly=8 dias, monthly=mes en
curso). Nadie se entero. Y al mismo tiempo el ERP habia PERDIDO 24 dias que solo
sobreviven en GCP: recargar el rango completo a ciegas los habria borrado.

De ahi las dos caras de esta herramienta:

  estado    lee y compara, dia a dia, ERP contra GCP. No escribe nada.
  reparar   carga SOLO los dias donde ganar es lo unico que puede pasar, y se
            niega a tocar los dias donde recargar significaria perder.

Las reglas de decision NO viven aqui: viven en ``src/os_system_agent/backfill.py``
del repo, que si pasa por ruff, mypy y pytest. Aqui solo esta el cableado con el
box (consultas, configs desplegadas, subprocesos).

Uso:
    python3 dinastia_ops.py estado   [--dias 60] [--pipeline ventas|margen|rotacion|todos]
    python3 dinastia_ops.py estado   --desde 20260801 --hasta 20260920
    python3 dinastia_ops.py reparar  [--dias 60] [--pipeline ...] [--aplicar] [--si]
    python3 dinastia_ops.py cargar   ventas 20260827 20260909
    python3 dinastia_ops.py refrescar margen

Sin ``--aplicar`` nada escribe: ``reparar`` enseña el plan y se detiene.
Requiere el entorno cargado:  set -a; . /etc/dinastia/dinastia.env; set +a
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

# --- donde vive cada cosa ---------------------------------------------------
# El repo (para las reglas puras) y los despliegues (para configs y drivers).
REPO_ROOT = Path(__file__).resolve().parents[3]
DEPLOY_BASE = os.environ.get("DINASTIA_DEPLOY_BASE", "/opt")
AUDIT_LOG = Path(os.environ.get("DINASTIA_OPS_LOG", Path.home() / "dinastia-ops.jsonl"))

sys.path.insert(0, str(REPO_ROOT / "src"))


@dataclass(frozen=True)
class Pipeline:
    """Un ETL: donde esta desplegado y como se ve su tabla en el destino."""

    nombre: str
    tabla: str            # nombre por defecto; manda el de su config si existe
    col_fecha: str
    col_sede: str
    col_medida: str
    compara_medida: bool  # solo ventas mide lo mismo que el ERP crudo

    @property
    def deploy(self) -> Path:
        return Path(DEPLOY_BASE) / f"dinastia-{self.nombre}"

    @property
    def python(self) -> Path:
        return self.deploy / ".venv" / "bin" / "python"

    @property
    def config_path(self) -> Path:
        return self.deploy / "config" / "pipeline_config.yaml"


# `margen` usa id_co, `ventas` centro_operacion y `rotacion` sede: las tres tablas
# ya existen y renombrarlas no esta sobre la mesa (mismo criterio que post_run).
PIPELINES: dict[str, Pipeline] = {
    "ventas": Pipeline("ventas", "ventas_dinastia", "fecha_dcto",
                       "centro_operacion", "venta_con_impuesto", True),
    "margen": Pipeline("margen", "margen_dinastia", "fecha_dcto",
                       "id_co", "ven_totales", False),
    "rotacion": Pipeline("rotacion", "rotacion_dinastia", "fecha_dia",
                         "sede", "venta_sin_impuesto", False),
}

# Origen comun a los tres: el mismo FROM/JOIN/WHERE que usan los ETL, para que
# "lo que el ERP tiene" signifique lo mismo aqui que alla.
SQL_ORIGEN = (
    "SELECT TRIM(m.FECHA_DCTO) AS dia, COUNT(*) AS filas, "
    "       GROUP_CONCAT(DISTINCT m.ID_CO ORDER BY m.ID_CO) AS sedes, "
    "       COALESCE(SUM(m.VEN_NETAS + m.IMP_NETOS), 0) AS medida "
    "FROM CMMOVIMIENTO_PDV m "
    "JOIN ITEMS i ON i.ID_ITEM = m.ID_ITEM AND i.ID_EXT_ITM = m.ID_ITMEXT "
    "WHERE TRIM(m.FECHA_DCTO) BETWEEN %s AND %s AND m.ID_TIPDOC_FC NOT LIKE 'Z%%' "
    "GROUP BY 1 ORDER BY 1"
)

ICONO = {
    "ok": "  ", "missing": "->", "both_empty": "  ",
    "hollow": "~>", "source_empty": "!!", "risky_sede": "!!", "risky_measure": "!!",
}


def _importar_del_deploy():
    """Trae ``common.*`` de un despliegue. Los tres son copias identicas."""
    base = PIPELINES["ventas"].deploy
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    from common.db import build_source
    from common.post_run import _pg_connect
    from common.utils import PipelineConfig
    return build_source, _pg_connect, PipelineConfig


def _tabla_de(pipe: Pipeline, PipelineConfig) -> str:
    """El nombre de tabla manda en la config desplegada, no en este archivo."""
    try:
        cfg = PipelineConfig(str(pipe.config_path))
        return str(cfg.get("target.cloudsql_postgres.table", pipe.tabla) or pipe.tabla)
    except Exception:
        return pipe.tabla


def _auditar(accion: str, detalle: dict) -> None:
    """Una linea JSON por accion. Sin esto, 'lo corri yo' no es verificable."""
    registro = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "usuario": os.environ.get("USER", "?"),
        "accion": accion,
        **detalle,
    }
    try:
        with AUDIT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(registro, ensure_ascii=False) + "\n")
    except OSError as exc:  # nunca romper la operacion por no poder auditar
        print(f"  (aviso: no se pudo escribir el registro en {AUDIT_LOG}: {exc})")


def _rango_por_defecto(dias: int) -> tuple[str, str]:
    """Hasta AYER, nunca hoy: el dia en curso todavia se esta escribiendo."""
    ayer = datetime.now().date() - timedelta(days=1)
    return (ayer - timedelta(days=dias - 1)).strftime("%Y%m%d"), ayer.strftime("%Y%m%d")


# --- lectura ---------------------------------------------------------------

def leer_origen(ini: str, fin: str) -> dict:
    from os_system_agent.backfill import DayStats
    build_source, _, PipelineConfig = _importar_del_deploy()
    src = build_source(PipelineConfig(str(PIPELINES["ventas"].config_path)))
    out = {}
    for r in src.fetch_all(SQL_ORIGEN, (ini, fin)):
        dia = str(r["dia"]).strip()
        sedes = frozenset(s for s in str(r["sedes"] or "").split(",") if s)
        out[dia] = DayStats(day=dia, rows=int(r["filas"]),
                            sites=sedes, measure=float(r["medida"] or 0))
    return out


def leer_destino(pipe: Pipeline, ini: str, fin: str) -> dict:
    from os_system_agent.backfill import DayStats
    _, _pg_connect, PipelineConfig = _importar_del_deploy()
    cfg = PipelineConfig(str(PIPELINES["ventas"].config_path))
    tabla = _tabla_de(pipe, PipelineConfig)
    con = _pg_connect(cfg)
    out = {}
    try:
        with con, con.cursor() as cur:
            # replace(...) normaliza date y text al mismo YYYYMMDD: `fecha_dia` es
            # date y `fecha_dcto` es texto, y compararlos crudos falla en silencio.
            cur.execute(
                f"SELECT replace({pipe.col_fecha}::text,'-','') AS dia, COUNT(*), "
                f"       string_agg(DISTINCT {pipe.col_sede}, ','), "
                f"       COALESCE(SUM({pipe.col_medida}), 0) "
                f"FROM {tabla} "
                f"WHERE replace({pipe.col_fecha}::text,'-','') BETWEEN %s AND %s "
                f"GROUP BY 1 ORDER BY 1", (ini, fin))
            for dia, filas, sedes, medida in cur.fetchall():
                sedes_set = frozenset(s.strip() for s in str(sedes or "").split(",") if s.strip())
                out[str(dia)] = DayStats(day=str(dia), rows=int(filas),
                                         sites=sedes_set, measure=float(medida or 0))
    finally:
        con.close()
    return out


def analizar(pipe: Pipeline, ini: str, fin: str):
    from os_system_agent.backfill import classify_range, days_in_range
    origen = leer_origen(ini, fin)
    destino = leer_destino(pipe, ini, fin)
    dias = days_in_range(ini, fin)
    veredictos = classify_range(dias, origen, destino, compare_measure=pipe.compara_medida)
    return origen, destino, veredictos


# --- comandos --------------------------------------------------------------

def cmd_estado(pipes: list[Pipeline], ini: str, fin: str, detalle: bool) -> int:
    from os_system_agent.backfill import Verdict, blocked_days, plan_ranges, summarize
    for pipe in pipes:
        print(f"\n=== {pipe.nombre.upper()}  {ini}..{fin} ===")
        origen, destino, veredictos = analizar(pipe, ini, fin)
        if detalle:
            print(f"{'dia':<9} {'ERP':>9} {'sedes':>7} | {'GCP':>9} {'sedes':>7}  veredicto")
            for dia in sorted(veredictos):
                o, d = origen.get(dia), destino.get(dia)
                print(f"{dia:<9} {(o.rows if o else 0):>9,} "
                      f"{(','.join(sorted(o.sites)) if o else ''):>7} | "
                      f"{(d.rows if d else 0):>9,} "
                      f"{(','.join(sorted(d.sites)) if d else ''):>7}  "
                      f"{ICONO[veredictos[dia]]} {veredictos[dia]}")
        resumen = summarize(veredictos.values())
        print("  resumen:", ", ".join(f"{k}={v}" for k, v in sorted(resumen.items())))
        plan = plan_ranges(veredictos)
        print("  a cargar:", ", ".join(f"{a}..{b}" for a, b in plan) or "nada")
        bloqueados = blocked_days(veredictos)
        if bloqueados:
            print(f"  BLOQUEADOS ({len(bloqueados)}) — recargarlos PERDERIA datos:")
            for dia, motivo in bloqueados[:10]:
                print(f"    {dia}  {motivo}")
            if len(bloqueados) > 10:
                print(f"    ... y {len(bloqueados) - 10} mas")
        faltan = resumen.get(Verdict.MISSING, 0) + resumen.get(Verdict.HOLLOW, 0)
        _auditar("estado", {"pipeline": pipe.nombre, "rango": [ini, fin],
                            "faltan": faltan, "bloqueados": len(bloqueados)})
    return 0


def _correr_rango(pipe: Pipeline, ini: str, fin: str) -> int:
    """Llama al runner desplegado: carga + refresca vistas + reporta a Telegram."""
    cmd = [str(pipe.python), "scripts/run_pipeline.py", "--start-date", ini, "--end-date", fin]
    print(f"  $ cd {pipe.deploy} && {' '.join(cmd[1:])}")
    proc = subprocess.run(cmd, cwd=str(pipe.deploy), check=False)  # noqa: S603
    return proc.returncode


def cmd_reparar(pipes: list[Pipeline], ini: str, fin: str, aplicar: bool, si: bool) -> int:
    from os_system_agent.backfill import blocked_days, plan_ranges
    rc_total = 0
    for pipe in pipes:
        print(f"\n=== {pipe.nombre.upper()}  {ini}..{fin} ===")
        _, _, veredictos = analizar(pipe, ini, fin)
        plan = plan_ranges(veredictos)
        bloqueados = blocked_days(veredictos)
        if bloqueados:
            print(f"  {len(bloqueados)} dias BLOQUEADOS, fuera del plan a proposito "
                  f"(p.ej. {bloqueados[0][0]}: {bloqueados[0][1]})")
        if not plan:
            print("  nada que reparar.")
            continue
        print("  plan:", ", ".join(f"{a}..{b}" for a, b in plan))
        if not aplicar:
            print("  (simulacion — para ejecutarlo de verdad, agrega --aplicar)")
            continue
        if not si and sys.stdin.isatty():
            if input("  escribe APLICAR para continuar: ").strip() != "APLICAR":
                print("  cancelado.")
                continue
        for a, b in plan:
            _auditar("reparar", {"pipeline": pipe.nombre, "rango": [a, b]})
            rc = _correr_rango(pipe, a, b)
            rc_total = rc_total or rc
            print(f"  -> {pipe.nombre} {a}..{b} rc={rc}")
    return rc_total


def cmd_cargar(pipe: Pipeline, ini: str, fin: str, si: bool) -> int:
    """Rango explicito, sin preguntar al ERP. Es la puerta de atras: avisa."""
    print(f"OJO: carga directa {pipe.nombre} {ini}..{fin} sin comprobar el origen.")
    print("     replace_by_date BORRA cada dia y reinserta lo que traiga el ERP.")
    if not si and sys.stdin.isatty():
        if input("escribe APLICAR para continuar: ").strip() != "APLICAR":
            print("cancelado.")
            return 1
    _auditar("cargar", {"pipeline": pipe.nombre, "rango": [ini, fin]})
    return _correr_rango(pipe, ini, fin)


def cmd_refrescar(pipe: Pipeline) -> int:
    cmd = [str(pipe.python), "scripts/run_pipeline.py", "--refresh-only"]
    _auditar("refrescar", {"pipeline": pipe.nombre})
    return subprocess.run(cmd, cwd=str(pipe.deploy), check=False).returncode  # noqa: S603


# --- cli -------------------------------------------------------------------

def _pipes(nombre: str) -> list[Pipeline]:
    return list(PIPELINES.values()) if nombre == "todos" else [PIPELINES[nombre]]


def main() -> int:
    p = argparse.ArgumentParser(prog="dinastia_ops", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def _rango(sp):
        sp.add_argument("--dias", type=int, default=60)
        sp.add_argument("--desde")
        sp.add_argument("--hasta")
        sp.add_argument("--pipeline", choices=[*PIPELINES, "todos"], default="todos")

    sp = sub.add_parser("estado", help="comparar ERP vs GCP (no escribe nada)")
    _rango(sp)
    sp.add_argument("--detalle", action="store_true", help="una linea por dia")

    sp = sub.add_parser("reparar", help="cargar solo los dias que faltan")
    _rango(sp)
    sp.add_argument("--aplicar", action="store_true", help="ejecutar de verdad")
    sp.add_argument("--si", action="store_true", help="no preguntar (para timers)")

    sp = sub.add_parser("cargar", help="rango explicito, sin comprobar el origen")
    sp.add_argument("pipeline", choices=list(PIPELINES))
    sp.add_argument("desde")
    sp.add_argument("hasta")
    sp.add_argument("--si", action="store_true")

    sp = sub.add_parser("refrescar", help="solo refrescar vistas/funciones")
    sp.add_argument("pipeline", choices=list(PIPELINES))

    a = p.parse_args()

    if a.cmd in ("estado", "reparar"):
        if bool(a.desde) != bool(a.hasta):
            print("ERROR: --desde y --hasta van juntos.", file=sys.stderr)
            return 2
        ini, fin = (a.desde, a.hasta) if a.desde else _rango_por_defecto(a.dias)
        if a.cmd == "estado":
            return cmd_estado(_pipes(a.pipeline), ini, fin, a.detalle)
        return cmd_reparar(_pipes(a.pipeline), ini, fin, a.aplicar, a.si)

    if a.cmd == "cargar":
        return cmd_cargar(PIPELINES[a.pipeline], a.desde, a.hasta, a.si)
    return cmd_refrescar(PIPELINES[a.pipeline])


if __name__ == "__main__":
    sys.exit(main())
