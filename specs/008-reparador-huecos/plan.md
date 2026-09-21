# Plan — 008 Reparador de huecos

## Arquitectura

Dos capas, separadas por dónde las mira el CI:

```text
repo (se lintea, se tipa, se testea)
  src/os_system_agent/backfill.py     reglas puras: clasificar y planificar
  tests/test_backfill.py             26 casos, incluido el incidente real

repo (excluido de ruff/leak guard — solo cableado)
  empresas/dinastia/tools/dinastia_ops.py   consultas, configs, subprocesos
  empresas/dinastia/tools/menu.sh           menú interactivo

despliegue (no está en git)
  /opt/dinastia-{ventas,margen,rotacion}/   configs vivas, venvs, run_pipeline.py
```

La herramienta **no** reimplementa la carga: invoca el `run_pipeline.py` de cada
despliegue, que ya sabe cargar, refrescar vistas y reportar a Telegram.

## Flujo de datos

```text
ERP MySQL ──(SELECT agregado por día)──► dinastia_ops
GCP Postgres ──(SELECT agregado por día)──► dinastia_ops
                        │
                        ▼
             backfill.classify_range()      -> veredicto por día
             backfill.plan_ranges()         -> rangos cargables
                        │  (solo con --aplicar)
                        ▼
   subprocess: /opt/dinastia-<x>/.venv/bin/python scripts/run_pipeline.py
                        │
                        ▼
              carga -> refresca vistas -> Telegram
```

El origen se consulta **una vez** con el mismo `FROM/JOIN/WHERE` de los ETLs
(`CMMOVIMIENTO_PDV` + `ITEMS`, `ID_TIPDOC_FC NOT LIKE 'Z%'`), para que "lo que el
ERP tiene" signifique aquí lo mismo que allá.

## Interfaces

| Subcomando | Escribe | Qué hace |
|---|---|---|
| `estado` | no | compara y resume; `--detalle` para una línea por día |
| `reparar` | solo con `--aplicar` | plan + ejecución de los rangos cargables |
| `cargar` | sí, tras confirmar | rango explícito, sin comprobar el origen |
| `refrescar` | vistas | `run_pipeline.py --refresh-only` |

## Credenciales

Ninguna nueva. Todo sale de `/etc/dinastia/dinastia.env` (ERP, GCP, Telegram),
`640 root:osagent`, cargado con `set -a; . …; set +a`. El menú lo carga solo.

## Observabilidad

- JSONL en `~/dinastia-ops.jsonl` (configurable con `DINASTIA_OPS_LOG`): una
  línea por acción con usuario, timestamp, pipeline y rango.
- Las cargas siguen reportando por Telegram desde el `post_run` del ETL.
- Opción 7 del menú enseña las últimas 20 líneas del registro.

## Modos de fallo

| Fallo | Comportamiento |
|---|---|
| Sin entorno cargado | la conexión falla; no se adivina ningún valor |
| ERP devuelve cero para todo el rango | todos los días salen `source_empty` o `both_empty`; el plan queda vacío |
| ERP parcial (una sede de dos) | `risky_sede`; el día se bloquea y se lista |
| Rotación con filas pero sin ventas | `hollow`; se recarga, que solo puede mejorar |
| El runner falla a mitad | el rango queda a medias; volver a correr `reparar` lo detecta y lo repite (es idempotente) |
| No se puede escribir el JSONL | avisa y continúa: no auditar no debe impedir operar |

## Rollback

No hay deshacer para una carga: `replace_by_date` borra y reinserta. La
protección es *no cargar* lo que no se puede recuperar, que es justo lo que
hacen las reglas. Para el tramo irrecuperable (28-jul a 20-ago) la única red es
la copia `*_bkp_*` en GCP, que se crea aparte.

## Tests

- `tests/test_backfill.py`: 26 casos sobre las reglas puras — cada veredicto, la
  tolerancia de la medida, el agrupamiento en rangos, los cortes por día
  bloqueado y el incidente del 2026-09-21 como regresión.
- El cableado (`dinastia_ops.py`) no se testea en CI: depende de dos bases
  reales. Se valida a mano en el box, en modo solo lectura primero.

## Verificación manual (en el box)

```bash
set -a; . /etc/dinastia/dinastia.env; set +a
cd ~/os-system-agent/empresas/dinastia/tools

# 1. solo lectura, sin riesgo
/opt/dinastia-ventas/.venv/bin/python dinastia_ops.py estado --dias 60 --detalle

# 2. el plan, sin ejecutarlo
/opt/dinastia-ventas/.venv/bin/python dinastia_ops.py reparar --dias 60

# 3. comprobar que bloquea el tramo perdido del ERP
/opt/dinastia-ventas/.venv/bin/python dinastia_ops.py estado \
    --desde 20260728 --hasta 20260820 --detalle
#    -> los 24 dias deben salir source_empty y el plan vacio

# 4. el menu
./menu.sh
```

El paso 3 es el que de verdad importa: si alguna vez propone cargar ese tramo,
la herramienta está mal y no debe usarse.
