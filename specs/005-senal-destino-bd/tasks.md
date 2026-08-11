# Tasks — 005 Señal de destino: ¿llegó el dato?

Ordenadas para que **todo lo que no toca producción vaya primero** (CLAUDE.md §1.6).
T1–T10 son cambios de repo, testeables en CI, sin una sola credencial ni conexión:
con el catálogo sin bloques `destination:` el comportamiento es byte a byte el de
hoy, así que se pueden mergear y desplegar sin riesgo. T11–T15 tocan producción y
llevan riesgo y aprobación explícitos. T16–T18 quedan diferidas con su motivo.

**Cada tarea de producción se presenta antes de ejecutarla con: comando exacto,
servidor, propósito, impacto esperado, rollback y nivel de riesgo (§17).**

---

## T0 — Enmendar `spec.md`  ·  riesgo: low  ·  aprobación: no

**Objetivo:** El plan no puede contradecir la spec en silencio (CLAUDE.md §2).
Cuatro enmiendas, todas salidas de la revisión adversarial:

- **Actors:** el vigilante de destinos corre desde un host **distinto** del
  vigilado. Es la única comprobación que funcionó el 8-9 de agosto.
- **NFR-3:** "una conexión por corrida" → "una conexión por `connection` distinta
  por corrida".
- **FR-4b nuevo:** la medida no-cero es su propio requisito, no una nota dentro de
  FR-4. Motivo: FR-4 tal como está escrito **no habría atrapado el caso del 3-ago**
  — 10.365 filas con `venta_sin_impuesto` en cero pasan cualquier `min_rows`
  razonable. Sin FR-4b, una implementación puede "cumplir" FR-4 y seguir ciega al
  incidente para el que se escribió.
- **AC-9:** día presente con medida en cero → CRITICAL.
  **AC-10 (no-fatiga, medible):** una semana de operación normal → **cero** alertas
  de destino. Ningún AC actual mide esto, y es lo que un D-1 fijo rompería.
- **Out of scope:** el destino local del 232 y el heartbeat, con su motivo.

**Archivos:** `specs/005-senal-destino-bd/spec.md`

**Verificar:** el plan no contiene ninguna afirmación que la spec contradiga.

---

## T1 — `DestinationCheck` en el catálogo  ·  riesgo: low  ·  aprobación: no

**Objetivo:** Declarar la verificación sin cambiar nada del comportamiento actual.

**Archivos:** `src/os_system_agent/catalog.py`, `config/alert-rules.example.yml`,
`tests/test_catalog.py`

- `DestinationCheck` frozen junto a `FreshnessRule`; `EtlJob.destinations: tuple[...] = ()`.
- `_parse_destination`: ausente → `()`. **Falla cerrado** en: falta
  `connection`/`table`/`date_column`/`date_type`; `date_type` fuera de
  `{yyyymmdd_text, date}` (sin default silencioso); `table` sin calificar con
  esquema (el rol fija `search_path=''`); `run_days` fuera de 1..7; `day_offset`
  negativo; `min_rows` ≤ 0; `timezone` inválida; `ready_after` ausente **y** el job
  sin `expected_finish_before`.
- **Validación de identificadores como frontera de seguridad, no cosmética:**
  `connection` contra `^[a-z][a-z0-9_]{0,31}$`; cada parte de `table`,
  `date_column` y `measure_column` contra `^[a-z_][a-z0-9_]{0,62}$`, **en carga**,
  no en tiempo de consulta.
- El bloque `destination:` se agrega al job **existente** `daily_sales` del ejemplo,
  no como job nuevo: `tests/test_catalog.py:11-27` afirma `len(jobs) == 1`.

**Comandos:** `uv run pytest -q` · `uv run ruff check .` · `uv run mypy`

**Verificar:** los 9 tests de fail-closed siguen pasando con YAML sin
`destination`; `table: "public.t; DROP TABLE x"` → `CatalogError` y no se renderiza
SQL.

---

## T2 — Día esperado derivado del horario  ·  riesgo: low  ·  aprobación: no

**Objetivo:** **La tarea que hace viable toda la spec.** Un D-1 fijo daría CRITICAL
falso en 4 de las 12 corridas diarias y todos los sábados para los jobs `Mon..Fri`,
o sea dos Telegram por job por día para siempre. Verificado: `expected_finish_before`
y `schedule` se parsean hoy y **ningún evaluador los lee**.

**Archivos:** `src/os_system_agent/monitors/destination.py`, `tests/test_destination_dia.py`

`expected_day(check, now) -> date | None`, pura, con `zoneinfo` (stdlib, sin
dependencia nueva): última corrida programada cuyo `ready_after` ya pasó, menos
`day_offset`, saltando `run_days` y `skip_dates`.

**Ojo, off-by-one real:** los tres entrypoints pasan `datetime.now(UTC)`
(`collect_etl_status.py:41`, `send_daily_report.py:95`, `alert_incidents.py:109`).
A las 02:00 UTC del 12 son las 21:00 del 11 en Bogotá; D-1 en UTC daría el 11
cuando el negocio espera el 10.

**Verificar:** 00:15 con ETL de las 07:00 → espera anteayer, INFO · sábado con
`Mon..Fri` → espera jueves, INFO · lunes festivo → retrocede al hábil anterior ·
`ready_after` distinto por destino en el mismo job.

---

## T3 — Evaluador puro + combinador  ·  riesgo: low  ·  aprobación: no

**Objetivo:** Convertir `(día_esperado, día_observado, presencia, medida, filas)` en
severidad. Sin reloj, sin conexión (NFR-5).

**Archivos:** `src/os_system_agent/monitors/destination.py`,
`src/os_system_agent/monitors/freshness.py` (`DestinationOutcome` + campo con
default en `JobStatus`), `tests/test_destination.py`

- `combine_statuses` vive en `destination.py`, que ya importa `freshness` y
  `severity`. Sin ciclo, sin tocar `reports/daily.py` ni `severity.py`.
- `delay_minutes`/`latest_at` se conservan **los de systemd**.
- **El evaluador itera sobre los checks DECLARADOS** y busca su observación.
  Ausencia = `no verificable`, jamás omisión.
- Medida en cero → **CRITICAL**; `measure_column: null` → "no declarada", que **no**
  puede leerse como que pasó.

**Verificar:** el campo nuevo de `JobStatus` tiene el mismo default en dry-run y
live, para que `test_collect_statuses_dry_matches_collect_dry_run` siga pasando.

---

## T4 — Constructores de SQL  ·  riesgo: low  ·  aprobación: no

**Objetivo:** Las dos ramas de tipo, misma forma de salida, con
`psycopg.sql.Identifier`. **Cero `per-file-ignore` nuevos en `pyproject.toml`**: si
`S608` dispara, quedó un f-string — se arregla el f-string, no el linter.

**Archivos:** `src/os_system_agent/db/query.py`, `tests/test_destination_sql.py`

**Verificar (golden, a nivel string):** rama texto contiene `IS NOT NULL`,
`ORDER BY … DESC`, `LIMIT 1` y **no** contiene `::text` sobre la columna ni
`COLLATE`; rama date lleva el cast del lado constante; `count(*)` aparece **solo**
si se declaró `min_rows`; el SQL renderizado no contiene el literal `20260803`.

---

## T5 — "No verificado" deja de salir como palomita  ·  riesgo: low  ·  aprobación: no

**Objetivo:** Hoy `render_chat_report` solo imprime la línea `↳` para incidentes y
avisos, así que un job INFO con destino no verificable mostraría `✅` limpio y su
evidencia **no se imprimiría nunca**.

**Archivos:** `src/os_system_agent/reports/daily.py`, `tests/test_chat_report.py`

`NOT_YET`/`UNVERIFIABLE` con severidad INFO → icono **`❔`** y línea de detalle.
`NOT_DECLARED` → idéntico a hoy.

**Verificar:** `tests/test_chat_report.py:52` (`"Estado: INFO · incidentes: 0 ·
avisos: 0"`) sigue pasando cuando no hay checks configurados.

---

## T6 — `collect_destinations` como fase hermana  ·  riesgo: low  ·  aprobación: no

**Objetivo:** La fase de destino **no puede vivir dentro de `collect_live`**, que
solo se ejecuta si SSH funcionó. Es la causa raíz del punto ciego.

**Archivos:** `src/os_system_agent/collector.py`, `tests/test_collector.py`

- `collect_destinations(jobs, now, *, prober)` independiente; `prober` keyword-only
  con default, construido **perezosamente** y solo si hay checks.
- Corregir el `return []` temprano de `collector.py:49-50`: hoy un job sin
  `systemd_unit` pero con `destination:` quedaría sin verificar en silencio.

**Verificar:** `test_collect_statuses_dry_matches_collect_dry_run` y los asserts
del comando exacto de `systemctl show` intactos; con `destinations=()` el prober
**no se llama** ni se importa psycopg.

---

## T7 — `alert_incidents` deja de cortar antes del destino  ·  riesgo: low  ·  aprobación: no

**Objetivo:** **El arreglo del caso motivador.** Verificado en
`scripts/alert_incidents.py:84-85`: retorna antes de llamar a `collect_statuses`,
así que con el box caído no corre ni un check de destino. La tabla "box caído +
destino atrasado → CRITICAL con certeza" es hoy inalcanzable en la única ruta que
alerta.

**Archivos:** `scripts/alert_incidents.py`, `src/os_system_agent/alerting.py`,
`tests/test_alert_incidents.py`, `tests/test_alerting.py`

- La fase de destino corre **siempre**. La aridad de la tupla se conserva, así que
  `test_alert_incidents.py:34-38` no se rompe.
- `DESTINATION_ID_PREFIX = "__destino__"` + `destination_unverifiable_status(...)`,
  molde de `SERVER_DOWN_ID`. **Una** condición por `connection`, no por job.
- `diff_incidents(previous, current, *, scope=None)`: `recovered` solo puede
  contener ids evaluados por completo. Sin esto, con SSH caído todos los incidentes
  de systemd saldrían como recuperados → ráfaga de "✅ todo bien" con el ETL caído.
  `scope=None` mantiene el comportamiento actual.
- Etiqueta amable para el id sintético en el dict `names` (`:122-123`).

**Verificar:** SSH caído + destino atrasado → **dos** incidentes, no uno · corrida
parcial → **cero** recuperaciones falsas · state file con clave desconocida → no
produce recuperación.

---

## T8 — Escalada por persistencia  ·  riesgo: low  ·  aprobación: no

**Objetivo:** Un WARNING transitorio que no se cura da **un ping y luego silencio
indefinido** con `diff_incidents`. A las 6 corridas consecutivas (~12 h) pasa a
CRITICAL.

**Archivos:** `src/os_system_agent/alerting.py`, `scripts/alert_incidents.py`, tests

**Archivo aparte `.destination-state.json`.** Nunca mezclar con
`.alert-state.json`: `diff_incidents` trata toda clave como `job_id` y `_save_state`
lo reescribe cada corrida.

---

## T9 — Credenciales + prober  ·  riesgo: low  ·  aprobación: no

**Archivos:** `src/os_system_agent/db/credentials.py`, `db/prober.py`,
`src/os_system_agent/config.py`, `pyproject.toml`, `.gitignore`, tests

- `psycopg[binary]>=3.2` como **`[project.optional-dependencies] db`**, no
  obligatoria: MMAUTOML01 está detrás de WARP y el box de Dinastia ya demostró que
  se queda sin red; hacerla obligatoria haría que `uv sync` pueda romper el reporte
  de systemd que hoy funciona en un box que ni declara destinos. **Import perezoso**
  dentro de la fábrica del prober — si `monitors/destination.py` arrastrara psycopg,
  CI y `tests/test_mcp_estado.py:49` se caen.
- `DbCredentials` frozen, `__repr__` enmascarado, `connect_kwargs()`. **Nunca se
  construye un DSN.** `require_connection` falla cerrado nombrando **variables, no
  valores**.
- **La frontera lanza `DestinationUnavailable(connection, sqlstate, etiqueta)` sin
  campo de texto libre**, para que sea imposible pasar `str(exc)`: los errores de
  psycopg traen `connection to server at "10.x.x.x" … user "os_agent_ro"`, y
  `redaction.py` **no** tiene ningún patrón de host/IP, así que `redact()` no lo
  taparía.
- `.gitignore`: añadir `*.env` y `.pgpass`.

---

## T10 — Evals doradas  ·  riesgo: low  ·  aprobación: no

**Archivos:** `evals/cases/destino_cases.yaml`

**AC-8:** `margen_final_roll` en `20260721` vs esperado `20260803` con systemd
`success` → CRITICAL, afirmando los **13 días** (la aritmética se rompe sola) y que
la evidencia menciona literalmente que la unit dijo éxito (AC-1).
**Rotación 3-ago:** 10.365 filas con la medida en cero → CRITICAL. **Hoy este caso
da ✅.**

Se valida contra el evaluador puro, sin BD — más fuerte como regresión que una
comprobación en vivo.

---

## T11 — Preflight contra GCP  ·  riesgo: low (solo lectura)  ·  aprobación: **sí** (primera conexión a producción)

**Objetivo:** Confirmar contra la BD lo que hoy es **suposición no verificada**:
`margen_final_roll` —la tabla del AC-8— no está en ninguna lista de esquemas
verificados, así que su columna de fecha y su tipo hay que confirmarlos **antes**
de escribir su bloque en el catálogo.

**Archivos:** `scripts/preflight_destinos.py`

Comprueba, en una sola pasada y solo con `SELECT`: existencia de tabla y columna,
tipo real vs `date_type` declarado, **índice cuya primera columna sea la de fecha**
(`pg_index.indkey[0]`), `prosecdef` de las funciones `refresh_%roll%`, y
`rolvaliduntil`.

**Sin índice → el destino NO se da de alta.** Crear el índice es DDL y lo pide el
DBA (CLAUDE.md §4).

**Rollback:** ninguno, no cambia estado.

---

## T12 — Rol `os_agent_ro` + GRANTs por columna  ·  riesgo: **MEDIUM**  ·  aprobación: **sí**

**Archivos:** `sql/role_os_agent_ro.sql` (versionado, **sin password**)

`CREATE ROLE` + `ALTER ROLE SET` + `GRANT SELECT (columnas)`. Lo ejecuta el DBA, no
el agente. Password fuera de banda (`psql -v agent_pw="$(openssl rand -base64 33)"`),
nunca en el archivo ni en argv.

**Impacto esperado:** un rol nuevo sin ningún privilegio de escritura; ningún ETL
existente cambia.

**Rollback:** `REVOKE ALL ON <tablas> FROM os_agent_ro; DROP ROLE os_agent_ro;`
previa verificación de que ningún ETL lo usaba. **`NOLOGIN` no es rollback** — no
revierte los GRANT; es el kill switch de emergencia y va al `security-runbook.md`.

**Verificar:** la lista de GRANT se **genera desde el catálogo** (T14), no se
escribe a mano — así no vuelve a faltar `margen_final_roll`, que es justo la tabla
del AC-8.

---

## T13 — Cloud SQL Auth Proxy en MMAUTOML01  ·  riesgo: **MEDIUM**  ·  aprobación: **sí**

**Objetivo:** Llegar a GCP **sin allowlistear ninguna IP**. La de MMAUTOML01 no es
estable y es compartida con todo el pool de Cloudflare WARP: meterla en
*authorized networks* autorizaría a un rango enorme de terceros contra producción,
y lo haría **para todos los roles**, no solo el nuestro.

SA dedicada con **solo** `roles/cloudsql.client` (+ `cloudsql.instanceUser` si IAM
authn). Con `--auto-iam-authn` **desaparece el password de BD**. Unit systemd con
`EnvironmentFile=` 0600 en un directorio 0700 — **nunca `Environment=`**, que lo lee
cualquier usuario local con `systemctl show` (hueco que hoy ya existe con el
`TELEGRAM_BOT_TOKEN` en los `.example`, y que esta tarea corrige de paso).

**Rollback:** `systemctl disable --now cloud-sql-proxy` + borrar la SA.

---

## T14 — `verify_db_role.py` + recibo  ·  riesgo: low  ·  aprobación: **sí** para la sonda de escritura

**Objetivo:** Que **AC-6 sea un gate de runtime**, no una línea de checklist.
`addopts = "-q -m 'not dbproof'"` significa que nada corre la prueba de forma
automática, así que se podría desplegar el rol mal concedido y AC-6 no comprobarse
nunca. *"Un control que vive en el mismo artefacto que el bug no es un control"* —
aplicado al propio plan de verificación.

**Archivos:** `scripts/verify_db_role.py`, `tests/test_db_readonly_proof.py`
(marcada `dbproof`), `pyproject.toml`, `CLAUDE.md` §16

- Imprime el conjunto **exacto** de privilegios requerido por el catálogo y lo
  compara contra `information_schema.column_privileges` + `pg_roles`
  (`rolsuper`, `rolbypassrls`, `rolconfig`). Un privilegio inesperado es **fallo**,
  y se reporta con severidad **SECURITY** (§12).
- Escribe `~/.config/os-system-agent/verify-<connection>.json` con el hash del
  conjunto y un timestamp. **El prober se niega a conectar** sin recibo fresco
  (< 30 días) → WARNING "rol sin auditar", nunca ✅.
- Sonda de escritura: `UPDATE <tabla> SET <col> = <col> WHERE false`, **inofensiva
  aunque la guarda esté rota**, cada bloque en `try/finally: rollback()` — no como
  línea siguiente, que no correría justo en el caso que el test existe para
  detectar. `CREATE TABLE` contra un **esquema inexistente** (peor caso 3F000,
  nunca una tabla huérfana en producción). Y el paso que la hace no-decorativa:
  **apagar `default_transaction_read_only` a propósito** y repetir, para que
  responda el GRANT (42501) y no la bandera, que el propio rol puede apagar.
- La corrida queda documentada en `docs/security-runbook.md` con clasificación §17
  y aprobación registrada.

---

## T15 — Habilitar destinos job por job  ·  riesgo: low  ·  aprobación: **sí** (por job)

**Objetivo:** Adopción incremental (FR-6). Empezar por **`sync_gcp_diario`**, que
es el que motivó la spec.

**Archivos (gitignored, por box):** `config/alert-rules.yml`

**Verificar antes de agregar el siguiente:** **una semana completa con cero alertas
de destino** (AC-10). Es lo que un D-1 fijo rompería, y lo que ningún AC medía.

---

## T16 — DIFERIDA: destino local del 232  ·  riesgo: **MEDIUM**  ·  aprobación: **sí**

Exige túnel SSH (sale del camino auditado de `run_read_only`, deja un forward
alcanzable por cualquier usuario local del box), edición de
`/etc/postgresql/18/main/pg_hba.conf` y de `authorized_keys` con
`restrict,port-forwarding,permitopen="127.0.0.1:5432"`. Y los rollups locales están
congelados desde el 2026-07-21 **a propósito**: son residuo, ~17,5 GB. El valor
está en GCP.

Si se hace: helper dedicado con argv fijo, **sin `-f`** (proceso hijo con `Popen`,
cerrado en `finally`), puerto efímero, y documentado en el runbook que el forward
es visible localmente.

---

## T17 — DIFERIDA: `REVOKE` de las funciones `refresh_*`  ·  riesgo: **MEDIUM**  ·  aprobación: **sí**

Solo si T11 encuentra `prosecdef = true`. **Va después de que la señal de destino
esté viva y verde**, nunca antes: si el `GRANT EXECUTE` compensatorio va al rol
equivocado, `visor-etl-sync` empieza a fallar **dentro del mismo
`|| { log "WARN…"; return 0; }` que originó esta spec** — o sea, en silencio. Con la
005 activa, el propio monitor es el detector del daño.

**Verificación obligatoria:** `margen_final_roll` avanza en la corrida siguiente.
**Rollback:** `GRANT EXECUTE ON FUNCTION … TO PUBLIC`.

---

## T18 — DIFERIDA: heartbeat (spec 006)

La 005 quita el punto ciego del box vigilado, pero no el del host vigilante: si
MMAUTOML01 muere, vuelve el silencio y el silencio se sigue pareciendo a la salud.
Hace falta un latido por corrida y un verificador **en otro host** que alerte si el
latido esperado no llegó. Hasta entonces, **la 005 reduce el riesgo de silencio, no
lo elimina** — y eso queda escrito, no supuesto.

Mitigación disponible mientras tanto: `produxdia` en GCP es alcanzable desde
MMAUTOML01 **y** desde server232, así que el destino puede verificarse desde dos
sitios sin infraestructura nueva.
