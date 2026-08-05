-- ============================================================================
-- DISEÑO: rotacion_base_item_dia_sede PARTICIONADA POR MES
-- ============================================================================
-- Estrategia : RANGE sobre fecha_dia — una partición por mes calendario
-- Ventaja    : consultas por rango de fecha leen solo la(s) partición(es)
--              relevantes, ignorando todos los demás meses
-- Índices    : se definen en la tabla madre y PostgreSQL los replica
--              automáticamente en cada partición hija
-- Migración  : al final del script hay el procedimiento para migrar los
--              datos existentes desde la tabla actual a la particionada
-- ============================================================================


-- ── 1. TABLA MADRE (particionada) ────────────────────────────────────────────
-- Reemplaza a rotacion_base_item_dia_sede actual.
-- PARTITION BY RANGE (fecha_dia) → cada partición cubre un mes exacto.
-- La PK incluye fecha_dia (obligatorio en tablas particionadas por rango de fecha).

CREATE TABLE public.rotacion_base_item_dia_sede (
    -- Llave primaria
    empresa                     VARCHAR(20)     NOT NULL,
    fecha_dia                   DATE            NOT NULL,   -- clave de partición
    sede                        VARCHAR(10)     NOT NULL,
    bodega_local                VARCHAR(10)     NOT NULL DEFAULT '',
    id_item                     VARCHAR(10)     NOT NULL,

    -- Descriptor sede
    nombre_sede                 VARCHAR(120),

    -- Maestro ítem
    nombre_item                 VARCHAR(255),
    id_unidad                   VARCHAR(10),

    -- Categoría
    id_categoria                VARCHAR(10),
    nombre_categoria            VARCHAR(100),

    -- Línea
    id_linea_nivel_1            VARCHAR(10),
    nombre_linea_nivel_1        VARCHAR(100),

    -- Ventas del día
    cantidad_vendida            NUMERIC(18,4)   DEFAULT 0,
    venta_sin_impuesto          NUMERIC(18,2)   DEFAULT 0,
    total_costo                 NUMERIC(18,2)   DEFAULT 0,

    -- Última venta (dos fuentes)
    ultima_venta_pdv            DATE,
    ultima_venta_inventario     DATE,
    estado_ultima_venta_item    VARCHAR(25),

    -- Inventario (foto del día)
    lapso_inventario            VARCHAR(6),
    can_disponible_foto         NUMERIC(18,4)   DEFAULT 0,
    fecha_ultima_compra         DATE,
    fecha_ultima_entrada        DATE,
    costo_uni_inventario        NUMERIC(18,4)   DEFAULT 0,
    fecha_foto_inventario       DATE,

    -- Control snapshot
    inv_foto_bloqueada          BOOLEAN         DEFAULT FALSE,
    fecha_carga                 TIMESTAMP       DEFAULT now(),
    fecha_actualizacion         TIMESTAMP       DEFAULT now(),

    PRIMARY KEY (empresa, fecha_dia, sede, bodega_local, id_item)

) PARTITION BY RANGE (fecha_dia);


-- ── 2. PARTICIONES 2026 (una por mes) ────────────────────────────────────────
-- Nomenclatura: rotacion_p_YYYYMM
-- Rango      : [inicio_mes, inicio_mes_siguiente)  → el límite superior es exclusivo

CREATE TABLE public.rotacion_p_202601
    PARTITION OF public.rotacion_base_item_dia_sede
    FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');

CREATE TABLE public.rotacion_p_202602
    PARTITION OF public.rotacion_base_item_dia_sede
    FOR VALUES FROM ('2026-02-01') TO ('2026-03-01');

CREATE TABLE public.rotacion_p_202603
    PARTITION OF public.rotacion_base_item_dia_sede
    FOR VALUES FROM ('2026-03-01') TO ('2026-04-01');

CREATE TABLE public.rotacion_p_202604
    PARTITION OF public.rotacion_base_item_dia_sede
    FOR VALUES FROM ('2026-04-01') TO ('2026-05-01');

CREATE TABLE public.rotacion_p_202605
    PARTITION OF public.rotacion_base_item_dia_sede
    FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');

CREATE TABLE public.rotacion_p_202606
    PARTITION OF public.rotacion_base_item_dia_sede
    FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');

CREATE TABLE public.rotacion_p_202607
    PARTITION OF public.rotacion_base_item_dia_sede
    FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');

CREATE TABLE public.rotacion_p_202608
    PARTITION OF public.rotacion_base_item_dia_sede
    FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');

CREATE TABLE public.rotacion_p_202609
    PARTITION OF public.rotacion_base_item_dia_sede
    FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');

CREATE TABLE public.rotacion_p_202610
    PARTITION OF public.rotacion_base_item_dia_sede
    FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');

CREATE TABLE public.rotacion_p_202611
    PARTITION OF public.rotacion_base_item_dia_sede
    FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');

CREATE TABLE public.rotacion_p_202612
    PARTITION OF public.rotacion_base_item_dia_sede
    FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');

-- Partición por defecto: captura cualquier fecha fuera del rango definido.
-- Protege contra inserts con fechas inesperadas sin lanzar error.
CREATE TABLE public.rotacion_p_default
    PARTITION OF public.rotacion_base_item_dia_sede
    DEFAULT;


-- ── 3. ÍNDICES (se crean en la madre — PostgreSQL los replica en cada partición)
-- En tablas particionadas los índices se definen una sola vez en la madre.

CREATE INDEX idx_rot_empresa_fecha
    ON public.rotacion_base_item_dia_sede (empresa, fecha_dia);

CREATE INDEX idx_rot_sede_fecha
    ON public.rotacion_base_item_dia_sede (sede, fecha_dia);

CREATE INDEX idx_rot_item_fecha
    ON public.rotacion_base_item_dia_sede (id_item, fecha_dia);

CREATE INDEX idx_rot_linea_fecha
    ON public.rotacion_base_item_dia_sede (id_linea_nivel_1, fecha_dia);

CREATE INDEX idx_rot_categoria_fecha
    ON public.rotacion_base_item_dia_sede (id_categoria, fecha_dia);

CREATE INDEX idx_rot_estado_venta
    ON public.rotacion_base_item_dia_sede (fecha_dia, estado_ultima_venta_item);


-- ── 4. FUNCIÓN: crear partición del mes siguiente automáticamente ─────────────
-- Se puede llamar desde un cron job el día 25 de cada mes para pre-crear
-- la partición del mes siguiente antes de que el ETL la necesite.
-- Ejemplo de llamada: SELECT public.crear_particion_rotacion_mes_siguiente();

CREATE OR REPLACE FUNCTION public.crear_particion_rotacion_mes_siguiente()
RETURNS TEXT
LANGUAGE plpgsql
AS $$
DECLARE
    v_mes_inicio    DATE;
    v_mes_fin       DATE;
    v_nombre        TEXT;
    v_sql           TEXT;
BEGIN
    -- Primer día del mes siguiente
    v_mes_inicio := DATE_TRUNC('month', CURRENT_DATE + INTERVAL '1 month')::DATE;
    v_mes_fin    := (v_mes_inicio + INTERVAL '1 month')::DATE;
    v_nombre     := 'rotacion_p_' || TO_CHAR(v_mes_inicio, 'YYYYMM');

    -- Verificar si ya existe
    IF EXISTS (
        SELECT 1 FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = v_nombre
    ) THEN
        RETURN 'La partición ' || v_nombre || ' ya existe.';
    END IF;

    -- Crear la partición
    v_sql := FORMAT(
        'CREATE TABLE public.%I PARTITION OF public.rotacion_base_item_dia_sede '
        'FOR VALUES FROM (%L) TO (%L);',
        v_nombre, v_mes_inicio, v_mes_fin
    );
    EXECUTE v_sql;

    RETURN 'Partición creada: ' || v_nombre
           || '  (' || v_mes_inicio || ' → ' || v_mes_fin || ')';
END;
$$;


-- ── 5. MIGRACIÓN desde tabla actual (ejecutar en mantenimiento) ───────────────
-- Pasos para migrar sin pérdida de datos:
--
-- PASO 1: Renombrar la tabla actual
--   ALTER TABLE public.rotacion_base_item_dia_sede
--       RENAME TO rotacion_base_item_dia_sede_old;
--
-- PASO 2: Crear la nueva tabla particionada (DDL de arriba)
--
-- PASO 3: Migrar los datos mes a mes para no saturar la BD
--   INSERT INTO public.rotacion_base_item_dia_sede
--   SELECT * FROM public.rotacion_base_item_dia_sede_old
--   WHERE fecha_dia >= '2026-01-01' AND fecha_dia < '2026-02-01';
--
--   INSERT INTO public.rotacion_base_item_dia_sede
--   SELECT * FROM public.rotacion_base_item_dia_sede_old
--   WHERE fecha_dia >= '2026-02-01' AND fecha_dia < '2026-03-01';
--   -- ... repetir por cada mes ...
--
-- PASO 4: Verificar conteos
--   SELECT COUNT(*) FROM public.rotacion_base_item_dia_sede_old;
--   SELECT COUNT(*) FROM public.rotacion_base_item_dia_sede;
--
-- PASO 5: Una vez validado, eliminar la tabla vieja
--   DROP TABLE public.rotacion_base_item_dia_sede_old;


-- ── 6. CRON para crear partición del mes siguiente (en el servidor Linux) ─────
-- Agregar al crontab del usuario etlrotacion o postgres:
--
--   # Crear partición del mes siguiente el día 25 de cada mes a medianoche
--   0 0 25 * * psql -U postgres -d produXdia -c "SELECT public.crear_particion_rotacion_mes_siguiente();" >> /var/log/etl_rotacion/particiones.log 2>&1


-- ── 7. CONSULTAS DE VERIFICACIÓN ─────────────────────────────────────────────
-- Ver particiones existentes y su tamaño:
-- SELECT
--     child.relname                          AS particion,
--     pg_size_pretty(pg_relation_size(child.oid)) AS tamanio,
--     pg_stat_user_tables.n_live_tup         AS filas_aprox
-- FROM pg_inherits
-- JOIN pg_class parent ON pg_inherits.inhparent = parent.oid
-- JOIN pg_class child  ON pg_inherits.inhrelid  = child.oid
-- LEFT JOIN pg_stat_user_tables ON pg_stat_user_tables.relname = child.relname
-- WHERE parent.relname = 'rotacion_base_item_dia_sede'
-- ORDER BY child.relname;
