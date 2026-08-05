# ETL margenes — Dinastia (informe de variacion / margenes)

Carga **margen por linea de factura** (ingreso - costo) desde el ERP **MySQL 8.0**
(Siesa/Biable, base `BD_BIABLE01` en `192.168.30.1`, **solo lectura**) hacia **GCP
Cloud SQL for PostgreSQL**, para el informe de margenes y su **variacion** en el tiempo.

Es un **port 1:1** del ETL de Mercamio (`_reference/margen/cargar_margen.py`, que es
**PostgreSQL**): mismos campos y grano — mismo ERP Siesa — cambiando solo los **codigos**
de Dinastia (`ID_TIPO='1'`, linea impoconsumo `'50'`) y la sintaxis PG->MySQL. Ver el
analisis del original en [`ANALYSIS.md`](ANALYSIS.md).

## Estructura

```
margen/
├── ANALYSIS.md            # como funciona el ETL de Mercamio (referencia del port)
├── README.md              # este archivo
├── requirements.txt       # pymysql + PyYAML + psycopg2-binary (Cloud SQL Postgres)
├── config.example.yaml    # config SIN secretos (copiar a config/pipeline_config.yaml)
├── common/                # framework compartido (copiado de ventas/)
│   ├── db.py              #   origen MySQL (pymysql, streaming, SESSION READ ONLY)
│   ├── loader.py          #   loaders GCP (CloudSqlPostgresLoader, replace_by_date)
│   └── utils.py           #   PipelineConfig (${ENV}), fechas, logging
├── etl/
│   └── margen_rango.py    # ETL: consulta 1:1 + carga idempotente por dia
├── scripts/
│   ├── run_pipeline.py    # runner: --mode daily (ayer) | monthly (reconstruir el mes)
│   └── probe_margen.py    # smoke test READ-ONLY (cuenta filas + totales, no escribe)
└── systemd/
    ├── dinastia-margen-daily.service    # oneshot NO-root; ayer; replace_by_date
    ├── dinastia-margen-daily.timer      # diario 07:15
    ├── dinastia-margen-monthly.service  # reconstruccion mes-a-la-fecha
    └── dinastia-margen-monthly.timer    # dia 1, 02:30 (reconstruye el mes cerrado)
```

## Como funciona

1. **Origen (solo lectura):** `common/db.py` abre MySQL con `pymysql` (cursor server-side,
   `SET SESSION TRANSACTION READ ONLY`). El usuario del ERP debe tener **solo SELECT**.
2. **Extraccion:** `etl/margen_rango.py` ejecuta la consulta 1:1 de Mercamio (movimiento
   unificado `CMMOVIMIENTO_PDV`, costo de kits via `V_KITS`, terceros, ajuste de impoconsumo
   en la linea `50`), filtrando `ID_TIPO='1'`.
3. **Carga idempotente:** el loader usa `write_mode: replace_by_date` — por cada dia del
   rango **BORRA esa fecha** en el destino y **REINSERTA** (delete-then-insert). Re-correr un
   dia **no duplica**.
4. **Destino:** `common/loader.py::CloudSqlPostgresLoader` (psycopg2) escribe a la tabla
   `margen` en Cloud SQL Postgres.

## Configuracion

Sin secretos en el repo. Copiar la plantilla:

```bash
mkdir -p config && cp config.example.yaml config/pipeline_config.yaml   # y editar
```

Las contrasenas **nunca van en el YAML**: se referencian como `${VAR}` y se exportan por
entorno. Variables necesarias:

- `DINASTIA_MYSQL_USER` / `DINASTIA_MYSQL_PASSWORD` — cuenta read-only del ERP MySQL.
- `GCP_CLOUDSQL_HOST` / `GCP_CLOUDSQL_DB` / `GCP_CLOUDSQL_USER` / `GCP_CLOUDSQL_PASSWORD`
  (`GCP_CLOUDSQL_SSLMODE` opcional, default `require`) — destino Cloud SQL Postgres.
- `DINASTIA_ETL_CONFIG` — ruta al `pipeline_config.yaml` (las units la fijan).

## Instalacion (Debian 12)

```bash
uv venv && uv pip install -r requirements.txt   # estandar del proyecto (no pip/venv a mano)
mkdir -p config && cp config.example.yaml config/pipeline_config.yaml   # y editar
```

## Uso

```bash
# runner por modo (calcula el rango y carga con replace_by_date):
python scripts/run_pipeline.py --mode daily              # ayer (uso normal)
python scripts/run_pipeline.py --mode monthly            # 1o del mes..ayer (reconstruir)
python scripts/run_pipeline.py --mode daily --dry-run    # valida cableado, no carga

# ETL directo, para un rango arbitrario o un dia puntual:
python etl/margen_rango.py --date 20260721
python etl/margen_rango.py --start-date 20260701 --end-date 20260721
python etl/margen_rango.py --dry-run

# smoke test READ-ONLY (no escribe en GCP; valida numeros contra el ERP):
python scripts/probe_margen.py --date 20260721
```

Codigos de salida: `0` OK | `1` error | `2` uso invalido | `3` esquema sin confirmar.

## Programacion (systemd, usuario NO-root)

```bash
# Usuario dedicado (una vez)
sudo useradd -r -s /usr/sbin/nologin dinastia

# Deploy en /opt/dinastia-margen (ruta que asumen las units)
sudo install -d -o dinastia -g dinastia /opt/dinastia-margen/logs

# Secretos en un env-file con permisos restringidos (NO en el repo)
sudo install -d -m 750 /etc/dinastia-margen
sudo sh -c 'umask 077; cat > /etc/dinastia-margen/dinastia-margen.env' <<'EOF'
DINASTIA_MYSQL_USER=...
DINASTIA_MYSQL_PASSWORD=...
GCP_CLOUDSQL_HOST=...
GCP_CLOUDSQL_DB=dinastia
GCP_CLOUDSQL_USER=...
GCP_CLOUDSQL_PASSWORD=...
EOF
sudo chown root:dinastia /etc/dinastia-margen/dinastia-margen.env
sudo chmod 640 /etc/dinastia-margen/dinastia-margen.env

# Instalar y activar timers (diario + reconstruccion mensual)
sudo cp systemd/dinastia-margen-*.service systemd/dinastia-margen-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dinastia-margen-daily.timer dinastia-margen-monthly.timer

systemctl list-timers 'dinastia-margen-*'
journalctl -u dinastia-margen-daily.service -n 80 --no-pager
sudo systemctl start dinastia-margen-monthly.service   # reconstruir el mes a mano
```

## Seguridad

- Origen MySQL **solo lectura** (usuario SELECT-only + sesion READ ONLY).
- Sin secretos en el repo ni en `pipeline_config.yaml`; solo variables de entorno `${VAR}`.
- Servicio systemd **no-root** y endurecido (`ProtectSystem=strict`, `NoNewPrivileges`,
  `PrivateTmp`, etc.).

## Convenciones

Sigue el patron de los ETL de Dinastia (`../ventas/`, `../rotacion/`): config YAML con
`${ENV}`, CLI `--date/--start-date/--end-date/--dry-run`, idempotencia por dia
(`replace_by_date`), systemd oneshot + timer (daily + monthly). Comparte el framework
`common/` con ventas y rotacion.
