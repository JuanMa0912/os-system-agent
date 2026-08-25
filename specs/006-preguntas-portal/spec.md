# Spec 006 — El agente responde preguntas de negocio por la API del portal

> Estado: **borrador para aprobación.** Producto de dos rondas de análisis multi-agente
> (9 + 11 agentes) sobre el código real de ambos repos, con panel de arquitecturas
> independientes y críticos adversariales. El expediente está en los `_*.md` de esta carpeta.
>
> Sustituye al borrador anterior, que asumía acceso a Cloud SQL desde el 232. Esa premisa era
> falsa (ver `_diseno-sintesis.md` y el commit `7581826`).

## 1. Problema

El agente hoy responde **una** pregunta: *«¿corrió el job?»*. El operador pregunta otra cosa:
cuánto vendimos, qué margen dejó una categoría, por qué cayó la rotación. Para saberlo entra
al portal o entra al servidor — que es justo lo que este proyecto existe para evitar.

Hay un antecedente que esta spec debe respetar: el pull interactivo `/estado` se intentó y
quedó **parqueado** (`specs/002-estado-etl-pull/tasks.md`) porque `command-dispatch` no
alcanzaba las herramientas MCP y el modelo gratuito se colgaba o se disparaba. Repetir el
mismo intento con otra pregunta daría el mismo resultado. Por eso el núcleo es
**determinista**: el modelo no construye URLs, ni parámetros libres, ni código.

## 2. Decisión de arquitectura

El agente **no tendrá credenciales de base de datos**. Consumirá por HTTPS la API del portal
`visor-productividad`, autenticado como un **usuario dedicado de solo lectura**.

Razones, en orden:

1. **El requisito de fondo se cumple por construcción.** Sin credencial de BD no hay nada que
   el agente pueda borrar. No depende de que unas capas de permisos se mantengan bien.
2. **El 232 no alcanza Cloud SQL** (confirmado por el operador). Quien habla con Cloud SQL es
   la VM de la nube que sirve el portal (`docs/DEPLOYMENT.md:37-61`).
3. **Los rollups viven solo en GCP** — `margen_final_roll`, `margen_item_dia_roll`,
   `rotacion_item_periodo_std`. Leer el Postgres local del 232 daría los hechos base pero
   obligaría a recalcular la lógica de negocio, que es donde se cometen los errores.
4. Desaparecen el compilador de SQL, el rol de Postgres, el DDL y la dependencia del DBA — el
   subsistema más delicado del diseño anterior.

## 3. Hechos verificados sobre la autenticación del portal

Todos con evidencia en el código; ninguno es supuesto.

| Hecho | Evidencia | Consecuencia |
| --- | --- | --- |
| **No existe token de API, bearer ni service account.** La única autenticación es usuario/contraseña. | grep de `Bearer\|x-api-key\|apiKey` sobre `src/` → cero | El agente hace **login programático** y gestiona cookies. |
| **`createSessionReplacingOthers` revoca todas las sesiones previas del mismo usuario**, en transacción con advisory lock. | `src/lib/auth/index.ts:209-237` | **Cuenta dedicada, no negociable.** Compartirla expulsa al humano en cada login. |
| **La sesión dura 5 minutos deslizantes** y **solo `POST /api/auth/heartbeat` la renueva**. Ningún GET de datos extiende `expires_at`. | `SESSION_IDLE_MINUTES = 5`; `extendSessionOnActivity` solo se llama desde `heartbeat/route.ts` | Sin latido, la sesión muere a los 5 min. |
| **La contraseña caduca a los 30 días** y entonces `requireAuthSession()` devuelve `null` → 401 en las 42 rutas de datos. | `PASSWORD_MAX_AGE_DAYS = 30` | Tope absoluto real de la credencial. Es la mina operativa: el día 30 el agente muere en silencio. |
| **No existe el concepto de «solo lectura» en el modelo de permisos.** El mismo permiso de subtablero habilita GET y POST/PATCH. | `portal-permissions`, `special-role-features` | La contención real es la whitelist de `allowed_subdashboards` + rol `user` (que cierra `/api/admin/*`) **más un allowlist de método nuestro**. |
| **La auditoría es de sesión y login, NO de consulta.** | `app_user_login_logs`, `app_user_sessions` | **No queda rastro en BD de qué datos consultó el agente.** El rastro hay que construirlo del lado del agente. |
| **`/api/health` es público**, devuelve `{ok, db, latencyMs, pool}` o 503. | `src/app/api/health/route.ts` | Señal gratis y sin credenciales para saber si el portal vive antes de intentar login. |
| Rate limit: 10 fallos por IP y 5 por usuario en 15 min, con 250 ms de retardo. | `src/app/api/auth/login/route.ts:121-144` | Un bucle de login del agente **puede dejar sin entrar a los humanos que salgan por esa IP**. |

## 4. Requisitos funcionales

- **RF-01** El agente responde preguntas de negocio por Telegram consumiendo la API del portal.
- **RF-02** El modelo **nunca** construye una URL, una ruta ni un parámetro libre. Devuelve
  `(intent, parámetros)` validados contra un **enum cerrado**. El endpoint y sus parámetros
  salen de un catálogo declarativo.
- **RF-03** Allowlist de **método** (solo `GET`) y de **ruta**, verificado antes de cada
  petición. El modelo de permisos del portal no distingue lectura de escritura; esa distinción
  la ponemos nosotros.
- **RF-04** Toda respuesta declara la **fecha de corte** del dato. Un número sin su fecha
  engaña.
- **RF-05** Si el rango pedido no tiene dato, se dice explícitamente. Nunca un agregado
  parcial presentado como completo.
- **RF-06** Ninguna cifra puede aparecer en la prosa si no está en el JSON que devolvió la API,
  verificado mecánicamente.
- **RF-07** El agente **nunca reintenta login en bucle**. Tras un fallo, backoff persistido en
  disco; tras 401 terminal, se detiene y avisa.
- **RF-08** El agente **no late en vacío**. El heartbeat alimenta las métricas de uso del
  portal (`admin/uso-tableros`); latir por temporizador inventaría actividad humana. Solo
  renueva si de verdad hizo una consulta.
- **RF-09** Memoria persistente en SQLite que **se lee**, no solo se escribe: historia de
  incidentes, preguntas sin intent, presupuesto consumido y ledger de auditoría.
- **RF-10** Multi-modelo por escalones, con tope **en dólares**, no en número de turnos.

## 5. Requisitos no funcionales

- **RNF-01** Zona horaria explícita en todo cálculo de fecha. El repo corre en UTC y el
  negocio en hora local: un «ayer» mal calculado devuelve el día equivocado en silencio.
- **RNF-02** Un solo login concurrente en todo el sistema, con lock entre procesos.
- **RNF-03** Fail-closed: sin catálogo, sin credenciales o sin verificación de alcance válida,
  el agente no arranca.
- **RNF-04** Todo lo que se escriba a memoria o a log pasa por `redact()` al escribir **y** al
  leer.

## 6. Frontera de seguridad

| Capa | Control | Ataque que detiene |
| --- | --- | --- |
| Cuenta | Usuario dedicado, rol `user`, whitelist mínima de subtableros | Acceso a `/api/admin/*` y a tableros no autorizados |
| Cliente | Allowlist de método (`GET`) y de ruta, validado antes de cada petición | Escrituras que la cookie del agente **sí** podría emitir |
| Cliente | Un login concurrente, backoff persistido, cero reintentos en bucle | Agotar el rate limit y dejar fuera a los humanos |
| Canal | DM 1:1 con allowlist de `chat_id` numéricos | Que la respuesta la lea quien no debe |
| Datos | `drop_fields` **obligatorio**, aplicado antes de que nada salga del proceso | Que datos personales lleguen a un modelo externo o a Telegram |
| Respuesta | Grounding: ninguna cifra en prosa ausente del JSON | Que el modelo invente cifras |

### 6.1 Hallazgo heredado que no debe perderse

El borrador anterior documentó que en la base del portal **no hay un solo `REVOKE`** en `db/`
y que **12 migraciones** fijan `statement_timeout = 0` dentro de funciones `refresh_*`. Como
PostgreSQL otorga `EXECUTE` a `PUBLIC` por defecto, cualquier rol con `CONNECT` puede invocar
esas funciones sin techo de tiempo.

Ya **no es superficie del agente** (no se conectará a la base), pero **sigue siendo un riesgo
real** para quien administre esa instancia. Queda registrado aquí para que no se pierda al
cambiar de arquitectura.

## 7. Riesgos abiertos que el análisis adversarial encontró

Estos **no están resueltos** y el `plan.md` debe cerrarlos antes de implementar:

1. **`/api/margenes/data` son 13 endpoints en un solo path**, y algunos exponen NIT de cliente
   y cédula de vendedor. El «alcance mínimo» propuesto **ya los alcanzaría**. Hay que decidir
   endpoint por endpoint, no path por path.
2. **El grounding acepta la inyección**: comprueba que la cifra esté en el JSON, no que sea un
   hecho. Un dato malicioso en un nombre de producto pasa el control.
3. **El allowlist de rutas viviría en un fichero gitignored**, así que el control «se revisa en
   el diff del PR» no existe. O se versiona, o el control es teatro.
4. **Zona horaria**: ausente del diseño. Todo el repo corre en UTC.
5. **La memoria persistente propuesta es de solo escritura**: nada la lee. Eso no es memoria,
   y no cumple el pedido.
6. **Ollama local no está instalado** en ningún box (verificado). El escalón que dependía de él
   no existe todavía.
7. **El sandbox de OpenClaw está en `off`** según el runbook, y el diseño no lo menciona.

## 8. Criterios de aceptación

- **CA-01** Ninguna petición con método distinto de `GET` sale del cliente, probado con un
  intento explícito.
- **CA-02** Con la cuenta del agente, `/api/admin/users` devuelve 401/403.
- **CA-03** Desactivar la cuenta en `/admin/usuarios` produce 401 en menos de 10 segundos.
- **CA-04** Un fallo de login no genera un segundo intento inmediato; el backoff sobrevive al
  reinicio del proceso.
- **CA-05** Dos procesos del agente en paralelo producen **un solo** login.
- **CA-06** Ningún secreto (cookie, contraseña, `x-csrf-token`) aparece en logs ni en memoria,
  probado con casos en `evals/`.
- **CA-07** Una pregunta sobre un rango sin dato responde «no hay dato» con la última fecha de
  corte, no un agregado parcial.
- **CA-08** Ninguna cifra de la prosa está ausente del JSON de la API.
- **CA-09** El reporte diario actual no cambia ni un carácter: `diff` vacío antes y después.
- **CA-10** `uv run pytest` verde, incluidos los 124 tests existentes; `uv run mypy` en 0.

## 9. Fuera de alcance

- Ejecución de ETLs desde la conversación (Fase 2, `specs/004`).
- Cualquier escritura, en cualquier sistema.
- Grupos de Telegram: solo DM 1:1 en la v1.
- WhatsApp.
- Datos personales nominales: cédula, nombre, cargo, incidencia.

## 10. Supuestos bloqueantes (M0)

| # | Supuesto | Cómo se confirma |
| --- | --- | --- |
| S1 | Contra qué despliegue habla el agente: LAN del 232 o el host cloud | Decisión del operador; cambia el allowlist entero |
| S2 | El box del agente y las oficinas **no** comparten IP de salida | `curl -s ifconfig.me` en ambos; si comparten, un bucle nuestro los bloquea |
| S3 | `AUDIT_IP_HMAC_SECRET` está o no definido en el app-server | `grep -c '^AUDIT_IP_HMAC_SECRET=' .env.local` — **sin imprimir el valor** |
| S4 | Versión de OpenClaw, estado del sandbox y existencia de `registerCommand()` | `openclaw --version`, `openclaw config get agents.defaults.sandbox.mode` |
| S5 | Modelos disponibles: Ollama local no está instalado en ningún box | `ollama list` |
| S6 | La contraseña filtrada de `produ` fue rotada | Consulta al operador |

**Ninguna línea de código antes de cerrar S1, S2 y S3.** S2 es el que puede hacer daño a
terceros: si el box comparte IP de salida con las oficinas, un bucle de login del agente deja
sin entrar a la gente durante 15 minutos.
