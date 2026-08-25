# Plan 006 — Diseño técnico

> Requiere haber leído `spec.md`. Este documento define **cómo**; la spec define **qué** y
> **por qué**. Los riesgos abiertos de `spec.md §7` se cierran aquí, cada uno con su control.

## 1. Arquitectura

```text
Telegram (DM 1:1, allowlist de chat_id numéricos)
   │
   ▼
OpenClaw Gateway  ── canal + orquestación ──┐
   │                                        │
   ▼                                        │
router determinista (Python)                │  el modelo NO entra aquí
   │  ¿resuelve por palabras clave?         │
   ├── sí ──────────────────────────────────┤
   └── no ──► modelo ──► (intent, params) ──┤  enum cerrado, validado
                                            │
   ┌────────────────────────────────────────┘
   ▼
catálogo de intents (YAML versionado)
   │  intent ──► endpoint + params + drop_fields + plantilla
   ▼
portal/client.py
   │  ┌─ allowlist de MÉTODO (solo GET)
   │  ├─ allowlist de RUTA (exacta, no prefijo)
   │  ├─ single-flight lock entre procesos
   │  └─ presupuesto de login persistido en disco
   ▼
HTTPS ──► API del portal  (cuenta dedicada, rol `user`, subtableros mínimos)
   │
   ▼
drop_fields ──► grounding ──► plantilla ──► redact() ──► Telegram
   │
   └──► var/state.db  (memoria: incidentes, intents fallidos, presupuesto, ledger)
```

**Principio rector:** el modelo traduce lenguaje a `(intent, parámetros)` y nada más. Nunca ve
una URL, nunca construye un parámetro libre, nunca elige un endpoint. Esa frontera es lo que
distingue esta spec del `/estado` que quedó parqueado.

## 2. El problema central: la IP compartida

**Hecho confirmado por el operador:** el box del agente y las oficinas salen a internet por la
**misma IP**. Con `FAILED_LOGIN_MAX_PER_IP = 10` en ventana de 15 minutos, **diez fallos de
login del agente dejan sin poder entrar a las personas durante 15 minutos**. Y si
`AUDIT_IP_HMAC_SECRET` no está definido en el portal, la clave es `a.b.c.0/24` y el bloqueo
alcanza a **254 direcciones**.

Esto no es un riesgo aceptable mitigado con «cuidado al programar». Es la restricción que
manda sobre el diseño del cliente HTTP.

### 2.1 Presupuesto de login, no reintentos

El agente tiene un presupuesto duro de **2 fallos de login por ventana de 15 minutos**,
dejando 8 de los 10 para las personas. El contador:

- vive en `var/portal-login-budget.json`, **persistido en disco**, no en memoria;
- se lee **antes** de cada intento y se incrementa **antes** de enviar la petición, no después
  (si el proceso muere a mitad, el intento cuenta igual: fail-closed);
- es compartido entre procesos mediante el mismo lock que el single-flight.

Persistirlo es el punto crítico. Un contador en memoria se reinicia con el proceso, y un
crash-loop — que es exactamente el escenario que produce fallos repetidos — martillearía el
portal reiniciando el contador cada vez.

### 2.2 Taxonomía de errores: qué es reintentable y qué no

| Respuesta | Significado | Acción |
| --- | --- | --- |
| `401` | Credencial incorrecta | **Terminal.** No reintentar nunca: una contraseña mala no se arregla sola. Avisar y detenerse. |
| `403` | Cuenta desactivada | **Terminal.** Es una revocación deliberada. Detenerse. |
| `429` | Rate limit ya agotado | **Terminal en esta ventana.** Respetar `Retry-After`. Nunca reintentar antes. |
| `503` / timeout / DNS | El portal está caído | Reintentable con backoff largo, **pero no consume presupuesto de login** si no llegó a la ruta de login. |
| `200` | Éxito | Reiniciar el contador de fallos. |

El portal distingue 401 de 403 a propósito (`login/route.ts:214-228`), y el agente debe
aprovechar esa distinción en vez de tratar todo error como reintentable.

### 2.3 Pre-vuelo con `/api/health`

Antes de cualquier login, el agente consulta `GET /api/health` — que es **público, sin
autenticación y fuera del rate limit de login**. Si devuelve 503 o no responde, el agente
**no intenta login**: el portal está caído y un intento solo gastaría presupuesto.

Esto convierte el caso más común de fallo (portal caído) en cero consumo del presupuesto
compartido.

### 2.4 Salidas estructurales, a pedir en paralelo

El presupuesto es nuestra defensa y funciona. Pero hay dos arreglos estructurales que
eliminan el problema de raíz y no dependen de que programemos bien:

1. **Token de servicio** — pedir al equipo del portal un `/api/agent/*` con token de alcance
   fijo. Elimina el login, y con él la caducidad a 30 días, la revocación mutua de sesiones,
   la ventana de 5 minutos y el riesgo de rate limit. Es un PR pequeño en un repo del mismo
   equipo. **Es la mejor inversión de este proyecto.**
2. **Egreso separado** — una regla de NAT que le dé al box del agente una IP de salida propia.
   Depende de infraestructura.

Ninguna de las dos bloquea el arranque; ambas convierten un control nuestro en una propiedad
del sistema.

## 3. La cuenta del agente

- Usuario **dedicado**, nunca compartido (`createSessionReplacingOthers` lo hace obligatorio).
- Rol `user` — cierra todo `/api/admin/*`.
- Whitelist de subtableros **mínima**, decidida endpoint por endpoint.
- La contraseña vive en un `EnvironmentFile` con permisos `0600`, nunca en el repo.

### 3.1 El alcance se decide por endpoint, no por path

`/api/margenes/data` son **13 endpoints distintos bajo un mismo path**, y algunos exponen
**NIT de cliente y cédula de vendedor**. Conceder «márgenes» concede todo eso.

Por lo tanto: el allowlist es de **ruta exacta más combinación de parámetros**, nunca de
prefijo. Y `drop_fields` es **obligatorio** en cada intent — si un intent no lo declara, el
catálogo **no carga**. Los campos se eliminan **dentro del proceso**, antes de que el JSON
llegue al modelo, a la plantilla, al log o a la memoria.

### 3.2 El portal no sabe decir «solo lectura»

El mismo permiso de subtablero habilita `GET` y `POST`/`PATCH`. No existe una cuenta de solo
lectura que pedirle al portal. La distinción la ponemos nosotros:

```python
# portal/client.py — el único punto de salida HTTP del agente
if call.method != "GET":
    raise MethodNotAllowed(call.method)   # nunca alcanzable desde el catálogo
```

Y como control estructural complementario, pedir a infraestructura un
`limit_except GET { deny all; }` en nginx para la IP del box. Defensa en profundidad, no
primera línea.

### 3.3 El allowlist se versiona

El catálogo de intents y el allowlist de rutas **se versionan en el repo** (`config/ask-*.yml`),
no en un fichero ignorado. Si vive en un fichero gitignored, el control «se revisa en el diff
del PR» no existe — y ese era el control central. Lo que **sí** va fuera del repo son
únicamente las credenciales.

## 4. Sesión: latir poco y con motivo

La sesión dura **5 minutos** y solo `POST /api/auth/heartbeat` la renueva. Pero el heartbeat
alimenta las **métricas de uso** del portal (`admin/uso-tableros`,
`admin/users/[id]/metrics`): latir por temporizador inventaría actividad humana constante y
contaminaría los tableros que alguien usa para tomar decisiones.

**Regla:** el agente late **solo dentro de una ráfaga de trabajo real**, entre consultas de
un mismo turno, y deja expirar la sesión al terminar. Un turno típico (una pregunta, dos o
tres llamadas) cabe en la ventana sin un solo latido.

Un login por ráfaga de preguntas es ruido honesto y atribuible en `app_user_login_logs`.
Latir 24/7 es contaminar datos ajenos.

## 5. Zona horaria

El repo corre en **UTC** y el negocio en hora local. Un «ayer» calculado en UTC devuelve el
día equivocado durante buena parte de la jornada, **en silencio y con cifras plausibles** —
el peor modo de fallo posible.

- Una sola función `dateparse.today_local()` con zona explícita (`zoneinfo`), nunca
  `date.today()`.
- La zona es configuración del catálogo, no una constante enterrada.
- Tests con casos en los bordes del día en ambas zonas.

## 6. El grounding no basta

Verificar que «toda cifra de la prosa está en el JSON» detecta que el modelo **invente**
números, pero **no** detecta inyección: un dato malicioso guardado en un nombre de producto
está *en el JSON*, y pasa el control.

Dos controles adicionales:

1. **La prosa sale de plantilla, no del modelo.** El modelo elige el intent; el texto lo
   renderiza una plantilla con los campos ya filtrados por `drop_fields`. Si el modelo no
   escribe la respuesta, no puede ser inyectado en ella.
2. **Los campos de texto libre del portal se tratan como datos, nunca como instrucciones**:
   se truncan, se escapan y nunca se concatenan a un prompt de sistema.

## 7. Memoria persistente que se lee

El diseño anterior escribía memoria que nadie leía. Eso no es memoria. `var/state.db`
(SQLite) con cuatro usos concretos, **cada uno con su lector**:

| Tabla | Qué guarda | **Quién la lee y para qué** |
| --- | --- | --- |
| `incident` | Historia de incidentes con apertura y cierre | Responder «¿desde cuándo está roto?» y «¿es la tercera vez esta semana?» |
| `ask_intent_miss` | Preguntas sin intent | Alimentar el backlog de intents nuevos, con evidencia de demanda real |
| `budget_day` | Costo del modelo acumulado por día | Frenar el turno **antes** de gastar, no después |
| `ledger` | Registro append-only de cada consulta | Reconstruir qué consultó el agente — el rastro que **el portal no guarda** |

Ese último es la respuesta a lo que corregí: la auditoría del portal es de sesión y login, no
de consulta. Si queremos saber qué miró el agente, lo tenemos que registrar nosotros.

**Guarda anti-secreto:** todo pasa por `redact()` al escribir **y al leer**. Redactar solo al
escribir deja pasar lo que ya estaba guardado de una versión anterior.

## 8. Ruteo de modelos

| Peldaño | Tarea | Modelo | Por qué |
| --- | --- | --- | --- |
| T0 | Comando explícito (`/p venta ayer`) | **ninguno** | Determinista, instantáneo, no puede dispararse |
| T1 | Frase que resuelve por palabras clave | **ninguno** | Cubre la mayoría sin costo ni riesgo |
| T2 | Frase ambigua → `(intent, params)` | Ollama cloud, escalón gratuito | Suficiente para clasificar contra un enum cerrado |
| T3 | Pregunta difícil o multi-consulta | Modelo de pago | Solo cuando T2 no resuelve |

**Ollama local no está instalado en ningún box** (verificado con `ollama list`). El peldaño
local no existe hoy; queda fuera de la v1 y se añade si aparece una razón concreta.

**Tope en dólares, no en turnos.** Un contador de turnos no protege de un turno desbocado; el
costo se acumula en `budget_day.costo_usd` y el agente **rehúsa** al llegar al tope, diciendo
explícitamente por qué.

## 9. Modos de fallo

| Fallo | Detección | Respuesta |
| --- | --- | --- |
| Portal caído | `/api/health` 503 | No intentar login. Avisar una vez. |
| Sesión expirada a mitad de ráfaga | 401 en una consulta | Un relogin, dentro del presupuesto. Si falla, terminal. |
| **Contraseña caducada (día 30)** | 401 en todas las rutas | Terminal. **Avisar a 7 y 2 días de la caducidad**, antes de que rompa. |
| Cuenta desactivada | 403 | Terminal. Es revocación deliberada. |
| Rate limit agotado | 429 | Respetar `Retry-After`. Nunca reintentar antes. |
| El portal cambia la forma de una respuesta | Contrato por intent validado al recibir | Rehusar y reportar el intent roto, no responder con basura. |
| Modelo inventa cifras | Grounding | Rehusar la respuesta. |
| Rango sin dato | `updatedAt` nulo o vacío | «No hay dato para ese rango», con la última fecha de corte. |

## 10. Rollback

Cada pieza es reversible sin tocar producción:

- **Del agente:** desactivar el timer/servicio. El reporte diario actual sigue igual.
- **De la cuenta:** desactivarla en `/admin/usuarios` — 401 en menos de 10 segundos.
- **Del catálogo:** revertir el commit. El catálogo es versionado, así que el rollback es
  `git revert`.
- **Nada que revertir en la base de datos**, porque el agente no la toca. Ese es el beneficio
  de haber elegido la API.

## 11. Verificación manual

- [ ] `curl -s -o /dev/null -w '%{http_code}' "$BASE/api/health"` → `200`
- [ ] `curl -s ifconfig.me` en el box y en una máquina de oficina → **comparar**
- [ ] Confirmar si `AUDIT_IP_HMAC_SECRET` está definido (sin imprimir el valor)
- [ ] Con la cuenta del agente: `/api/admin/users` → 401/403
- [ ] Desactivar la cuenta y cronometrar hasta el primer 401 → < 10 s
- [ ] Provocar un fallo de login y confirmar que **no hay segundo intento inmediato**
- [ ] Reiniciar el proceso y confirmar que el contador de fallos **sobrevivió**
- [ ] Lanzar dos procesos a la vez y confirmar **un solo** login
- [ ] `grep -riE 'vp_session|password' var/*.db logs/` → vacío

## 12. Tests

| Categoría | Qué prueba |
| --- | --- |
| `test_portal_client` | Allowlist de método y ruta; timeouts; que no exista camino a un no-GET |
| `test_portal_budget` | Presupuesto persistido; sobrevive al reinicio; dos procesos = un login |
| `test_portal_errors` | Taxonomía 401/403/429/503/timeout; que 401 sea terminal |
| `test_dateparse` | Bordes de día en ambas zonas horarias |
| `test_catalog_ask` | `drop_fields` obligatorio; catálogo sin él → error de carga |
| `test_grounding` | Cifra inventada → rechazo; cifra presente → aceptación |
| `test_redaction` | Cookie, contraseña, `x-csrf-token`, JSON con `password` |
| `test_state` | Lectura de las cuatro tablas, no solo escritura |
| Regresión | Los 124 tests actuales, verdes, y el reporte diario sin un carácter de cambio |
