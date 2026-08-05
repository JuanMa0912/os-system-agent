# ANALYSIS — Mercamio `cargar_margen.py` (referencia para el margen de Dinastia)

Analisis del ETL de margenes de Mercamio que estamos adaptando. Fuente (solo lectura):
`_reference/margen/cargar_margen.py` y `_reference/margen/README.md`.
Las citas `(cargar_margen.py:NN)` apuntan a lineas del reference.

> Mercamio es **PostgreSQL**. Dinastia es **MySQL** con un destino GCP por decidir.
> Este documento describe QUE hace Mercamio; lo especifico de Mercamio (categorias 3/4,
> impoconsumo linea 33, rollup en GCP) se traslada como **preguntas** en `SCHEMA_NEEDS.md`,
> no se copia a ciegas.

---

## 1. Que produce

Una fila por **linea de factura** con su **ingreso** y su **costo**, de las 3 BD POS de
origen (`192.168.35.217`: mercamio / mtodo / bogota) hacia la tabla
`produXdia.margen_final` en el server 232 (cargar_margen.py:1-4). De ese crudo el tablero
deriva el **margen** (ingreso - costo) y su **variacion** en el tiempo.

El script recorre **empresa x dia** (cargar_margen.py:220-224): 3 empresas, una conexion
por empresa, y dentro un bucle por cada dia del rango.

## 2. La query de "movimiento unificado" (`SQL_TEMPLATE`, cargar_margen.py:68-148)

Estructura en 3 partes:

**a) CTE `mv` (cargar_margen.py:69-74)** — filtra el movimiento crudo `cmmovimiento_pdv`
por rango de fechas y **excluye notas credito**: `id_tipdoc_fc NOT LIKE 'Z%'` (ZZ/ZX/ZY...).

**b) CTE `costo_kit` (cargar_margen.py:75-86)** — resuelve el costo de los **kits**
(productos compuestos). Explota la lista de materiales `v_kits`, une `items` para el
`ultimo_costo_ed` de cada componente y suma
`ultimo_costo_ed * cantidad * factor` agrupando por el item padre (`id_cod_item_p`).
Sirve de **fallback** cuando el movimiento no trae costo.

**c) SELECT principal (cargar_margen.py:87-148)** — arma la fila final uniendo:
- `mv m` (movimiento/linea),
- `items i` por `m.id_item = i.id_item` — maestro del item (descripcion, `id_tipo`,
  `id_linea1/2`, `ultimo_costo_ed`),
- `lineas l1/l2/l3` — descripcion de cada nivel de linea; **join doble** por
  `id_linea AND id_tipo` (cargar_margen.py:138-140),
- `LEFT JOIN costo_kit ck` por item padre (cargar_margen.py:141),
- `LEFT JOIN terceros t` por `(m.id_terc = t.codigo AND m.id_suc = t.sucursal)` para el
  **nombre del cliente** (cargar_margen.py:146). Es **LEFT** a proposito: un INNER
  borraria las ventas de contado cuyo `id_terc` no exista en `terceros`
  (cargar_margen.py:14-16, 142-146).

## 3. Margen por linea: ingreso y costo (el corazon)

`margen_final` **no** guarda una sola columna "margen"; guarda **ingreso** y **costo** por
linea, y el margen se calcula despues. Columnas clave que produce el SELECT:

- **Ingreso (revenue):**
  - `vlrtot_bru` — bruto de la linea, con el ajuste de impoconsumo para linea 33 (ver 4.2).
  - `ven_totales = m.vlrtot_bru + m.vlrimpcon1` — bruto **original** + impoconsumo
    (cargar_margen.py:116). Usa el bruto original, por eso **no** duplica el impoconsumo.
  - `precio_unitario = ROUND(ven_totales / NULLIF(cantidad,0), 2)` (cargar_margen.py:117).

- **Costo (cost)** con **fallback** en cascada (cargar_margen.py:118-125):
  - si `m.tot_costo > 0` -> usar `m.tot_costo`;
  - si no -> `ROUND(COALESCE(costo_unitario_kit, i.ultimo_costo_ed, 0) * cantidad, 2)`.
  - `costo_unitario` analogo (por unidad).
  - Motivo: el ERP registra `tot_costo = 0` en movimientos de **kits**; el fallback usa el
    costo de kit (`costo_kit`) o el `ultimo_costo_ed` del maestro. (El mismo patron aparece
    en rotacion: `_reference/rotacion/etl_rotacion_v3.py:343-349`.)

- **Margen** = ingreso - costo. Se calcula en el destino/tablero (rollup), no en el crudo.

## 4. Reglas de negocio (ESPECIFICAS de Mercamio — trasladar como preguntas)

### 4.1 Solo categorias 3 y 4 (mercado)
`WHERE i.id_tipo IN ('3','4')` (cargar_margen.py:147). La categoria `'V'` se **excluye**
desde 2026-07-06 (no interesa en el tablero de margenes). README del reference:1-8, 17-22.

### 4.2 Impoconsumo en BEBIDAS ALCOHOLICAS (linea1 = '33')
Desde 2026-07-06, para la linea `33` (licores, cerveza, vino) el bruto cargado incluye el
impoconsumo (cargar_margen.py:107-114):
```sql
CASE WHEN TRIM(i.id_linea1) = '33' THEN m.vlrtot_bru + COALESCE(m.vlrimpcon1,0)
     ELSE m.vlrtot_bru END AS vlrtot_bru
```
Asi el impoconsumo entra a **ventas Y margen**. `ven_totales` sigue usando el bruto
**original**, por eso no se duplica. El impoconsumo vive en `cmmovimiento_pdv.vlrimpcon1`;
la tabla `impuestos` lo marca con `id_ind_impocon = 1` (codigos `A` = IVA 5% + IMPOCONSUMO,
`C` = IVA 19% + IMPOCONSUMO) (README del reference:24-38).

### 4.3 Factura + cliente (2026-07-21)
Se agregaron `documento_docfc` (documento POS acumulado, 16), `id_terc` y `nombre_terc`.
El nombre maestro **no** vive en `cmmovimiento_pdv`: se resuelve con `LEFT JOIN terceros`
por `(codigo, sucursal)` proyectando solo `descripcion` (cargar_margen.py:12-16, 130-146).

## 5. Idempotencia: "reemplazar el dia"

Por cada empresa y dia, en **una transaccion** (cargar_margen.py:238-246):
```sql
DELETE FROM margen_final WHERE fecha_dcto = %s AND empresa = %s;   -- borra el corte
COPY margen_final (COLS) FROM STDIN;                               -- lo vuelve a cargar
```
Re-correr un dia **no duplica** (cargar_margen.py:17-18). El `commit` es por dia.

## 6. Carga rapida: COPY postgres -> postgres

No hay INSERT fila a fila. Se hace `COPY (query) TO STDOUT` en el origen a un buffer en
memoria (formato texto = NULL-safe) y `COPY margen_final (COLS) FROM STDIN` en el destino
(cargar_margen.py:233-245). El orden posicional del COPY debe casar con `COLS`
(cargar_margen.py:57-63); por eso las 3 columnas de factura+cliente se **anexan al final**
tanto en el SELECT como en `COLS` (cargar_margen.py:130-135).

> **Implicacion para Dinastia:** MySQL **no tiene** `COPY (query) TO STDOUT`. La extraccion
> se hace con `SELECT` (pymysql, cursor server-side) y la carga con INSERT por lotes /
> mecanismo del destino GCP. El **patron idempotente** (DELETE particion + insert atomico)
> se conserva; el transporte cambia. Ver `db.py` (`MargenLoader.replace_partition`).

## 7. Config y CLI

- **Config:** un solo `.env.etl` en la raiz del deploy, **compartido** con el sync a GCP
  (cargar_margen.py:24-26, 46-47). Destino desde `DB_*_LOCAL`; origen POS desde `DB_*_POS`
  + una clave por empresa (`DB_PWD_POS_MERCAMIO/MTODO/BOGOTA`, cargar_margen.py:50-54).
  Las fechas van **inline** (validadas `YYYYMMDD`) porque COPY no acepta parametros
  (cargar_margen.py:65-67). Los secretos salen de env; nunca del codigo.
- **CLI** (cargar_margen.py:259-290): `--date YYYYMMDD`, `--desde/--hasta`, `--dry-run`
  (solo cuenta filas en origen, no escribe). Sin flags = **ayer**.
  Exit codes: `0` OK | `1` error | `2` uso invalido.

## 8. Rollup GCP y "variacion" (README del reference:78-113)

El crudo `margen_final` se sube a GCP (`sync-local-to-gcp.sh`). En **GCP** viven los rollups
que alimentan el tablero:
- `margen_final_roll` — rollup factura+item; el endpoint `/api/margenes/data` lee de aqui,
  **no** del crudo. Existe **solo en GCP** (Cloud SQL); en la local 232 el tablero lee
  `margen_final` directo.
- `margen_item_dia_roll` — rollup por item/dia; es el que sostiene el **informe de
  variacion** (comparar el margen de cada item/linea dia a dia).

Refresh: el sync 07:50 sincroniza una ventana de `margen_final_roll` + `margen_item_dia_roll`
cuando sube `margen_final`; ademas el timer `visor-refresh-variacion.timer` (08:30) reconstruye
`margen_item_dia_roll` via `refresh-variacion-roll.sh`. Sin rollup poblado, el tablero cae a
leer `margen_final` (mas lento).

> **Implicacion para Dinastia:** la "variacion" es un **rollup por item/dia** sobre el crudo,
> que hoy en Mercamio vive en GCP. Para Dinastia esta por decidir **donde** se calcula
> (en el ETL, en el destino GCP con vistas/materializadas, o en el BI) y con que grano.
> Ver `SCHEMA_NEEDS.md`.

## 9. Que NO hace

No genera CSV (el `consulta_Movimiento_bd.py` original si; aqui esta desactivado, README del
reference:114-120). No calcula el margen final como columna: guarda ingreso y costo y deja el
margen/variacion al rollup del destino.

## 10. Mapa Mercamio -> Dinastia (resumen)

| Mercamio (Postgres) | Dinastia (MySQL, Siesa/Biable) | Nota |
|---|---|---|
| `cmmovimiento_pdv` (linea, plano) | `CMMOVIMIENTO_VENTAS` (detalle) | linea de factura |
| — (header aplanado en POS) | `CMENCABEZADO_VENTAS` (encabezado) | fecha, doc, cliente, id_co |
| `items` | `ITEMS` | maestro: desc, categoria, costo |
| `lineas` | `LINEAS` | descripcion de lineas |
| `impuestos` | `IMPUESTOS` | impoconsumo / IVA |
| `v_kits` | ??? (BOM de kits) | **TBD** |
| `terceros` | ??? (maestro cliente) | **TBD** |
| `COPY ... TO/FROM STDIN` | `SELECT` + carga por lotes | MySQL no tiene COPY |
| `margen_final` (Postgres 232) | destino **GCP por decidir** | BigQuery o Cloud SQL |
| `margen_item_dia_roll` (GCP) | rollup de variacion **TBD** | grano/lugar por definir |

Las incognitas de este mapa son exactamente las preguntas de `SCHEMA_NEEDS.md`.
