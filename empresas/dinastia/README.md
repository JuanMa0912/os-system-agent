# dinastia-etl

ETL pipelines para **Dinastia** — extraen del ERP (MySQL 8.0, base `BD_BIABLE01`,
Siesa/Biable en `192.168.30.1`) y cargan a **GCP**. Se desarrollan aquí y se despliegan
en el servidor de Dinastia (`servidorUAID`, Debian 12).

> Proyecto **separado** del agente de monitoreo (`os-system-agent`): aquí viven los ETLs
> (el "qué se ejecuta"); el agente solo los **monitorea**. Cuando un ETL exista como
> servicio systemd en Dinastia, se registra en el catálogo del agente (`empresa: Dinastia`).

## Origen vs. referencia

Estos pipelines se **adaptan** de los de Mercamio (en `_reference/`), que son
**PostgreSQL**. Dinastia es **MySQL**, así que la capa de extracción se reescribe
(`pymysql`, SQL de MySQL, tablas del ERP). Se reusa el **patrón** (orquestador, config,
reintentos, reportes JSON, systemd).

## Pipelines

| Carpeta | Reporte | Referencia Mercamio |
|---------|---------|---------------------|
| `ventas/`   | Rentabilidad por línea | `_reference/ventas/` (orquestador + runner + utils + ETLs por categoría) |
| `rotacion/` | Inventario con baja salida | `_reference/rotacion/etl_rotacion_v3.py` |
| `margen/`   | Informe de variación / márgenes | `_reference/margen/cargar_margen.py` |

## Estado

Scaffold + análisis generados por agentes (cada carpeta trae `ANALYSIS.md`, un scaffold
MySQL, y `SCHEMA_NEEDS.md`). **Pendiente para completar los ETLs reales:**
1. Esquema a nivel de **columnas** de las tablas del ERP (hoy solo tenemos los nombres).
2. **Destino GCP** definido (BigQuery vs Cloud SQL Postgres).
3. **Reglas de negocio** de cada reporte.

## Seguridad

Sin credenciales en el repo. Config por archivos `*.example` / variables de entorno.
Origen MySQL: **solo lectura** (SELECT). Deploy en Dinastia bajo usuario no-root.
