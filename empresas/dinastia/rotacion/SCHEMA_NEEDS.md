# SCHEMA_NEEDS — Rotación / baja salida (Dinastia, ERP Siesa/Biable MySQL)

Lo que falta para pasar del **scaffold** al **ETL real**. El scaffold reproduce la
ESTRUCTURA de Mercamio; aquí está lo que hay que **confirmar contra el ERP** antes de
poner `rotacion.schema_confirmed: true`.

Solo tenemos **nombres de tabla**, no columnas. Todo lo marcado `⧗` es una **suposición**
tomada de la referencia PostgreSQL de Mercamio y debe validarse (p. ej. con
`SHOW COLUMNS FROM <tabla>` / `INFORMATION_SCHEMA.COLUMNS` en `BD_BIABLE01`, o con el
diccionario de datos de Siesa/Biable).

---

## 1. Mapa de tablas Mercamio (PostgreSQL) → Dinastia (MySQL)

| Rol                                | Mercamio (ref)          | Dinastia (ERP) ⧗          |
|------------------------------------|-------------------------|---------------------------|
| Maestro de ítem                    | `items`                 | `ITEMS`                   |
| Grupo / categoría (nombre)         | `categorias`            | `GRUPO_INVENTARIO`        |
| Línea / sublínea (nombre)          | `lineas`                | `LINEAS`                  |
| Foto mensual de inventario         | `cmresumen_inventario`  | `CMRESUMEN_INVENTARIO`    |
| Salidas por venta                  | `cmmovimiento_pdv`      | `CMMOVIMIENTO_VENTAS`     |
| Maestro de bodega/sede (nombre)    | `centro_operacion`      | `BODEGAS`                 |
| Movimientos de inventario (alt.)   | *(n/a)*                 | `CMMOVIMIENTO_INVENTARIO` |

## 2. Columnas requeridas por tabla (confirmar nombre y tipo)

### `ITEMS` — maestro
| Necesito (rol)                | Mercamio               | Dinastia ⧗          | Notas |
|-------------------------------|------------------------|---------------------|-------|
| id de ítem (PK)               | `id_item`              | `id_item` ?         | ¿trae espacios? (Mercamio hace BTRIM) |
| descripción                   | `descripcion`          | `descripcion` ?     | |
| unidad de inventario          | `unimed_inv_1`/`unimed_com` | `unidad_inv` ? | fallback de unidad |
| grupo/categoría (FK)          | `id_tipo`              | `id_grupo` ?        | **¿existe el filtro tipo id_tipo='4'?** ver §4 |
| línea nivel 1 (FK)            | `id_linea1`            | `id_linea1` ?       | |
| línea nivel 2 (FK)            | `id_linea2`            | `id_linea2` ?       | |
| costo (fallback)              | `costo_act_acum`, `ultimo_costo_ed` | `costo_prom` ? | para kits y costo_uni=0 |

### `GRUPO_INVENTARIO` — nombre de grupo/categoría
- clave: `id_grupo` ⧗ · descripción: `descripcion` ⧗
- **Pregunta:** ¿hay un grupo objetivo (equivalente a `id_tipo='4'` = "mercado" en Mercamio)?
  ¿O el reporte de baja salida cubre **todos** los grupos?

### `LINEAS` — nombre de línea/sublínea
- clave: `id_linea` ⧗ · grupo: `id_grupo`/`id_tipo` ⧗ · descripción: `descripcion` ⧗
- **Pregunta:** ¿el join es solo por `id_linea` o también por grupo (como Mercamio, que une por
  `id_linea`+`id_tipo`)? Importa si un mismo `id_linea` se reusa entre grupos.

### `CMRESUMEN_INVENTARIO` — foto de inventario (el corazón del "stock")
| Necesito (rol)                    | Mercamio        | Dinastia ⧗       | Notas |
|-----------------------------------|-----------------|------------------|-------|
| sede / centro                     | `id_co`         | `id_co` ?        | ¿sede y bodega son campos distintos? |
| bodega                            | `id_local`      | `id_bodega` ?    | ¿existe "bodega principal"? ver §5 |
| ítem                              | `id_item`       | `id_item` ?      | |
| lapso (periodo)                   | `lapso_doc` (YYYYMM) | `lapso` ?   | **¿el resumen es MENSUAL? ¿formato YYYYMM?** clave para la lógica de foto |
| stock disponible                  | `can_disponible`| `can_disponible` ? | Mercamio usa disponible (excluye reservas), no `can_exis_fin` |
| última compra                     | `fecha_ultcom`  | `fecha_ultcom` ? | ¿DATE o texto YYYYMMDD? |
| última entrada                    | `fecha_ultent`  | `fecha_ultent` ? | |
| última venta                      | `fecha_ultvta`  | `fecha_ultvta` ? | fuente de `ultima_venta_inventario` |
| costo unitario                    | `costo_uni`     | `costo_uni` ?    | |

### `CMMOVIMIENTO_VENTAS` — salidas (ventas)
| Necesito (rol)                    | Mercamio          | Dinastia ⧗       | Notas |
|-----------------------------------|-------------------|------------------|-------|
| sede                              | `id_co`           | `id_co` ?        | |
| bodega                            | `id_local`        | `id_bodega` ?    | |
| ítem                              | `id_item`         | `id_item` ?      | |
| fecha del documento               | `fecha_dcto` (texto YYYYMMDD) | `fecha_dcto` ? | **¿DATE o texto?** ver §3 |
| unidad                            | `id_unidad`       | `id_unidad` ?    | |
| cantidad vendida                  | `cantidad`        | `cantidad` ?     | |
| valor sin impuesto                | `vlrtot_bru`      | `valor_bruto` ?  | |
| costo total                       | `tot_costo`       | `tot_costo` ?    | |
| tipo de documento                 | `docto_acumulacion` (excluye `Z%`) | `tipo_doc` ? | **¿hay notas crédito a excluir?** |

### `BODEGAS` — nombre de bodega/sede
- clave: `id_bodega`/`id_co` ⧗ · descripción: `descripcion` ⧗
- **Pregunta:** ¿hay una bodega/sede a excluir (equivalente a `PPT` = planta en Mercamio)?

### `CMMOVIMIENTO_INVENTARIO` — (alternativa/complemento)
- **Pregunta clave:** ¿las **salidas** para "baja salida" deben salir de `CMMOVIMIENTO_VENTAS`
  (solo ventas) o de `CMMOVIMIENTO_INVENTARIO` (todos los movimientos: ventas, traslados, ajustes,
  devoluciones)? Traslados y consumos también "sacan" stock; puede cambiar mucho la clasificación.
  Columnas típicas a confirmar: `id_bodega`, `id_item`, `fecha`, `tipo_movimiento`
  (entrada/salida), `cantidad`. Configurable con `rotacion.fuente_salidas` (`ventas` | `inventario`).

## 3. Fechas: ¿texto o DATE? (bloqueante para el SQL)

Mercamio guarda fechas como **texto `YYYYMMDD`** y las valida con regex antes de `TO_DATE`.
En Siesa/Biable normalmente son **`DATE`/`DATETIME` nativas**. Hay que confirmar por columna
(`fecha_dcto`, `fecha_ultcom`, `fecha_ultent`, `fecha_ultvta`, `lapso`). Impacto en el stub:
- Si es **DATE**: comparar `mv.fecha_dcto = STR_TO_DATE(%s,'%Y%m%d')` y **quitar** el
  `REGEXP '^[12][0-9]{7}$'` y los `STR_TO_DATE` de salida (ya son fechas).
- Si es **texto**: dejar el patrón regex y `STR_TO_DATE` como en el stub.

## 4. Universo de ítems (filtro de categoría)

Mercamio restringe a `id_tipo='4'`. **Confirmar el equivalente en Dinastia:** ¿un `id_grupo`
específico?, ¿varios?, ¿todos? Esto define el `WHERE` de `items_maestro` en `SOURCE_SQL_STUB`.

## 5. Sede vs. bodega y "bodega principal"

Mercamio separa **sede** (`id_co`) de **bodega** (`id_local`) y filtra `RIGHT(id_local,2)='01'`
(bodega principal). En Dinastia hay que confirmar:
- ¿`CMRESUMEN_INVENTARIO`/`CMMOVIMIENTO_VENTAS` tienen sede **y** bodega, o solo bodega?
- ¿Existe el concepto "bodega principal" y cómo se identifica? (config `rotacion.solo_bodegas_principales`).
- El scaffold hoy usa `id_bodega` como `sede` y deja `bodega_local=''` — **ajustar** cuando se
  aclare el modelo (afecta la PK).

## 6. Cómo se calcula "baja salida / rotación" (definición de negocio — PENDIENTE)

El scaffold implementa una definición **por defecto, reversible** en `compute_rotacion()`:

```
dias_sin_venta   = hoy − ultima_venta_pdv           # None si nunca vendió
rotacion_ventana = salidas_ventana / can_disponible # proxy de rotación (unidades/stock)
flag_baja_salida = stock > 0 AND (
                       salidas_ventana <= 0          # sin salidas en la ventana
                    OR dias_sin_venta  > umbral_dias # o última venta muy vieja
                    OR nunca_vendió
                    OR rotacion_ventana < umbral_rot # o rota por debajo del umbral
                   )
```

**Preguntas que negocio debe responder** (hoy son parámetros en `config.yaml`):
1. **Ventana de medición de salidas:** ¿30 / 60 / 90 / 180 / 365 días? El scaffold usa una
   ventana principal (`ventana_salida_dias=90`) + ventanas de reporte (aún no extraídas).
2. **Numerador/denominador de "rotación":** ¿`salidas_ventana / stock_actual` (proxy simple, el
   del scaffold) o `salidas / stock_promedio_del_periodo` (rotación contable clásica)? El promedio
   requiere stock histórico por lapso, que `CMRESUMEN_INVENTARIO` sí guarda (mensual).
3. **Umbrales:** ¿a partir de cuántos días sin venta y/o qué ratio se marca "baja salida"?
   (`dias_sin_venta_umbral`, `rotacion_umbral`).
4. **Fuente de salidas:** solo ventas vs. todos los movimientos de inventario (ver §2,
   `CMMOVIMIENTO_INVENTARIO`).
5. **¿Materializar o dejar al BI?** Mercamio guarda solo señales crudas y deja `dias_sin_venta`
   al portal. Si Dinastia prefiere ese enfoque, se desactiva `compute_rotacion()` y el BI calcula.

## 7. Destino GCP (PENDIENTE de decisión)

**Aún no está decidido** BigQuery vs Cloud SQL PostgreSQL. El loader es abstracto
(`common/gcp_loader.py`) con dos stubs. Para cerrar:
- **Si Cloud SQL (PostgreSQL):** ruta casi 1:1 con Mercamio — se reutiliza el DDL, el `ON CONFLICT
  DO UPDATE` con protección de foto (`inv_foto_bloqueada`) y `execute_values`. Recomendado si se
  quiere máxima reutilización.
- **Si BigQuery:** no hay UPSERT por fila; se necesita patrón MERGE (staging → target) o
  "reemplazar el día" (DELETE por `(empresa, fecha_dia)` + insert, como `cargar_margen.py`).
  Definir además particionado (por `fecha_dia`) y clustering (por `sede`, `id_item`).
- **Preguntas transversales:** proyecto/dataset o instancia destino, credenciales (service account
  vs cloud-sql-proxy), retención/particionado, y quién crea la tabla (¿el ETL con `ensure_target`
  o infra aparte?).

## 8. Checklist para activar el ETL real

- [ ] Confirmar columnas de las 6-7 tablas (§2) y rellenar `SOURCE_SQL_STUB`.
- [ ] Resolver fechas texto vs DATE (§3) y ajustar el SQL.
- [ ] Definir el filtro de universo de ítems (§4).
- [ ] Aclarar modelo sede/bodega y "bodega principal" (§5) — puede cambiar la PK.
- [ ] Cerrar la definición de "baja salida" con negocio (§6).
- [ ] Decidir destino GCP e implementar el loader correspondiente (§7).
- [ ] Poner `rotacion.schema_confirmed: true` y validar con `--check-only` y un backfill corto.
