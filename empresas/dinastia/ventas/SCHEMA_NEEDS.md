# SCHEMA_NEEDS — what we must confirm before the ventas ETL can run

The Dinastia scaffold reproduces the Mercamio pipeline **structure**, but the
source query in `etl/ventas_rango.py` is a **STUB** guarded by
`SCHEMA_CONFIRMED = False`. We have **table names only** for `BD_BIABLE01`
(MySQL 8.0, Siesa/Biable, `192.168.30.1`) — no column-level schema. This file is
the checklist to take to the Dinastia ERP DBA / Biable analyst. When every item
here is answered, fill `SOURCE_SQL`, finalize `TARGET_SCHEMA`, set
`SCHEMA_CONFIRMED = True`, implement the chosen loader, and remove this blocker.

> How to gather most of this quickly (read-only), once we have a SELECT account:
> ```sql
> SELECT table_name, column_name, data_type, is_nullable
> FROM information_schema.columns
> WHERE table_schema = 'BD_BIABLE01'
>   AND table_name IN ('CMENCABEZADO_VENTAS','CMMOVIMIENTO_VENTAS','ITEMS','LINEAS',
>                      'GRUPO_INVENTARIO','CATEGORIAS','CENTRO_OPERACION','CMDOCUMENTO_VENTAS')
> ORDER BY table_name, ordinal_position;
> -- and the keys:
> SELECT table_name, constraint_name, column_name, referenced_table_name, referenced_column_name
> FROM information_schema.key_column_usage
> WHERE table_schema = 'BD_BIABLE01' AND referenced_table_name IS NOT NULL
> ORDER BY table_name;
> ```

---

## 1. Tables in scope (names confirmed; columns NEEDED)

Legend: **[KEY]** = need the exact join key; **[MEASURE]** = need the numeric
column; **[ATTR]** = need the descriptive/id column; **[?]** = confirm the table
is actually used for this report.

### `CMENCABEZADO_VENTAS` — invoice/sales header (grain: one row per document)
- **[KEY]** primary key of the header (document id) — the column
  `CMMOVIMIENTO_VENTAS` joins back to.
- **[ATTR]** invoice **date** — name AND type. Is it a real `DATE`/`DATETIME`, or
  `YYYYMMDD` text like the Mercamio `fecha_dcto`? This decides the `BETWEEN` params.
- **[ATTR]** `centro_operacion` / store id (FK to `CENTRO_OPERACION`).
- **[ATTR]** document type / `tipo_doc` — is there a credit-note/annulment marker to
  exclude (Mercamio excludes `id_tipdoc_fc LIKE 'Z%'`)? What are Dinastia's codes?
- **[ATTR]** `tercero`/customer id (FK to `TERCEROS`), `vendedor` id (FK to `VENDEDORES`) — optional for line profitability but likely wanted for drill-down.

### `CMMOVIMIENTO_VENTAS` — invoice line / movement (grain: one row per line item)
- **[KEY]** FK back to `CMENCABEZADO_VENTAS` (header id).
- **[KEY]** FK to `ITEMS` (item id).
- **[MEASURE]** net sales value **pre-tax** (Mercamio `ven_netas`).
- **[MEASURE]** tax value (Mercamio `imp_netos`) — and is there an impoconsumo
  column like margen's `vlrimpcon1` for alcohol lines?
- **[MEASURE]** gross value (Mercamio `vlrtot_bru`).
- **[MEASURE]** **cost** (Mercamio `tot_costo`). **Central question — see section 3.**
- **[MEASURE]** quantity (Mercamio `cantidad`), unit id (`id_unidad`).
- **[ATTR]** does the line carry `centro_operacion`/bodega itself, or only the header?

### `ITEMS` — item master (item → line/group/category)
- **[KEY]** item primary key.
- **[KEY]** FK/code to `LINEAS` (Mercamio uses `id_linea1` + `id_tipo`; is Dinastia's
  line a single FK, or a `(id_linea, id_tipo)` pair as in Siesa?).
- **[ATTR]** category / `id_tipo` (FK to `CATEGORIAS`) and grouping (FK to
  `GRUPO_INVENTARIO`) — confirm which of category/group/line is "línea" for this report.
- **[ATTR]** description; unit of measure.
- **[MEASURE]** master cost columns for the **kit/zero-cost fallback** the references
  rely on (Mercamio `costo_act_acum`, `ultimo_costo_ed`). Do Dinastia items have
  equivalents? Are there kits/BOMs (a `v_kits`-like table) whose cost must be exploded?

### `LINEAS` — product line dimension
- **[KEY]** line primary key (matches the FK from `ITEMS`).
- **[ATTR]** line name/description. Is there a hierarchy (nivel 1 / nivel 2) like
  Mercamio's `id_linea1`/`id_linea2`? Which level = "línea" for rentabilidad?

### `CENTRO_OPERACION` — store/branch dimension
- **[KEY]** code (matches header FK). **[ATTR]** name. Any stores to exclude
  (Mercamio drops `PPT` production plant)?

### Confirm-if-used [?]
- `GRUPO_INVENTARIO`, `CATEGORIAS` — needed only if the report groups above/below
  "línea". Confirm the exact reporting hierarchy.
- `CMDOCUMENTO_VENTAS`, `CMESTADISTICO_VENTAS` — are these the **authoritative**
  sales source instead of encabezado+movimiento? Biable often ships a pre-aggregated
  `ESTADISTICO`/statistical table; if it already has value+cost per línea per day, the
  ETL could read it directly and skip the header/line join. **Confirm which is source of truth.**
- `LISTA_PRECIOS`, `BODEGAS`, `TERCEROS`, `VENDEDORES` — dimensions for later
  drill-downs; not required for the first line-profitability load.

---

## 2. Join keys to confirm (the spine of the query)

Fill each `?` — this is the `encabezado ↔ movimiento ↔ item ↔ línea` chain the stub
comments mark as TODO:

```
CMENCABEZADO_VENTAS.<pk_header>   =  CMMOVIMIENTO_VENTAS.<fk_header>     ?
CMMOVIMIENTO_VENTAS.<fk_item>     =  ITEMS.<pk_item>                     ?
ITEMS.<fk_linea>                  =  LINEAS.<pk_linea>                   ?
   (Siesa often: ITEMS.id_linea + ITEMS.id_tipo = LINEAS.id_linea + LINEAS.id_tipo)
CMENCABEZADO_VENTAS.<fk_co>       =  CENTRO_OPERACION.<pk_co>            ?
ITEMS.<fk_categoria/id_tipo>      =  CATEGORIAS.<pk>                     ? (if used)
```

Also confirm: are joins **numeric ids** or **space-padded text** (Siesa/Biable
frequently store codes as `CHAR` needing `TRIM()` — Mercamio wraps nearly every
join key in `BTRIM()`). If so, the MySQL query needs `TRIM()` on the keys.

---

## 3. Business-logic questions (block the "rentabilidad" definition)

1. **How is cost defined?** Rentabilidad = revenue − cost, but *which* cost?
   - The line's booked cost at sale time (`CMMOVIMIENTO_VENTAS.<tot_costo>`)?
   - Average/last cost from the item master when the line cost is 0 (the reference's
     kit fallback)? Confirm the fallback column and precedence.
   - Standard cost, or a costing table (FIFO/average) elsewhere in Biable?
2. **Revenue base:** pre-tax net (`ven_netas`) — confirmed as the profitability base?
   How are **discounts** handled (already netted, or a separate column to subtract)?
   Any **impoconsumo/alcohol** special-case like margen's línea 33?
3. **Grain of the output:** the scaffold assumes **(fecha, centro_operacion, id_linea,
   id_categoria)** per day. Is per-day right, or should it be per-month? Per store, or
   company-wide? Include seller/customer, or is that a separate drill-down report?
4. **Document scope:** which document types count as "ventas"? Exclude credit
   notes/returns/annulments, or net them in? (Mercamio excludes `Z%`; Dinastia's
   equivalent codes are unknown.)
5. **Time semantics:** invoice date vs posting date vs delivery date — which drives
   "the day a sale belongs to"? And the ERP timezone (assumed `America/Bogota`).
6. **Returns/negatives:** can lines be negative (returns)? Should rentabilidad net them?

---

## 4. GCP target question (blocks `common/loader.py`)

`common/loader.py` ships **abstract** with **two stubbed** implementations; the
concrete write path is TODO until this is decided:

- **BigQuery** or **Cloud SQL for PostgreSQL**? (config `target.type`)
- If **BigQuery**: project, dataset, table, location; **idempotency strategy**
  (BigQuery has no `ON CONFLICT` — MERGE from a temp/staging table, or
  `WRITE_TRUNCATE` per date partition?). Day-partition on `fecha`?
- If **Cloud SQL Postgres**: instance connection name (Cloud SQL connector) **or**
  direct host/IP over VPN? Then the Mercamio `INSERT ... ON CONFLICT DO UPDATE`
  upsert (or margen's `DELETE`+`COPY` day-replace) ports over almost verbatim.
- **Connectivity Dinastia box → GCP:** service account + `GOOGLE_APPLICATION_CREDENTIALS`
  for BigQuery, or Cloud SQL Auth Proxy / private IP / VPN for Cloud SQL. Which is
  approved on `servidorUAID` (Debian 12)?
- **Write mode** per run: `upsert` vs `replace_by_date` vs `append` (config
  `target.<type>.write_mode`).

---

## 5. Proposed output schema (best-guess — confirm & finalize)

`TARGET_SCHEMA` in `etl/ventas_rango.py`, grain **(empresa, fecha, centro_operacion,
id_linea, id_categoria)**:

| column | type | source / note |
|--------|------|---------------|
| `empresa` | string | literal `'dinastia'` (multi-empresa parity with the agent catalog) |
| `fecha` | date | header date (confirm col + type) |
| `centro_operacion` | string | header FK → `CENTRO_OPERACION` |
| `nombre_centro` | string | `CENTRO_OPERACION` name |
| `id_linea` | string | `ITEMS`→`LINEAS` key |
| `nombre_linea` | string | `LINEAS` description |
| `id_categoria` | string | `ITEMS.id_tipo` / `CATEGORIAS` (confirm if used) |
| `venta_sin_impuesto` | numeric | SUM net pre-tax value |
| `impuesto` | numeric | SUM tax value |
| `costo_total` | numeric | SUM cost — **definition per section 3** |
| `cantidad` | numeric | SUM quantity |
| `rentabilidad` | numeric | DERIVED: `venta_sin_impuesto − costo_total` |
| `margen_pct` | numeric | DERIVED: `rentabilidad / venta_sin_impuesto * 100` |
| `fecha_carga` | timestamp | load audit timestamp |

Open: is `id_categoria` part of the grain/PK, or just an attribute? Should
`nombre_linea`/`nombre_centro` live here (denormalized) or in separate dim tables?

---

## 6. Definition of done (flip the guard)

- [ ] Every `<placeholder>` in `SOURCE_SQL` replaced with a confirmed column.
- [ ] All join keys in section 2 confirmed (and `TRIM()` added if codes are padded text).
- [ ] "Rentabilidad"/cost definition (section 3) agreed and encoded in the SQL/transform.
- [ ] Output grain + `TARGET_SCHEMA` finalized.
- [ ] GCP target chosen; the matching loader in `common/loader.py` implemented.
- [ ] `SCHEMA_CONFIRMED = True`; validated with `--dry-run` then a one-day load on a
      non-prod GCP target; row counts sanity-checked vs a known Biable report.
