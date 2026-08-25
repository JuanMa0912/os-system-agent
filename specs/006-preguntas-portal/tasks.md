# Tasks 006 — Unidades de implementación

> Requiere `spec.md` y `plan.md`. Cada tarea es pequeña, revisable y verificable con un
> comando. **Nada de T3 en adelante se toca hasta cerrar T1.**
>
> Convención de riesgo: `low` = solo código local · `medium` = toca configuración o red
> · `high` = toca producción o credenciales.

---

## T1 — Cerrar los supuestos bloqueantes

**Objetivo:** ninguno de los tres supuestos que pueden hacer daño a terceros queda abierto.

**Archivos:** `specs/006-preguntas-portal/_supuestos.md` (nuevo).

**Comandos:**
```bash
# ¿comparten IP de salida el box del agente y las oficinas?
curl -s ifconfig.me                      # en el box del agente
curl -s ifconfig.me                      # en una maquina de oficina

# ¿esta definido el secreto? SIN imprimir el valor
grep -c '^AUDIT_IP_HMAC_SECRET=' .env.local

# ¿contra que despliegue habla el agente?
curl -s -o /dev/null -w '%{http_code}\n' "$OS_PORTAL_BASE_URL/api/health"
```

**Verificación:** `_supuestos.md` responde S1, S2 y S3 con evidencia y **sin transcribir
ningún valor de secreto** — solo presencia o ausencia.

**Riesgo:** `low` · **Aprobación:** no

> El operador ya confirmó S2: **comparten IP**. Queda por confirmar si
> `AUDIT_IP_HMAC_SECRET` está definido, porque decide si el radio de daño es una IP o
> una `/24` completa.

---

## T2 — `redaction.py` v2, antes de que exista una cookie que ocultar

**Objetivo:** que el redactor cubra lo que este proyecto va a manejar, no lo que manejaba.

**Archivos:** `src/os_system_agent/redaction.py`, `evals/cases/redaction_cases.yaml`.

**Alcance:** patrones para `Set-Cookie: vp_session=`, `Cookie:`, `x-csrf-token`, JSON
`{"password": ...}`, PEM, JWT, IP privada, correo corporativo. Aplicado al escribir **y al
leer**. Los casos suben de 4 a ≥ 20.

**Comandos:**
```bash
uv run pytest -q tests/test_redaction.py tests/test_evals.py
uv run python -c "import yaml;d=yaml.safe_load(open('evals/cases/redaction_cases.yaml'));assert len(d)>=20,len(d)"
```

**Verificación:** ambos comandos en verde.

**Riesgo:** `low` · **Aprobación:** no

---

## T3 — Cliente HTTP contra el único endpoint público, sin credenciales

**Objetivo:** probar red, TLS, DNS, forma del cliente y taxonomía de errores **sin tocar una
sola credencial**.

**Archivos:** `src/os_system_agent/portal/{__init__,client,errors}.py`,
`scripts/portal_probe.py`, `tests/test_portal_client.py`, `tests/test_portal_errors.py`,
`evals/cases/portal_errors_cases.yaml`.

**Alcance:** solo `GET`. `Fetcher` inyectable (mismo patrón que `collector.py`). Timeouts
explícitos. `max_retries=0`. Concurrencia 1. Allowlist de método y de **ruta exacta**.

**Comandos:**
```bash
uv add "httpx>=0.27"
uv run pytest -q tests/test_portal_client.py tests/test_portal_errors.py
uv run python scripts/portal_probe.py --health --json | python -c "import json,sys;assert json.load(sys.stdin)['ok']"
```

**Verificación:** existe un test que intenta un `POST` y comprueba que el cliente **lanza**
antes de emitir la petición.

**Riesgo:** `low` · **Aprobación:** no

---

## T4 — Presupuesto de login persistido

**Objetivo:** que sea **estructuralmente imposible** que el agente agote el rate limit
compartido.

**Archivos:** `src/os_system_agent/portal/budget.py`, `tests/test_portal_budget.py`.

**Alcance:** máximo **2 fallos por ventana de 15 min**. Contador en
`var/portal-login-budget.json`, incrementado **antes** de enviar la petición. Lock
`O_EXCL` compartido entre procesos. Pre-vuelo con `/api/health` obligatorio antes de todo
login.

**Comandos:**
```bash
uv run pytest -q tests/test_portal_budget.py -k "sobrevive_reinicio or dos_procesos_un_login or preflight_evita_login"
```

**Verificación:** el test `sobrevive_reinicio` escribe el contador, simula muerte del
proceso, y confirma que el contador **no** se reinició.

**Riesgo:** `low` · **Aprobación:** no

> Esta es la tarea que protege a los compañeros de oficina. Si algo de este plan se
> implementa a medias, que no sea esto.

---

## T5 — Fechas con zona horaria explícita

**Objetivo:** que «ayer» sea ayer para el negocio, no para UTC.

**Archivos:** `src/os_system_agent/ask/dateparse.py`, `tests/test_dateparse.py`.

**Alcance:** `today_local()` con `zoneinfo`, zona tomada del catálogo. Prohibido
`date.today()`.

**Comandos:**
```bash
uv run pytest -q tests/test_dateparse.py
grep -rn "date.today()\|datetime.now()" src/os_system_agent/ask/ && echo "FALLO: fecha sin zona" || echo "OK"
```

**Verificación:** el grep no encuentra nada, y hay casos de prueba en los bordes del día.

**Riesgo:** `low` · **Aprobación:** no

---

## T6 — Catálogo de intents versionado

**Objetivo:** añadir una pregunta es editar un YAML, no escribir código. Y el allowlist
queda revisable en el diff del PR.

**Archivos:** `config/ask-queries.yml` (**versionado**, no ignorado),
`src/os_system_agent/ask/{intents,catalog}.py`, `tests/test_catalog_ask.py`.

**Alcance:** cada intent declara ruta exacta, parámetros, `drop_fields` **obligatorio**,
plantilla de respuesta y contrato de la respuesta esperada. Sin `drop_fields`, el catálogo
**no carga**.

**Comandos:**
```bash
uv run pytest -q tests/test_catalog_ask.py -k "drop_fields_obligatorio or ruta_exacta_no_prefijo"
git check-ignore config/ask-queries.yml && echo "FALLO: el allowlist esta ignorado" || echo "OK: versionado"
```

**Verificación:** un intent sin `drop_fields` produce error de carga, y el catálogo **no**
está en `.gitignore`.

**Riesgo:** `low` · **Aprobación:** no

---

## T7 — Decidir el alcance real de la cuenta, endpoint por endpoint

**Objetivo:** saber exactamente qué va a poder leer el agente **antes** de que exista la
cuenta.

**Archivos:** `specs/006-preguntas-portal/_alcance-cuenta.md` (nuevo).

**Alcance:** para cada endpoint candidato, documentar qué campos devuelve y **si alguno es
dato personal**. `/api/margenes/data` se analiza como **13 endpoints distintos**, no como uno.

**Verificación:** el documento marca explícitamente qué endpoints exponen NIT o cédula, y la
lista final de subtableros a conceder no incluye ninguno de ellos — o lo hace con
`drop_fields` que los elimina y una nota de aceptación de riesgo firmada por el operador.

**Riesgo:** `low` · **Aprobación:** no (pero su **resultado** alimenta la aprobación de T8)

---

## T8 — Crear la cuenta y verificar su alcance

**Objetivo:** la cuenta existe y su alcance real es **exactamente** el declarado. Un permiso
de más es un hallazgo `SECURITY`.

**Archivos:** `scripts/verify_portal_user.py`, `src/os_system_agent/portal/credentials.py`,
sección nueva en `docs/security-runbook.md`.

**Quién hace qué:** **el operador crea el usuario** (es escritura en producción). Nosotros
solo verificamos.

**Comandos:**
```bash
uv run python scripts/verify_portal_user.py --strict --json
diff <(uv run python scripts/verify_portal_user.py --print-expected) \
     <(uv run python scripts/verify_portal_user.py --print-actual)
uv run python scripts/verify_portal_user.py --assert-denied /api/admin/users
```

**Verificación:** el `diff` sale **vacío**, `--assert-denied` sale 0, y desactivar la cuenta
en `/admin/usuarios` produce 401 en **menos de 10 segundos** (cronometrado a mano).

**Riesgo:** `high` · **Aprobación:** **sí** — presentar antes: comando exacto, alcance
solicitado, impacto y procedimiento de revocación.

---

## T9 — Sesión: login, cookie, latido perezoso

**Objetivo:** una sesión que aguanta una ráfaga de preguntas sin contaminar las métricas del
portal.

**Archivos:** `src/os_system_agent/portal/session.py`, `scripts/portal_doctor.py`,
`tests/test_portal_session.py`.

**Alcance:** fichero de sesión `0600`. Latido **solo** dentro de una ráfaga activa; nunca por
temporizador. Al terminar el turno, dejar expirar. `portal_doctor` explica cualquier fallo
con su causa probable.

**Comandos:**
```bash
uv run pytest -q tests/test_portal_session.py -k "relogin_unico or no_late_en_vacio or secreto_no_aparece"
uv run python scripts/portal_doctor.py --json
```

**Verificación:** el test `no_late_en_vacio` confirma que sin consultas no se emite un solo
heartbeat.

**Riesgo:** `medium` · **Aprobación:** **sí** (primer login real contra producción)

---

## T10 — Memoria persistente, con sus lectores

**Objetivo:** memoria que **se lee**. Si nadie la lee, no es memoria.

**Archivos:** `src/os_system_agent/state/{schema.sql,store.py}`, `scripts/state_init.py`,
`tests/test_state.py`.

**Alcance:** las cuatro tablas de `plan.md §7`, **cada una con su lector implementado en la
misma tarea**: historia de incidentes, backlog de intents fallidos, freno por presupuesto y
ledger de consultas.

**Comandos:**
```bash
uv run python scripts/state_init.py --db var/state.db --apply
sqlite3 var/state.db "PRAGMA integrity_check;"
uv run pytest -q tests/test_state.py -k "lee_historia or frena_por_presupuesto or ledger_reconstruye"
```

**Verificación:** existe un test por cada lector. Una tabla sin lector es una tarea
incompleta.

**Riesgo:** `low` · **Aprobación:** no

---

## T11 — Primer lazo cerrado, con cero modelo

**Objetivo:** el operador escribe un comando en Telegram y recibe una cifra correcta con su
fecha de corte. Sin modelo en el camino.

**Archivos:** `scripts/ask_cli.py`, un intent real en `config/ask-queries.yml`.

**Comandos:**
```bash
uv run python scripts/ask_cli.py --intent <intent> --params '{...}' --json
```

**Verificación:** la respuesta trae la cifra **y** su fecha de corte; y una consulta sobre un
rango sin dato responde «no hay dato», no un cero.

**Riesgo:** `medium` · **Aprobación:** no

---

## T12 — Grounding y prosa por plantilla

**Objetivo:** que el modelo no pueda inventar cifras **ni** ser inyectado a través de los
datos.

**Archivos:** `src/os_system_agent/ask/ground.py`, `tests/test_grounding.py`.

**Alcance:** la prosa la renderiza una **plantilla**, no el modelo. Los campos de texto libre
del portal se truncan y escapan, y nunca se concatenan a un prompt de sistema.

**Comandos:**
```bash
uv run pytest -q tests/test_grounding.py -k "cifra_inventada_rechazada or texto_malicioso_no_es_instruccion"
```

**Verificación:** el segundo test mete texto con forma de instrucción en un campo de datos y
comprueba que no altera el comportamiento.

**Riesgo:** `low` · **Aprobación:** no

---

## T13 — Ruteo de modelos con tope en dólares

**Objetivo:** que el escalón de pago no pueda desbocarse.

**Archivos:** `src/os_system_agent/ask/router.py`, `config/ask-models.yml`.

**Alcance:** T0/T1 deterministas; T2 escalón gratuito; T3 de pago con tope en
`budget_day.costo_usd`. Al llegar al tope, el agente **rehúsa y lo dice**.

**Comandos:**
```bash
uv run pytest -q tests/test_router.py -k "t0_sin_modelo or rehusa_al_tope"
```

**Verificación:** el test confirma que al alcanzar el tope la respuesta explica la
degradación en vez de fallar en silencio.

**Riesgo:** `low` · **Aprobación:** no

---

## T14 — Aviso de caducidad de contraseña

**Objetivo:** que el día 30 no sorprenda a nadie.

**Archivos:** `scripts/alert_incidents.py` (fase nueva).

**Alcance:** avisar por Telegram a **7 y 2 días** de la caducidad. Procedimiento de dos pasos
en el runbook: rotar en el portal **y** actualizar el `EnvironmentFile` del box, como una sola
tarea, no como recordatorio.

**Comandos:**
```bash
uv run pytest -q tests/test_password_expiry.py
```

**Verificación:** el test simula día 23 y día 28 y confirma que avisa una sola vez por umbral.

**Riesgo:** `low` · **Aprobación:** no

---

## T15 — Canal de Telegram y semana en sombra

**Objetivo:** ponerlo en manos del operador, con red.

**Alcance:** DM 1:1, allowlist de `chat_id` numéricos. **Sin grupos.** Una semana respondiendo
en paralelo al uso normal del portal, comparando cifras.

**Verificación:** durante una semana, ninguna respuesta del agente difiere de lo que muestra
el portal para la misma pregunta.

**Riesgo:** `medium` · **Aprobación:** **sí**

---

## Pendientes que no son tareas de código

- **Rotar la contraseña de `produ`** — sigue pendiente del operador. No bloquea esta spec,
  pero sigue abierto.
- **Pedir el token de servicio** (`/api/agent/*` con alcance fijo) al equipo del portal.
  Elimina de raíz la caducidad a 30 días, la revocación mutua de sesiones, la ventana de
  5 minutos y el riesgo de rate limit. **Es la mejor inversión de este proyecto** y es un PR
  pequeño en un repo del mismo equipo.
- **Definir `AUDIT_IP_HMAC_SECRET`** en el portal si no lo está: reduce el radio de daño del
  rate limit de una `/24` entera a una sola IP. Mejora barata e independiente de este proyecto.
- **Pedir a infraestructura** un egreso separado para el box del agente, o un
  `limit_except GET { deny all; }` para su IP.
