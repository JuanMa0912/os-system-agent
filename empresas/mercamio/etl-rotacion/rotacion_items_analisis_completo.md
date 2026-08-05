# Tablero de Rotación de Ítems — Análisis Completo
### Sistema: ERP Biable (Mercamio) · Motor: PostgreSQL
**Arquitecto de datos:** Claude · **Fecha:** 2026-04-24

---

## 1. ANÁLISIS DEL ESQUEMA.SQL

### 1.1 Tablas principales detectadas

| Tabla | Schema | Rol en el tablero |
|---|---|---|
| `cmmovimiento_ventas` | public | Fuente principal de ventas por ítem |
| `cmmovimiento_pdv` | public | Ventas punto de venta (alternativa) |
| `cmresumen_inventario` | public | Inventario periódico por CO / bodega / ítem |
| `items` | public | Maestro de ítems |
| `lineas` | public | Jerarquía de líneas (nivel 1, 2, 3) |
| `criterios_itm_4` | public | Categoría 4 del ítem |
| `criterios_itm_1/2/3` | public | Otras clasificaciones del ítem |
| `centro_operacion` | public | Sedes / centros de operación |
| `empresas` | public | Maestro de empresas |
| `bodegas` | public | Maestro de bodegas / localidades |
| `cmmovimiento_inventario` | public | Movimientos detallados de inventario |
| `cmmovimiento_compras` | public | Compras por ítem |
| `cmestadistico_ventas` | public | Estadístico de ventas (sin fechas diarias explícitas) |

---

### 1.2 Tabla de ventas: `public.cmmovimiento_ventas`

**Campos clave para el tablero:**

| Campo | Tipo | Uso |
|---|---|---|
| `id_emp` | char(2) | Empresa |
| `id_co` | char(3) | Centro de operación / sede |
| `id_local` | char(5) | Bodega (las principales terminan en `01`) |
| `id_item` | char(6) | Ítem |
| `fecha_dcto` | char(8) | Fecha de la venta en formato **YYYYMMDD** |
| `cantidad` | numeric(20,4) | Unidades vendidas |
| `tot_venta` | numeric(20,4) | Venta neta (sin impuesto) — ver nota abajo |
| `tot_bruto` | numeric(20,4) | Venta bruta antes de descuentos |
| `imp_netos` | numeric(20,4) | Impuestos (IVA + impoconsumo) |
| `dscto_netos` | numeric(20,4) | Descuentos (puede ser negativo) |
| `costo_vta` | numeric(20,4) | Costo de la venta |
| `vta_gravada` | numeric(20,4) | Valor gravado |
| `vta_exenta` | numeric(20,4) | Valor exento |
| `lapso_doc` | char(6) | Período YYYYMM del documento |
| `id_ext_itm` | char(3) | Extensión del ítem (talla/color) |

> **Nota sobre `tot_venta`:** La vista materializada `ver.v_m_ventas` la etiqueta como `valor_neto`, mientras que `tot_bruto + dscto_netos` sería el valor bruto. Por tanto, **`tot_venta` es la venta neta sin impuesto** y es el campo correcto para el tablero. Validar con: `SELECT SUM(tot_venta), SUM(tot_bruto - dscto_netos - imp_netos) FROM cmmovimiento_ventas LIMIT 100` — deben aproximarse.

---

### 1.3 Tabla de inventario: `public.cmresumen_inventario`

**⚠️ PUNTO CRÍTICO:** `lapso_doc` es **YYYYMM** (periodicidad mensual), NO diaria. Esto tiene implicaciones importantes en la estrategia ETL.

| Campo | Tipo | Uso |
|---|---|---|
| `id_co` | char(3) | Centro de operación |
| `id_local` | char(5) | Bodega |
| `lapso_doc` | char(6) | Período **YYYYMM** (mensual) |
| `id_item` | char(6) | Ítem |
| `id_ext_itm` | char(3) | Extensión del ítem |
| `can_exis_fin` | numeric(20,4) | **Existencia final del período** — inventario actual |
| `vlr_cost_fin` | numeric(20,4) | **Valor del inventario al costo** |
| `costo_uni` | numeric(20,4) | **Costo promedio unitario** |
| `ult_costo` | numeric(20,4) | Último costo de entrada |
| `fecha_ultent` | char(8) | Fecha último ingreso (YYYYMMDD) |
| `fecha_ultvta` | char(8) | Fecha última venta (YYYYMMDD) |
| `fecha_ultsal` | char(8) | Fecha última salida (YYYYMMDD) |
| `fecha_ultsalmov` | char(8) | Fecha último movimiento de salida |
| `can_disponible` | numeric(20,4) | Cantidad disponible (descontando reservas) |
| `cant_vendida` | numeric(20,4) | Cantidad vendida en el período |
| `cant_comprada` | numeric(20,4) | Cantidad comprada en el período |
| `vlr_compras` | numeric(20,4) | Valor de compras del período |
| `vlr_ventas` | numeric(20,4) | Valor de ventas del período |

> **Sin campo `id_emp`:** `cmresumen_inventario` NO tiene campo de empresa. Si el sistema maneja múltiples empresas, el filtro de empresa debe venir del parámetro de entrada y aplicarse en la tabla de ventas. La relación CO ↔ Empresa se establece a través del movimiento de ventas.

---

### 1.4 Tabla maestra de ítems: `public.items`

| Campo | Tipo | Uso |
|---|---|---|
| `id_item` | char(6) | PK del ítem |
| `id_ext_itm` | char(3) | Extensión (talla/color) |
| `descripcion` | char(40) | Descripción del ítem |
| `id_tipo` | char(1) | **Tipo de inventario** — llave de join con lineas y criterios |
| `id_linea1` | char(6) | Código línea nivel 1 → `lineas.id_linea` |
| `id_linea2` | char(6) | Código línea nivel 2 → `lineas.id_linea` |
| `id_linea` | char(6) | Código línea nivel 3 (final) → `lineas.id_linea` |
| `id_cricla4` | char(4) | Código criterio/categoría 4 → `criterios_itm_4.id_cricla4` |
| `id_cricla1/2/3` | char(4) | Otros criterios de clasificación |
| `unimed_inv_1` | char(3) | Unidad de medida principal |
| `id_estado` | char(1) | Estado del ítem |
| `id_bodega_default` | char(5) | Bodega por defecto |

---

### 1.5 Tabla de líneas: `public.lineas`

| Campo | Tipo | Uso |
|---|---|---|
| `id_linea` | char(7) | PK — coincide con `items.id_linea1` (con BTRIM) |
| `id_tipo` | char(1) | Tipo de inventario — **llave secundaria de join** |
| `id_linea1` | char(3) | Código del nivel 1 dentro de la línea |
| `id_linea2` | char(5) | Código del nivel 2 dentro de la línea |
| `cmlineas_descripcion` | char(40) | **Descripción de la línea** |
| `id_linea1_6` | char(6) | Cód. nivel 1 en formato de 6 chars |

> **Diferencia de tipos:** `items.id_linea1` es `char(6)` y `lineas.id_linea` es `char(7)`. La vista existente `v_eos_items` hace el join sin BTRIM, lo que funciona en PostgreSQL porque los tipos CHAR se comparan por padding. Sin embargo, **siempre use BTRIM en ambos lados** para evitar bugs por espacios.

---

### 1.6 Tabla de categorías: `public.criterios_itm_4`

| Campo | Tipo | Uso |
|---|---|---|
| `id_cricla4` | char(4) | Código de la categoría 4 |
| `id_catego` | char(1) | Tipo de inventario — **llave secundaria de join** |
| `cmcricla_descripcion` | char(40) | **Nombre de la categoría 4** |

> **Join correcto:** `items.id_cricla4 = criterios_itm_4.id_cricla4 AND items.id_tipo = criterios_itm_4.id_catego`. El campo `id_catego` actúa como discriminador de tipo, confirmado por la vista `v_eos_items`.

---

### 1.7 Tablas de sedes: `public.centro_operacion` y `public.empresas`

**`centro_operacion`:**

| Campo | Tipo | Uso |
|---|---|---|
| `codigo` | char(3) | PK — coincide con `id_co` en ventas e inventario |
| `descripcion` | char(40) | **Nombre de la sede** |

**`empresas`:**

| Campo | Tipo | Uso |
|---|---|---|
| `codigo` | char(2) | PK — coincide con `id_emp` en ventas |
| `descripcion` | char(40) | Nombre de la empresa |

> **Sin FK directa:** No existe una FK explícita `centro_operacion → empresas`. La relación se establece a través del movimiento de ventas (`cmmovimiento_ventas.id_emp + id_co`). Si una sede pertenece a una sola empresa, esto es transparente. Si hay sedes compartidas entre empresas, debe manejarse con parámetro de empresa.

---

### 1.8 Tabla de bodegas: `public.bodegas`

| Campo | Tipo | Uso |
|---|---|---|
| `id_local` | char(5) | PK — las principales terminan en `01` (e.g., `'00101'`) |
| `cmlocal_descripcion` | char(40) | Nombre de la bodega |

---

### 1.9 Relaciones recomendadas

```
empresas (codigo)
    └──► cmmovimiento_ventas (id_emp)
              │
              ├── id_co ──────────────────► centro_operacion (codigo)
              ├── id_local ────────────────► bodegas (id_local)
              └── id_item ─────────────────► items (id_item)
                                                  │
                                          ┌───────┼───────────────┐
                                          │       │               │
                               id_linea1  │  id_cricla4    id_tipo
                            +  id_tipo ───▼       └──────────────►criterios_itm_4
                                        lineas              (id_cricla4 + id_catego)
                               (id_linea + id_tipo)

cmresumen_inventario (id_co + id_local + lapso_doc + id_item)
    ├── id_co ────────────────────────────► centro_operacion (codigo)
    ├── id_local ────────────────────────► bodegas (id_local)
    └── id_item ─────────────────────────► items (id_item)

JOIN principal ETL:
    cmmovimiento_ventas FULL OUTER JOIN cmresumen_inventario
    ON (id_co + id_local + id_item)
    Nota: lapso_doc en inventario = SUBSTRING(fecha_dcto ventas, 1, 6)
```

---

## 2. VALIDACIONES NECESARIAS ANTES DE CREAR LA QUERY

### 2.1 Columnas a confirmar

```sql
-- V1: ¿tot_venta es realmente venta sin impuesto?
SELECT
    SUM(tot_venta)                        AS tot_venta,
    SUM(tot_bruto + dscto_netos)          AS bruto_menos_dscto,
    SUM(tot_venta) - SUM(imp_netos)       AS tot_venta_menos_imp,
    SUM(vta_gravada + vta_exenta)         AS gravada_mas_exenta
FROM public.cmmovimiento_ventas
WHERE fecha_dcto = '20260101'  -- fecha de prueba
LIMIT 1;

-- V2: ¿El campo dscto_netos viene negativo o positivo?
SELECT MIN(dscto_netos), MAX(dscto_netos), AVG(dscto_netos)
FROM public.cmmovimiento_ventas
WHERE dscto_netos <> 0;

-- V3: ¿lapso_doc en cmresumen_inventario es YYYYMM?
SELECT DISTINCT LENGTH(BTRIM(lapso_doc)), lapso_doc
FROM public.cmresumen_inventario
LIMIT 10;

-- V4: ¿Las bodegas principales terminan exactamente en '01'?
SELECT DISTINCT id_local, cmlocal_descripcion
FROM public.bodegas
WHERE id_local LIKE '%01'
ORDER BY id_local;

-- V5: Confirmar el join items -> lineas (nivel 1)
SELECT i.id_item, i.id_linea1, i.id_tipo, l.id_linea, l.cmlineas_descripcion
FROM public.items i
LEFT JOIN public.lineas l
    ON BTRIM(i.id_linea1) = BTRIM(l.id_linea)
    AND BTRIM(i.id_tipo)  = BTRIM(l.id_tipo)
WHERE l.id_linea IS NULL
LIMIT 20;
-- Si retorna filas → hay ítems sin línea nivel 1 coincidente

-- V6: Confirmar el join items -> criterios_itm_4
SELECT i.id_item, i.id_cricla4, i.id_tipo, c4.id_cricla4, c4.cmcricla_descripcion
FROM public.items i
LEFT JOIN public.criterios_itm_4 c4
    ON BTRIM(i.id_cricla4) = BTRIM(c4.id_cricla4)
    AND BTRIM(i.id_tipo)   = BTRIM(c4.id_catego)
WHERE c4.id_cricla4 IS NULL
LIMIT 20;

-- V7: ¿Hay ítems con ventas pero sin registro en cmresumen_inventario?
SELECT COUNT(DISTINCT mv.id_item)
FROM public.cmmovimiento_ventas mv
WHERE mv.fecha_dcto BETWEEN '20260101' AND '20260131'
  AND NOT EXISTS (
      SELECT 1 FROM public.cmresumen_inventario ri
      WHERE BTRIM(ri.id_item) = BTRIM(mv.id_item)
        AND BTRIM(ri.id_co)   = BTRIM(mv.id_co)
        AND BTRIM(ri.id_local)= BTRIM(mv.id_local)
        AND ri.lapso_doc      = SUBSTRING(mv.fecha_dcto, 1, 6)
  );

-- V8: ¿La empresa tiene FKs implícitas con CO?
SELECT DISTINCT id_emp, id_co
FROM public.cmmovimiento_ventas
ORDER BY id_emp, id_co;
```

### 2.2 Llaves de unión

| Join | Llave izquierda | Llave derecha | Precaución |
|---|---|---|---|
| ventas → empresa | `cmmovimiento_ventas.id_emp` | `empresas.codigo` | BTRIM ambos |
| ventas → CO | `cmmovimiento_ventas.id_co` | `centro_operacion.codigo` | BTRIM ambos |
| ventas → bodega | `cmmovimiento_ventas.id_local` | `bodegas.id_local` | BTRIM ambos |
| ventas → ítem | `cmmovimiento_ventas.id_item` | `items.id_item` | BTRIM ambos |
| inventario → CO | `cmresumen_inventario.id_co` | `centro_operacion.codigo` | BTRIM ambos |
| inventario → ítem | `cmresumen_inventario.id_item` | `items.id_item` | BTRIM ambos |
| ítem → línea N1 | `items.id_linea1 + items.id_tipo` | `lineas.id_linea + id_tipo` | BTRIM + char length dif. |
| ítem → categoría 4 | `items.id_cricla4 + items.id_tipo` | `criterios_itm_4.id_cricla4 + id_catego` | BTRIM ambos |
| **ventas ↔ inventario** | `id_co + id_local + id_item` | `id_co + id_local + id_item` | lapso_doc = YYYYMM de fecha |

### 2.3 Problemas de tipos y fechas

- Todos los campos de fecha son `char(8)` en formato `YYYYMMDD`. Para comparar rangos, usar directamente como texto (funciona porque YYYYMMDD ordena lexicográficamente).
- Todos los IDs son `char(N)` con padding de espacios. **Siempre usar BTRIM** al comparar o mostrar.
- `lapso_doc` en inventario es `char(6)` formato `YYYYMM`. Extraer de la fecha con `SUBSTRING(fecha, 1, 6)`.
- `tot_venta` y campos numéricos pueden ser `NULL` en algunas filas. Usar `COALESCE(..., 0)`.
- Posibles duplicados en ventas si existe multidocumento: la query agrega por `(id_emp, id_co, id_local, id_item, fecha_dcto)`, lo cual es correcto.
- `dscto_netos` en `cmmovimiento_ventas` puede venir en negativo (descuento representado como salida). Confirmar con validación V2 arriba.

---

## 3. QUERY SQL OPTIMIZADA

```sql
/*
════════════════════════════════════════════════════════════════════════════════
  TABLERO: ROTACIÓN DE ÍTEMS
  Sistema: ERP Biable · Base de datos: PostgreSQL
  Autor:   Arquitecto de Datos ETL
  Versión: 1.0 · Fecha: 2026-04-24

  PARÁMETROS (reemplazar o pasar como bind params):
    :fecha_ini  → Fecha inicio en formato YYYYMMDD  (ej: '20260401')
    :fecha_fin  → Fecha fin   en formato YYYYMMDD  (ej: '20260401')
    :id_empresa → Código de empresa (char 2)       (ej: '01')
                  Pasar NULL para todas las empresas

  NOTA SOBRE INVENTARIO:
    cmresumen_inventario es un resumen MENSUAL (lapso_doc = YYYYMM).
    La "foto de inventario" capturada por el ETL corresponde al estado
    del inventario en el mes de la fecha consultada.
    La tabla destino (rotacion_base_item_dia_sede) protege el inventario
    histórico: una vez grabado, no se reemplaza en reprocesos.
════════════════════════════════════════════════════════════════════════════════
*/

WITH

-- ════════════════════════════════════════════════════════════════════════
-- CTE 1: VENTAS DEL PERÍODO
--   · Fuente: cmmovimiento_ventas
--   · Granularidad: empresa + CO + bodega + ítem + fecha
--   · Filtros: bodegas que terminan en '01' (principales)
--   · tot_venta = venta neta sin impuesto (confirmado por v_m_ventas)
-- ════════════════════════════════════════════════════════════════════════
ventas_dia AS (
    SELECT
        BTRIM(mv.id_emp)                            AS id_empresa,
        BTRIM(mv.id_co)                             AS id_co,
        BTRIM(mv.id_local)                          AS id_local,
        BTRIM(mv.id_item)                           AS id_item,
        BTRIM(mv.fecha_dcto)                        AS fecha_consulta,
        SUM(mv.tot_venta)                           AS venta_sin_impuesto,
        SUM(mv.cantidad)                            AS unidades_vendidas,
        SUM(mv.costo_vta)                           AS costo_total_ventas,
        -- Alternativa más explícita si tot_venta incluye IVA en alguna conf.:
        -- SUM(mv.vta_gravada + mv.vta_exenta)      AS venta_sin_impuesto_alt,
        SUM(mv.imp_netos)                           AS total_impuestos
    FROM public.cmmovimiento_ventas mv
    WHERE mv.fecha_dcto BETWEEN :fecha_ini AND :fecha_fin
      AND mv.id_local LIKE '%01'                    -- bodegas principales
      AND (:id_empresa IS NULL OR BTRIM(mv.id_emp) = BTRIM(:id_empresa))
    GROUP BY
        BTRIM(mv.id_emp),
        BTRIM(mv.id_co),
        BTRIM(mv.id_local),
        BTRIM(mv.id_item),
        BTRIM(mv.fecha_dcto)
),

-- ════════════════════════════════════════════════════════════════════════
-- CTE 2: INVENTARIO DEL PERÍODO
--   · Fuente: cmresumen_inventario
--   · lapso_doc es MENSUAL (YYYYMM)
--   · Se toma el lapso correspondiente al mes de fecha_ini
--   · can_exis_fin = saldo de inventario (running balance del mes)
--   · IMPORTANTE: este valor se tomará como "foto del día" en el ETL
--     y se almacenará de forma inmutable en la tabla destino.
-- ════════════════════════════════════════════════════════════════════════
inventario_mes AS (
    SELECT
        BTRIM(ri.id_co)                             AS id_co,
        BTRIM(ri.id_local)                          AS id_local,
        BTRIM(ri.lapso_doc)                         AS lapso_doc,
        BTRIM(ri.id_item)                           AS id_item,
        ri.can_exis_fin                             AS inv_cantidad,
        ri.vlr_cost_fin                             AS inv_valor,
        ri.costo_uni                                AS costo_promedio,
        ri.ult_costo                                AS ultimo_costo,
        ri.can_disponible                           AS inv_disponible,
        BTRIM(ri.fecha_ultent)                      AS fecha_ultimo_ingreso,
        BTRIM(ri.fecha_ultvta)                      AS fecha_ultima_venta_inv,
        BTRIM(ri.fecha_ultsalmov)                   AS fecha_ultimo_movimiento
    FROM public.cmresumen_inventario ri
    WHERE ri.lapso_doc  = SUBSTRING(:fecha_ini, 1, 6)   -- YYYYMM
      AND ri.id_local LIKE '%01'                        -- bodegas principales
),

-- ════════════════════════════════════════════════════════════════════════
-- CTE 3: MAESTRO ENRIQUECIDO DE ÍTEMS
--   · Incluye línea nivel 1 y categoría 4
--   · El join a lineas y criterios usa id_tipo como discriminador
-- ════════════════════════════════════════════════════════════════════════
maestro_item AS (
    SELECT
        BTRIM(i.id_item)                            AS id_item,
        BTRIM(i.id_ext_itm)                         AS id_ext_itm,
        BTRIM(i.descripcion)                        AS descripcion_item,
        BTRIM(i.id_tipo)                            AS id_tipo,
        BTRIM(i.unimed_inv_1)                       AS unidad_medida,
        -- Línea nivel 1
        BTRIM(i.id_linea1)                          AS id_linea_n1,
        COALESCE(BTRIM(ln1.cmlineas_descripcion),
                 'SIN LINEA')                       AS nombre_linea_n1,
        -- Categoría 4 del ítem
        BTRIM(i.id_cricla4)                         AS id_categoria_4,
        COALESCE(BTRIM(c4.cmcricla_descripcion),
                 'SIN CATEGORIA')                   AS nombre_categoria_4
    FROM public.items i
    -- JOIN línea nivel 1: items.id_linea1 + items.id_tipo → lineas.id_linea + id_tipo
    LEFT JOIN public.lineas ln1
        ON  BTRIM(i.id_linea1) = BTRIM(ln1.id_linea)
        AND BTRIM(i.id_tipo)   = BTRIM(ln1.id_tipo)
    -- JOIN categoría 4: items.id_cricla4 + items.id_tipo → criterios_itm_4.id_cricla4 + id_catego
    LEFT JOIN public.criterios_itm_4 c4
        ON  BTRIM(i.id_cricla4) = BTRIM(c4.id_cricla4)
        AND BTRIM(i.id_tipo)    = BTRIM(c4.id_catego)
),

-- ════════════════════════════════════════════════════════════════════════
-- CTE 4: UNIÓN BASE (FULL OUTER JOIN ventas ↔ inventario)
--   · Incluye ítems con venta pero sin inventario registrado
--   · Incluye ítems con inventario pero sin venta en el período
--   · La fecha para ítems sin venta se toma del parámetro
-- ════════════════════════════════════════════════════════════════════════
base_union AS (
    SELECT
        COALESCE(v.id_empresa,    'N/D')            AS id_empresa,
        COALESCE(v.id_co,         ri.id_co)         AS id_co,
        COALESCE(v.id_local,      ri.id_local)      AS id_local,
        COALESCE(v.id_item,       ri.id_item)       AS id_item,
        -- Fecha: para ítems solo en inventario, usar fecha_ini del parámetro
        COALESCE(v.fecha_consulta,
                 :fecha_ini)                        AS fecha_consulta,
        -- Ventas (NULL → 0 para ítems sin venta)
        COALESCE(v.venta_sin_impuesto, 0)           AS venta_sin_impuesto,
        COALESCE(v.unidades_vendidas,  0)           AS unidades_vendidas,
        COALESCE(v.costo_total_ventas, 0)           AS costo_total_ventas,
        -- Inventario
        COALESCE(ri.inv_cantidad,  0)               AS inv_cantidad,
        COALESCE(ri.inv_valor,     0)               AS inv_valor,
        COALESCE(ri.inv_disponible,0)               AS inv_disponible,
        ri.lapso_doc                                AS lapso_inventario,
        ri.fecha_ultimo_ingreso,
        ri.fecha_ultima_venta_inv,
        ri.fecha_ultimo_movimiento,
        -- Costo promedio: preferir del inventario; si no, calcular de ventas
        CASE
            WHEN COALESCE(ri.costo_promedio, 0) > 0
                THEN ri.costo_promedio
            WHEN COALESCE(v.unidades_vendidas, 0) > 0
                THEN ROUND(
                    COALESCE(v.costo_total_ventas, 0)
                    / NULLIF(v.unidades_vendidas, 0),
                    4)
            ELSE 0
        END                                         AS costo_promedio
    FROM ventas_dia v
    FULL OUTER JOIN inventario_mes ri
        ON  v.id_co    = ri.id_co
        AND v.id_local = ri.id_local
        AND v.id_item  = ri.id_item
)

-- ════════════════════════════════════════════════════════════════════════
-- QUERY FINAL: Une la base con maestros de empresa, CO, bodega e ítem
-- ════════════════════════════════════════════════════════════════════════
SELECT
    -- ── EMPRESA ──────────────────────────────────────────────────────
    b.id_empresa,
    COALESCE(BTRIM(e.descripcion), 'SIN EMPRESA')       AS nombre_empresa,

    -- ── SEDE / CENTRO DE OPERACIÓN ───────────────────────────────────
    b.id_co,
    COALESCE(BTRIM(co.descripcion), 'SIN SEDE')         AS nombre_co,

    -- ── BODEGA PRINCIPAL ─────────────────────────────────────────────
    b.id_local                                           AS id_bodega,
    COALESCE(BTRIM(bod.cmlocal_descripcion), '')         AS nombre_bodega,

    -- ── ÍTEM ─────────────────────────────────────────────────────────
    b.id_item,
    COALESCE(mi.descripcion_item, 'SIN DESCRIPCION')    AS descripcion_item,
    COALESCE(mi.unidad_medida, '')                       AS unidad_medida,

    -- ── FECHA ────────────────────────────────────────────────────────
    b.fecha_consulta,
    -- Formato fecha legible (opcional para BI)
    TO_DATE(b.fecha_consulta, 'YYYYMMDD')               AS fecha_consulta_date,

    -- ── VENTAS DEL DÍA ───────────────────────────────────────────────
    ROUND(b.venta_sin_impuesto, 2)                       AS venta_sin_impuesto,
    ROUND(b.unidades_vendidas,  4)                       AS unidades_vendidas,

    -- ── INVENTARIO (foto del mes/día) ────────────────────────────────
    ROUND(b.inv_cantidad,   4)                           AS inv_cantidad,
    ROUND(b.inv_valor,      2)                           AS inv_valor,
    ROUND(b.inv_disponible, 4)                           AS inv_disponible,
    b.lapso_inventario,

    -- ── FECHAS DE REFERENCIA ─────────────────────────────────────────
    b.fecha_ultimo_ingreso,
    -- Fecha última venta: preferir la de ventas del día si existe,
    -- de lo contrario usar la almacenada en inventario
    CASE
        WHEN b.unidades_vendidas > 0 THEN b.fecha_consulta
        ELSE COALESCE(b.fecha_ultima_venta_inv, '')
    END                                                  AS fecha_ultima_venta,
    b.fecha_ultimo_movimiento,

    -- ── COSTO ────────────────────────────────────────────────────────
    ROUND(b.costo_promedio,     4)                       AS costo_promedio,
    ROUND(b.costo_total_ventas, 2)                       AS costo_total_ventas,
    -- Margen bruto del día (solo aplica cuando hay venta)
    CASE
        WHEN b.venta_sin_impuesto <> 0
        THEN ROUND(
            (b.venta_sin_impuesto - b.costo_total_ventas)
            / NULLIF(b.venta_sin_impuesto, 0) * 100,
            2)
        ELSE NULL
    END                                                  AS margen_bruto_pct,

    -- ── LÍNEA NIVEL 1 ────────────────────────────────────────────────
    COALESCE(mi.id_linea_n1, '')                         AS id_linea_n1,
    COALESCE(mi.nombre_linea_n1, 'SIN LINEA')           AS nombre_linea_n1,

    -- ── CATEGORÍA 4 ──────────────────────────────────────────────────
    COALESCE(mi.id_categoria_4, '')                      AS id_categoria_4,
    COALESCE(mi.nombre_categoria_4, 'SIN CATEGORIA')    AS nombre_categoria_4,

    -- ── CAMPOS ADICIONALES RECOMENDADOS ──────────────────────────────
    -- Indicador de quiebre de inventario (ítem con venta pero sin stock)
    CASE
        WHEN b.unidades_vendidas > 0 AND b.inv_cantidad <= 0
        THEN 'SÍ' ELSE 'NO'
    END                                                  AS quiebre_inventario,

    -- Indicador de ítem inactivo (sin venta pero con inventario)
    CASE
        WHEN b.unidades_vendidas = 0 AND b.inv_cantidad > 0
        THEN 'SÍ' ELSE 'NO'
    END                                                  AS item_sin_rotacion,

    -- Días desde última venta (útil para ranking de rotación en BI)
    CASE
        WHEN b.fecha_ultima_venta_inv IS NOT NULL
             AND LENGTH(BTRIM(b.fecha_ultima_venta_inv)) = 8
        THEN (TO_DATE(b.fecha_consulta, 'YYYYMMDD')
              - TO_DATE(BTRIM(b.fecha_ultima_venta_inv), 'YYYYMMDD'))::INTEGER
        ELSE NULL
    END                                                  AS dias_desde_ultima_venta,

    -- Días desde último ingreso (rotación de entradas)
    CASE
        WHEN b.fecha_ultimo_ingreso IS NOT NULL
             AND LENGTH(BTRIM(b.fecha_ultimo_ingreso)) = 8
        THEN (TO_DATE(b.fecha_consulta, 'YYYYMMDD')
              - TO_DATE(BTRIM(b.fecha_ultimo_ingreso), 'YYYYMMDD'))::INTEGER
        ELSE NULL
    END                                                  AS dias_desde_ultimo_ingreso

FROM base_union b
-- JOIN empresa (solo para ítems que tienen ventas; el id_empresa viene de ahí)
LEFT JOIN public.empresas e
    ON  BTRIM(b.id_empresa) = BTRIM(e.codigo)
-- JOIN centro de operación
LEFT JOIN public.centro_operacion co
    ON  BTRIM(b.id_co) = BTRIM(co.codigo)
-- JOIN bodega
LEFT JOIN public.bodegas bod
    ON  BTRIM(b.id_local) = BTRIM(bod.id_local)
-- JOIN maestro ítem enriquecido
LEFT JOIN maestro_item mi
    ON  BTRIM(b.id_item) = BTRIM(mi.id_item)

ORDER BY
    b.id_co,
    b.id_local,
    mi.nombre_linea_n1,
    mi.nombre_categoria_4,
    b.id_item;
```

---

### 3.1 Campos adicionales justificados

| Campo | Por qué se incluye |
|---|---|
| `margen_bruto_pct` | Indicador clave de rentabilidad. En rotación de productos, los ítems con alta rotación y bajo margen requieren análisis distinto a los de baja rotación y alto margen. |
| `quiebre_inventario` | Alerta crítica para supermercados: ítem que se vendió pero quedó sin stock. Permite accionar reabastecimiento. |
| `item_sin_rotacion` | Identifica artículos con inventario inmovilizado. En retail, >60 días sin venta suele ser señal de obsolescencia o mal surtido. |
| `dias_desde_ultima_venta` | Métrica base para calcular la "velocidad de rotación" en el tablero BI. |
| `dias_desde_ultimo_ingreso` | Permite detectar ítems que llevan mucho tiempo sin recibir mercancía. |
| `inv_disponible` | Diferente de `inv_cantidad`: descuenta reservas/compromisos. Más preciso para decisiones de compra. |
| `lapso_inventario` | Trazabilidad del período del que proviene la foto de inventario. |
| `unidad_medida` | Necesaria para contextualizar las unidades vendidas e inventario. |
| `ultimo_costo` | Complementa el costo promedio. Útil cuando hay variaciones grandes de precio de compra. |

---

## 4. TABLA DESTINO PARA EL ETL

### 4.1 DDL propuesto: `rotacion_base_item_dia_sede`

```sql
/*
════════════════════════════════════════════════════════════════════════════════
  TABLA DESTINO ETL — TABLERO ROTACIÓN DE ÍTEMS
  Estrategia:
    · Una fila por (empresa + co + bodega + ítem + fecha)
    · El inventario se protege: una vez cargado, NO se reemplaza
      en reprocesos. Solo las ventas se actualizan.
    · La columna inv_bloqueado controla este comportamiento.
════════════════════════════════════════════════════════════════════════════════
*/
CREATE TABLE public.rotacion_base_item_dia_sede (

    -- ── LLAVES ───────────────────────────────────────────────────────
    id_empresa              VARCHAR(2)       NOT NULL,
    id_co                   VARCHAR(3)       NOT NULL,
    id_local                VARCHAR(5)       NOT NULL,
    id_item                 VARCHAR(6)       NOT NULL,
    fecha_consulta          CHAR(8)          NOT NULL,  -- YYYYMMDD

    -- ── EMPRESA Y SEDE ───────────────────────────────────────────────
    nombre_empresa          VARCHAR(40),
    nombre_co               VARCHAR(40),
    nombre_bodega           VARCHAR(40),

    -- ── ÍTEM ─────────────────────────────────────────────────────────
    descripcion_item        VARCHAR(40),
    unidad_medida           VARCHAR(3),

    -- ── VENTAS ───────────────────────────────────────────────────────
    venta_sin_impuesto      NUMERIC(20,2)    NOT NULL DEFAULT 0,
    unidades_vendidas       NUMERIC(20,4)    NOT NULL DEFAULT 0,
    costo_total_ventas      NUMERIC(20,2)    NOT NULL DEFAULT 0,
    margen_bruto_pct        NUMERIC(10,2),

    -- ── INVENTARIO (foto protegida) ──────────────────────────────────
    inv_cantidad            NUMERIC(20,4),
    inv_valor               NUMERIC(20,2),
    inv_disponible          NUMERIC(20,4),
    lapso_inventario        CHAR(6),         -- YYYYMM de origen
    costo_promedio          NUMERIC(20,4),
    ultimo_costo            NUMERIC(20,4),
    -- Bandera de protección: TRUE = inventario cargado, no sobrescribir
    inv_bloqueado           BOOLEAN          NOT NULL DEFAULT FALSE,

    -- ── FECHAS DE REFERENCIA ─────────────────────────────────────────
    fecha_ultimo_ingreso    CHAR(8),
    fecha_ultima_venta      CHAR(8),
    fecha_ultimo_movimiento CHAR(8),
    dias_desde_ultima_venta INTEGER,
    dias_desde_ultimo_ingreso INTEGER,

    -- ── CLASIFICACIÓN ────────────────────────────────────────────────
    id_linea_n1             VARCHAR(6),
    nombre_linea_n1         VARCHAR(40),
    id_categoria_4          VARCHAR(4),
    nombre_categoria_4      VARCHAR(40),

    -- ── INDICADORES ──────────────────────────────────────────────────
    quiebre_inventario      CHAR(2),         -- 'SÍ' / 'NO'
    item_sin_rotacion       CHAR(2),         -- 'SÍ' / 'NO'

    -- ── AUDITORÍA ETL ────────────────────────────────────────────────
    fecha_carga_etl         TIMESTAMP        NOT NULL DEFAULT NOW(),
    fecha_mod_etl           TIMESTAMP,
    usuario_etl             VARCHAR(30),
    origen_carga            VARCHAR(10),     -- 'DIARIO' / 'REPROCESO'

    -- ── LLAVE ÚNICA ──────────────────────────────────────────────────
    CONSTRAINT pk_rotacion_item_dia
        PRIMARY KEY (id_empresa, id_co, id_local, id_item, fecha_consulta)
);

-- Comentarios de columnas
COMMENT ON TABLE  public.rotacion_base_item_dia_sede
    IS 'Tabla destino ETL para tablero de Rotación de Ítems. Una fila por empresa+CO+bodega+ítem+fecha.';
COMMENT ON COLUMN public.rotacion_base_item_dia_sede.inv_bloqueado
    IS 'TRUE indica que el inventario de esta fila ya fue capturado y no debe sobrescribirse en reprocesos.';
COMMENT ON COLUMN public.rotacion_base_item_dia_sede.lapso_inventario
    IS 'Período YYYYMM del que proviene el snapshot de inventario (cmresumen_inventario.lapso_doc).';
```

---

## 5. ESTRATEGIA DE CARGA DIARIA Y REPROCESO

### 5.1 Carga diaria (modo normal)

Se ejecuta una vez al día, al final de la jornada operativa (ej. 23:30 o al día siguiente a las 05:00).

**Paso 1 — Ejecutar la query principal** con `:fecha_ini = :fecha_fin = FECHA_HOY` en formato YYYYMMDD.

**Paso 2 — INSERT con ON CONFLICT:**

```sql
INSERT INTO public.rotacion_base_item_dia_sede (
    id_empresa, id_co, id_local, id_item, fecha_consulta,
    nombre_empresa, nombre_co, nombre_bodega,
    descripcion_item, unidad_medida,
    venta_sin_impuesto, unidades_vendidas, costo_total_ventas, margen_bruto_pct,
    inv_cantidad, inv_valor, inv_disponible, lapso_inventario,
    costo_promedio, ultimo_costo, inv_bloqueado,
    fecha_ultimo_ingreso, fecha_ultima_venta, fecha_ultimo_movimiento,
    dias_desde_ultima_venta, dias_desde_ultimo_ingreso,
    id_linea_n1, nombre_linea_n1, id_categoria_4, nombre_categoria_4,
    quiebre_inventario, item_sin_rotacion,
    fecha_carga_etl, usuario_etl, origen_carga
)
SELECT
    -- ... (resultado de la query principal)
    NOW(), 'ETL_ROTACION', 'DIARIO'
FROM (
    -- Aquí va la query completa del punto 3
    ...
) AS fuente

ON CONFLICT (id_empresa, id_co, id_local, id_item, fecha_consulta)
DO UPDATE SET
    -- Actualizar SOLO ventas, no inventario
    venta_sin_impuesto      = EXCLUDED.venta_sin_impuesto,
    unidades_vendidas       = EXCLUDED.unidades_vendidas,
    costo_total_ventas      = EXCLUDED.costo_total_ventas,
    margen_bruto_pct        = EXCLUDED.margen_bruto_pct,
    fecha_ultima_venta      = EXCLUDED.fecha_ultima_venta,
    dias_desde_ultima_venta = EXCLUDED.dias_desde_ultima_venta,
    quiebre_inventario      = EXCLUDED.quiebre_inventario,
    item_sin_rotacion       = EXCLUDED.item_sin_rotacion,
    fecha_mod_etl           = NOW(),
    origen_carga            = 'DIARIO',
    -- El inventario SOLO se actualiza si aún no ha sido bloqueado
    inv_cantidad    = CASE WHEN rotacion_base_item_dia_sede.inv_bloqueado
                          THEN rotacion_base_item_dia_sede.inv_cantidad
                          ELSE EXCLUDED.inv_cantidad END,
    inv_valor       = CASE WHEN rotacion_base_item_dia_sede.inv_bloqueado
                          THEN rotacion_base_item_dia_sede.inv_valor
                          ELSE EXCLUDED.inv_valor END,
    inv_disponible  = CASE WHEN rotacion_base_item_dia_sede.inv_bloqueado
                          THEN rotacion_base_item_dia_sede.inv_disponible
                          ELSE EXCLUDED.inv_disponible END,
    costo_promedio  = CASE WHEN rotacion_base_item_dia_sede.inv_bloqueado
                          THEN rotacion_base_item_dia_sede.costo_promedio
                          ELSE EXCLUDED.costo_promedio END;

-- Paso 3 — Bloquear el inventario del día recién cargado
UPDATE public.rotacion_base_item_dia_sede
SET inv_bloqueado = TRUE
WHERE fecha_consulta = :fecha_hoy
  AND inv_bloqueado  = FALSE;
```

### 5.2 Reproceso de fechas anteriores

Se ejecuta cuando se detectan errores en una fecha pasada o cuando el ERP ajusta movimientos retroactivos.

**Regla de oro:** Las ventas SIEMPRE se pueden reprocesar. El inventario NUNCA se reemplaza si `inv_bloqueado = TRUE`.

```sql
-- Reproceso de ventas: solo actualiza ventas para la fecha indicada
-- El inventario histórico queda intacto por el mecanismo ON CONFLICT anterior.
-- Para reprocesar con actualización FORZADA de inventario (caso excepcional):

UPDATE public.rotacion_base_item_dia_sede
SET inv_bloqueado = FALSE  -- desbloquear explícitamente
WHERE fecha_consulta = :fecha_reproceso
  AND id_empresa     = :id_empresa
  -- SOLO ejecutar esto con autorización explícita del analista de datos.
  -- Luego volver a ejecutar el proceso de carga para esa fecha.
;

-- Para eliminar y reprocesar una fecha completa:
DELETE FROM public.rotacion_base_item_dia_sede
WHERE fecha_consulta = :fecha_reproceso
  AND id_empresa     = :id_empresa;
-- Luego ejecutar el INSERT normal del punto 5.1.
```

### 5.3 Tabla de control de procesos ETL (recomendada)

```sql
CREATE TABLE public.etl_control_rotacion (
    id              SERIAL PRIMARY KEY,
    fecha_proceso   CHAR(8)       NOT NULL,  -- YYYYMMDD
    id_empresa      VARCHAR(2)    NOT NULL,
    tipo_carga      VARCHAR(10)   NOT NULL,  -- 'DIARIO' / 'REPROCESO'
    estado          VARCHAR(15)   NOT NULL,  -- 'INICIADO' / 'OK' / 'ERROR'
    filas_insertadas INTEGER,
    filas_actualizadas INTEGER,
    mensaje_error   TEXT,
    fecha_inicio    TIMESTAMP     NOT NULL DEFAULT NOW(),
    fecha_fin       TIMESTAMP,
    usuario         VARCHAR(30),
    CONSTRAINT uq_etl_control
        UNIQUE (fecha_proceso, id_empresa, tipo_carga, fecha_inicio)
);
```

---

## 6. ÍNDICES RECOMENDADOS

### 6.1 Sobre `cmmovimiento_ventas`

```sql
-- Índice principal para filtros de fecha + bodega + empresa
-- Cubre el filtro más frecuente del ETL
CREATE INDEX idx_cmvtas_fecha_emp_co_local
    ON public.cmmovimiento_ventas (fecha_dcto, id_emp, id_co, id_local)
    WHERE id_local LIKE '%01';
-- Por qué: la query filtra siempre por fecha_dcto (rango) y luego por co/local/emp.

-- Índice para join con ítems y agrupación
CREATE INDEX idx_cmvtas_item_fecha
    ON public.cmmovimiento_ventas (id_item, fecha_dcto, id_co, id_local);
-- Por qué: acelera el GROUP BY de la CTE ventas_dia cuando se filtra por ítem específico.
```

### 6.2 Sobre `cmresumen_inventario`

```sql
-- Índice principal para el ETL de rotación
CREATE INDEX idx_cmrinv_lapso_local_item
    ON public.cmresumen_inventario (lapso_doc, id_local, id_co, id_item)
    WHERE id_local LIKE '%01';
-- Por qué: la query filtra por lapso_doc (YYYYMM) y bodegas principales primero.

-- Índice de cobertura para evitar table scan
CREATE INDEX idx_cmrinv_covering
    ON public.cmresumen_inventario (lapso_doc, id_co, id_local, id_item)
    INCLUDE (can_exis_fin, vlr_cost_fin, costo_uni, fecha_ultent, fecha_ultvta);
-- Por qué: si la query solo necesita estos campos, el plan puede ser index-only scan.
```

### 6.3 Sobre `items`

```sql
-- Índice para el join con líneas y criterios (id_tipo es discriminador)
CREATE INDEX idx_items_tipo_linea1
    ON public.items (id_tipo, id_linea1);
-- Por qué: el join maestro_item usa id_tipo + id_linea1 en la condición.

CREATE INDEX idx_items_tipo_cricla4
    ON public.items (id_tipo, id_cricla4);
-- Por qué: ídem para el join con criterios_itm_4.
```

### 6.4 Sobre la tabla destino `rotacion_base_item_dia_sede`

```sql
-- La PK ya crea un índice único en (id_empresa, id_co, id_local, id_item, fecha_consulta).
-- Índices adicionales para consultas del tablero BI:

-- Por fecha (filtro más frecuente en dashboards)
CREATE INDEX idx_rotacion_fecha
    ON public.rotacion_base_item_dia_sede (fecha_consulta, id_empresa, id_co);

-- Por línea + categoría (para drill-down en BI)
CREATE INDEX idx_rotacion_linea_cat
    ON public.rotacion_base_item_dia_sede (id_linea_n1, id_categoria_4, fecha_consulta);

-- Para detectar quiebres de inventario
CREATE INDEX idx_rotacion_quiebre
    ON public.rotacion_base_item_dia_sede (fecha_consulta, quiebre_inventario)
    WHERE quiebre_inventario = 'SÍ';

-- Para ítem sin rotación
CREATE INDEX idx_rotacion_sin_rotar
    ON public.rotacion_base_item_dia_sede (fecha_consulta, item_sin_rotacion)
    WHERE item_sin_rotacion = 'SÍ';
```

---

## 7. RIESGOS E INCONSISTENCIAS

### 7.1 Inventario mensual vs. análisis diario

**Riesgo:** `cmresumen_inventario.lapso_doc` es mensual. Si el ETL corre el 15 del mes, `can_exis_fin` refleja el saldo acumulado hasta ese momento, no el saldo al cierre del día 1, 2, etc.

**Impacto:** Los ítems cargados en días anteriores del mismo mes mostrarán el mismo inventario (el acumulado al día de la foto). Esto es un límite estructural del ERP, no de la query.

**Mitigación:** Documentar en el tablero BI que "el inventario corresponde al saldo corriente del mes al momento de la carga". Usar `lapso_inventario` para transparencia. Si se requiere precisión diaria, considerar calcular el inventario desde `cmmovimiento_inventario` sumando movimientos día a día (más costoso pero más preciso).

### 7.2 Empresa en inventario

**Riesgo:** `cmresumen_inventario` NO tiene `id_emp`. Si hay dos empresas con el mismo código de CO e ítem (poco probable pero posible en ambientes multi-empresa), el inventario podría asignarse a la empresa equivocada.

**Mitigación:** En el INSERT ON CONFLICT, el `id_empresa` viene siempre de las ventas. Para los ítems que solo tienen inventario pero ninguna venta en el período, el `id_empresa = 'N/D'`. Esto puede filtrarse o mapearse con una tabla de configuración `co_empresa` que explicite qué empresa controla cada CO.

### 7.3 Ítems con venta pero sin inventario registrado

Ocurre cuando:
- El ERP aún no procesó el cierre del período.
- La venta fue registrada en una bodega sin movimientos de inventario.
- El ítem es un servicio o producto sin control de existencias.

**Manejo:** El FULL OUTER JOIN los incluye con `inv_cantidad = 0`. El campo `quiebre_inventario = 'SÍ'` los marca. Validar con la query V7 del punto 2.

### 7.4 Ítems con inventario pero sin venta

Normal en retail: no todos los SKUs rotan cada día. El FULL OUTER JOIN los incluye con `venta_sin_impuesto = 0` y `item_sin_rotacion = 'SÍ'`. Son clave para el análisis de rotación lenta.

### 7.5 Bodegas y CO: diferentes granularidades

**Riesgo:** Un CO puede tener múltiples bodegas, no solo la `%01`. Si por alguna configuración el ERP registra ventas en bodegas secundarias (ej. `%02`, `%03`) y el filtro `LIKE '%01'` las excluye, las ventas de esas bodegas no aparecerán.

**Validación:**
```sql
SELECT DISTINCT id_local, COUNT(*)
FROM public.cmmovimiento_ventas
WHERE fecha_dcto = :fecha_ini
GROUP BY id_local
ORDER BY COUNT(*) DESC;
```
Si aparecen bodegas que no terminan en `01` con volumen significativo, revisar la regla de negocio.

### 7.6 Duplicados por join mal definido

El riesgo principal es en el join `items → lineas`. Si un ítem tiene `id_linea1` que coincide con múltiples registros en `lineas` (misma `id_linea` pero distinto `id_tipo`), el join sin el discriminador `id_tipo` multiplicaría filas.

**Mitigación:** La CTE `maestro_item` siempre incluye `AND BTRIM(i.id_tipo) = BTRIM(ln1.id_tipo)`. Validar con:
```sql
SELECT id_linea, id_tipo, COUNT(*) FROM public.lineas
GROUP BY id_linea, id_tipo HAVING COUNT(*) > 1;
```

### 7.7 Fechas de cierre del período

Si el ERP tiene un proceso de "cierre de período" que bloquea movimientos anteriores, los reprocesos sobre fechas cerradas pueden no reflejar cambios aunque el ETL se ejecute. Confirmar con el administrador del ERP si `lapso_doc` en `cmresumen_inventario` puede recalcularse tras el cierre.

### 7.8 Signos y anulaciones en ventas

Las devoluciones o notas crédito en `cmmovimiento_ventas` pueden generar `tot_venta` negativo o `cantidad` negativa. La query los suma correctamente (reducen la venta neta). Sin embargo, si una fecha tiene más devoluciones que ventas brutas, `venta_sin_impuesto` resultará negativa — lo cual es correcto desde el punto de vista contable pero puede confundir en el tablero. Agregar el campo `COUNT(DISTINCT documento_fc)` como `num_transacciones` permite detectar estos casos.

---

## RESUMEN DE TABLAS UTILIZADAS

| Tabla | Filtro principal | Join principal |
|---|---|---|
| `cmmovimiento_ventas` | `fecha_dcto BETWEEN` + `id_local LIKE '%01'` | id_emp, id_co, id_local, id_item |
| `cmresumen_inventario` | `lapso_doc = YYYYMM` + `id_local LIKE '%01'` | id_co, id_local, id_item |
| `items` | ninguno (lookup) | id_item + id_tipo |
| `lineas` | ninguno (lookup) | id_linea + id_tipo |
| `criterios_itm_4` | ninguno (lookup) | id_cricla4 + id_catego |
| `centro_operacion` | ninguno (lookup) | codigo |
| `empresas` | ninguno (lookup) | codigo |
| `bodegas` | `id_local LIKE '%01'` (lookup) | id_local |

---

*Documento generado el 2026-04-24 — Para uso exclusivo del proyecto ETL Rotación de Ítems (Mercamio)*
