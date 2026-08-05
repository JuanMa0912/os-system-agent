# Rotación / Inventario con baja salida — Dinastia

ETL que construye la foto diaria de **rotación de inventario** por ítem × día × sede/bodega
(foto de inventario + ventas del día + última venta), extrayendo del **ERP Siesa/Biable
(MySQL 8.0, `BD_BIABLE01` en `192.168.30.1`, solo lectura)** y cargando a **GCP Cloud SQL
for PostgreSQL**.

Es un **port 1:1** del ETL de Mercamio `_reference/rotacion/etl_rotacion_v3.py` (PostgreSQL):
mismo patrón y grano — mismo ERP Siesa — cambiando solo los **códigos** de Dinastia
(`ID_TIPO='1'`, bodega principal `RIGHT(id_local,2)='01'`, sedes `001`/`002`) y la sintaxis
PG→MySQL. Ver [`ANALYSIS.md`](ANALYSIS.md) para el desglose de la referencia.

---

## Estructura

```
rotacion/
├── config.example.yaml    # config SIN secretos (copiar a config/pipeline_config.yaml)
├── requirements.txt       # pymysql + PyYAML + psycopg2-binary (Cloud SQL Postgres)
├── common/                # framework compartido (copiado de ventas/)
│   ├── db.py              #   origen MySQL (pymysql, streaming, SESSION READ ONLY)
│   ├── loader.py          #   loaders GCP (CloudSqlPostgresLoader, replace_by_date)
│   └── utils.py           #   PipelineConfig (${ENV}), fechas, logging
├── etl/
│   └── rotacion_rango.py  # ETL: consulta 1:1 (foto inventario + ventas) por día
├── scripts/
│   ├── run_pipeline.py    # runner: --mode daily (ayer) | monthly (reconstruir el mes)
│   └── probe_rotacion.py  # smoke test READ-ONLY (cuenta filas + muestra, no escribe)
├── systemd/
│   ├── dinastia-rotacion-daily.service    # oneshot NO-root; ayer; replace_by_date
│   ├── dinastia-rotacion-daily.timer      # diario 07:00
│   ├── dinastia-rotacion-monthly.service  # reconstruccion mes-a-la-fecha
│   └── dinastia-rotacion-monthly.timer    # dia 1, 02:00 (reconstruye el mes cerrado)
├── ANALYSIS.md            # cómo funciona el ETL de Mercamio (referencia del port)
└── README.md
```

## Requisitos e instalación

Python 3.11+, Debian 12. Entorno con **uv** (estándar del proyecto — nunca `pip`/`venv`
a mano):

```bash
cd rotacion
uv venv && uv pip install -r requirements.txt
mkdir -p config && cp config.example.yaml config/pipeline_config.yaml   # y editar
```

## Configuración

Sin secretos en el repo. Las contraseñas se referencian como `${VAR}` y se exportan por
entorno:

- `DINASTIA_MYSQL_USER` / `DINASTIA_MYSQL_PASSWORD` — cuenta read-only del ERP MySQL.
- `GCP_CLOUDSQL_HOST` / `GCP_CLOUDSQL_DB` / `GCP_CLOUDSQL_USER` / `GCP_CLOUDSQL_PASSWORD`
  (`GCP_CLOUDSQL_SSLMODE` opcional, default `require`) — destino Cloud SQL Postgres.
- `DINASTIA_ETL_CONFIG` — ruta al `pipeline_config.yaml` (las units la fijan).

## Uso

```bash
# runner por modo (recorre el rango día a día con replace_by_date):
python scripts/run_pipeline.py --mode daily              # ayer (uso normal)
python scripts/run_pipeline.py --mode monthly            # 1o del mes..ayer (reconstruir)
python scripts/run_pipeline.py --mode daily --dry-run    # valida cableado, no carga

# ETL directo, para un rango arbitrario o un día puntual:
python etl/rotacion_rango.py --date 20260721
python etl/rotacion_rango.py --start-date 20260701 --end-date 20260721

# smoke test READ-ONLY (no escribe en GCP; valida números contra el ERP):
python scripts/probe_rotacion.py --date 20260721
```

Códigos de salida: `0` OK | `1` error | `2` uso inválido | `3` esquema sin confirmar.

### Modos
- **daily** — carga *ayer* (rango de 1 día). El loader borra esa fecha e inserta.
- **monthly** — reprocesa *1° del mes .. ayer* día por día, para **reconstruir el mes**.

> v1 carga la foto de inventario del día + ventas del día + última venta (PDV/inventario).
> Omite el `rolling`/lock de la foto que traía Mercamio; la idempotencia es por
> `(empresa, fecha_dia, sede, bodega_local, id_item)` vía `replace_by_date`.

## Seguridad

- Origen ERP **solo lectura**: el usuario del ERP debe tener únicamente `SELECT`.
- **Sin secretos** en el repo; solo variables de entorno `${VAR}`. `config/`, `*.env` y
  `logs/` están en `.gitignore`.
- Deploy **non-root**: los units systemd corren como usuario dedicado `dinastia`, con
  `ProtectSystem=strict`, `NoNewPrivileges`, `PrivateTmp`, etc. Secretos en un
  `EnvironmentFile` con permisos restringidos, fuera del repo.

## Despliegue (systemd)

```bash
sudo mkdir -p /opt/dinastia-rotacion && sudo rsync -a ./ /opt/dinastia-rotacion/
sudo useradd -r -s /usr/sbin/nologin dinastia
sudo install -d -o dinastia -g dinastia /opt/dinastia-rotacion/logs

sudo install -d -m 750 /etc/dinastia-rotacion
sudo install -o root -g dinastia -m 640 /dev/stdin /etc/dinastia-rotacion/dinastia-rotacion.env <<'ENV'
DINASTIA_MYSQL_USER=...
DINASTIA_MYSQL_PASSWORD=...
GCP_CLOUDSQL_HOST=...
GCP_CLOUDSQL_DB=dinastia
GCP_CLOUDSQL_USER=...
GCP_CLOUDSQL_PASSWORD=...
ENV

sudo cp systemd/dinastia-rotacion-*.service systemd/dinastia-rotacion-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dinastia-rotacion-daily.timer dinastia-rotacion-monthly.timer
systemctl list-timers 'dinastia-rotacion-*'
```

> Cuando corra como servicio en Dinastia, se monitorea desde el agente
> `os-system-agent` (`config/alert-rules.dinastia.example.yml`, `empresa: Dinastia`):
> este repo es el "qué se ejecuta"; el agente solo lo observa y alerta por Telegram.
