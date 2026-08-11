# Plan — 005 Señal de destino: ¿llegó el dato?

Este plan fija **un solo contrato**. El diseño se revisó de forma adversarial antes
de escribirlo y salieron dos fallos que lo hundían; los dos están cerrados aquí y
se nombran explícitamente porque son la parte no obvia:

1. **El día esperado NO es un D-1 fijo.** Con el timer real
   (`OnCalendar=*-*-* 0/2:15:00`) y ETLs que corren 07:00–08:30, un D-1 fijo
   produciría CRITICAL falso en 4 de las 12 corridas diarias, y todos los sábados
   para los jobs `Mon..Fri`. Con `diff_incidents` alertando al cambiar, eso son
   **dos Telegram por job por día, para siempre** — el NFR-1 de la 003 roto por
   diseño. El día esperado se deriva del horario declarado (§4).
2. **`scripts/alert_incidents.py:84-85` retorna antes de consultar el destino**
   cuando SSH falla. Es la única ruta que alerta fuera del digest, y con el box
   caído no correría ni un solo check — o sea el apagón de Dinastia del 8-9 de
   agosto seguiría igual de ciego. La fase de destino pasa a ser **hermana** de la
   de systemd, no anidada dentro de ella (§6).

## Alcance de esta entrega

**Dentro:** el destino **GCP (`produxdia`)**, que es donde viven los rollups vivos
y donde ocurrieron los dos incidentes que motivan la spec (el `refresh` que
`visor-etl-sync` se traga, y el hueco de Dinastia del 7-8 de agosto).

**Fuera, con motivo:**

- **El Postgres local del 232 (rama "local" de FR-7).** Verificarlo desde
  MMAUTOML01 exige un túnel SSH, que sale del camino auditado de
  `run_read_only`, deja un forward alcanzable por cualquier usuario local del box,
  y obliga a editar `pg_hba.conf` y `authorized_keys` en producción. Y los
  rollups locales están congelados desde el 2026-07-21 **a propósito**: son
  residuo. El valor está en GCP. Se difiere con su propia aprobación (T16).
- **Heartbeat / dead-man switch.** La 005 quita el punto ciego del box vigilado,
  pero no el del host vigilante: si MMAUTOML01 muere, vuelve el silencio. Eso es
  una spec hermana (006), y hasta que exista **la 005 reduce el riesgo de
  silencio, no lo elimina**. Queda escrito como riesgo residual, no como
  descuido.
- **`REVOKE` de las funciones `refresh_*`.** Endurecer eso puede romper
  `visor-etl-sync`, y el fallo se escondería en el mismo `|| return 0` que originó
  esta spec. Va **después** de que la señal de destino esté viva y verde, para que
  el propio monitor sea el detector del daño (T17).

---

## Architecture

Dos señales independientes, con **loci distintos a propósito**:

```
                         ┌──────────────────────────────┐
   MMAUTOML01            │  fase systemd (co-dependiente)│
   ├─ os_system_agent    │  SSH ──► server232            │  ¿corrió el job?
   │   ├─ fase systemd ──┘                               │
   │   └─ fase destino ──┐                               │
   └─ cloud-sql-proxy    │  127.0.0.1:15432              │
       (SA, mTLS)        └──► GCP produxdia (SELECT RO)  │  ¿llegó el dato?
                                                          │
   Combinación: gana la peor  ──► JobStatus ──► reporte / alerta
```

La propiedad que importa: **la fase de destino no depende de alcanzar el box
vigilado**. Es lo único que funcionó el 8-9 de agosto — consultar GCP desde otra
máquina. Por eso corre siempre, incluso con SSH caído.

**Cloud SQL Auth Proxy, no authorized network.** MMAUTOML01 es un PC corporativo
detrás de Cloudflare WARP: su IP de salida no es estable y es **compartida con
todo el pool de WARP**, así que meterla en *authorized networks* autorizaría a un
rango enorme de terceros contra producción. El proxy sale por la API de Google con
mTLS, no necesita IP autorizada, y con `--auto-iam-authn` **elimina el password de
BD**. Con el proxy, `sslmode=disable` es correcto y solo ahí: el cifrado y la
autenticación mutua los pone el proxy sobre un socket de loopback.

---

## Contrato del catálogo (único)

Claves **en inglés**, como el resto del esquema (`systemd_unit`, `freshness`,
`paths`, `alerts`); los valores siguen en español. `esquema.tabla` va en **un solo
campo calificado**, porque el rol fija `search_path = ''`.

```yaml
  - id: sync_gcp_diario
    systemd_unit: visor-etl-sync.service
    schedule: "diario 07:35"
    expected_finish_before: "08:30"
    freshness: { max_delay_minutes_warning: 1500, max_delay_minutes_critical: 1560 }
    destination:
      - connection: gcp_produxdia      # id -> prefijo OS_DB_GCP_PRODUXDIA_*
        table: public.margen_final_roll
        date_column: fecha_dcto
        date_type: yyyymmdd_text       # yyyymmdd_text | date  (sin default)
        ready_after: "08:30"           # hora local; por defecto expected_finish_before
        timezone: America/Bogota
        run_days: [1, 2, 3, 4, 5, 6, 7]   # ISO: 1=Lun … 7=Dom
        skip_dates: ["2026-08-17"]     # festivos
        day_offset: 1
        measure_column: ven_totales
        min_rows: null
```

`connection` es un **id validado** (`^[a-z][a-z0-9_]{0,31}$`) del que se deriva el
prefijo de entorno. **No** existe `dsn_env`: un catálogo no puede nombrar una
variable de entorno arbitraria (p.ej. `TELEGRAM_BOT_TOKEN`) ni cargar un DSN
completo en un string.

| Decisión | Elegida | Descartada, y por qué |
|---|---|---|
| Credenciales | `connection` + `OS_DB_<ID>_{HOST,PORT,DBNAME,USER,PASSWORD,SSLMODE,SSLROOTCERT}` | `dsn_env`: deja apuntar a cualquier variable y obliga a construir un DSN, que es justo el string que un formateador arrastra a un mensaje |
| Construcción de SQL | `psycopg.sql.Identifier` | f-string + `per-file-ignore` de `S608`: pedirle un waiver a bandit para hacer exactamente lo que avisa |
| Dónde vive `DestinationCheck` | `catalog.py`, junto a `FreshnessRule` | `monitors/destination.py`: crea el ciclo `catalog → destination → catalog` |
| Combinador | `monitors/destination.py` | `freshness.py` o `reports/daily.py`: obliga a mover `_SEVERITY_RANK` y arriesga 2 tests para nada |
| Medida en cero | **CRITICAL**, con `EXISTS (… <> 0)` | WARNING y `SUM(…)`: el tablero muestra ceros, que para el consumidor es idéntico a "no llegó". `SUM=0` además dispara si devoluciones y ventas se cancelan |
| Tipo de fecha | Declarado (`date_type`, sin default) | Autodetección por `information_schema`: un round-trip por tabla y **falla abierto** ante un typo |
| Columna de medida | Declarada por job | `_MEASURE_CANDIDATES` como en `post_run.py`: si ninguna candidata existe, la señal se pierde **en silencio** — el mismo `|| return 0` que originó la spec |

---

## §4 — El día esperado, derivado del horario

La regla, y es el corazón de la spec:

> **día esperado = (última corrida programada cuyo `ready_after` ya pasó) − `day_offset`**

Algoritmo puro, sin reloj propio (`now` inyectado), en la `timezone` del check:

1. `local_now = now.astimezone(ZoneInfo(check.timezone))`.
2. `R` = hoy si `hoy ∈ run_days`, `hoy ∉ skip_dates` y `local_now.time() ≥ ready_after`;
   si no, se retrocede día a día hasta el `run_day` anterior que no esté en `skip_dates`.
3. `día_esperado = R − day_offset` días.

Qué arregla, con los jobs reales del catálogo:

| Situación | D-1 fijo | Regla derivada |
|---|---|---|
| Alerta de las 00:15, ETL corre 07:00 | CRITICAL falso ×4/día | `R` = ayer → espera anteayer → **INFO** |
| Sábado 00:15, `ventas_item_diario` es `Mon..Fri` | CRITICAL falso todos los sábados | `R` = viernes → espera jueves → **INFO** |
| Lunes festivo | CRITICAL falso cada festivo | `skip_dates` retrocede al hábil anterior |
| `sync_gcp_diario` a las 07:50 (local ya cargó, GCP no) | CRITICAL falso en la ventana normal | `ready_after: 08:30` en el destino GCP → `aún no evaluado` |

**`ready_after` es por destino, no por job.** Es lo que resuelve la carrera
local→GCP: el destino GCP de `sync_gcp_diario` se evalúa después de 08:30, y si
algún día se verifica el local (T16), ese se evaluaría después del
`expected_finish_before` de `margen_diario` (07:30). Un solo campo, dos ventanas.

**Segundo supresor, gratis:** si la unit que alimenta el destino está
`ActiveState=active`, no se evalúa. El dato ya viene en `SystemdState`
(`systemd.py:53`) en la misma pasada. Eso mata la falsa alarma de los domingos,
cuando `reconcile_gcp_semanal` corre 16:00–18:00 haciendo *replace* de 7 días y
tomaría `ACCESS EXCLUSIVE`.

`ready_after` es **obligatorio**: se hereda de `expected_finish_before` del job y,
si el job tampoco lo declara, es `CatalogError`. Falla cerrado; no hay default
silencioso que reintroduzca el D-1 ciego.

---

## Data flow

```
catálogo → checks declarados
     │
     ├─ fase systemd  : SSH → systemctl show → SystemdState → JobStatus
     │
     └─ fase destino  : preflight → 1 conexión por connection → SELECT → Observation
                         │
                         └─ evaluate_destination(check, obs, now) → Verdict   [PURA]
     │
     └─ combine_statuses(systemd, verdicts)  → "gana la peor"  → JobStatus
```

**El evaluador itera sobre los checks DECLARADOS y busca su observación.** Nunca
al revés. Un check sin observación es `no verificable`, jamás una omisión — si se
iterara sobre las observaciones, un bug de agrupación haría desaparecer el check y
el job saldría verde. Es el patrón `if measure:` que criticamos en `post_run.py`,
y no lo vamos a reproducir dentro del detector que existe para cazarlo.

---

## Interfaces

```python
# catalog.py — dato declarado, inmutable, junto a FreshnessRule
@dataclass(frozen=True)
class DestinationCheck:
    connection: str; table: str; date_column: str; date_type: str
    ready_after: str; timezone: str = "America/Bogota"
    run_days: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7)
    skip_dates: tuple[str, ...] = ()
    day_offset: int = 1
    measure_column: str | None = None
    min_rows: int | None = None

# EtlJob gana un campo con default -> FR-6/AC-3 gratis
    destinations: tuple[DestinationCheck, ...] = ()

# monitors/freshness.py — junto a JobStatus, para que no haya ciclo
class DestinationOutcome(StrEnum):
    NOT_DECLARED  = "no_declarado"     # el job no declara destino: idéntico a hoy
    OK            = "ok"
    NOT_YET       = "aun_no_evaluado"  # antes de ready_after, o la unit corriendo
    UNVERIFIABLE  = "no_verificable"   # la consulta falló
    FAILED        = "fallo"            # el dato no llegó / medida en 0 / flaco

@dataclass(frozen=True)
class JobStatus:
    ...  # campos actuales, sin tocar
    destination: DestinationOutcome = DestinationOutcome.NOT_DECLARED

# monitors/destination.py — todo puro
def expected_day(check: DestinationCheck, now: datetime) -> date | None
def evaluate_destination(check, obs: DestinationObservation, now) -> DestinationVerdict
def combine_statuses(systemd: JobStatus, verdicts: Sequence[DestinationVerdict]) -> JobStatus

# collector.py — HERMANA de collect_live, no anidada dentro
Prober = Callable[[Sequence[tuple[str, DestinationCheck]], datetime], Sequence[DestinationObservation]]
def collect_destinations(jobs, now, *, prober: Prober | None = None) -> dict[str, list[DestinationVerdict]]
```

`DestinationObservation` lleva **campos tipados**: `latest_day: str | None`,
`day_present: bool | None`, `measure_nonzero: bool | None`, `rows: int | None`,
`sqlstate: str | None`, `error_label: str | None`. **No tiene campo de texto
libre**, para que sea imposible pasarle `str(exc)` (§AC-7 más abajo).

---

## SQL

Las dos ramas de tipo devuelven **la misma forma**; la diferencia se absorbe en el
SQL, no en Python. Composición con `psycopg.sql.Identifier`, parámetros ligados,
nunca interpolación.

```sql
-- rama yyyymmdd_text.  %(dia)s = '20260803'
WITH ultimo AS (
    SELECT {col} AS dia FROM {tabla}
    WHERE {col} IS NOT NULL
    ORDER BY {col} DESC LIMIT 1
)
SELECT (SELECT dia FROM ultimo)                                          AS ultimo_dia,
       EXISTS (SELECT 1 FROM {tabla} WHERE {col} = %(dia)s)              AS dia_presente,
       EXISTS (SELECT 1 FROM {tabla} WHERE {col} = %(dia)s
                 AND {medida} <> 0)                                      AS medida_no_cero,
       NULL::bigint                                                      AS filas;
-- rama date: {col} = %(dia)s::date  y  to_char(…, 'YYYYMMDD') sobre ultimo_dia
```

Cuatro detalles que son load-bearing y van comentados en el código:

- **`ORDER BY … DESC LIMIT 1`, nunca `max()`.** `margen_final` tiene 56 M filas /
  31 GB; el costo tiene que quedar a la vista en el código y en el `EXPLAIN`.
- **`WHERE col IS NOT NULL` es obligatorio.** En Postgres `ORDER BY col DESC` es
  `NULLS FIRST`: **una** fila con la fecha en NULL haría que la consulta devuelva
  NULL y el monitor concluya "la tabla está vacía". Y no se arregla con
  `NULLS LAST`, que fuerza un sort sobre 56 M filas; `IS NOT NULL` sí es qual de
  índice y mantiene el scan ordenado y barato.
- **El cast va del lado constante** (`%(dia)s::date`), nunca sobre la columna.
  `col::text = …` es no-sargable y convierte un probe de índice en un scan de
  31 GB. Ese anti-patrón existe hoy en `post_run.py` (`_range_stats`).
- **`dia_presente` manda sobre `ultimo_dia`.** Un backfill puede dejar D-3 y D+0
  con un hueco justo en D-1; `max()` diría "al día" y estaría mintiendo.
  `ultimo_dia` se conserva solo para la evidencia de FR-3.

**El orden lexicográfico de `'YYYYMMDD'` coincide con el cronológico porque es de
ancho fijo con ceros a la izquierda.** Es lo que hace legítimo el `ORDER BY`, y va
comentado como dependencia. Sin `COLLATE "C"`: cambiaría la collation del
ordenamiento y el índice dejaría de servir.

**`min_rows` cambia la consulta** a `count(*)` + `count(*) FILTER (WHERE medida <> 0)`
en un solo recorrido del día. Es opt-in: sin declararlo, no se paga.

**Preflight**, una sola consulta para todas las tablas del catálogo, antes de
emitir nada: `information_schema.columns` para confirmar que tabla y columna
existen y que el tipo real concuerda con `date_type`. Desalineado → **una**
condición CRITICAL de "catálogo desalineado" y **cero** consultas contra esa
tabla. Esto también detecta el tercer tipo que deliberadamente no soportamos: si
la columna resultara `timestamp`, `= %(dia)s::date` no matchearía nunca y el
monitor reportaría "no llegó el dato" todos los días.

**Índice**: se verifica una vez en el despliegue (`pg_index` con `indkey[0]` = la
columna de fecha). Sin índice, `LIMIT 1` es scan completo y el `statement_timeout`
dispara en cada pasada. Cero filas → **el destino no se da de alta**. Crear el
índice es DDL y lo pide el DBA (CLAUDE.md §4).

---

## §5 — Modelo de acceso a BD

### Por qué no basta con "el código solo hace SELECT"

Está escrito en el propio sistema: `visor-etl-sync` **escribe con un SELECT** —
`SELECT refresh_margen_final_roll(...)`. Un guardia textual tipo "empieza por
SELECT" (el patrón de `DESTRUCTIVE_TOKENS`) aprueba esa línea sin dudar. Tampoco
detiene un CTE con DML (`WITH d AS (DELETE … RETURNING *) SELECT …`), ni un
`SELECT … FOR UPDATE`, ni el *simple query protocol* con dos sentencias.

Y el punto de principio: **un control que vive en el mismo artefacto que el bug no
es un control.** El bug que AC-6 quiere atajar es *un bug en este código*.

Jerarquía real, de fuera hacia dentro:

1. **GRANT** — la frontera de verdad. Sin `INSERT/UPDATE/DELETE/EXECUTE` no hay
   escritura posible aunque todo lo demás falle.
2. **`default_transaction_read_only=on`** — **no es una frontera**: el propio rol
   puede apagarla con `SET`. Es un **cazador de bugs**: convierte un error de
   programación en un 25006 ruidoso en vez de una escritura silenciosa.
3. El constructor de consultas en Python — ergonomía, no seguridad.

### El rol

```sql
CREATE ROLE os_agent_ro LOGIN PASSWORD :'agent_pw'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT NOBYPASSRLS
  CONNECTION LIMIT 3
  VALID UNTIL '2027-02-11';

ALTER ROLE os_agent_ro SET default_transaction_read_only     = on;
ALTER ROLE os_agent_ro SET statement_timeout                 = '15s';
ALTER ROLE os_agent_ro SET lock_timeout                      = '2s';
ALTER ROLE os_agent_ro SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE os_agent_ro SET search_path                       = '';
ALTER ROLE os_agent_ro SET jit                               = off;
```

**Los ajustes van en `ALTER ROLE`, no en `SET` del cliente.** `SET` es
transaccional: un `ROLLBACK` tras el primer error revertiría `statement_timeout`
y `default_transaction_read_only` justo cuando más importan. Los defaults del rol
los aplica el motor en el login y sobreviven a cualquier rollback.
`application_name` va como kwarg de conexión. Si un job con `min_rows` necesita
otro techo, `SET LOCAL` dentro de su transacción; el valor del rol es techo
generoso (15s), no el valor efectivo.

**`GRANT` por columna, no por tabla** — el agente literalmente no puede leer
identificadores de cliente, artículo ni sede:

```sql
GRANT SELECT (fecha_dcto, ven_totales)        ON public.margen_final_roll TO os_agent_ro;
GRANT SELECT (fecha_dcto, venta_sin_impuesto) ON public.ventas_dinastia   TO os_agent_ro;
GRANT SELECT (fecha_dia,  venta_sin_impuesto) ON public.rotacion_dinastia TO os_agent_ro;
…
```

La lista **se genera desde el catálogo**, no se escribe a mano: `verify_db_role.py`
imprime el conjunto exacto requerido y lo compara contra
`information_schema.column_privileges`. Una tabla del catálogo sin grant es un
fallo de **despliegue**, no de runtime. Un privilegio inesperado es un fallo, no
una advertencia, y se reporta con severidad **SECURITY** (§12).

**Sin `ALTER DEFAULT PRIVILEGES`** (una tabla nueva no queda legible por
accidente) y **sin `pg_read_all_data`**: `produxdia` es compartida entre Mercamio y
Dinastia, y ese atajo daría SELECT sobre todo, con alcance que crece solo.

**Lectura cruzada entre empresas, declarada:** el vigilante lee agregados de las
dos empresas desde una sola conexión. Es aceptable porque `produxdia` ya es una
base compartida bajo un mismo operador y los grants son por columna y solo
agregados. Si esa premisa cambia, se parte en dos roles. Queda como supuesto
escrito, no como descuido.

**Interruptor de emergencia:** `ALTER ROLE os_agent_ro NOLOGIN;` — revoca al
agente sin tocar ningún ETL. Va al `security-runbook.md`.

### Cómo se demuestra que es solo lectura (AC-6)

CI no tiene Postgres, así que la prueba se parte y **AC-6 la satisface la segunda
capa**:

- **En CI (puro):** el módulo expone exactamente dos constructores y **ninguna API
  acepta SQL libre**; identificadores inválidos → `CatalogError`; credenciales
  faltantes → `ConfigError` nombrando **variables, nunca valores**; `repr`
  enmascarado.
- **Contra el motor** (`-m dbproof`, se corre en el despliegue):
  1. Los defaults llegaron **del rol** en el login, no del cliente.
  2. `UPDATE {tabla} SET {col} = {col} WHERE false` → 25006. `WHERE false` la hace
     inofensiva aunque la guarda estuviera rota; el test afirma que **lanza**, así
     que un éxito silencioso reprueba. Cada bloque en `try/finally: rollback()`
     — **no** como línea siguiente, que no correría justo en el caso que el test
     existe para detectar.
  3. **`SET default_transaction_read_only = off` y repetir.** Ahora responde el
     GRANT (42501), no la bandera. Sin este paso la prueba es decorativa: la
     bandera la puede apagar el propio rol.
  4. `CREATE TABLE os_agent_scratch.probe` — esquema inexistente, peor caso 3F000,
     nunca una tabla huérfana en producción.
  5. `SELECT *` falla, la columna concedida pasa → el grant por columna se sostiene.
  6. `SELECT public.refresh_margen_final_roll(…)` → lanza. La escritura disfrazada.
  7. `pg_has_role(current_user,'pg_read_all_data','member')` es falso.

**Recibo, para que AC-6 no sea una línea de checklist.** `verify_db_role.py`
escribe `~/.config/os-system-agent/verify-<connection>.json` con el hash del
conjunto exacto de privilegios y un timestamp. **El prober se niega a conectar**
si el recibo falta, no corresponde o tiene más de 30 días → condición sintética
WARNING "destino no verificado (rol sin auditar)", nunca un ✅. Eso convierte
AC-6 en un gate de runtime y hace que la erosión de privilegios se detecte sola.

### Credenciales

`connection` → prefijo `OS_DB_<ID>_*`. `DbCredentials` es `frozen`, con `__repr__`
enmascarado y `connect_kwargs()`: **el password solo existe como argumento con
nombre de `psycopg.connect(**kw)`**. No se construye un DSN, así que no hay string
que un formateador pueda arrastrar a un mensaje. Nunca `psql` con `PGPASSWORD` en
argv (`/proc/<pid>/cmdline` lo lee cualquier usuario local).

Las units usan **`EnvironmentFile=` (0600), no `Environment=`** — que lo lee
cualquier usuario local con `systemctl show`. Eso arregla de paso un hueco que ya
existe: `dinastia-agent-daily.service.example` pone el `TELEGRAM_BOT_TOKEN` así.

---

## Severidad

**FR-2, gana la peor**: `worst(systemd, *destinos)`. `delay_minutes` y `latest_at`
se conservan **los de systemd** — `render_chat_report` los usa para el "hace 6h
53m" y pisarlos cambiaría la forma de todas las líneas del reporte. `evidence` es
`f"{systemd.evidence} · {destino.evidence}"`, systemd primero.

| Condición | Severidad | Motivo |
|---|---|---|
| Día presente, medida ≠ 0, filas ≥ min | INFO | |
| **Día ausente** | **CRITICAL** | el dato no llegó (AC-1, AC-8) |
| **Medida en cero con filas > 0** | **CRITICAL** | el tablero muestra ceros; indistinguible de "no llegó" (AC-9) |
| Filas < `min_rows` (con las dos anteriores verdes) | **WARNING**, nunca escala | AC-4 |
| `08xxx` conexión · `57014` timeout · `55P03` bloqueo | WARNING | se cura solo |
| `28xxx` auth · `42501` permisos · `42P01`/`42703` catálogo · `3D000` base | **CRITICAL** | **nunca** se cura solo |
| `ConfigError` (faltan credenciales) · driver ausente | **CRITICAL** | nunca se cura solo |
| Excepción sin sqlstate | **CRITICAL** | falla cerrado |

**Escalada por persistencia:** un WARNING transitorio que persiste **6 corridas
consecutivas** (~12 h con el timer de 2 h) pasa a CRITICAL. Sin esto, con
`diff_incidents` alertando solo al cambiar, un destino roto da **un ping y luego
silencio indefinido** con doce ✅ en pantalla — exactamente el modo de falla que
esta spec existe para eliminar. El contador vive en **`.destination-state.json`,
un archivo aparte**: `diff_incidents` trata toda clave de `.alert-state.json` como
un `job_id`, así que una entrada ajena aparecería como "recuperado" en la corrida
siguiente y además `_save_state` la borraría.

**El volumen solo habla si las dos señales fuertes están verdes.** Nunca tres
alertas para un problema.

**`min_rows` es un piso, no un pronóstico.** Se deriva del **mínimo histórico** de
ese job × 0.5, jamás de la mediana. El dato medido en Dinastia
(Sáb 9.113 · Jue 7.020 · Mar 6.572 · Mié 6.011 · Vie 5.574 · Dom 5.382 · Lun 4.444)
muestra que un umbral contra la mediana global daría 58 % un lunes: falsas alarmas
garantizadas. Un piso bajo el mínimo del peor día sirve **para todos los días a la
vez** con un solo número, y por eso no hace falta complejidad por día de semana.
La calibración se corre a mano (nunca desde el timer) y la revisa un humano antes
de tocar el catálogo. **No se calibra contra `margen_final`**: 56 días × 56 M filas
es justo lo que NFR-3 prohíbe.

---

## §6 — Reporte y alertado

**"No verificado" no puede salir como palomita.** Hoy `render_chat_report` solo
imprime la línea `↳` de detalle para incidentes y avisos, así que un job INFO cuyo
destino no se pudo verificar mostraría `✅ Sync GCP — hace 26m` y su sufijo
`· destino: no verificable` **no se imprimiría nunca**. Por eso:

- `DestinationOutcome ∈ {NOT_YET, UNVERIFIABLE}` con severidad INFO → icono **`❔`**
  en vez de `✅`, y la línea entra en el bloque de detalle.
- `NOT_DECLARED` → todo idéntico a hoy (FR-6/AC-3).

**Una condición sintética por `connection`, no por job.** GCP caído = **un**
incidente, apunten 3 o 12 jobs. Molde exacto de `SERVER_DOWN_ID`
(`alerting.py:28-40`), así que entra a `incident_statuses` y `diff_incidents` como
cualquier otro y se deduplica solo. Los jobs afectados **conservan su severidad de
systemd** (AC-5) pero su evidencia dice `destino: no verificable`, nunca
`destino: OK`.

**La fase de destino corre siempre.** `_current_incidents` se reordena:

```python
verdicts = collect_destinations(jobs, now)          # SIEMPRE, no depende de SSH
if not _server_reachable(alias):
    statuses = [server_down_status(server), *destination_only(verdicts)]
    scope = {SERVER_DOWN_ID} | evaluated_ids(verdicts)
else:
    statuses = combine(collect_statuses(...), verdicts)
    scope = None                                     # verdicto completo de todo
return incident_statuses(statuses), server, empresa
```

La aridad de la tupla se conserva, así que `tests/test_alert_incidents.py:34-38`
no se rompe. Con esto la tabla de la spec por fin es alcanzable:

| Box | Destino | Reporte |
|---|---|---|
| inalcanzable | fresco | CRITICAL *sobre el box*, con el dato al día. Diagnóstico accionable. |
| inalcanzable | atrasado | CRITICAL con certeza: no corrió **y** no llegó. |
| OK | atrasado | CRITICAL, diciendo que la unit reportó `success` (AC-1). |

Antes, la primera fila era **silencio**.

**`diff_incidents` gana `scope`**, y es obligatorio para que lo anterior no
mienta: `recovered` solo puede contener ids **que se pudieron evaluar por
completo** en esta corrida. Sin eso, con SSH caído todos los incidentes de systemd
aparecerían como recuperados y saldría una ráfaga de "✅ todo bien" con el ETL
caído. Firma: `diff_incidents(previous, current, *, scope: set[str] | None = None)`;
`None` mantiene el comportamiento de hoy, así que ningún test existente cambia.

---

## Qué NO se toca

- **`ssh_client.py`.** No se agrega `psql` a `READ_ONLY_ALLOWLIST` ni se ablandan
  `_UNSAFE_CHARS`/`DESTRUCTIVE_TOKENS`. Esa allowlist vale por ser una lista
  **cerrada** de tokens que no sabe leer SQL. Trampa verificada: hoy
  `"psql -c 'drop table daily_sales'"` se rechaza por `drop`, **no** por `psql`, así
  que si alguien agregara `psql` a la allowlist el test seguiría verde y
  `psql -c 'select …'` quedaría habilitado sin que nada avise.
- **`severity.classify_freshness`.** El destino no es "minutos de atraso", es "el
  día esperado está o no está". Clasificador propio y puro.
- **`monitors/systemd.py`.** Intacto, incluida la rama `en ejecución → INFO`
  (`:145-162`): ese trade-off se aceptó por escrito porque *la señal de destino*
  es la que cubre un job colgado. La 005 es lo que vuelve cierto ese comentario.
- **`redaction.py`** como control primario. Sigue siendo red de seguridad.
- **`config.py::Settings`.** El DSN no es un campo fijo: los destinos son
  por-check y su nombre lo dice el catálogo (FR-7).

---

## Observabilidad

Una línea JSONL por verificación: `ts, connection, tabla, sqlstate, elapsed_ms,
dia_esperado, dia_observado, filas`. **Sin datos de fila.**
`application_name='os_system_agent'` para que el DBA identifique la conexión en
`pg_stat_activity`.

---

## Failure modes

| Fallo | Comportamiento |
|---|---|
| Sin credenciales / driver ausente | 1 condición **CRITICAL** por `connection`; ningún job cambia (AC-5) |
| Timeout / bloqueo / red | 1 condición WARNING; **escala a CRITICAL a las 6 corridas** |
| Tabla o columna inexistente | preflight → 1 CRITICAL de catálogo desalineado; cero consultas a esa tabla |
| Check declarado sin observación | `no verificable`, nunca omisión silenciosa |
| Antes de `ready_after` / unit corriendo | `❔ aún no evaluado` — ni verde ni alarma |
| Rol sin recibo de auditoría | el prober **no conecta**; WARNING "rol sin auditar" |
| `VALID UNTIL` a < 14 días | WARNING en el reporte diario (se lee `rolvaliduntil` en el preflight, que ya consulta `pg_roles`) |

---

## Costo

Timer real: `0/2:15` → **12 corridas/día**. Con ≤1 conexión y ~4 sentencias por
corrida son **≤48 sentencias/día**. No hace falta capa de caché — se descarta
`min_intervalo_minutos`, que sería estado corrompible para cero beneficio.

Una conexión **por `connection`**, no por job; perezosa (un catálogo sin destinos
abre **cero**); deduplicada por `(connection, tabla, día)`; secuencial; sin pool
(el proceso es efímero). `connect_timeout=5`.

**Enmienda a NFR-3:** "una conexión por corrida" → "una conexión por `connection`
distinta por corrida". Hoy es 1; con el destino local sería 2.

---

## Rollback

- **Repo (T1–T10):** todo aditivo. `destinations` default `()`; sin bloque
  `destination:` el comportamiento es byte a byte el de hoy. Revertir = quitar el
  bloque del catálogo, sin desplegar código.
- **Rol de BD:** `REVOKE ALL ON <tablas> FROM os_agent_ro; DROP ROLE os_agent_ro;`
  previa verificación de que ningún ETL lo usaba. El kill switch `NOLOGIN` **no es
  rollback**: no revierte los GRANT.
- **Proxy:** `systemctl disable --now cloud-sql-proxy` + borrar la SA.
- **Por job:** quitar su bloque `destination:` del catálogo (gitignored, por box).

---

## Tests

Puros (CI, sin BD): día esperado (00:15, sábado con `Mon..Fri`, festivo, unit
activa, `ready_after` por destino); evaluador (los 13 casos de §"Severidad",
incluido `medida: null` → "no declarada", jamás leído como verde); "gana la peor"
(las 16 combinaciones); golden del SQL de cada rama (el test que evita que un
refactor reintroduzca `col::text`); identificadores maliciosos → `CatalogError` sin
renderizar SQL; prober que devuelve lista vacía con 3 checks → 3 `no verificable`,
cero verdes; SSH caído + destino atrasado → **dos** incidentes; corrida parcial →
**cero** recuperaciones falsas; AC-7 con un DSN de fixture (ni host, ni usuario, ni
clave en reporte ni alerta).

Evals versionadas (`evals/cases/destino_cases.yaml`, ya se ejecutan en CI):
**AC-8** (`margen_final_roll` en 20260721 vs esperado 20260803 → CRITICAL,
afirmando los 13 días) y **la rotación del 3-ago** (10.365 filas con
`venta_sin_impuesto` en cero → CRITICAL; hoy da ✅). Son las dos decisiones caras
de esta spec y merecen ser casos dorados.

Contra el motor (`-m dbproof`, fuera de CI): las 7 aserciones de §5.

---

## Manual verification checklist

- [ ] Catálogo sin ningún bloque `destination:` → reporte idéntico al de hoy (AC-3).
- [ ] `uv run python scripts/preflight_destinos.py` en verde: tabla, columna, tipo e índice.
- [ ] `uv run python scripts/verify_db_role.py --connection gcp_produxdia` en verde, recibo escrito.
- [ ] `OS_DB_PROOF=1 uv run pytest -m dbproof` en verde en el box (AC-6).
- [ ] Corrida a las 00:15 con el ETL sano → **cero** alertas (AC-10, no-fatiga).
- [ ] Un sábado con `ventas_item_diario` (`Mon..Fri`) → INFO, no CRITICAL.
- [ ] `--send` con el destino apagado → **una** condición, ningún job en CRITICAL (AC-5).
- [ ] Una semana de operación normal sin alertas de destino antes de habilitar el resto.
- [ ] Ningún secreto en reporte, alerta ni línea de auditoría (AC-7).
