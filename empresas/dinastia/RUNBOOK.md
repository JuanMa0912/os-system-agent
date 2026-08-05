# RUNBOOK — Dinastia ETL (cómo correr y mantener)

Guía rápida paso a paso para operar los 3 ETLs de Dinastia en el box
(`servidorUAID`). Todos: **ERP MySQL `BD_BIABLE01` (192.168.30.1) → GCP Cloud SQL
`produxdia`**, tablas `ventas_dinastia`, `margen_dinastia`, `rotacion_dinastia`.

> **Rutas:** `/opt/dinastia-ventas`, `/opt/dinastia-margen`, `/opt/dinastia-rotacion`
> **Secretos:** `/etc/dinastia/dinastia.env` (ERP + GCP + Telegram cortana)
> **Usuario de producción:** `osagent`
> **Historia disponible:** el ERP arranca el **2026-06-14** (no hay datos antes).

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
