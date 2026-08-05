# SCHEMA_NEEDS — margen Dinastia (lo que falta para construir el ETL real)

El scaffold reproduce la ESTRUCTURA de Mercamio pero la query esta **STUBBED**: solo
tenemos los **nombres** de las tablas del ERP, no las columnas. Este documento lista lo que
hay que confirmar. Cada `<<placeholder>>` de `cargar_margen.py` (`SQL_STUB`) corresponde a
una fila de las tablas de abajo.

ERP: **MySQL 8.0**, base `BD_BIABLE01` en `192.168.30.1` (Siesa/Biable), usuario `BIABLE01`
(**solo lectura**). Tablas conocidas (solo nombres): `CMMOVIMIENTO_VENTAS`,
`CMENCABEZADO_VENTAS`, `ITEMS`, `LINEAS`, `IMPUESTOS`.

> Como obtener el esquema (una vez haya acceso de solo lectura), por tabla:
> `SELECT column_name, data_type, is_nullable FROM information_schema.columns`
> `WHERE table_schema='BD_BIABLE01' AND table_name='CMMOVIMIENTO_VENTAS' ORDER BY ordinal_position;`
> y un `SELECT ... LIMIT 5` para ver valores reales (categorias, prefijos de documento, etc.).

---

## 1. Ingreso por linea (revenue) — `CMMOVIMIENTO_VENTAS` (detalle) + `CMENCABEZADO_VENTAS`

Necesitamos, POR LINEA de factura:

| Dato | Placeholder en SQL_STUB | Tabla esperada | Pregunta a confirmar |
|---|---|---|---|
| Clave detalle<->encabezado | `d.<<fk_encabezado>>` = `h.<<pk_encabezado>>` | detalle/encabezado | Como se relaciona una linea con su factura? (numero de doc + tipo + centro? un id?) |
| Fecha de la factura | `h.<<fecha>>` | `CMENCABEZADO_VENTAS` | Nombre y **tipo** (DATE vs char `YYYYMMDD`). Define el `BETWEEN` y el particionado. |
| Centro de operacion / sede | `h.<<id_co>>` | encabezado | Nombre de la columna. Hay varias sedes/id_co en Dinastia? |
| Item vendido | `d.<<id_item>>` | detalle | Nombre; casa con `ITEMS.<<id_item>>`. |
| Cantidad | `d.<<cantidad>>` | detalle | Nombre; unidad (por unidad de venta o de inventario?). |
| Valor bruto de la linea | `d.<<valor_bruto>>` | detalle | **Cual columna es el ingreso**: bruto con o sin IVA? Equivalente a `vlrtot_bru`. |
| Unidad | `d.<<id_unidad>>` | detalle | Nombre. |
| Numero de factura | `d.<<documento>>` | detalle/encabezado | Para el grano factura+linea. |

**Pregunta clave (revenue):** en Mercamio el ingreso base es `vlrtot_bru` (bruto), y
`ven_totales = vlrtot_bru + vlrimpcon1`. En Dinastia, cual columna representa el ingreso de
la linea, y **incluye o no impuestos**? Definir si el margen se calcula sobre ingreso con o
sin IVA.

## 2. Costo por linea (cost) — `CMMOVIMIENTO_VENTAS` + `ITEMS` (+ kits)

| Dato | Placeholder | Tabla | Pregunta |
|---|---|---|---|
| Costo total de la linea | `d.<<tot_costo>>` | detalle | Existe un costo por linea en el movimiento (como `tot_costo` de Mercamio)? |
| Costo unitario del maestro | `it.<<ultimo_costo>>` | `ITEMS` | Cual columna es el costo del item (equiv. `ultimo_costo_ed`)? Ultimo costo? Costo promedio? |
| Costo de kits | (CTE de kits, hoy omitido) | ??? | **Dinastia maneja kits/combos?** Si si, cual es la tabla BOM (equiv. `v_kits`) y sus columnas (item padre, componente, cantidad, factor)? |

**Pregunta clave (cost):** replicar el **fallback** de Mercamio (usar `tot_costo` si > 0, si
no `costo_maestro * cantidad`)? Aplica el problema de `tot_costo = 0` en kits? Que costo se
usa: ultimo costo, costo promedio, costo estandar?

## 3. Categoria y lineas — `ITEMS` + `LINEAS`

| Dato | Placeholder | Tabla | Pregunta |
|---|---|---|---|
| Descripcion del item | `it.<<descripcion>>` | `ITEMS` | Nombre. |
| Categoria del item | `it.<<id_categoria>>` | `ITEMS` | Equivalente a `items.id_tipo`. Que valores tiene? (para el filtro de categorias) |
| Linea nivel 1 | `it.<<id_linea1>>` | `ITEMS` | Nombre. Hay niveles linea1/linea2/linea? |
| Descripcion de linea | `l1.<<descripcion>>` | `LINEAS` | Como se une `LINEAS` con `ITEMS`: solo por `id_linea`, o tambien por categoria (como el join doble de Mercamio `id_linea AND id_tipo`)? |

## 4. Cliente (tercero) — tabla maestro TBD

Mercamio resuelve el nombre del cliente con `LEFT JOIN terceros` por `(codigo, sucursal)`.

- Cual es la tabla de **terceros/clientes** en Dinastia? (no esta en la lista de nombres dada)
- El `CMENCABEZADO_VENTAS` trae `id_tercero`? Con que columna del maestro casa?
- Confirmar **LEFT JOIN** (no descartar ventas de contado sin tercero).
- El nombre del cliente es requisito del reporte de Dinastia, o solo el codigo?

## 5. Reglas de negocio (abiertas — NO copiar las de Mercamio a ciegas)

Estas son especificas de Mercamio; para Dinastia son decisiones de negocio:

1. **Filtro de categorias.** Mercamio carga solo `id_tipo IN ('3','4')` (mercado) y excluye
   `'V'`. **Dinastia:** que categorias entran al informe de margenes? (config `etl.categorias`)
2. **Exclusion de notas credito.** Mercamio: `id_tipdoc_fc NOT LIKE 'Z%'`. **Dinastia:** como
   se identifican las notas credito / devoluciones? Prefijo de tipo de documento? Se restan
   del margen o se excluyen? (config `etl.excluir_tipdoc_prefijo`)
3. **Impoconsumo.** Mercamio suma el impoconsumo al bruto **solo en la linea 33** (bebidas
   alcoholicas). **Dinastia:** aplica impoconsumo? En que lineas? La tabla `IMPUESTOS` lo
   marca (equivalente a `id_ind_impocon`)? Entra al ingreso y al margen?
4. **Signo del margen y devoluciones.** Como tratar cantidades/valores negativos (devoluciones)?
5. **Grano del crudo.** Una fila por (factura, linea) como Mercamio, o por (factura, item)?

## 6. Variacion (el "informe de variacion")

En Mercamio la **variacion** es un rollup por **item/dia** (`margen_item_dia_roll`) que vive en
GCP y se reconstruye con un timer aparte. Preguntas para Dinastia:

- **Grano de la variacion:** por item/dia? por linea/dia? por sede/item/dia?
- **Que se compara:** margen absoluto dia a dia? % de margen? variacion vs dia anterior / vs
  mismo dia del mes/ano anterior?
- **Donde se calcula:** en este ETL (una tabla/rollup adicional), en el destino GCP
  (vista/materializada o scheduled query), o en el BI? Recomendacion: mantener el crudo
  (margen por linea) aqui y el rollup de variacion en el destino, como Mercamio.

## 7. Destino GCP (bloqueante) — `destination.backend`

**No esta decidido** BigQuery vs Cloud SQL Postgres. Impacta el loader (`db.py`) y la
idempotencia:

| Tema | Cloud SQL Postgres | BigQuery |
|---|---|---|
| Driver | `psycopg2` | `google-cloud-bigquery` |
| Idempotencia por dia | `DELETE (empresa,fecha)` + `execute_values`, transaccional | overwrite de particion (`tabla$YYYYMMDD`, WRITE_TRUNCATE) o `DELETE`+load job |
| DDL | tabla + PK + indices (como `margen_final`) | tabla particionada por `fecha_dcto`, clustering por item/sede |
| Auth | usuario/clave (via cloud-sql-proxy) | service account / ADC |
| Costo DML | barato | DELETE/DML tiene cuotas y costo — preferir overwrite de particion |

Preguntas:
- Cual backend? (define que `requirements.txt` y que stub de `db.py` se completa)
- Esquema/tabla destino y su **DDL** (columnas y tipos definitivos — los de `COLS` en
  `cargar_margen.py` son provisionales).
- Como llega el ETL a GCP: corre en Dinastia y escribe directo a GCP? o carga local + sync
  (como Mercamio con `sync-local-to-gcp.sh`)? Hay VPN/proxy?

## 8. Operacion / deploy

- Usuario no-root del servicio en Dinastia (`servidorUAID`, Debian 12): confirmar nombre
  (el systemd usa `dinastia-etl`).
- Ruta de deploy (el systemd asume `/opt/dinastia-etl/margen`).
- Ventana horaria del timer (hoy 07:15, copiada de Mercamio) — confirmar cuando cierra el dia
  contable del ERP para no cargar un dia incompleto.
- Ventana de reproceso / backfill (Mercamio reprocesa con `--desde/--hasta`).

---

### Resumen: top bloqueantes para el build real
1. **Columnas** de `CMMOVIMIENTO_VENTAS` / `CMENCABEZADO_VENTAS` / `ITEMS` / `LINEAS` y la
   **clave detalle<->encabezado** (secciones 1-3).
2. **Cual columna es ingreso y cual es costo** por linea, y el **fallback de costo** / kits (1-2).
3. **Destino GCP** (BigQuery vs Cloud SQL) + DDL de la tabla destino (seccion 7).
4. **Reglas de negocio** de Dinastia: categorias, notas credito, impoconsumo (seccion 5).
5. **Grano y definicion de la variacion** + tabla de cliente/terceros (secciones 4 y 6).
