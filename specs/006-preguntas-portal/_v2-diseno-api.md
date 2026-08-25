# Decisión de arquitectura — Spec 006 «preguntas de negocio por la API del portal»

**Columna vertebral elegida: el Diseño 2 (el que optimiza *«que siga respondiendo dentro de un año»*).**
**Injertos tomados del Diseño 1: el orden de hitos con lazo cerrado temprano, y las tres listas de veto explícitas del catálogo (`denied_prefixes`, `denied_params`, `drop_fields`).**

Por qué esa y no la otra, contra los criterios en su orden:

- **(1) que el agente no pueda romper ni borrar.** Empatan en los controles de cliente (solo GET, concurrencia 1, un login por invocación). El Diseño 2 gana por **un control que el Diseño 1 no tiene**: excluir de la *ficha del usuario* los subtableros cuyo permiso de lectura arrastra verbos de escritura (`rotacion`, `checklists`, `registro-de-horarios`, `inventario-x-item`), de modo que **es el portal quien responde 403**, no nuestro código. Un allowlist de método en nuestro cliente es un `if`; una cuenta sin esa sección es una negativa del servidor.
- **(2) respuestas correctas con su fecha de corte.** Aquí la diferencia es grande y decide. El Diseño 2 convierte RF-04 en una **restricción de la base** (`CHECK (verdict <> 'ok' OR cut_off IS NOT NULL)`), hace que **la respuesta la escriba una plantilla del catálogo y no el modelo**, y añade lo que al Diseño 1 le falta: `endpoint_contract` + `PortalContractError`, es decir, *si el portal renombra una clave, el agente rehúsa; nunca reporta cero*. El Diseño 1 propone el mismo test (`portalproof`) pero sin tabla que registre la deriva ni timer que la vigile.
- **(3) reutilización.** Idéntica en los dos: `notify.py`, `reports/daily.py`, `alerting.py`, el patrón `Runner`→`Fetcher`, revivir `config.py`.
- **(4) incrementos verificables.** **Aquí gana el Diseño 1 y por eso se injerta.** El Diseño 2 no produce una respuesta útil hasta M9/M10; el Diseño 1 la produce en H4 con un intent, cero modelo y cero SQLite. Ese hito se mete tal cual, movido a la posición 7.

Lo que **no** se toma del Diseño 1: su decisión de descartar `portal_doctor` (no lo tiene) y su ausencia de M0. Lo que **no** se toma del Diseño 2: la autorrotación de contraseña, ni siquiera opt-in (§9).

---

## 0. Hechos verificados

Dos raíces:
`PORTAL = C:\Users\PROYECTOS\Desktop\visor-productividad-master\`
`AGENTE = C:\Users\PROYECTOS\Desktop\Claude_Multi_Agents_Projects\OS_system_AI\`
Las rutas de abajo son relativas a esas raíces.

**Re-verificados por mí en esta sesión (lectura directa del portal):**

| # | Hecho | Evidencia |
|---|---|---|
| V1 | La sesión muere a los **5 minutos** de inactividad. | `PORTAL src/lib/auth/session-idle.ts:2` (`SESSION_IDLE_MINUTES = 5`) |
| V2 | **Las lecturas NO renuevan la sesión.** El único camino que mueve `expires_at` es `extendSessionOnActivity`, con el comentario literal *«Solo actividad real (heartbeat). No usar en /me ni en APIs de lectura»*. `applySessionCookies` reemite la cookie con la fecha que ya tenía. | `PORTAL src/lib/auth/index.ts:295` (comentario), `:282-290` (el UPDATE), `:440-450` |
| V3 | **No existe rol de solo lectura.** En `cero-estados`, el GET (línea 259) y el PATCH (línea 419) llaman **el mismo** `rotacionAuthGate(session)`. El PATCH solo añade `verifyCsrf` (línea 412), y el bot tiene ambas cookies. | `PORTAL src/app/api/rotacion/cero-estados/route.ts:259, 412, 419`; gate en `:172-200` |
| V4 | La contraseña caduca a los **30 días**. | `PORTAL src/lib/auth/password-policy.ts:3` |
| V5 | Pool de **15 conexiones**, `statement_timeout` por defecto **800 000 ms** (13 min). | `PORTAL src/lib/db/index.ts:134, :107` |
| V6 | La caché de márgenes indexa por querystring crudo con tope de **300 entradas** y TTL 5 min. | `PORTAL src/lib/margenes/query-cache.ts:17-19` |
| V7 | `app_user_sessions.ip` y `app_user_login_logs.ip` son de tipo **`inet`**. | `PORTAL db/schema-auth.sql:33, :41` |

**Transcritos del reconocimiento, con su evidencia, NO re-verificados por mí:**

| # | Hecho | Evidencia (del reconocimiento) |
|---|---|---|
| R1 | Única autenticación: usuario/contraseña → cookie `vp_session` (httpOnly, lax, `Expires` = +5 min) + `vp_csrf` (no httpOnly). No hay bearer, API key ni service account en las 71 rutas. | `PORTAL src/lib/auth/index.ts:43-44, :387-400`; grep de `Bearer\|x-api-key\|apiKey` sobre `src/` → cero |
| R2 | `createSessionReplacingOthers` revoca **todas** las sesiones previas del mismo usuario (advisory lock + `revokeAllSessionsForUser`). | `PORTAL src/lib/auth/index.ts:209-237, :256-276` |
| R3 | Contraseña caducada → `requireAuthSession()` devuelve `null` → **401 en las 42 rutas de datos**, mientras `/api/auth/me` **sigue respondiendo 200**. | `PORTAL src/lib/auth/index.ts:617-631, :641-647`; `src/app/api/auth/me/route.ts:4-10` |
| R4 | `null` = **TODAS**, `[]` = **NINGUNA** en `allowed_dashboards`/`allowed_subdashboards`. `precios-proveedor` y `ordenes-compra` son opt-in y **no** se heredan de `null`. | `PORTAL src/lib/shared/portal-sections.ts:191-243, :249-256` |
| R5 | Rate limit de login: **10 por red, 5 por usuario**, ventana de 15 min fijada en el **primer** fallo, evaluado **antes** de validar credenciales. | `PORTAL src/app/api/auth/login/route.ts:23-29, :115-144` |
| R6 | La clave de red es HMAC de la IP si `AUDIT_IP_HMAC_SECRET` está puesta; si no, **el /24**. Y ese HMAC (`hmac:<hex>`) se escribe en columnas `inet` → error 22P02 → **500 opaco en el login**. | `PORTAL src/lib/auth/index.ts:133-154, :177-199`; cruzado con V7 |
| R7 | `/api/rotacion`, `/api/margenes/data`, `/api/informe-variacion`, `/api/analisis-de-inventario`, `/api/participacion-comercial` **no tienen rate limit ninguno**. | ausencia de `checkRateLimit` en esos handlers |
| R8 | `/api/productivity` devuelve **HTTP 200 con `{dailyData:[], error:"…"}`** y header `X-Data-Source: fallback` cuando la BD falla. | `PORTAL src/app/api/productivity/route.ts:182-197` |
| R9 | `/api/portal/freshness` se traga sus errores: 200 con `{updatedAt:null, sources:[]}`. | `PORTAL src/app/api/portal/freshness/route.ts:25-29` |
| R10 | Único error con código máquina de toda la API: `code:"DATE_NOT_FOUND"` en `ventas-x-item`, que trae `availableStart/availableEnd`. | `PORTAL src/lib/ventas/x-item-date-range.ts:127-160` |
| R11 | `/api/ingresar-horarios/people` autoriza por sección `operacion` y **NO consulta `allowedSedes`**: un GET devuelve la nómina completa. | `PORTAL src/app/api/ingresar-horarios/people/route.ts:57-70` |
| R12 | `POST /api/proveedores/ingreso` es **público, sin sesión**, acepta 120 caracteres de texto libre sin filtro de charset a 40/min, y ese valor sale como `visitanteNombre`. | `PORTAL src/app/api/proveedores/ingreso/route.ts:70-77`; `src/lib/proveedores/types.ts:42-50` |
| R13 | `/api/hourly-analysis` envía `overtimeEmployees` (cédula, nombre, departamento) **en la respuesta base**, sin `includePeople=1`. | `PORTAL src/app/api/hourly-analysis/route.ts:2004`; `src/types.ts:108-127` |
| R14 | La auditoría de exportaciones la reporta **el propio cliente**, voluntariamente. No hay access log de consultas: mil GET no dejan una fila. | `PORTAL src/app/api/exports/log/route.ts:96` |
| R15 | Revocación: `PATCH is_active:false` surte efecto al siguiente request (`getUserSession` valida `u.is_active` sin caché), pero **no revoca la fila de sesión**. | `PORTAL src/lib/auth/index.ts:545-551`; `src/app/api/admin/users/[id]/route.ts:782-783` |
| R16 | Un usuario `role:'user'` **debe** tener `sede` o `allowedSedes` no vacío, o el POST responde 400. | `PORTAL src/app/api/admin/users/route.ts:520-525` |
| R17 | `/api/rotacion` recorta la ventana a **93 días en silencio**. `margenes/meta` e `informe-variacion/meta` usan **YYYYMMDD** mientras el resto usa ISO. | `PORTAL src/app/api/rotacion/route.ts:521`; `src/app/api/margenes/meta/route.ts:19-31` |
| R18 | `src/proxy.ts` deja pasar **todo** `/api/*` sin verificar: no hay chokepoint central. | `PORTAL src/proxy.ts:109` |
| R19 | En el agente: `httpx 0.28.1` **ya está resuelto** en `uv.lock` vía `mcp`. `[tool.mypy]` está configurado y **ningún step de CI lo invoca**. `_KV_SECRET` no enmascara `{"password": "…"}` en JSON. `.alert-state.json` es un dict sin timestamps en ruta relativa al CWD cuyo lector se traga los errores. | `AGENTE uv.lock:263-274`; `pyproject.toml:78-87`; `src/os_system_agent/redaction.py:26-30`; `scripts/alert_incidents.py:49, 62-74` |

**SUPUESTOS — no confirmados por nadie, y M0 existe para cerrarlos:**

- **S1** `AUDIT_IP_HMAC_SECRET` **no** está definida en el despliegue. Si lo estuviera, el login devuelve 500 para el bot **y para los humanos** (V7+R6). Si no lo está, el rate limit del login es por **/24** y un bucle nuestro bloquea a la oficina.
- **S2** El box del agente y las oficinas no comparten `/24` de salida. Sin confirmar, no se puede cuantificar el bloqueo cruzado ni el reparto de cupo por IP.
- **S3** Contra cuál despliegue habla el agente (`VISOR_DEPLOYMENT`: LAN del 232 o GCP). Una `allowed_paths` escrita contra el equivocado falla entera.
- **S4** El box alcanza la URL base por HTTPS con certificado válido.
- **S5** Id exacto del modelo local (`ollama list`), versión de OpenClaw y existencia de `registerCommand` en ella, y que el `sqlite3` del box trae FTS5.
- **S6** **La forma exacta del JSON de cada endpoint.** Ninguno de los dos diseños la verificó contra una respuesta real: los JSONPath de los catálogos de ejemplo son razonados, no medidos. Es el supuesto que más trabajo esconde.
- **S7** `EXCEL_DIAN_PUBLIC_ACCESS`, `TRUST_PROXY`, `SESSION_COOKIE_SECURE`, y si el nginx desplegado coincide con el documentado (`docs/DEPLOYMENT.md:230`, que **apenda** XFF y lo vuelve falsificable).
- **S8** El tipo `inet` de V7 es del schema versionado; los snapshots reales son del 23 de junio. Confirmar con `\d app_user_sessions` antes de tocar nada de auditoría de IP.

---

## 1. Arquitectura

**Un proceso por turno, sin demonios nuevos.** Todo el carril de preguntas vive en procesos efímeros lanzados por OpenClaw (`execFile`, sin shell) o por un timer. No hay servicio de larga vida que mantener vivo: si algo falla, falla dentro del turno, se ve y se registra. Lo único persistente son dos ficheros: `var/state.db` y `var/portal-session.json`.

**Tres funciones puras y una impura.** `router.route()`, `ground.verify_grounding()` y el render por plantilla son puras, con `now` inyectado — el mismo contrato que hoy hace testeable `alerting.diff_incidents` (`AGENTE src/os_system_agent/alerting.py:62`). La única impura es `portal.client.PortalClient`, y se escribe con un `Fetcher` inyectable, igual que `Runner` en `collector.py:30` y `Sender` en `notify.py:32`. **Esa es la decisión que preserva que la suite corra sin red**; si se pierde, se pierden los 124 tests actuales.

**La respuesta no la escribe un modelo.** Se rellena `render.template` del catálogo con valores extraídos del JSON. La narración de T3/T4 es una capa opcional encima, y además pasa grounding. Consecuencia comprobable y no negociable: con `OS_ASK_NO_MODEL=1`, con ollama caído y sin clave de Anthropic, **el agente sigue respondiendo** — degradado, y diciéndolo.

**Cuatro fronteras, en orden de dureza.** (1) *El portal*: el alcance por sede/empresa/línea lo impone el servidor desde la ficha del usuario dedicado. Es la ganancia gratis de la decisión HTTP. (2) *La ficha de la cuenta*: sin los subtableros con verbos de escritura, la propia API responde 403. (3) *El catálogo*: `allowed_paths` + `method: GET` + `denied_prefixes` + `denied_params`, validados al cargar, fail-closed. (4) *El cliente*: concurrencia 1, token bucket persistido, un login por invocación. El modelo vive **fuera de las cuatro**: recibe `{id, descripción, nombres de parámetro}` y devuelve `{"intent_id", "params"}`. Nunca ve un `path`, nunca ve `allowed_paths`, nunca ve la URL base.

```
┌─ Telegram — DM 1:1, allowlist de chat_id NUMÉRICOS, SIN grupos en la v1 ──────┐
└──────────────────────────────┬────────────────────────────────────────────────┘
                               │ el TEXTO viaja por STDIN, JAMÁS por argv
                 ┌─────────────▼──────────────────────────────────┐
                 │ OpenClaw Gateway (WSL2, bind loopback)         │
                 │ extensions/os-agent-ask → registerCommand()    │
                 │  /p /pro /frescura /preguntas /porque /ayuda   │
                 │  execFile sin shell · sin LLM en el lazo       │
                 └─────────────┬──────────────────────────────────┘
                 ┌─────────────▼──────────────────────────────────┐
                 │ scripts/ask_cli.py — un proceso POR TURNO      │
                 │ presupuesto de 30 s con reloj monótono         │
                 │ SIN --target · SIN --direct                    │
                 └─────────────┬──────────────────────────────────┘
 ┌──────────── ask/router.py — PURA (now inyectado, sin red, sin reloj) ────────┐
 │ T0  atajos + dateparse.py + alias de entidad   ~60 %  0 tokens   <1 s        │
 │ T1  BM25 sobre FTS5 de la stdlib               ~20 %  0 tokens   <100 ms     │
 │ T2  ollama LOCAL ≤4B, JSON estricto            ~15 %  el texto NO sale       │
 │ T3  ollama-cloud gpt-oss:120b                  ~4 %   desambigua y narra     │
 │ T4  anthropic/claude-opus-5  (/pro, «por qué») ~1 %   tope en DÓLARES        │
 │ ── el CÓDIGO decide la escalada; el modelo NUNCA decide escalar ──           │
 │ salida única: (intent_id, params) validados contra el catálogo YA CARGADO    │
 └──────────────┬───────────────────────────────────────────────────────────────┘
                │ nunca una URL · nunca un path · nunca SQL · nunca una cabecera
 ┌──────────────▼──────────────┐        ┌─────────────────────────────────────┐
 │ ask/intents.py — FAIL-CLOSED│◄───────│ config/ask-queries.yml  (gitignored)│
 │ allowed_paths · solo GET    │        │ config/ask-queries.example.yml (git)│
 │ denied_prefixes/params      │        └─────────────────────────────────────┘
 └──────────────┬──────────────┘
 ┌──────────────▼── portal/ ── CONCURRENCIA 1, cola FIFO, bucket persistido ────┐
 │ session.py  var/portal-session.json 0600 · lock O_EXCL · UN login/invocación │
 │ client.py   solo GET · querystring desde lista blanca en ORDEN FIJO          │
 │ errors.py   PortalAuthError · PortalCredentialExpired · PortalRateLimited    │
 │             PortalUnavailable · PortalContractError  (ninguna lleva la URL)  │
 └──────────────┬───────────────────────────────────────────────────────────────┘
                │ HTTPS · ~16 rutas de las 71 · User-Agent identificable
 ┌──────────────▼───────────────────────────────────────────────────────────────┐
 │ visor-productividad (Next.js, PM2 fork, pool de 15, statement_timeout 800 s) │
 │ EL PORTAL impone sede/empresa/línea/sección DEL LADO SERVIDOR                │
 │ cuenta dedicada: role='user', portalProfile='personalizado', whitelists      │
 └──────────────┬───────────────────────────────────────────────────────────────┘
                │ payload → drop_fields (la PII sale del proceso antes de nada)
 ask/ground.py ─► render por PLANTILLA del catálogo (NO prosa libre del modelo)
   ├ cifra de la prosa ausente del JSON            → rechaza
   ├ nombre propio ausente del JSON               → rechaza
   ├ falta el cut_off declarado                   → rechaza
   ├ JSONPath declarado que no resuelve           → PortalContractError (NO cero)
   └ rango pedido MÁS ALLÁ del corte              → J3 «no hay dato»
                ▼
 redaction.redact() v2 ─► reports/daily.py ─► notify.send_chunked ─► Telegram
                │           ▲ REUTILIZADO TAL CUAL — no se toca una línea
                ├──► var/state.db   ask_turn · ask_intent_miss · budget_day
                │                   http_budget · endpoint_contract · incident
                │                   portal_session_meta (huella, NO la cookie)
                └──► var/audit-ledger.jsonl  append-only, hash encadenado, uid aparte

 ══ carril EXISTENTE, INTACTO (diff vacío del reporte diario = criterio de aceptación) ══
 systemd/SSH → collector.py → monitors/freshness.py → reports/daily.py → Telegram
```

---

## 2. La cuenta del agente en el portal

Esta cuenta **es** la frontera de seguridad. Todo lo demás es defensa en profundidad.

**Perfil exacto.** `username` dedicado y exclusivo (nunca compartido con un humano: R2 lo hace imposible, no solo desaconsejable). `role: 'user'` — cierra el 100 % de `/api/admin/*` con un solo campo. `portalProfile: 'personalizado'` — para que ningún preset reintroduzca permisos. `specialRoles: []`. `allowedSedes` no vacío (R16 lo exige). `allowedEmpresas`, `allowedLines` según alcance.

**`allowedDashboards` y `allowedSubdashboards` SIEMPRE explícitos, nunca `null`** — R4 es el error más fácil y más caro del sistema: dejarlos sin especificar entrega el portal entero.

**Subtableros concedidos (consulta pura):** `margenes`, `informe-variacion`, `mix-y-linea`, `participacion-comercial`, `ventas-x-item`, `analisis-de-inventario`.
**Subtableros NEGADOS aunque el negocio los pida** — son aquellos cuyo permiso de lectura arrastra verbos de escritura, verificado en V3: `rotacion`, `checklists`, `registro-de-horarios`, `inventario-x-item`. Con eso, la propia API responde 403 a `PATCH /api/rotacion/cero-estados`, `POST /api/checklists/runs`, `PUT /api/inventario-x-item/presets`. Si más adelante hace falta rotación, se entra por `/api/rotacion/gestion` y **es una decisión de riesgo consciente del operador**, no un descuido.
**Sección `operacion` NEGADA**, por R11 en particular: un solo GET a `/api/ingresar-horarios/people` devuelve la nómina completa ignorando `allowedSedes`.

**Cómo se crea.** Por el operador humano en `/admin/usuarios` (o `POST /api/admin/users`). Es una **escritura en producción**: riesgo `high`, aprobación explícita según `CLAUDE.md §17`. La contraseña debe pasar la política (≥8, mayúscula, minúscula, número, especial, no común) y **no se transcribe a ningún artefacto del agente**: entra por `EnvironmentFile` 0600, nunca por `Environment=`.

**Cómo se verifica.** `scripts/verify_portal_user.py --strict` en **cada arranque**: login + `GET /api/auth/me`, compara `role`, `allowedEmpresas`, `allowedSedes`, `allowedSubdashboards`, `specialRoles` contra lo declarado en el catálogo con **igualdad exacta**. Un permiso que **falte** es un fallo de configuración; un permiso que **sobre** es severidad `SECURITY` — el primer emisor real de esa categoría en todo el proyecto, hoy decorativa — y **el agente no arranca**. Más `--assert-denied /api/admin/users,/api/ingresar-horarios/people,/api/excel-dian/export,/api/proveedores/visitas`: los cuatro deben responder 401/403.

**Cómo se revoca (≤10 s, sin desplegar nada).** `/admin/usuarios` → desactivar la cuenta (`PATCH is_active:false`). Efectivo al siguiente request porque `getUserSession` valida `is_active` sin caché (R15). Reversible y auditado en `app_user_admin_audit`. **Detalle documentado en el runbook:** desactivar **no** revoca la fila de sesión; si se reactiva antes de 5 minutos, la sesión vieja revive. Palanca quirúrgica alternativa: `UPDATE app_user_sessions SET revoked_at = now() WHERE user_id = (SELECT id FROM app_users WHERE username = '<bot>')`. El runbook lleva el `username` exacto para que quien esté de guardia no improvise.

---

## 3. Cliente HTTP

Módulo `AGENTE src/os_system_agent/portal/`: `credentials.py`, `session.py`, `client.py`, `errors.py`.
Dependencia: **`httpx>=0.27`**, ya resuelto como 0.28.1 en `uv.lock:263-274` vía `mcp` — declararlo explícito **no añade una sola transitiva nueva**. Se elige sobre `urllib` por dos razones concretas: timeouts **separados** de connect/read («el portal tardó» y «el portal no está» son incidentes distintos), y porque `urlopen` dispara bandit `S310`, hoy exceptuado solo para `notify.py` (`pyproject.toml:62-68`) — meterlo por urllib obligaría a un segundo waiver.

**Credenciales.** `OS_PORTAL_<ID>_{BASE_URL,USER,PASSWORD}`, con `<ID>` **derivado** del campo `connection:` del catálogo (validado `^[a-z][a-z0-9_]{0,31}$`). No existe un campo `*_env` nombrable desde el YAML: un catálogo no puede apuntar a `TELEGRAM_BOT_TOKEN`. `PortalCredentials` es `frozen` con `__repr__` enmascarado, reviviendo `_mask()` de `config.py:14-15,28-35` (hoy código muerto) en vez de crear un tercer módulo de config.

**Login.** `POST /api/auth/login` con `{username,password}` JSON, sin CSRF. User-Agent fijo `os-system-agent/0.1 (+ask)` — queda grabado en `app_user_login_logs` y es la única trazabilidad gratis que existe (R14). Del cuerpo 200 se guardan `passwordDaysUntilExpiry` y el perfil de permisos.

| Estado | Significado real | Acción del cliente |
|---|---|---|
| 200 | sesión nueva | guarda cookie, `expires_at = now + 5 min − 30 s` |
| 403 + `cloudUrl` | portal local cerrado por env, evaluado **antes** de mirar el usuario | **no consume intento**; `PortalUnavailable`; «apunta a la instancia cloud» |
| 403 sin `cloudUrl` | cuenta desactivada **a propósito** | TERMINAL + Telegram `SECURITY` |
| 401 | credencial mala o caducada | **TERMINAL**. Nunca reintentar la misma credencial. `login_not_before = now + 15 min`, **persistido** |
| 429 | bloqueo de rate-limit, evaluado antes de validar (R5) | respeta `Retry-After`; escribe `login_not_before`; no reintenta en el proceso |
| 500 | sospecha fundada: `AUDIT_IP_HMAC_SECRET` escribiendo `hmac:` en columna `inet` (R6+V7) | `PortalUnavailable` **con ese diagnóstico literal en el mensaje** |

**Regla dura: máximo UN login por invocación del proceso.** `login_not_before` vive en SQLite, no en memoria: un contador de proceso no sobrevive al siguiente arranque del timer, que es exactamente cuando el bucle se reanuda. Con S1 sin cerrar, un bucle nuestro puede dejar sin entrar a toda la subred.

**Cookie.** `vp_session` (httpOnly, lax, `Expires` +5 min) y `vp_csrf` (no httpOnly, sin `Expires`). Persistencia en `var/portal-session.json`, modo **0600**, ruta **ABSOLUTA** desde `OS_PORTAL_SESSION` — nunca relativa al CWD, que es el bug conocido de `.alert-state.json` (R19). **Single-flight con lock `O_EXCL`** y TTL de 10 s: es obligatorio, no una optimización, porque R2 hace que dos procesos del agente se expulsen mutuamente en bucle indefinido. **La cookie NO entra en `state.db`, ni en logs, ni en el ledger**: allí va `sha256(token)[:16]`.

**Expiración y heartbeat — renovación perezosa dirigida por el uso, sin demonio.** Verificado en V1/V2: leer no mantiene viva la sesión. Algoritmo local a cada petición:

1. Si `expires_at − now > 60 s` → usa la cookie tal cual.
2. Si quedan ≤60 s, o han pasado >3 min desde el último renovado → `POST /api/auth/heartbeat` **antes** de la petición.
3. Si el latido da 401, o no hay sesión → **un** login.
4. **Antes y después de todo intent marcado `heavy: true`** en el catálogo — injerto del Diseño 1. Motivo: con `statement_timeout` de hasta 800 s (V5), una sola consulta puede consumir la ventana entera de 5 minutos.

Se **descarta** el latido en bucle 24/7: escribe actividad falsa en `app_user_activity_log` para siempre, contamina `/api/admin/uso-tableros` y es una pieza más que puede morir en silencio. El latido manda `path: "/agente-telegram"`, constante y filtrable: contamina métricas a cambio de aparecer identificado en `/admin/usuarios/accesos/en-linea`. La trazabilidad vale más.

**Los dos 401 que parecen el mismo.** `requireAuthSession` devuelve `null` tanto por sesión vencida como por contraseña caducada, y ambos salen como 401 idéntico (R3). Desambiguación obligatoria: tras un re-login exitoso, si la siguiente llamada sigue en 401 se consulta `GET /api/auth/me`; si `/me` responde 200 y los datos siguen 401 → **es la contraseña caducada** → `PortalCredentialExpired` + CRITICAL, cero reintentos. Corolario: **el health-check pega a un endpoint de datos real, nunca a `/me`**, que sigue diciendo que todo va bien con el agente completamente ciego.

**Reintentos.** `httpx.Timeout(connect=5, read=20, write=5, pool=5)`, presupuesto de turno de 30 s con `time.monotonic()`, `max_retries=0` explícito, nunca `verify=False` (S501 lo caza).

| Situación | Reintentos | Por qué |
|---|---|---|
| 401 en datos | 1 re-login + 1 repetición, nunca un tercero | 5 fallos queman el cupo por usuario |
| 401 en login | **0**, `login_not_before = +15 min` | terminal, alerta |
| 403 | **0** | es una decisión, no un fallo transitorio |
| 400 | 0, **salvo** `code:"DATE_NOT_FOUND"` (R10) | ese error trae el rango válido: un reintento acotado |
| 429 | 1, respetando `Retry-After` (tope 60 s) | el segundo se convierte en «el portal está saturado» |
| 5xx | 1, espera 2 s | el segundo → `PortalUnavailable` |
| timeout de lectura | **0** | reintentar una consulta pesada es cómo se agota el pool |
| `PortalContractError` | **0** | rehúsa y avisa; nunca degrada a cero |

**Rate limit — el control más importante del cliente: concurrencia 1, cola FIFO, no configurable desde el prompt.** El pool es de 15 con techo de 800 s (V5) y las rutas más útiles no tienen límite ninguno (R7): nada en el portal va a frenarnos antes de saturar la base para las personas. **La cuota del agente se mide en conexiones simultáneas, no en peticiones por minuto.** Encima, token bucket propio **persistido en `state.db`**: 10 req/min por endpoint, 30/min global — muy por debajo de los límites reales, porque el bucket del portal es por IP cruda y se comparte con cualquier humano que salga por la misma (S2).

**El querystring se construye desde una lista blanca en orden fijo, descartando cualquier otro parámetro.** No es cosmética: V6. Un parámetro basura variable convierte cada llamada en fallo de caché **y** desaloja las entradas calientes de los humanos. Con orden fijo, el agente **reusa** sus claves en vez de competir.

**Árbol de decisión de la respuesta** (`portal/errors.py::classify_response`, puro) — sin esto el agente confunde «la BD falló» con «no hubo venta»:

```
fallo            := status >= 500 or status == 429 or (status == 200 and body.get("error"))
sin_dato         := status == 200 and not body.get("error") and (body.get("message") or filas vacías)
fuera_de_alcance := status == 403
mi_culpa         := status == 400
```

Los dos casos que hay que memorizar: R8 (`/api/productivity` con 200 + `error`) y R9 (`updatedAt: null` es **INDETERMINADO**, jamás «el dato es de hoy»).

**`scripts/portal_doctor.py` — el fallo que se explica solo.** Siete peldaños, nombra **el primero roto y solo ese**: `tcp → tls → GET /api/health → POST /api/auth/login → GET /api/auth/me → un intent barato → contrato de JSONPath`. Con causa probable literal: `login:500` → «revisa `AUDIT_IP_HMAC_SECRET`: escribe `hmac:` en columna `inet`»; `login:403 sin cloudUrl` → «la cuenta está desactivada»; `me:200 + intent:401` → «contraseña caducada a los 30 días»; `contrato:falta $.totales.ventaNeta` → «el portal renombró una clave; corrige el intent X». Es la diferencia entre cinco minutos y tres días de arqueología dentro de un año.

---

## 4. Catálogo de preguntas

`config/ask-queries.example.yml` versionado con nombres genéricos; `config/ask-queries.yml` real y **gitignored** (`.gitignore` gana `config/ask-*.yml`, igual que ya cubre `config/alert-rules.yml`). Loader en `ask/intents.py`, copiando el patrón fail-closed de `AGENTE src/os_system_agent/catalog.py:119-159`.

**Dar de alta una pregunta = un bloque YAML + 3 atajos + un caso en evals. Cero Python.** Y si el bloque pide una ruta nueva, `allowed_paths` cambia — y ese cambio es visible y revisable en el diff del PR. Ese es el punto de control.

```yaml
version: 1
empresa: NombreEmpresa
connection: portal_prod          # deriva OS_PORTAL_PORTAL_PROD_{BASE_URL,USER,PASSWORD}
                                 # NO existe campo *_env nombrable desde el YAML

defaults:
  timeout_seconds: 20            # < cualquier timeout del servidor
  max_rows: 200
  cache_ttl_seconds: 300
  max_window_days: 31
  rate_per_minute: 10
  concurrencia: 1                # el loader RECHAZA cualquier valor > 1

# ── OBLIGATORIO y NO VACÍO, o CatalogError al cargar ─────────────────────────
allowed_paths:
  - /api/health                  # público, sin sesión
  - /api/portal/freshness
  - /api/ventas-x-item/v2
  - /api/analisis-de-inventario
  - /api/participacion-comercial
  - /api/margenes/data
  - /api/margenes/meta
  - /api/informe-variacion/meta
  - /api/ordenes-compra

# ── INJERTO del Diseño 1: prefijos vetados ESTRUCTURALMENTE.
#    Se valida el CRUCE: un intent aquí NO carga aunque alguien lo meta
#    en allowed_paths por error.
denied_prefixes:
  - /api/admin                   # usuarios, auditoría, IPs, presencia
  - /api/auth                    # login y heartbeat los emite session.py, cableados
  - /api/exports                 # la auditoría de descargas es auto-reportada
  - /api/excel-dian              # XLSX contable de meses enteros
  - /api/debug
  - /api/ingresar-horarios       # IGNORA allowed_sedes → nómina completa
  - /api/horarios-comparar
  - /api/jornada-extendida/alex-report
  - /api/exp/efectividad-cajero
  - /api/proveedores/visitas     # visitanteCedula + qrLinks
  - /api/proveedores/oipv
  - /api/proveedores/ingreso     # endpoint PÚBLICO que RECIBE cédula
  - /api/checklists
  - /api/rotacion                # v1: solo /api/rotacion/gestion, si se aprueba

# ── INJERTO del Diseño 1: los tres primeros son DoS accidental; el resto, PII.
denied_params:
  [force, refresh, explain, matviewSql,
   includePeople, overtimeOnly, peopleDateStart, peopleDateEnd]

salud:
  freshness_path: /api/portal/freshness
  indeterminado_si: "$.updatedAt == null"     # null ≠ «es de hoy»

entidades:
  sedes:  [{id: "001", nombre: "Sede A", alias: ["sede a", "la principal"]}]
  lineas: [{id: "50",  nombre: "Licores", alias: ["alcohol", "licor"]}]
```

### Ejemplo 1 — el intent que cierra el lazo en M7 (cero modelo, cero SQLite)

```yaml
intents:
  - id: frescura_portal
    dominio: meta
    descripcion: "De cuándo es el dato del portal: sello global del snapshot"
    riesgo: READ_ONLY
    heavy: false
    endpoint:
      path: /api/portal/freshness
      method: GET                 # cualquier otro valor → CatalogError
      query: []
    params: []
    response:
      # OBLIGATORIO. Un intent sin cut_off no carga.
      # Este endpoint se traga sus errores y responde 200 con updatedAt:null.
      cut_off: {inline: "$.updatedAt", kind: iso_datetime,
                nullable_means: indeterminado}
      rows:    {path: "$.sources"}
      figures:
        fuentes: {path: "$.sources", kind: count, unit: fuentes}
    render:
      template: >-
        Sello del portal: {cut_off}. {fuentes} fuentes reportando.
        (Este es el refresco del SNAPSHOT; el último día de dato lo declara
        el meta de cada tablero.)
    atajos:                       # mínimo 3, o CatalogError
      - "de cuando es el dato"
      - "frescura"
      - "esta actualizado el portal"
```

### Ejemplo 2 — venta por sede, con el corte tomado de una **sonda** declarada

```yaml
  # intent INTERNO: sirve de sonda de corte, no se ofrece en /preguntas
  - id: ventas_meta
    interno: true
    dominio: ventas
    descripcion: "Rango de fechas cargado en venta por ítem"
    riesgo: READ_ONLY
    endpoint:
      path: /api/ventas-x-item/v2
      method: GET
      query: [{name: mode, const: meta}]
    params: []
    response:
      cut_off: {inline: "$.maxDate", kind: date_iso}
      figures:
        desde: {path: "$.minDate", kind: date_iso}
        hasta: {path: "$.maxDate", kind: date_iso}
    render: {template: "Venta por ítem cargada del {desde} al {hasta}."}
    atajos: ["hasta cuando hay venta", "que dias hay cargados de venta",
             "frescura de venta"]

  - id: venta_dia_sede
    dominio: ventas
    descripcion: "Venta neta y unidades de un rango de días, por sede"
    riesgo: READ_ONLY
    heavy: false
    endpoint:
      path: /api/ventas-x-item/v2
      method: GET
      query:                      # ORDEN FIJO → reusa la clave de caché humana
        - {name: mode,  const: summary}
        - {name: start, from: params.desde, format: date_iso}
        - {name: end,   from: params.hasta, format: date_iso}
        - {name: idCo,  from: params.sede,  optional: true}
    params:
      - {name: desde, type: date, required: true}
      - {name: hasta, type: date, required: true, ge: desde, max_span_days: 31}
      - {name: sede,  type: sede, required: false}   # contra entidades.sedes
    response:
      # el corte NO viaja en esta respuesta: se toma de la sonda declarada
      cut_off: {probe: ventas_meta, path: "$.maxDate", kind: date_iso}
      rows:    {path: "$.rows", max: 200}
      figures:
        venta_neta: {path: "$.rows[*].venta_sin_impuesto_acum", kind: sum, unit: cop}
        unidades:   {path: "$.rows[*].und_acum",                kind: sum, unit: und}
      sin_dato_si: "$.rows|length == 0"
      fallo_si:    "$.error != null"        # el 200-con-error de R8
    render:
      template: >-
        Venta {sede_nombre} del {desde} al {hasta}: {venta_neta} COP,
        {unidades} unidades. Corte del dato: {cut_off}.
    atajos:
      - "venta de ayer"
      - "cuanto vendimos ayer"
      - "venta de la semana pasada en {sede}"
```

### Ejemplo 3 — el intent de negocio más limpio de la API, con `drop_fields`

```yaml
  - id: oc_vencidas
    dominio: compras
    descripcion: "Órdenes de compra vencidas contra el SLA de 7 días"
    riesgo: READ_ONLY
    heavy: false
    endpoint:
      path: /api/ordenes-compra
      method: GET
      query:
        - {name: vista, const: vencidas}
        - {name: sedes, from: params.sede,  optional: true}
        - {name: desde, from: params.desde, format: yyyymmdd, optional: true}
        - {name: hasta, from: params.hasta, format: yyyymmdd, optional: true}
    params:
      - {name: sede,  type: sede, required: false}
      - {name: desde, type: date, required: false}
      - {name: hasta, type: date, required: false, ge: desde, max_span_days: 31}
    response:
      cut_off: {inline: "$.meta.loadedAt", kind: iso_datetime}
      rows:    {path: "$.rows", max: 200}
      figures:
        vencidas:     {path: "$.kpis.vencidas",            kind: int,   unit: ordenes}
        abiertas:     {path: "$.kpis.abiertas",            kind: int,   unit: ordenes}
        valor_bruto:  {path: "$.kpis.totBrutoAbiertas",    kind: money, unit: cop}
        pct_recibida: {path: "$.kpis.pctRecibidaAbiertas", kind: ratio, unit: pct}
      # INJERTO del Diseño 1: se DESCARTAN antes de que nada más vea el payload.
      # No basta con no pedirlos: /api/hourly-analysis manda overtimeEmployees
      # SIN el flag (R13).
      drop_fields:
        [compradorNom, overtimeEmployees, personContributions,
         visitanteNombre, visitanteCedula, qrLinks]
    render:
      template: >-
        {vencidas} OC vencidas contra el SLA de 7 días
        ({abiertas} abiertas, {valor_bruto} COP pendientes, {pct_recibida} recibido).
        Corte: {cut_off}.
    atajos:
      - "cuantas oc estan vencidas"
      - "ordenes de compra vencidas"
      - "que oc se pasaron del sla"
```

### Validaciones fail-closed al cargar (`CatalogError`, con test negativo cada una)

| # | Regla | Qué atrapa |
|---|---|---|
| 1 | `allowed_paths` ausente o vacío | catálogo sin frontera |
| 2 | `path` fuera de `allowed_paths` | ruta añadida sin revisión |
| 3 | `path` bajo un `denied_prefixes` (**cruce**) | PII / admin / exports por descuido |
| 4 | `method != GET` | el único control de solo-lectura en nuestro código (V3) |
| 5 | intent sin `response.cut_off` | número sin fecha = número engañoso |
| 6 | `figures` vacío | intent que no puede fundamentar nada |
| 7 | `< 3 atajos` | intent que T0 nunca resolverá y siempre gastará modelo |
| 8 | query con nombre en `denied_params` | DoS accidental y PII por bandera |
| 9 | `id` duplicado | mismo patrón que `catalog.py:152` |
| 10 | `max_span_days > defaults.max_window_days` | ventana que el portal recorta en silencio (R17) |
| 11 | `format` distinto de `date_iso`/`yyyymmdd` sin declarar | R17: `margenes/meta` usa YYYYMMDD y el resto ISO |
| 12 | `concurrencia > 1` | el pool de 15 es un recurso compartido con humanos |

---

## 5. Ruteo de modelos

**Premisa verificada:** OpenClaw **no tiene router por complejidad**. `model.fallbacks` solo dispara ante rate-limit/quota, nunca ante «el modelo respondió mal», que es justo nuestro criterio. Por eso el router es nuestro, es una **función pura**, y **la escalada la decide código determinista: el modelo nunca decide escalar.** Darle esa decisión es exactamente cómo se llegó al consumo desbocado de la 002 (`AGENTE specs/002-estado-etl-pull/tasks.md:93-112`).

| Peldaño | Tarea | Modelo | Proveedor | Por qué |
|---|---|---|---|---|
| — | `/frescura`, `/preguntas`, `/porque`, `/ayuda`, `/estado` | **ninguno** | plugin `registerCommand()` | Cero tokens, ~2 s. **Lo que no invoca un LLM no puede ser inyectado ni desbocarse.** |
| **T0** | Atajo del catálogo + fechas en español + alias de entidad | **ninguno** | `ask/dateparse.py` | ~60 % del tráfico real: el operador repite diez preguntas. Coste 0, <1 s, determinista |
| **T1** | Ruteo pregunta → intent cuando no casa un atajo | **ninguno** — BM25 sobre **FTS5 de la stdlib** | — | Con decenas de intents acierta igual que embeddings, sin `ollama pull` de 274 MB, sin índice que reindexar, sin dependencia vectorial. Acepta si `top1 ≥ umbral` **y** `top1 − top2 ≥ margen` |
| **T2** | Rellenar ≤3 huecos y elegir intent cuando T1 no separa | local ≤4B (`qwen3:4b` — **SUPUESTO S5**, confirmar con `ollama list`) | **ollama local** `127.0.0.1:11434` | Cada pregunta lleva nombres de sede y de empresa: **el texto no sale del box**. Salida forzada a JSON y validada contra el catálogo: un modelo mediocre degrada a *refusal*, jamás a consulta equivocada |
| **T3** | Desambiguar top-5 y narrar 2-4 frases sobre el resultado | `gpt-oss:120b` | **ollama-cloud** `https://ollama.com` (API **nativa**, nunca la `/v1`, que rompe el tool calling) | Ya configurado y probado. Techo `timeout=25 s`, un reintento. **Aviso del expediente: el 120b hizo timeout en el incidente de julio**; si el p95 medido en M0 supera 15 s, T3 baja al local y la narración degrada a plantilla |
| **T4** | `/pro`, verbos de causa/comparación («por qué», «compara», «qué cambió»), o T3 falló grounding | **`claude-opus-5`** ($5/$25 MTok, 1M ctx) | **anthropic** | La 002 concluyó que un pull fiable necesita *un plugin determinista **o** un modelo de pago*; hacemos las dos cosas. Modelo por defecto del proyecto: no se baja de tier por coste sin decisión del operador |
| **T-batch** | Propuesta nocturna de intents desde `ask_intent_miss` | **`claude-haiku-4-5`** ($1/$5, 200K) | **anthropic** | Offline, sin latencia que importe. Abre un **PR para revisión humana**; jamás toca el catálogo |
| utility | Títulos y resúmenes internos | `gpt-oss:120b` | ollama-cloud | Nunca gastar un modelo de pago en tareas triviales |

**Contrato con el modelo, idéntico en T2/T3/T4.** Recibe `[{id, descripcion, nombres_de_parametro}]`. **Nunca** `path`, **nunca** `allowed_paths`, **nunca** la URL base ni el host. Devuelve `{"intent_id": "...", "params": {...}}` y nada más. Validación **contra el catálogo YA CARGADO**: `intent_id` inexistente → *refusal* + fila en `ask_intent_miss`; parámetro no declarado en **ese** intent → *refusal*; tipo que no case → *refusal*; el modelo devuelve una URL, un `path`, un querystring o SQL → **refusal explícito, no «se ignora el campo»**.

**Tope en dólares, estructural.** Turno T4 típico con caché: ~4-6k tokens de entrada (≈90 % cacheados) + ~500 de salida ≈ **$0,02–0,03**. Con `OS_ASK_T4_DAILY_MAX_USD = 0.80` → ≈ **$24/mes**. El contador vive en `budget_day` y **se evalúa antes de la llamada**. Agotado, **degrada al peldaño gratis y LO DICE en la respuesta** («respondido con el modelo económico: presupuesto diario agotado»), jamás en silencio. Un tope en número de turnos no acota el gasto; uno en dólares sí.

**Corrección documental que hay que respetar:** `/p` **sí** puede llegar al modelo (entra al router). Solo `/estado`, `/frescura`, `/preguntas`, `/porque` y `/ayuda` son verdaderamente model-free. Prometer lo contrario hace que la prueba de aceptación «apunta el modelo a uno inexistente y mira que responde» se lea como prueba de algo que no es.

**Detalles de API que ya costaron una corrección:** `thinking:{type:"adaptive"}` (**`budget_tokens` está removido y devuelve 400**; el thinking va encendido por defecto — para abaratar se baja `output_config.effort`, no se apaga). `output_config:{effort}` va **dentro** de `output_config`. Prefill de assistant removido: 400; el formato se fuerza con `output_config.format` + `strict:true` **en la definición de la tool**. `cache_control:{type:"ephemeral"}` sobre el prefijo estable (system + catálogo), verificado con `usage.cache_read_input_tokens` — **si sale 0 en turnos consecutivos hay un invalidador silencioso**. Comprobar siempre `stop_reason` **antes** de leer `content`. **`claude-sonnet-4-5` no existe** en el catálogo vigente; el escalón medio, si algún día hace falta, es `claude-sonnet-5`.

**Config.** Claves como SecretRef (`--ref-source env --ref-id ...`), verificadas con `openclaw secrets audit --check`. En systemd, `EnvironmentFile=` 0600 — **nunca** `Environment=TOKEN=...`, visible en `systemctl show`. Y `memory.search.enabled = false`: su proveedor de embeddings por defecto es OpenAI y cada pregunta lleva nombres de sede.

---

## 6. Memoria persistente

`var/state.db` — **SQLite de la stdlib, cero dependencias nuevas.** `PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; busy_timeout=5000`. Permisos **0600**, ruta **ABSOLUTA** desde `OS_STATE_DB`. Migraciones versionadas e idempotentes por `scripts/state_init.py`, con `--check` que no escribe y `--seed-from .alert-state.json` (antitormenta: la primera corrida no manda una sola alerta).

Punto de partida real: hoy hay un dict sin timestamps escrito de forma no atómica en ruta relativa al CWD, cuyo lector se traga `OSError`/`ValueError` y devuelve `{}` → tormenta silenciosa de alertas esperando a ocurrir (R19).

```sql
CREATE TABLE schema_migration (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);

-- (a) SESIÓN DEL PORTAL — metadatos SIN el secreto ───────────────────────────
CREATE TABLE portal_session_meta (
  connection                 TEXT PRIMARY KEY,
  token_fingerprint          TEXT NOT NULL,      -- sha256(cookie)[:16] ← NO la cookie
  expires_at                 TEXT NOT NULL,
  logged_in_at               TEXT NOT NULL,
  last_heartbeat_at          TEXT,
  password_days_until_expiry INTEGER,            -- el aviso de los 30 días
  login_not_before           TEXT,               -- backoff PERSISTIDO: mata el bucle
  consecutive_auth_failures  INTEGER NOT NULL DEFAULT 0,
  CHECK (length(token_fingerprint) = 16));

-- (b) TURNOS — la observabilidad de lo que el agente RESPONDE ────────────────
--     El CHECK final convierte RF-04 de convención en restricción de la base:
--     es IMPOSIBLE guardar un turno 'ok' sin fecha de corte.
CREATE TABLE ask_turn (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  trace_id     TEXT NOT NULL UNIQUE,
  ts           TEXT NOT NULL,
  canal        TEXT NOT NULL CHECK (canal IN ('telegram','cli','mcp','timer')),
  operador     TEXT NOT NULL,                    -- chat_id normalizado, no el nombre
  empresa      TEXT NOT NULL,
  pregunta     TEXT NOT NULL CHECK (length(pregunta) <= 512),  -- YA redactada
  intent_id    TEXT,
  endpoint_id  TEXT,                             -- id del catálogo, NUNCA una URL
  params_json  TEXT CHECK (length(params_json) <= 512),
  tier         TEXT NOT NULL CHECK (tier IN ('t0','t1','t2','t3','t4')),
  modelo       TEXT NOT NULL,                    -- 'ninguno' cuando es determinista
  degradado    INTEGER NOT NULL DEFAULT 0,
  degradado_motivo TEXT CHECK (length(degradado_motivo) <= 128),
  http_status  INTEGER, rows_seen INTEGER, cut_off TEXT, elapsed_ms INTEGER NOT NULL,
  tokens_in    INTEGER, tokens_out INTEGER, costo_usd REAL,
  verdict      TEXT NOT NULL CHECK (verdict IN (
                 'ok','cached','sin_intent','fuera_de_alcance','sin_dato',
                 'error_portal','error_modelo','error_contrato',
                 'rechazado_presupuesto','rechazado_grounding',
                 'credencial_vencida','rate_limited')),
  CHECK (verdict <> 'ok' OR cut_off IS NOT NULL),
  CHECK (degradado = 0 OR degradado_motivo IS NOT NULL));
CREATE INDEX ix_ask_turn_op_ts   ON ask_turn(operador, ts DESC);
CREATE INDEX ix_ask_turn_verdict ON ask_turn(verdict, ts DESC);

CREATE TABLE ask_feedback (
  trace_id   TEXT PRIMARY KEY REFERENCES ask_turn(trace_id) ON DELETE CASCADE,
  ts         TEXT NOT NULL,
  valor      TEXT NOT NULL CHECK (valor IN ('util','inutil','incorrecto')),
  comentario TEXT CHECK (length(comentario) <= 512));

-- (c) EL VOLANTE — hace crecer el catálogo por evidencia, no por intuición ───
CREATE TABLE ask_intent_miss (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  ts               TEXT NOT NULL,
  operador         TEXT NOT NULL,
  pregunta         TEXT NOT NULL CHECK (length(pregunta) <= 512),  -- redactada
  top1_intent      TEXT, top1_score REAL, top2_score REAL,
  propuesta_intent TEXT, pr_url TEXT,
  estado           TEXT NOT NULL DEFAULT 'abierta'
                   CHECK (estado IN ('abierta','propuesta','implementada','descartada')));
CREATE VIRTUAL TABLE ask_intent_miss_fts USING fts5(
  pregunta, content='ask_intent_miss', content_rowid='id',
  tokenize='unicode61 remove_diacritics 2');

-- (d) CONTRATO CON EL PORTAL — detecta que el portal cambió ANTES de que
--     alguien pregunte y reciba un cero.
CREATE TABLE endpoint_contract (
  intent_id    TEXT NOT NULL,
  json_path    TEXT NOT NULL,
  rol          TEXT NOT NULL CHECK (rol IN ('cut_off','figure','rows','sin_dato')),
  ok_desde     TEXT, ok_hasta TEXT,
  roto_desde   TEXT,                             -- NULL = sano
  ultimo_error TEXT CHECK (length(ultimo_error) <= 256),
  PRIMARY KEY (intent_id, json_path));
CREATE INDEX ix_contract_roto ON endpoint_contract(roto_desde) WHERE roto_desde IS NOT NULL;

-- (e) PRESUPUESTO — dólares para el modelo, PETICIONES para el portal ────────
CREATE TABLE budget_day (
  dia TEXT NOT NULL, operador TEXT NOT NULL, tier TEXT NOT NULL,
  turnos INTEGER NOT NULL DEFAULT 0, costo_usd REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (dia, operador, tier));

CREATE TABLE http_budget (                       -- token bucket PERSISTIDO
  ventana_min TEXT NOT NULL,                     -- 'YYYY-MM-DDTHH:MM'
  endpoint_id TEXT NOT NULL,
  requests INTEGER NOT NULL DEFAULT 0, ms_total INTEGER NOT NULL DEFAULT 0,
  http_429 INTEGER NOT NULL DEFAULT 0, http_5xx INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (ventana_min, endpoint_id));

CREATE TABLE query_cache (
  cache_key   TEXT PRIMARY KEY, endpoint_id TEXT NOT NULL,
  computed_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  payload     TEXT NOT NULL CHECK (length(payload) < 8192));
CREATE INDEX ix_query_cache_exp ON query_cache(expires_at);

-- (f) TELEMETRÍA E INCIDENTES — reemplaza .alert-state.json ──────────────────
CREATE TABLE run_observation (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  observed_at TEXT NOT NULL, empresa TEXT NOT NULL, job_id TEXT NOT NULL,
  signal   TEXT NOT NULL CHECK (signal   IN ('systemd','http_portal','freshness',
                                             'credencial','contrato')),
  severity TEXT NOT NULL CHECK (severity IN ('INFO','WARNING','CRITICAL','SECURITY')),
  latest_at TEXT, delay_minutes REAL, rows_seen INTEGER,
  evidence TEXT NOT NULL CHECK (length(evidence) <= 512),   -- de campos PARSEADOS,
  trace_id TEXT NOT NULL);                                  -- jamás str(exc)

CREATE TABLE incident (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  empresa TEXT NOT NULL, job_id TEXT NOT NULL, signal TEXT NOT NULL,
  severity TEXT NOT NULL, peak_severity TEXT NOT NULL,
  opened_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, closed_at TEXT, alerted_at TEXT,
  consecutive INTEGER NOT NULL DEFAULT 1,
  first_evidence TEXT NOT NULL, last_evidence TEXT NOT NULL, trace_id TEXT NOT NULL);
CREATE UNIQUE INDEX ux_incident_open ON incident(empresa, job_id, signal)
  WHERE closed_at IS NULL;

-- La ÚNICA superficie de escritura del agente sobre memoria de negocio,
-- y solo anclada a un incidente que una MÁQUINA observó.
CREATE TABLE incident_note (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  incident_id INTEGER NOT NULL REFERENCES incident(id) ON DELETE CASCADE,
  trace_id  TEXT REFERENCES ask_turn(trace_id),
  creado_at TEXT NOT NULL,
  autor     TEXT NOT NULL CHECK (autor     IN ('operador','agente')),
  kind      TEXT NOT NULL CHECK (kind      IN ('causa','accion','resolucion','observacion')),
  confianza TEXT NOT NULL CHECK (confianza IN ('confirmado','inferido')),
  vigente   INTEGER NOT NULL DEFAULT 1,
  cuerpo    TEXT NOT NULL CHECK (length(cuerpo) <= 2000));
CREATE TRIGGER nota_agente_no_confirma BEFORE INSERT ON incident_note
  WHEN NEW.autor = 'agente' AND NEW.confianza = 'confirmado'
  BEGIN SELECT RAISE(ABORT, 'el agente no puede escribir hechos confirmados'); END;

CREATE TABLE operator_pref (
  key TEXT PRIMARY KEY, value TEXT NOT NULL,
  set_by TEXT NOT NULL CHECK (set_by = 'operador'), set_at TEXT NOT NULL,
  source TEXT NOT NULL);
```

**Ledger append-only, en fichero aparte.** `var/audit-ledger.jsonl` con `chattr +a` y propietario distinto del usuario del agente; su proyección de solo consulta en `var/audit.db`:

```sql
CREATE TABLE audit_entry (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
  empresa TEXT NOT NULL, trace_id TEXT NOT NULL, task_id TEXT,
  actor   TEXT NOT NULL CHECK (actor   IN ('operador','agente','timer','plugin')),
  phase   TEXT NOT NULL CHECK (phase   IN ('answer','refuse','approval','verify','login')),
  action  TEXT NOT NULL,
  endpoint_id TEXT,                     -- id del catálogo, NUNCA la URL armada
  params_json TEXT CHECK (length(params_json) <= 512),
  risk    TEXT NOT NULL CHECK (risk    IN ('READ_ONLY','SESSION_KEEPALIVE','LOW_RISK',
                                           'MEDIUM_RISK','HIGH_RISK','FORBIDDEN')),
  outcome TEXT NOT NULL CHECK (outcome IN ('ok','failed','refused')),
  prev_hash TEXT NOT NULL, entry_hash TEXT NOT NULL);
CREATE UNIQUE INDEX ux_audit_hash ON audit_entry(entry_hash);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_entry
  BEGIN SELECT RAISE(ABORT, 'audit ledger is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_entry
  BEGIN SELECT RAISE(ABORT, 'audit ledger is append-only'); END;
```

`entry_hash = sha256(prev_hash || canonical_json(entry))`. **El agente no tiene herramienta de escritura al ledger.** Nótese la clase de riesgo `SESSION_KEEPALIVE`: el latido y el login **son escrituras en el portal**, aunque solo sobre la propia fila de sesión. Se declaran como categoría propia en vez de esconderlas dentro de `READ_ONLY`, para que nadie las cuente dos veces en la aprobación de la Fase 1.

**Herramientas:** `scripts/state_init.py` (`--check` / `--seed-from` / `--apply`), `scripts/audit_verify.py --strict` (recorre la cadena de hash), `scripts/budget_report.py`, `scripts/ask_report.py`, `scripts/secret_sweep.py`.

**Seis guardas anti-secreto, todas estructurales:**

1. **La cookie NO existe en `state.db`.** Vive solo en `var/portal-session.json` 0600; en la base va la huella. Es lo que permite que un volcado de auditoría no sea el propio incidente.
2. **No hay ninguna columna que pueda contener una URL armada, una cabecera, una cookie ni un cuerpo crudo.** Se guarda `endpoint_id` + `params_json`. La evidencia se construye de campos parseados, jamás de `str(exc)`.
3. **`redact()` corre ANTES del INSERT** en `ask_turn.pregunta` e `ask_intent_miss.pregunta`, con la regla **«si el texto CAMBIÓ, se RECHAZA la escritura»** — no se guarda `***REDACTED***`, porque un enmascarado guardado para siempre es evidencia de que un secreto pasó cerca y no corrige nada.
4. **`CHECK (length(...) <= N)`** en todo texto libre: impide volcar un log entero.
5. **`redact()` también AL LEER**, para atrapar lo guardado antes de una mejora del patrón.
6. **Retención de 90 días** con purga de `ask_turn` y `query_cache`, más `PortalCredentials.__repr__` enmascarado.

**`redaction.py` v2 es prerrequisito duro, antes de la primera credencial.** Hoy `_KV_SECRET` exige `[=:]` inmediato y por eso **no enmascara `{"password": "..."}` en JSON** (R19). Faltan además, específicos de HTTP: `Set-Cookie: vp_session=…`, cabecera `Cookie:`, `x-csrf-token`, PEM, JWT, IP privada, correo corporativo. `evals/cases/redaction_cases.yaml` pasa de 4 a ≥20 casos.

---

## 7. Controles de seguridad

| # | Control | Ataque o fallo que previene | Dónde vive |
|---|---|---|---|
| 1 | **Ficha de la cuenta sin los subtableros con verbos de escritura** (`rotacion`, `checklists`, `registro-de-horarios`, `inventario-x-item`) y sin la sección `operacion` | Escritura de negocio con la cookie del agente. V3 demuestra que el mismo `rotacionAuthGate` protege el GET (`:259`) y el PATCH (`:419`); el CSRF no es barrera porque el bot tiene ambas cookies | procedimiento humano + `verify_portal_user.py` |
| 2 | **Allowlist de método: solo `GET`**, validada al cargar el catálogo y de nuevo en el cliente | Lo mismo, en profundidad: si mañana alguien concede un subtablero «para probar», el cliente sigue sin poder emitir otro verbo | `ask/intents.py` + `portal/client.py` |
| 3 | **`allowed_paths` obligatorio y no vacío + cruce contra `denied_prefixes`** | Llegar a `/api/admin/*`, `/api/excel-dian/export`, `/api/exports/*`. R18: `src/proxy.ts:109` deja pasar todo `/api/*`, **no hay red de seguridad centralizada** | catálogo, fail-closed |
| 4 | **`drop_fields` al parsear** | R13: `overtimeEmployees` (cédula, nombre, departamento) viaja en la respuesta base **sin `includePeople`**. No basta con no pedirlo | parser del catálogo |
| 5 | **`denied_params`: `force`, `refresh`, `explain`, `matviewSql`** | DoS accidental: invalidan caché y fuerzan el camino lento. Son la trampa perfecta para un agente que interprete «tráeme lo más reciente» | catálogo, fail-closed |
| 6 | **Concurrencia 1, cola FIFO, no configurable desde el prompt** + timeout de cliente 20 s | Agotar el pool de 15 con `statement_timeout` de 800 s (V5) → todo humano con timeout hasta 13 min → `/api/health` 503 → el watchdog reinicia PM2 → **se borran los contadores de fuerza bruta del login** | `portal/client.py`; `concurrencia > 1` → `CatalogError` |
| 7 | **Querystring desde lista blanca en ORDEN FIJO** | Thrash de caché: V6, 300 entradas indexadas por `url.search` crudo. 300 peticiones con `&x=N` desalojan las entradas calientes de los humanos **y** pagan 300 consultas caras | `portal/client.py` |
| 8 | **Token bucket persistido en `state.db`** (10/min por endpoint, 30/min global) | Gastar el cupo **por IP** de las personas. R7: las rutas más útiles no tienen rate limit ninguno. Persistido, no en memoria: un contador de proceso no sobrevive al arranque del timer | `state.db` |
| 9 | **UN login por invocación + `login_not_before` persistido** | R5: bloquear el login de toda la subred /24 durante 15 min. El bloqueo se evalúa **antes** de validar, así que ni la contraseña correcta pasa | `portal/session.py` + `state.db` |
| 10 | **401/403 terminales; 429 un solo reintento con `Retry-After`** | El bucle que quema el cupo y deja fuera a personas reales | `portal/errors.py` |
| 11 | **Sesión compartida entre procesos con lock `O_EXCL`** | R2: `createSessionReplacingOthers` revoca todas las sesiones previas → los tres timers se expulsan mutuamente en bucle. Y cada login es una escritura en producción que hace irreconocible una entrada anómala real | `portal/session.py` |
| 12 | **`verify_portal_user.py --strict` en cada arranque, IGUALDAD EXACTA** | Deriva de permisos: alguien amplía el alcance «para probar» y nadie lo revierte. Un permiso **de más** es `SECURITY` y el agente **no arranca** | contra `GET /api/auth/me` |
| 13 | **El modelo devuelve `(intent_id, params)` contra un enum cerrado del catálogo cargado** | Inyección de prompt convertida en petición nueva. Lo peor que puede hacer un modelo comprometido es decir «no entendí» | `ask/router.py` + test de introspección en CI |
| 14 | **Datos del portal en bloque marcado como no-instrucción + CERO herramientas en el turno de redacción** | R12: `POST /api/proveedores/ingreso` es **público, sin sesión**, y su texto libre sale como `visitanteNombre`. Sanear no corta (siempre se escapa un payload); el corte es que no haya nada que ejecutar | `ask/spotlight.py` |
| 15 | **Render por PLANTILLA del catálogo, no prosa libre del modelo** | Que la corrección de la respuesta dependa de que el modelo esté bien ese día. Además es lo que permite responder con todos los modelos caídos | `ask/render.py` |
| 16 | **Grounding: cifras es-CO + entidades literales + `cut_off` obligatorio + rango pedido vs. corte** | **El riesgo dominante**: un número real, pequeño y falso. Sin el cruce rango-vs-corte el control queda decorativo justo en el caso que más importa (preguntar por un día que el ETL no cargó) | `ask/ground.py`, puro |
| 17 | **`PortalContractError`: JSONPath ausente → rehúsa, NUNCA cero** + tabla `endpoint_contract` + timer semanal | El modo de falla más venenoso de esta arquitectura: el portal renombra una clave y el agente reporta 0 en silencio durante meses | `ask/ground.py` + marker `portalproof` |
| 18 | **`CHECK (verdict <> 'ok' OR cut_off IS NOT NULL)`** | Que RF-04 se degrade a convención. Con el CHECK, una respuesta sin fecha de corte es un error de escritura, no un descuido invisible | esquema de `ask_turn` |
| 19 | **`fallo := status>=500 or 429 or (200 and body.error)`** | R8: confundir «la base falló» con «no hubo venta» | `portal/errors.py` |
| 20 | **`/api/health` solo como señal INFO, no alertable por sí sola** | CRITICAL falsos fabricables: es público, sin rate limit, y toma un cliente del pool. Solo escala si COINCIDE con un fallo del camino autenticado | `monitors/` |
| 21 | **Solo DM 1:1, allowlist de `chat_id` NUMÉRICO, sin grupos en la v1** | El agente tiene **una** sesión, luego **un** conjunto de permisos: en un grupo eso se aplica a los 30 miembros por igual. El gating por mención no resuelve nada — el problema no es quién pregunta, es **quién lee**. `@username` se cambia; el `chat_id` no | `openclaw.json` + `ask_cli.py` |
| 22 | **El texto por STDIN; `ask_cli` SIN `--target` ni `--direct`** | `/p --direct --target <chat_ajeno> venta de ayer` exfiltra con el token del bot. Los CLIs hermanos ya exponen esas banderas (`AGENTE scripts/send_daily_report.py:69-88`) | test estructural: `assert "--target" not in parser.format_help()` |
| 23 | **La cookie fuera de `state.db`; `redact()` al escribir Y al leer** | Que el propio volcado de auditoría sea el incidente | `memory/` |
| 24 | **Ledger append-only con hash encadenado, uid distinto, sin herramienta de escritura para el agente** | Borrado de rastro | `var/audit-ledger.jsonl` |
| 25 | **Presupuesto en DÓLARES + degradación declarada en la respuesta** | Consumo desbocado — ocurrió de verdad en julio | `ask/router.py` |
| 26 | **Alarma escalonada de caducidad (WARNING ≤7 d, CRITICAL ≤2 d) con el procedimiento exacto en el mensaje; health-check contra un endpoint de DATOS** | R3: el apagón programado del día 30, mientras `/api/auth/me` sigue diciendo 200 | `scripts/portal_credential_status.py` |
| 27 | **Kill-switch documentado con el `username` exacto** | Que quien esté de guardia improvise. Con el matiz de R15 escrito: desactivar no revoca la fila de sesión | `docs/security-runbook.md` |

**Controles que son TEATRO frente a este agente — declararlos para que nadie los cuente dos veces en la aprobación:** CSP, HSTS, X-Frame-Options y COOP son controles de **navegador** y el cliente los ignora por completo. SameSite + CSRF protegen contra un sitio de terceros que abusa de la sesión de un navegador; **el agente no es un navegador** y sostiene la cookie directamente. El cierre por inactividad de 5 minutos deja de ser contención en cuanto el agente late. La auditoría de exportaciones la reporta el propio cliente voluntariamente (R14). Y la auditoría del portal en general registra la **sesión**, no las **consultas**: mil GET a `/api/margenes/data` no dejan **una sola fila**. La trazabilidad real hay que construirla del lado del agente.

---

## 8. Plan por hitos

| ID | Objetivo | Entregable | Verificación | Riesgo | Aprob. |
|---|---|---|---|---|---|
| **M0** | Cerrar los ocho supuestos (S1..S8) antes de escribir código. Sin esto, la mitad del diseño es adivinanza. | `specs/006-preguntas-portal/_supuestos.md` con las respuestas y su evidencia. **Ningún valor de secreto se transcribe: solo presencia o ausencia.** | `curl -s -o /dev/null -w '%{http_code}' "$OS_PORTAL_BASE_URL/api/health"` → `200`; `ollama list`; `openclaw --version`; `python3 -c "import sqlite3;sqlite3.connect(':memory:').execute('CREATE VIRTUAL TABLE t USING fts5(x)')"`; en el app-server, **sin imprimir valores**: `grep -c '^AUDIT_IP_HMAC_SECRET=' .env.local` y `grep -c '^VISOR_DEPLOYMENT='`; `curl -s ifconfig.me` en ambos hosts para comparar /24; `psql -c '\d app_user_sessions'` para confirmar `inet` | low | no |
| **M1** | Higiene del repo y red de tipos, antes de que exista la primera credencial. mypy está configurado y **ningún step de CI lo invoca**: un módulo de red con errores tipados es justo donde paga. | `git mv specs/006-preguntas-gcp specs/006-preguntas-portal` + `{spec,plan,tasks}.md` reescritos; `.gitignore` gana `config/ask-*.yml`, `var/`, `*.session.json`; step `uv run mypy` en `ci.yml`; `docs/operations-runbook.md §ficha del usuario del agente` | `git check-ignore -v config/ask-queries.yml var/state.db var/portal-session.json` → una regla para los tres; `uv run mypy` → 0; `uv run ruff check . && uv run pytest` verde | low | no |
| **M2** | `redaction.py` v2 antes de que exista una cookie que enmascarar. Hoy no cubre `{"password": "..."}` en JSON. | Patrones nuevos: `Set-Cookie: vp_session=`, `Cookie:`, `x-csrf-token`, JSON `password`, PEM, JWT, IP privada, correo; aplicado al escribir **y** al leer; `evals/cases/redaction_cases.yaml` de 4 a ≥20 | `uv run pytest -q tests/test_redaction.py tests/test_evals.py` y `python -c "import yaml;assert len(yaml.safe_load(open('evals/cases/redaction_cases.yaml')))>=20"` | low | no |
| **M3** | Probar red, TLS, DNS, la forma del cliente y la taxonomía de errores **SIN tocar una credencial**: el único endpoint público del portal. *(injerto: H1 del Diseño 1)* | `portal/{__init__,client,errors}.py` con `Fetcher = Callable[[EndpointCall], HttpResponse]` inyectable (mismo patrón que `collector.py:30`); solo GET; `httpx.Timeout(5,20,5,5)`; `max_retries=0`; concurrencia 1; `httpx>=0.27` declarado; `scripts/portal_probe.py`; `evals/cases/portal_errors_cases.yaml` ≥12 casos | `uv run pytest -q tests/test_portal_client.py tests/test_portal_errors.py`; `uv run python scripts/portal_probe.py --health --json \| python -c "import json,sys;assert json.load(sys.stdin)['ok']"`; y los casos obligatorios: `productivity_200_con_error`, `freshness_200_updatedat_null`, `date_not_found`, `tabla_503`, `timeout_504`; `uv tree --depth 1` sin dependencia nueva más allá de httpx | low | no |
| **M4** | La cuenta dedicada existe y su alcance real es **exactamente** el declarado. Un permiso de más es `SECURITY`. **Este hito ES la frontera de seguridad.** | El operador crea el usuario (escritura en producción). Nosotros: `scripts/verify_portal_user.py` con `--strict`, `--print-expected`, `--print-actual`, `--assert-denied`; `portal/credentials.py` con `__repr__` enmascarado; `docs/security-runbook.md` con las cuatro palancas de revocación y el `username` exacto | `uv run python scripts/verify_portal_user.py --strict --json` → exit 0 y `.alcance_inesperado == []`; `diff <(… --print-expected) <(… --print-actual)` → **vacío**; `--assert-denied /api/admin/users,/api/ingresar-horarios/people,/api/excel-dian/export,/api/proveedores/visitas` → exit 0; prueba de revocación cronometrada: desactivar en `/admin/usuarios` y confirmar 401 en <10 s | **high** | **sí** |
| **M5** | La sesión que aguanta, y el comando que hace que cualquier fallo futuro se explique solo. Primer login real. | `portal/session.py` (0600, ruta absoluta, lock `O_EXCL`, huella sha256[:16], `login_not_before` persistido, heartbeat perezoso + `heavy:true`); `scripts/portal_doctor.py` con los siete peldaños y el catálogo de causas | `uv run pytest -q tests/test_portal_session.py -k "relogin_unico or no_hay_tercer_intento or backoff_persistido or dos_procesos_un_login or secreto_no_aparece"`; `portal_doctor --json \| jq -e '.primer_fallo == null'`; y que el diagnóstico funcione: `OS_PORTAL_..._BASE_URL=https://127.0.0.1:9 … \| jq -e '.primer_fallo.peldano == "tcp"'`; `grep -c 'vp_session=' var/state.db` → 0 | medium | **sí** |
| **M6** | El catálogo declarativo con validación fail-closed. Una pregunta nueva es un bloque YAML, no un despliegue. | `config/ask-queries.example.yml` con ≥8 intents; `ask/{intents,dateparse}.py`; `scripts/ask_catalog_check.py`; `scripts/ask_explain.py` que imprime `endpoint_id` y `params`, **nunca la URL** | `ask_catalog_check --strict` → exit 0; `uv run pytest -q tests/test_ask_catalog.py` con **un test negativo por cada una de las 12 reglas**; `ask_explain --json \| jq -e '.endpoint_id and (has("url")\|not)'`; `pytest -k "ayer or antier or mes_pasado or ultimos_7"` | low | no |
| **M7** | **LA PRIMERA RESPUESTA CORRECTA.** Un intent, cero modelo, cero SQLite: pregunta → intent → GET → cifra → fecha de corte. *(injerto: H4 del Diseño 1)* | `ask/router.py` con solo T0; `ask/render.py`; `scripts/ask_cli.py` (texto por **STDIN**, sin `--target` ni `--direct`). Reutiliza `redact()` + `send_chunked()` sin tocar una línea. **Se ejecuta por CLI, invocado por el operador**; el carril automático a Telegram NO se abre hasta M9+M12, porque hasta entonces no hay rastro de auditoría | `echo 'de cuando es el dato' \| uv run python scripts/ask_cli.py --json \| python -c "import json,sys;d=json.load(sys.stdin);assert d['verdict']=='ok' and d['tier']=='t0' and d['intent_id']=='frescura_portal' and d.get('cut_off'),d;print(d['respuesta'])"`; **cero regresión**: `diff` vacío del reporte diario antes/después | medium | **sí** |
| **M8** | La prueba de contrato contra el portal real y su vigilancia semanal. Sin esto, una migración del portal convierte al agente en un generador silencioso de ceros. | Marker `portalproof`; `tests/test_portal_contract.py`; `scripts/portal_contract_check.py` + timer semanal; tabla `endpoint_contract`; `PortalContractError` cableado a `verdict='error_contrato'` y alerta CRITICAL | `OS_PORTAL_PROOF=1 uv run pytest -q -m portalproof` → cada intent 200, `cut_off` existe y **todos** los `figures` existen; `sqlite3 var/state.db "SELECT count(*) FROM endpoint_contract WHERE roto_desde IS NOT NULL"` → `0`; y la detección: `pytest -k jsonpath_ausente` → levanta `PortalContractError` y **no** devuelve 0 | medium | **sí** |
| **M9** | Memoria persistente y ledger, matando `.alert-state.json` **sin provocar una tormenta de alertas al migrar**. | `state/{schema.sql,store.py,migrate.py}`, `scripts/state_init.py`, `audit.py` + `scripts/audit_verify.py`, `memory/notes.py`. `alert_incidents.py` y `send_daily_report.py` pasan a `--state-db`; `diff_incidents` sigue **pura** | `state_init --seed-from .alert-state.json --apply` y `sqlite3 var/state.db "PRAGMA integrity_check; PRAGMA journal_mode; SELECT count(*) FROM incident WHERE closed_at IS NULL;"` → `ok`, `wal`, e igual al nº de claves del JSON; `pytest -k antitormenta` → **cero alertas** en corrida en seco; el INSERT de un `ask_turn` `'ok'` sin `cut_off` **falla**; `sqlite3 var/audit.db "UPDATE audit_entry …"` → `append-only`; `audit_verify --ledger tests/fixtures/ledger_manipulado.jsonl` → exit ≠ 0 | medium | **sí** |
| **M10** | Router T0/T1 y grounding. Meta: ≥80 % de las preguntas reales resueltas **sin invocar un modelo**, y ninguna cifra a Telegram sin estar en el JSON y sin su fecha de corte. | `ask/{router,ground}.py` (puras, `now` inyectado); `scripts/route_eval.py`; `evals/cases/routing_cases.yaml` ≥60 preguntas reales etiquetadas; `evals/cases/grounding_cases.yaml` | `route_eval --min-top1 0.90 --no-model` → verde **sin modelo alguno**; `pytest -q tests/test_ground.py -k "cifra_ausente or formato_es_co or redondeo_declarado or ratio_no_declarado or entidad_ausente or rango_supera_corte or sin_cut_off"`; `echo 'cuanto vendimos manana' \| ask_cli --json` → `verdict in ('sin_dato','sin_intent')` y **ninguna cifra en la prosa** | medium | no |
| **M11** | Cinco preguntas de negocio reales y un presupuesto de peticiones que frena **antes** de salir a la red. | Intents `oc_vencidas`, `oc_cumplimiento`, `venta_dia_sede`, `sobrestock_di`, `margen_sede`. Token bucket persistido + `http_budget`. `query_cache` con TTL. **Ningún intent toca `/api/rotacion` completo** | `echo 'cuantas oc estan vencidas' \| ask_cli --json` → `ok` con `cut_off` y `figures.vencidas`; y `for i in $(seq 1 12); do … --no-cache; done` → aparece `rate_limited` y el bucket dice en qué petición frenó | medium | no |
| **M12** | Los dos carriles a Telegram, con la prueba de que **el núcleo no necesita modelo**. Respuesta directa al fallo de la 002. | `extensions/os-agent-ask/` con `registerCommand()` (`execFile` sin shell, texto por STDIN); allowlist de `chat_id` numéricos; `config/openclaw.006.example.json` regenerado; `mcp_server.py` gana `frescura_portal` | `openclaw plugins validate && openclaw plugins install -l … && openclaw config validate`; **prueba clave**: `openclaw config set agents.defaults.model.primary ollama/no-existe` + reinicio → `/frescura` responde en **<5 s**; `python -c "import ask_cli;h=ask_cli.build_parser().format_help();assert '--target' not in h and '--direct' not in h"`; `openclaw mcp tools osagent \| grep -Eqv 'url\|path\|endpoint\|host\|sql\|cookie\|ruta'`; `openclaw config get memory.search \| grep -q false`; un `chat_id` no allowlisted → sin respuesta y fila en el ledger con `phase='refuse'` | medium | **sí** |
| **M13** | Los peldaños T2 (local) y T3 (cloud), con la degradación probada. El criterio no es que el modelo acierte: es que cuando no está, el agente siga respondiendo y **lo diga**. | `ask/{llm_ollama,spotlight}.py`; validación estricta contra el catálogo; `evals/cases/injection_cases.yaml` ≥12 cargas (incluida una inyectada vía el campo público `visitanteNombre`); `scripts/adversarial_probe.py`; p95 de `gpt-oss:120b` medido y registrado | `OS_ASK_NO_MODEL=1 ask_cli --json <<< 'venta de ayer'` → `ok`/`t0`; con ollama caído → `ok` y `degradado==true` con motivo; `pytest -q tests/test_llm_contract.py -k "intent_inexistente or param_no_declarado or tipo_invalido or devuelve_url or devuelve_sql"` → refusal en los cinco; `adversarial_probe --expect-obeyed 0 --expect-exfil 0 --expect-peticiones-fuera-de-intent 0` (**criterio observable**, no interpretativo) | medium | no |
| **M14** | T4 `claude-opus-5` con tope **en dólares**, no en turnos. | `ask/llm_anthropic.py` (`thinking:{type:'adaptive'}`, `output_config.effort`, `cache_control:{ephemeral}` sobre el prefijo estable, `strict:true` en la tool, `stop_reason` antes de `content`, streaming); `scripts/budget_report.py`; `.env.example` gana `OS_ASK_T4_DAILY_MAX_USD` | `openclaw secrets audit --check` limpio; `ask_cli --pro --json <<< 'compara el margen de julio contra junio y explica la diferencia'` → `tier=='t4'`, `modelo=='claude-opus-5'`; **caché real**: `cache_read_input_tokens > 0` en el segundo turno; `budget_report \| jq -e '.t4.costo_usd <= .t4.max_usd'`; con `OS_ASK_T4_DAILY_MAX_USD=0` la respuesta sale por T3 **y contiene la frase que declara la degradación** | medium | **sí** |
| **M15** | Cerrar el apagón programado del día 30. Es el único fallo **garantizado** del sistema si nadie hace nada. | `scripts/portal_credential_status.py` + timer diario: WARNING ≤7 días, CRITICAL ≤2, con el **procedimiento exacto** en el mensaje de Telegram; línea fija de credencial en el reporte diario; health-check contra un **endpoint de datos**, no `/me`. **Sin autorrotación** (§9) | `portal_credential_status --json \| jq -e '.dias_restantes >= 0'`; `--simulate-days 5` → `WARNING`; `--simulate-days 1` → `CRITICAL`; `pytest -k "warning_7 or critical_2 or me_no_sirve_de_healthcheck"`; `portal_probe --credencial --json \| jq -e '.healthcheck_endpoint != "/api/auth/me"'` | medium | no |
| **M16** | Semana en sombra, no regresión y **puerta de decisión con números**. Nada se declara terminado por estar escrito. | `scripts/ask_report.py` + auto-reporte semanal; `/feedback` cableado; `scripts/ask_backlog.py` con `claude-haiku-4-5` que **propone en un PR** y nunca aplica; runbook del carril; units con `EnvironmentFile=` | No regresión: `diff` vacío del reporte diario; `uv run pytest` verde; `openclaw security audit --deep --json` → 0 critical; `! grep -rn '^Environment=.*TOKEN' config/systemd/`; `sqlite3 var/state.db "SELECT count(*) FROM ask_turn WHERE verdict='ok' AND cut_off IS NULL"` → **0**; puerta: `ask_report --desde -7d --json` → `cobertura>=0.80`, `incorrectos==0`, `costo_usd<=8`, `contratos_rotos==0`, y `run_observation` con `signal='http_portal'` y severidad CRITICAL/SECURITY → **0** | medium | **sí** |

---

## 9. Descartado

**Descartes que vienen de los dos diseños y se mantienen:**

- **Text-to-SQL en cualquier forma**, incluido «SQL validado con parser». El propio sistema demuestra por qué: `visor-etl-sync` escribe con un SELECT, así que un guardia textual «empieza por SELECT» lo aprueba.
- **Que el modelo arme una URL, un path, un querystring o SQL.** Devuelve `(intent_id, params)` o nada. Un modelo que devuelva una URL provoca un *refusal*, no «se ignora el campo».
- **`psycopg[binary]`, `cloud-sql-proxy`, el JSON de service account, el rol `os_agent_ro` y las vistas `agente_ro.v_*`.** Con HTTP desaparecen dos hitos enteros y el activo más valioso del sistema anterior.
- **Embeddings (`nomic-embed-text:v1.5`, 274 MB) y cualquier índice vectorial en la v1.** BM25 con FTS5 de la stdlib acierta igual con decenas de intents y no deja un modelo que pullear ni un índice que reindexar dentro de un año.
- **Heartbeat como demonio 24/7.** Escribe actividad falsa en `app_user_activity_log` para siempre, contamina `/api/admin/uso-tableros` y es una pieza más que puede morir en silencio. Se late solo dentro de la invocación y alrededor de los intents `heavy: true`.
- **Compartir la cuenta del portal con un humano, o dos instancias del agente con la misma cuenta.** R2 lo hace imposible: se expulsan mutuamente en bucle. No es un bug a esquivar, es el diseño del portal.
- **Grupos de Telegram en la v1.** Una sesión = un conjunto de permisos, aplicado a los 30 miembros por igual. El gating por mención no resuelve nada: el problema no es quién pregunta, es quién **lee**.
- **Concurrencia > 1 contra el portal.** El loader la rechaza. La cuota se mide en conexiones simultáneas, no en peticiones/minuto.
- **`force=1`, `refresh=1`, `explain=1`, `matviewSql`.** Prohibidos en el **loader**, no en el prompt.
- **Todas las rutas con PII nominal**, y en particular `/api/ingresar-horarios/people` (R11) y `/api/proveedores/ingreso` (R12). Más los modos `cliente|cliente-facturas|vendedor|vendedor-facturas|fact-nav|fact-list` de `/api/margenes/data`; los demás modos sí son usables.
- **`/api/excel-dian/export` y `/api/exports/log`.** Y **no** usar «tenemos auditoría de exportaciones» como argumento de seguridad (R14).
- **`command-dispatch: tool` apuntando a MCP.** Verificado en la 002: el dispatcher no resuelve tools MCP. Reintentarlo sería ignorar el dato más caro del repo.
- **`model.fallbacks` de OpenClaw como mecanismo de calidad.** Solo dispara ante rate-limit/quota.
- **`claude-sonnet-4-5`** — no existe. **`verify=False`** en httpx. **Un segundo waiver de bandit** para meter el cliente por urllib. **Reintentos ciegos.** **Toda la Fase 2** (ejecución aprobada, parser `APPROVE`). **WhatsApp**, **Prometheus/Grafana**, **búsqueda semántica sobre la memoria**.
- **Activar `AUDIT_IP_HMAC_SECRET` «para endurecer».** Está recomendada en `docs/DEPLOYMENT.md` del portal y, contra V7, escribiría `hmac:<hex>` en columnas `inet` → 500 opaco en el login para el bot **y para los humanos**. No tocarla sin cerrar S1/S8.

**Descartes propios de esta decisión (donde los dos diseños discrepaban):**

- **La autorrotación de contraseña del agente, ni siquiera detrás de `OS_PORTAL_SELF_ROTATE=1`.** El Diseño 2 la dejaba como escape opt-in. Se descarta entera y se toma la postura del Diseño 1: sería la **única capacidad de escritura del agente en todo el sistema** (más el CSRF que hoy no necesita), obligaría a decidir dónde vive la clave nueva, y ese código existiría en el repo esperando a que alguien encienda una variable. Rota un humano; el agente solo avisa a los 7 y a los 2 días. Si nadie lo hace, se apaga solo el día 30 — que es un fallo **seguro**, no uno peligroso. Se entrega la alarma (M15), no la rotación.
- **`/api/rotacion` completo en la v1.** El endpoint más caro de la API (handler de 4165 líneas, consulta por sede en paralelo, ventana que recorta a 93 días en silencio). Si hace falta el KPI, se entra por `/api/rotacion/gestion` — y aun así exige conceder el subtablero `rotacion`, que arrastra el PATCH (V3). Es decisión del operador (§10.6), no nuestra.
- **La memoria SQLite antes del primer lazo cerrado.** El Diseño 2 la ponía en M3. Se mueve a M9: no es que sobre, es que retrasa un mes la primera prueba de que la arquitectura funciona. A cambio, M7 se ejecuta **por CLI invocado por el operador** y el carril automático a Telegram no se abre hasta que existe `ask_turn` + ledger, para no violar «toda acción del agente deja rastro».

---

## 10. Decisiones que necesitan al operador

**10.1 — `AUDIT_IP_HMAC_SECRET` y el /24 (S1, S2). BLOQUEANTE.**
Opciones: (a) verificar en el `.env.local` del app-server y en la salida de ambos hosts, antes de conectar nada; (b) conectar y ver qué pasa. **Recomendación: (a), y es M0.** Es la diferencia entre «un bucle nuestro nos afecta a nosotros» y «un bucle nuestro deja sin entrar a la oficina 15 minutos». Y si la variable estuviera puesta, el login devuelve 500 para todos: hay que saberlo antes, no después.

**10.2 — Quién crea la cuenta y con qué alcance exacto.**
Opciones: (a) alcance mínimo de consulta pura — `margenes`, `informe-variacion`, `mix-y-linea`, `participacion-comercial`, `ventas-x-item`, `analisis-de-inventario`; (b) añadir `rotacion` porque es donde están las preguntas de inventario; (c) darle todo «mientras probamos». **Recomendación: (a).** (c) es el error más caro y más fácil (R4). (b) es una decisión de riesgo consciente: concede también el PATCH (V3) y hay que escribirlo así en la aprobación, no esconderlo.

**10.3 — Rotación de la contraseña cada 30 días.**
Opciones: (a) humano rota en `/admin/usuarios` y actualiza el `EnvironmentFile`, avisado por el agente a 7 y 2 días; (b) autorrotación opt-in del agente; (c) pedir al equipo del portal un `/api/agent/*` con **token de servicio de alcance fijo**. **Recomendación: (a) ahora + (c) como salida.** (c) es un PR pequeño en un repo del mismo equipo y elimina de golpe los cuatro problemas de sesión (caducidad de 30 días, revocación mutua, ventana de 5 minutos, login como escritura). No re-litiga la decisión de HTTP: es una mejora **dentro** de ella. Falta saber: ¿quién puede pedirlo y en qué plazo?

**10.4 — Quién actualiza la credencial en el box.** Nadie lo ha dicho en ninguno de los dos diseños. El agente corre en MMAUTOML01 (WSL2). Si el humano rota la contraseña en el portal y no toca el `EnvironmentFile` del box, el agente entra en 401 terminal y — correctamente — deja de intentarlo. **Recomendación: procedimiento de dos pasos en el runbook, con el segundo paso (actualizar el box) como parte de la misma tarea, no como recordatorio.**

**10.5 — Grupos de Telegram.** Opciones: (a) solo DM 1:1 con allowlist de `chat_id` numéricos en la v1; (b) un grupo con gating por mención. **Recomendación: (a), sin excepción.** El gating por mención no resuelve nada porque el problema no es quién pregunta sino quién lee. Si el negocio pide grupo, la respuesta correcta es una sesión por persona, y eso es otro proyecto.

**10.6 — ¿Se concede rotación?** Ver 10.2(b). Si sí: entrar solo por `/api/rotacion/gestion` (mode=kpis), nunca por `/api/rotacion` completo, y aceptar por escrito que el subtablero habilita `PATCH /api/rotacion/cero-estados` y que el único freno es nuestro allowlist de método. **Recomendación: no en la v1**; reevaluar tras la semana en sombra con evidencia de cuántas preguntas reales lo pidieron (`ask_intent_miss` lo dirá).

**10.7 — ¿Se pide a infra un filtro de método por IP?** Un `limit_except GET { deny all; }` en nginx para la IP del box convierte el control #2 en estructural en vez de en un `if` nuestro. **Recomendación: pedirlo, pero no bloquear M4 con ello.** Es defensa en profundidad, no la primera línea.

**10.8 — Presupuesto mensual del modelo.** Propuesta: `OS_ASK_T4_DAILY_MAX_USD = 0.80` ≈ **$24/mes** de techo estructural. **Recomendación: aprobarlo explícitamente antes de M14**, porque el control es que el agente **rehúse** al llegar al tope, y un tope aprobado a medias produce degradaciones que el operador no entiende.

**10.9 — Contra qué despliegue habla el agente (S3).** LAN del 232 o GCP. Cambia `allowed_paths` entera y cambia si `/api/excel-dian/export` está público. **Recomendación: fijarlo en M0 y escribirlo en el catálogo, no en una variable suelta.**

---

**Resumen de la decisión en una línea:** columna vertebral del **Diseño 2** (correctitud estructural y supervivencia a un año: `CHECK` de fecha de corte, render por plantilla, `endpoint_contract`, `portal_doctor`, M0 de supuestos, cuenta sin verbos de escritura), con tres injertos del **Diseño 1** (el lazo cerrado temprano en M7 con un intent y cero modelo, las listas `denied_prefixes`/`denied_params`/`drop_fields` explícitas y validadas al cargar, y el heartbeat reducido a intra-invocación), más un descarte propio: **ninguna capacidad de escritura para el agente, ni siquiera la de rotar su propia contraseña.**