# ANALYSIS — Mercamio `etl_rotacion_v3.py` (referencia PostgreSQL)

Análisis del pipeline de rotación de Mercamio que sirve de base al scaffold MySQL de
Dinastia. Todas las citas apuntan a
`_reference/rotacion/etl_rotacion_v3.py` (1360 líneas, PostgreSQL / psycopg2).

> **Para qué sirve este doc:** entender exactamente qué hace Mercamio para reproducir
> el PATRÓN en Dinastia (MySQL) sin arrastrar detalles específicos de PostgreSQL.

---

## 1. Topología y propósito

- **Origen:** `192.168.35.217` — réplica de las BD POS de 3 empresas (`mercamio`, `mtodo`,
  `bogota`). Es una réplica, no el ERP directo, por eso el ETL se permite `SET work_mem='256MB'`
  (líneas 782-785).
- **Destino:** `192.168.35.232`, BD `produXdia`, tabla `public.rotacion_base_item_dia_sede`
  (líneas 5-6, 62).
- **Propósito:** construir una **foto diaria por ítem × sede × bodega** que combina las
  **ventas del día**, la **foto de inventario** (stock disponible + últimas fechas) y el
  **maestro de ítem** (categoría, líneas, costo). El portal/BI encima calcula la rotación y
  los "días sin venta" de forma dinámica.

## 2. Grano y llave primaria

PK = **(empresa, fecha_dia, sede, bodega_local, id_item)** (líneas 13-14, 151).
No hay `id_ext_itm` en la PK: el ítem se identifica por esa tupla. `fecha_dia` = un día.

## 3. Tablas de origen y su rol

| Tabla (PostgreSQL)             | Rol en el ETL                                                        | Columnas usadas (referencia)                                                                 |
|--------------------------------|---------------------------------------------------------------------|----------------------------------------------------------------------------------------------|
| `public.items`                 | Maestro de ítem                                                      | `id_item`, `descripcion`, `unimed_inv_1`, `unimed_com`, `id_tipo`, `id_linea1`, `id_linea2`, `costo_act_acum`, `ultimo_costo_ed` |
| `public.categorias`            | Nombre de categoría (por `id_tipo`)                                  | `id_tipo`, `cmtipinv_descripcion`                                                             |
| `public.lineas`                | Nombre de línea/sublínea (join por `id_linea`+`id_tipo`)             | `id_linea`, `id_tipo`, `cmlineas_descripcion`                                                 |
| `public.cmresumen_inventario`  | Foto mensual de inventario (stock + fechas + costo)                 | `id_co`, `id_local`, `id_item`, `lapso_doc`, `can_disponible`, `fecha_ultcom`, `fecha_ultent`, `fecha_ultvta`, `costo_uni` |
| `public.cmmovimiento_pdv`      | Movimientos de venta POS (salidas)                                   | `id_co`, `fecha_dcto`, `id_local`, `id_item`, `id_unidad`, `cantidad`, `vlrtot_bru`, `tot_costo`, `docto_acumulacion` |
| `public.centro_operacion`      | Nombre de la sede                                                    | `codigo`, `descripcion`                                                                       |

## 4. Estructura de la consulta origen (`SOURCE_SQL`, líneas 182-420)

CTEs encadenados:

1. **`items_cat4`** (líneas 187-215) — maestro filtrado a `id_tipo='4'`. Trae categoría
   (`categorias`) y líneas nivel 1/2 (doble `LEFT JOIN` a `lineas` por `id_linea*`+`id_tipo`).
   Calcula `costo_item_maestro = COALESCE(NULLIF(costo_act_acum,0), NULLIF(ultimo_costo_ed,0), 0)`
   como fallback de costo para kits.
2. **`inventario_max_lapso`** (líneas 220-236) — por (sede, bodega, ítem) toma
   `MAX(lapso_doc)` con `lapso_doc BETWEEN eneroDelAño AND lapsoConsultado`. Resuelve el caso
   de los primeros días del mes: si aún no hay datos del lapso actual, cae al mes anterior.
   Filtra `RIGHT(id_local,2)='01'` (solo bodega principal).
3. **`inventario_foto`** (líneas 244-270) — para el lapso ganador: `SUM(can_disponible)` y
   `MAX(...) FILTER (WHERE fecha ~ '^[12][0-9]{7}$')` para últimas fechas de compra/entrada/venta,
   más `MAX(costo_uni)`.
4. **`mov_base_anio`** (líneas 277-294) — movimientos de venta desde 1-enero hasta el día.
   Filtros: `id_tipo='4'` (join a `items_cat4`), `RIGHT(id_local,2)='01'`,
   `docto_acumulacion NOT LIKE 'Z%'` (excluye notas Z).
5. **`ventas_dia`** (líneas 296-308) — agrega `mov_base_anio` al día exacto:
   `SUM(cantidad)`, `SUM(vlrtot_bru)`, `SUM(tot_costo)`.
6. **`ultima_venta_pdv`** (líneas 312-319) — `MAX(fecha_dcto)` por (sede, ítem) en la ventana año.
7. **SELECT final** (líneas 321-419) — parte de `inventario_foto` (INNER JOIN a `items_cat4`),
   LEFT JOIN a `ventas_dia` y `ultima_venta_pdv`, y a `centro_operacion` por el nombre de sede.

## 5. Reglas de negocio / filtros (las que hay que replicar)

- **Universo:** solo ítems `id_tipo='4'` (línea 214). *En Dinastia el equivalente sería un
  filtro por `GRUPO_INVENTARIO` — a confirmar (SCHEMA_NEEDS.md).*
- **Bodega principal:** `RIGHT(id_local,2)='01'` (líneas 229, 292).
- **Excluir notas Z:** `docto_acumulacion NOT LIKE 'Z%'` (línea 293).
- **Excluir planta:** `inv.sede <> 'PPT'` (línea 418).
- **Ventana de lapso de inventario:** `MAX(lapso_doc) <= lapsoConsultado` y `>= enero` (fallback
  al mes anterior).
- **Exclusión de filas vacías** (líneas 412-417): se descarta la fila solo si NO hay venta, NO
  hay stock **y** el costo unitario es 0. Ítems con `costo_uni>0` pero `can_disponible=0` **sí**
  se cargan (reflejan que el stock bajó a cero).

## 6. El cálculo de "rotación / baja salida" — QUÉ se almacena vs QUÉ se deriva

Punto clave (líneas 15-19): **el ETL NO calcula "días sin venta" ni un ratio de rotación.**
Guarda **señales crudas** y deja el cálculo dinámico al portal/BI. Las señales:

- `can_disponible_foto` — stock disponible (lado "inventario ocioso").
- `cantidad_vendida`, `venta_sin_impuesto`, `total_costo` — ventas del día (lado "salida").
- `ultima_venta_pdv` — `MAX(fecha_dcto)` en la ventana del año (última venta real en POS).
- `ultima_venta_inventario` — `cmresumen_inventario.fecha_ultvta` (fecha de última venta según ERP).
- `estado_ultima_venta_item` — `'CON VENTA EN EL AÑO'` / `'SIN VENTA EN EL AÑO'` (líneas 366-371).
- `fecha_ultima_compra`, `fecha_ultima_entrada` — para análisis de antigüedad.

Entonces "inventario con baja salida" se obtiene aguas abajo como: ítems con `can_disponible_foto`
alto pero `ultima_venta_*` antigua (muchos días sin venta) y baja venta acumulada.

> **Decisión para Dinastia:** como el reporte de Dinastia ES "inventario con baja salida", el
> scaffold **materializa** la clasificación en `compute_rotacion()` (días sin venta, rotación =
> salidas_ventana / stock, flag `baja_salida`) en vez de dejarla al BI. Es una elección reversible:
> si negocio prefiere el enfoque Mercamio (solo señales crudas), basta no llamar a esa función.
> La definición exacta de la ventana y los umbrales está **pendiente de negocio** (SCHEMA_NEEDS.md).

## 7. Transformaciones (a portar al dialecto MySQL)

| Transformación (PostgreSQL)                                              | Equivalente MySQL en el scaffold                    |
|--------------------------------------------------------------------------|-----------------------------------------------------|
| `BTRIM(x)`                                                                | `TRIM(x)`                                            |
| `TO_DATE(x,'YYYYMMDD')`                                                   | `STR_TO_DATE(x,'%Y%m%d')` **o** columna `DATE` directa (a confirmar) |
| `x ~ '^[12][0-9]{7}$'` (validación de fecha texto)                       | `x REGEXP '^[12][0-9]{7}$'`                          |
| `MAX(col) FILTER (WHERE p)`                                               | `MAX(CASE WHEN p THEN col END)`                      |
| `%s::text`                                                                | `CAST(%s AS CHAR)`                                   |
| CTE `AS MATERIALIZED`                                                     | CTE normal (MySQL 8.0 materializa derivadas solo)   |
| `ON CONFLICT ... DO UPDATE` + `EXCLUDED.col`                             | `INSERT ... AS new ... ON DUPLICATE KEY UPDATE col = new.col` (loader) |
| **Fix kits** (líneas 344-349): si `tot_costo=0` y `cantidad>0` ⇒ `costo = cantidad * costo_uni` | idéntico en el SQL stub y en `compute` |
| **Fallback de costo** (líneas 392-393): `COALESCE(NULLIF(costo_uni,0), costo_item_maestro, 0)` | idéntico |
| **Fallback de unidad** (líneas 330-334): vendida → maestro → `'SIN_VENTA'` | idéntico |

## 8. Carga al destino

- **DDL** (líneas 100-169): `CREATE TABLE` con PK compuesta + 7 índices (fecha/empresa,
  sede/fecha, item/fecha, linea1/2, categoría, sin_venta_año). `--recreate-table` hace DROP+CREATE.
- **`UPSERT_FULL_SQL`** (líneas 424-464): daily/backfill. `ON CONFLICT DO UPDATE` de todo, pero
  las columnas de inventario se protegen con `CASE WHEN inv_foto_bloqueada THEN <old> ELSE <excluded>`.
- **`UPSERT_VENTAS_SQL`** (líneas 468-504): rolling. Solo pisa ventas y derivados; **el bloque de
  inventario NO se toca**.
- **`LOCK_INV_SQL`** (líneas 704-710): `UPDATE ... SET inv_foto_bloqueada=TRUE` para el día — así el
  rolling nunca sobreescribe la foto real que capturó el daily.
- **Batching:** `psycopg2.extras.execute_values(..., page_size=2000)` (líneas 874-885).

## 9. Modos de ejecución

- **`daily`** (líneas 897-910): procesa **ayer**, UPSERT completo, y **bloquea** la foto de
  inventario (`lock_inventory_snapshot`). ⚠ Llama `extract_day(..., backfill=True)` (ver §11).
- **`rolling`** (líneas 913-930): reprocesa los últimos N días (default 15) con `UPSERT_VENTAS_SQL`;
  inventario histórico intacto.
- **`backfill`** (líneas 1125-1176): rango histórico. **Optimizado**: inventario cargado **una vez
  por mes** (`extract_inventory_month`, cache), ventas por día (`extract_sales_day`, consulta liviana),
  y **merge en Python** (`merge_day`, líneas 1069-1122). Convierte el proceso de horas a minutos.

## 10. Configuración y scheduling

- **Config por variables de entorno** por empresa (`COMPANY_ENV`, líneas 79-95) + `TARGET_*` para el
  destino. `load_env_file()` (líneas 740-750) lee un `config/rotacion.env` opcional con
  `os.environ.setdefault` (no pisa el entorno real). **Sin secretos en código.**
- **Timers** (docstring, líneas 24-32): `daily` 7am; `rolling` 1am los días 1, 11, 21.
- **CLI** (líneas 1217-1236): `--mode`, `--rolling-days`, `--date-start/-end`, `--empresas`,
  `--recreate-table`, `--dry-run`, `--check-only`, `--log-dir`, `--log-retention-days`.
- **`--check-only`** (líneas 1274-1291): valida identidad de origen y destino
  (`current_database()`, `current_user`, `inet_server_addr()`) sin escribir.

## 11. Hallazgos notables (importan para el rewrite)

1. **`SOURCE_SQL` (ventana año completa) es código muerto.** `extract_day` solo se invoca con
   `backfill=True` (líneas 903 y 927), que usa `SOURCE_SQL_BACKFILL`. La rama `backfill=False`
   (`SOURCE_SQL`, líneas 182-420) **nunca se ejecuta**, pese a que el docstring (línea 24) dice que
   el daily usa la ventana año. En la práctica el **daily procesa solo el día**, así que su
   `ultima_venta_pdv` refleja si el ítem vendió **ese día**, no el acumulado del año. La ventana-año
   real la aportan el reproceso `rolling`/`backfill` y el BI. → **Para Dinastia solo hay que portar
   el patrón de un día**; la ventana de rotación se maneja explícitamente con `rotacion.ventana_salida_dias`.
2. **`process_backfill` no usa `extract_day`.** Usa la ruta cache-mensual + merge en Python
   (`extract_inventory_month`/`extract_sales_day`/`merge_day`). Es la implementación más eficiente y
   la que conviene portar si el backfill de Dinastia es pesado.
3. **La foto de inventario es mensual, no diaria.** `cmresumen_inventario` está indexada por
   `lapso_doc` (YYYYMM); la "foto del día" reusa el lapso del mes. El bloqueo `inv_foto_bloqueada`
   existe justamente para que el reproceso de ventas no altere esa foto.
4. **Fechas como texto `YYYYMMDD`.** En Mercamio las fechas del ERP vienen como texto y se validan
   con regex antes de `TO_DATE`. **En Dinastia (Siesa) hay que confirmar** si son `DATE`/`DATETIME`
   nativas o texto — cambia el SQL (SCHEMA_NEEDS.md §fechas).
