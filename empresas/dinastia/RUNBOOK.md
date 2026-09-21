# RUNBOOK — Dinastia ETL (cómo correr y mantener)

Guía rápida paso a paso para operar los 3 ETLs de Dinastia en el box
(`servidorUAID`). Todos: **ERP MySQL `BD_BIABLE01` (192.168.30.1) → GCP Cloud SQL
`produxdia`**, tablas `ventas_dinastia`, `margen_dinastia`, `rotacion_dinastia`.

> **Rutas:** `/opt/dinastia-ventas`, `/opt/dinastia-margen`, `/opt/dinastia-rotacion`
> **Secretos:** `/etc/dinastia/dinastia.env` (ERP + GCP + Telegram cortana)
> **Usuario de producción:** `osagent`
> **Historia disponible:** el ERP tiene `CMMOVIMIENTO_PDV` desde el **2025-01-02**
> y `CMRESUMEN_INVENTARIO` desde el lapso **202401**. GCP solo tiene desde
> mediados de junio 2026, así que hay ~19 meses cargables (verificado el
> 2026-08-12 con `MIN/MAX(FECHA_DCTO)`).
>
> ⚠️ Esta línea decía antes "el ERP arranca el 2026-06-14 (no hay datos antes)".
> **Era falso**, y esa frase fue la que sostuvo la decisión de no cargar historia.
> De enero a mayo de 2026 hay ~$41.000 millones de venta que el tablero nunca vio,
> más todo 2025. **La sede 002 abrió en junio de 2026**: hasta mayo el ERP tiene
> una sola sede, lo que explica por qué no se puede comparar año contra año.
>
> ⚠️ **Y esa historia ya NO es continua (2026-09-21):** `CMMOVIMIENTO_PDV` perdió
> del **28-jul al 20-ago de 2026** — julio corta en el 27 y agosto arranca en el 21.
> No es el ETL (el conteo crudo y el que ve el ETL coinciden en todos los días).
> **GCP es hoy la única copia de esos 24 días: nunca recargues ese rango.**

---

## 0. Cargar el entorno (UNA vez por terminal)
```bash
set -a; . /etc/dinastia/dinastia.env; set +a
```
Esto exporta ERP + GCP + Telegram. Necesario para las Formas 1 y 2 (no para systemd).

---

## 1. Correr un pipeline por MODO — carga + refresca vista + reporta Telegram
Reemplaza `<x>` por `ventas`, `margen` o `rotacion`:
```bash
cd /opt/dinastia-<x> && .venv/bin/python scripts/run_pipeline.py --mode daily     # AYER
cd /opt/dinastia-<x> && .venv/bin/python scripts/run_pipeline.py --mode weekly    # últimos 8 días
cd /opt/dinastia-<x> && .venv/bin/python scripts/run_pipeline.py --mode monthly   # 1° del mes .. ayer
```
Ejemplo:
```bash
cd /opt/dinastia-margen && .venv/bin/python scripts/run_pipeline.py --mode daily
```

## 2. Correr un RANGO de fechas específico (backfill) — CON refresco de vista
`run_pipeline` acepta rango explícito: **carga + refresca la vista + reporta** (igual que un modo).
```bash
cd /opt/dinastia-<x> && .venv/bin/python scripts/run_pipeline.py --start-date 20260614 --end-date 20260630
```

Para **backfills largos** (muchos meses) es más eficiente cargar con el **ETL directo**
en loop (rápido, sin refrescar cada mes) y **refrescar UNA vez al final**:
```bash
cd /opt/dinastia-<x> && .venv/bin/python etl/<x>_rango.py --start-date 20250101 --end-date 20250131   # ...loop de meses...
cd /opt/dinastia-<x> && .venv/bin/python scripts/run_pipeline.py --refresh-only                        # <- refresca al terminar
```
> El **ETL directo** (`etl/<x>_rango.py`) **NO** refresca la vista ni reporta (solo carga).
> `--refresh-only` NO carga: solo refresca las vistas/funciones de `refresh_views`.
> **rotación** recorre día por día (~8 s/día); un rango largo tarda → usa `nohup ... &`.

## 2.b Dimensión de tipos de documento (`tipos_documentos_dinastia`)

Copia el catálogo `TIPOS_DOCUMENTOS` del ERP a GCP y le deriva una `clase`
(FACTURA / NOTA_CREDITO / NOTA_DEBITO / DEVOLUCION / OTRO) desde la `DESCRIPCION`.
Vive en el deploy de **ventas** porque es el que corre primero.

```bash
set -a; . /etc/dinastia/dinastia.env; set +a
cd /opt/dinastia-ventas && .venv/bin/python etl/tipos_documentos.py --dry-run  # valida
cd /opt/dinastia-ventas && .venv/bin/python etl/tipos_documentos.py            # carga (~130 filas)
```

**Para qué sirve:** `ventas_dinastia` y `margen_dinastia` ya llevan `id_tipdoc_fc`.
El BI hace `JOIN tipos_documentos_dinastia t ON t.codigo = f.id_tipdoc_fc` y ya
puede cortar factura vs nota sin que nadie mantenga una lista.

**Por qué la clase se deriva y no se lista:** el filtro de los ETLs es
`ID_TIPDOC_FC NOT LIKE 'Z%'` — por exclusión. Un prefijo nuevo entra solo
(verificado: `FP` y `NZ` nacieron el 2026-06-26 y se cargaron sin tocar código).
Si la clase saliera de una lista a mano, ese prefijo llegaría sin clasificar.

> ⚠️ **`VD` = "VENTAS DIARIAS" queda como `OTRO`, a propósito.** Es el asiento
> resumen de lo que el POS ya tiene en detalle (329.441 filas en 5 semanas). Si
> alguien lo tratara como factura, duplicaría toda la venta del punto de venta.

**Cadencia:** el catálogo cambia rarísimo. Basta correrlo a mano cuando aparezca
un tipo nuevo, o colgarlo de un timer semanal. No necesita ser diario.

**Detectar tipos sin clasificar** (lo que caiga en `OTRO` y sí esté facturando):

```sql
SELECT f.id_tipdoc_fc, t.descripcion, t.clase, COUNT(*)
FROM ventas_dinastia f
LEFT JOIN tipos_documentos_dinastia t ON t.codigo = f.id_tipdoc_fc
WHERE t.codigo IS NULL OR t.clase = 'OTRO'
GROUP BY 1,2,3 ORDER BY 4 DESC;
```

Si eso devuelve algo distinto de vacío, hay un tipo nuevo que revisar.

## 3. Vía systemd (igual que el timer automático: background, journal, refresca+reporta)
```bash
sudo systemctl start dinastia-<x>-daily.service          # o -weekly / -monthly
journalctl -u dinastia-<x>-daily.service -n 30 --no-pager # ver el log
systemctl is-active dinastia-<x>-daily.service            # OK = 'inactive' tras terminar (oneshot)
```

## 4. Verificar en GCP (qué quedó cargado)
```bash
set -a; . /etc/dinastia/dinastia.env; set +a
/opt/dinastia-margen/.venv/bin/python - <<'PY'
import os, psycopg2
c=psycopg2.connect(host=os.environ["GCP_CLOUDSQL_HOST"],port=5432,dbname=os.environ["GCP_CLOUDSQL_DB"],
    user=os.environ["GCP_CLOUDSQL_USER"],password=os.environ["GCP_CLOUDSQL_PASSWORD"],sslmode="require")
cur=c.cursor()
for t,col in (("ventas_dinastia","fecha_dcto"),("margen_dinastia","fecha_dcto"),("rotacion_dinastia","fecha_dia")):
    cur.execute(f'SELECT MIN("{col}"), MAX("{col}"), COUNT(*) FROM {t}')
    print(t, cur.fetchone())
cur.close(); c.close()
PY
```

---

## 5. Timers automáticos (ya activos)
| | Daily | Semanal (Sáb) | Mensual (1er domingo) |
|---|---|---|---|
| ventas   | 07:00 | 20:00 | 18:00 |
| margen   | 07:15 | 20:15 | 18:15 |
| rotación | 07:30 | 20:30 | 18:30 |
```bash
systemctl list-timers 'dinastia-*' --all     # ver próximos disparos
```

## 6. Aplicar un CAMBIO al box

**a) Cambio de config** (ej. agregar una vista/función de refresco):
```bash
# edita /opt/dinastia-<x>/config/pipeline_config.yaml (nano) y listo — se lee en cada corrida.
grep -n "refresh_views\|linea_impoconsumo\|table:" /opt/dinastia-<x>/config/pipeline_config.yaml
```

**b) Cambio de código** (viene del repo `dinastia-etl`): re-copiar por Samba el/los
archivo(s) a `/opt/dinastia-<x>/...` (o `git pull` si el box tiene el repo). No hace
falta `daemon-reload`: el código Python se lee en cada corrida. Solo se hace
`daemon-reload` si cambian archivos `.service`/`.timer`.

Tras un cambio que afecte los datos ya cargados (ej. impoconsumo), **recargar**:
```bash
sudo systemctl start dinastia-<x>-monthly.service    # recalcula el mes con el cambio
```

---

## 7. Menú de operación y reparador de huecos

Vive en el repo (`~/os-system-agent/empresas/dinastia/tools/`), no en el despliegue:
se actualiza con `git pull` y lee las configs de `/opt/dinastia-*` sin redesplegar
nada. Nació del incidente del 2026-09-21 (14 días sin cargar que ninguna ventana
automática podía ya reparar, y 24 días que el ERP había perdido y solo viven en GCP).

```bash
cd ~/os-system-agent/empresas/dinastia/tools && ./menu.sh
```

El menú se rebaja solo a `osagent` si lo lanza root, y carga el `.env` por su cuenta.
Por debajo son cuatro subcomandos, usables sueltos:

```bash
set -a; . /etc/dinastia/dinastia.env; set +a
PY=/opt/dinastia-ventas/.venv/bin/python
cd ~/os-system-agent/empresas/dinastia/tools

$PY dinastia_ops.py estado --dias 60 --detalle      # compara ERP vs GCP. NO escribe.
$PY dinastia_ops.py reparar --dias 60               # enseña el plan y se detiene
$PY dinastia_ops.py reparar --dias 60 --aplicar     # lo ejecuta (pide confirmar)
$PY dinastia_ops.py cargar ventas 20260827 20260909 # rango explícito (puerta de atrás)
$PY dinastia_ops.py refrescar margen                # solo vistas
```

**La regla que lo hace seguro:** `reparar` solo carga días donde ganar es lo único
que puede pasar — el destino está vacío (`missing`) o tiene filas con la medida en
cero mientras el origen sí mide (`hollow`, el punto ciego de rotación, que devuelve
~10.600 filas diarias aunque no haya una venta). Se **niega** a tocar los días donde
el origen tiene menos que el destino (`source_empty`, `risky_sede`, `risky_measure`):
los lista y los deja fuera del plan, incluso partiendo un rango en dos para no
atravesarlos. Las reglas viven en `src/os_system_agent/backfill.py`, con tests.

Cada acción queda en `~/dinastia-ops.jsonl` (usuario, hora, pipeline, rango).

> **Lo que NO hace:** decidir sobre un día bloqueado. Eso es siempre humano, porque
> recargarlo significa sustituir lo que hay por lo que el ERP tenga hoy — y el
> 2026-09-21 se comprobó que el ERP puede tener menos.

---

## Notas
- **Idempotente:** `replace_by_date` borra la fecha y reinserta — re-correr un día/rango
  NO duplica, solo sobreescribe. Seguro repetir.
- **Reporte Telegram (health-check):** cada corrida (Formas 1, 2 y 3) manda a cortana un
  reporte que sirve de alerta. El encabezado marca severidad: `✅` todo bien · `⚠️` algo
  falta · `❌` carga o refresco falló. Incluye:
  - **Filas cargadas (rango):** volumen del día/rango. `0` ⇒ ⚠️ "SIN datos" (el ERP
    quizá aún no cargó ese día).
  - **Última fecha en GCP + frescura:** `✅ al día` si llegó la fecha esperada, o
    `⚠️(atrasada)` si GCP va detrás. Esto avisa solo cuando "falta información".
  - **Refresco por vista:** `✅` refrescó (incremental/full/matview), `❌` falló,
    `⏭️` sin conexión. Así se ve de una si la vista quedó vieja.
  - **⚠️ ACCIÓN:** aparece solo cuando hay algo que revisar, con el motivo.
- **Refresco de vistas:** margen (`refresh_margen_dinastia_roll`) y rotación
  (`refresh_rotacion_dinastia_item_periodo_std`) se refrescan solas tras cargar.
  El refresco es **incremental si la función acepta rango** (`f(desde, hasta)`) o
  **completo si es de 0 args** (`f()`); se auto-detecta por la aridad en `pg_proc`.
  Hoy son de 0 args (rebuild completo); el día que BI publique una versión con rango,
  el daily la usa sola sin tocar código.
- **Correr como osagent** (idéntico a producción):
  ```bash
  sudo -u osagent bash -c 'set -a; . /etc/dinastia/dinastia.env; set +a; cd /opt/dinastia-margen && .venv/bin/python scripts/run_pipeline.py --mode daily'
  ```
