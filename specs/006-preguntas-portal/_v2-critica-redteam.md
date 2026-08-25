He verificado el plan contra los dos repos. Ejecuté la suite (124 tests, verde), leí los handlers citados y probé `redact()` en vivo. Lo que sigue está ordenado por daño.

---

## 1. El intent estrella `venta_dia_sede` devuelve **siempre 0** — y ningún control del plan lo detecta

`mode=summary` de `/api/ventas-x-item/v2` **hardcodea a cero** justo los dos campos que el plan suma:

```sql
0::float8 AS und_acum,
0::float8 AS venta_sin_impuesto_acum
```
`visor-productividad-master/src/app/api/ventas-x-item/v2/route.ts:355-356`

El catálogo del plan (§4, ejemplo 2) declara `venta_neta: $.rows[*].venta_sin_impuesto_acum` y `unidades: $.rows[*].und_acum`. La respuesta a *«cuánto vendimos ayer»* sería **«0 COP, 0 unidades. Corte: 2026-08-24»**. Y es el caso que el plan llama «el riesgo dominante: un número real, pequeño y falso».

Peor: **los tres controles diseñados para esto lo dejan pasar.** `PortalContractError` (control #17) sólo dispara si el JSONPath *no resuelve* — aquí resuelve. `ground.py` (control #16) exige que la cifra esté en el JSON — el 0 está. El `CHECK (verdict<>'ok' OR cut_off IS NOT NULL)` (control #18) se cumple. Los tres verifican *presencia*, ninguno verifica *semántica*.

**Corrección:** los campos correctos en `mode=summary` son `venta_sin_impuesto_dia` y `und_dia` (`:365-366`, agregados por `SUM(...) GROUP BY empresa,fecha_dcto,id_co,id_item`). Y añadir a M8 una regla que ninguna versión del plan tiene: cada `figure` necesita un **caso dorado con valor esperado no trivial** medido contra el portal real, no sólo «el JSONPath existe». Un contrato que sólo comprueba claves no distingue un endpoint sano de uno que devuelve ceros.

---

## 2. `oc_vencidas` responderá **403**: la cuenta que el plan especifica no puede leer órdenes de compra

`/api/ordenes-compra` exige `canAccessOrdenesCompra(role, allowedDashboards, allowedSubdashboards)` → 403 (`src/app/api/ordenes-compra/route.ts:58-72`), y `ordenes-compra` es **opt-in**: no se hereda de `null` (`src/lib/shared/portal-sections.ts:249-252`).

El plan §2 lista los subtableros concedidos: `margenes, informe-variacion, mix-y-linea, participacion-comercial, ventas-x-item, analisis-de-inventario`. **`ordenes-compra` no está.** Pero §4 lo mete en `allowed_paths`, lo usa como «el intent de negocio más limpio de la API» (ejemplo 3), y M11 lo convierte en criterio de aceptación:

> `echo 'cuantas oc estan vencidas' | ask_cli --json` → `ok` con `cut_off` y `figures.vencidas`

Ese test falla con 403 el día que se ejecute. Y `verify_portal_user.py --strict` (M4) lo detectaría como «permiso que falta» **un hito antes**, bloqueando el arranque — es decir, la contradicción se manifiesta primero como un fallo de arranque inexplicable.

**Corrección:** decidir explícitamente y escribirlo en 10.2: o se concede `ordenes-compra` (marcándolo como opt-in, no basta con no negarlo), o `oc_vencidas`/`oc_cumplimiento` salen de M11 y `/api/ordenes-compra` sale de `allowed_paths`. Igual con `mix-y-linea`: se concede como subtablero y no tiene ni ruta ni intent.

---

## 3. `frescura_portal` — el primer lazo cerrado (M7) responde una cifra sin significado y un sello engañoso

Dos defectos verificados en `src/lib/shared/portal-freshness.ts:96-115`:

- **`sources` tiene siempre exactamente 3 entradas**, construidas literalmente en el código. El plan renderiza `{fuentes} fuentes reportando` con `kind: count` sobre `$.sources`. Eso imprime **«3 fuentes»** siempre — incluso con las tres `at: null`. Es una constante disfrazada de medición, y es *la cifra* del test de aceptación de M7.
- **`updatedAt = pickLatestIso(...)` es un MÁXIMO** de tres señales heterogéneas. Una de ellas (`horas`) no es una fecha de dato: sale de `GREATEST(last_vacuum, last_autovacuum, last_analyze, last_autoanalyze)` sobre `pg_stat_user_tables` (`:88-93`). Un autovacuum sin datos nuevos **rejuvenece el sello del portal entero**. Para una pregunta de frescura, el MAX es exactamente el operador equivocado: dice «todo fresco» cuando sólo una de tres lo está.

**Corrección:** la cifra útil es `sources[?at != null]` (fuentes que reportan) y el sello honesto es el **mínimo** de los `at` no nulos, más el nombre de la fuente más atrasada. Y quitar `horas` del cálculo o etiquetarla como lo que es. Si M7 va a ser «la primera respuesta correcta», que la respuesta sea correcta.

---

## 4. Zona horaria: no aparece **una sola vez** en el plan, y hoy todo el repo corre en UTC

Verificado: `datetime.now(UTC)` en los cinco entrypoints (`scripts/alert_incidents.py:109`, `send_daily_report.py:95`, `collect_etl_status.py:41`, `check_etl_freshness.py:42`, `src/os_system_agent/mcp_server.py:53`). Cero apariciones de `ZoneInfo`/`America/Bogota` en `src/` o `scripts/`.

El plan introduce `ask/dateparse.py` («ayer, antier, mes pasado, últimos 7 días») con `now` inyectado, el cruce `rango pedido vs. cut_off` como control #16, y `budget_day.dia`. Nada declara la zona. Con UTC-5, **entre las 19:00 y las 24:00 hora local «ayer» resuelve al día equivocado** — cinco horas al día, justo la franja en que un gerente pregunta por el cierre. Y el portal formatea en `America/Bogota` (`src/lib/shared/portal-freshness.ts:30`), así que el `cut_off` que devuelve y la fecha que el agente calcula no viven en el mismo calendario.

**Corrección:** `OS_AGENT_TZ=America/Bogota` como configuración obligatoria fail-closed; `dateparse` recibe `now` **ya localizado** y devuelve fechas civiles; `budget_day.dia` y la retención de 90 días en hora local. Test en M6: `pytest -k "ayer_a_las_2300_local"` con `now = 2026-08-25T23:30-05:00` → `desde == hasta == 2026-08-24`. Sin esto, M10 puede dar 0.90 de acierto de intent y responder el día equivocado.

---

## 5. El sandbox de OpenClaw está **`off`** en el box, y el plan no lo menciona ni una vez

`docs/openclaw-phase1-runbook.md:88-96`:

> `agents.defaults.sandbox.mode` es **`off`**. Docker Desktop no tenía integración WSL en este host […] pesado para un box de 8 GB.
> **Re-enable a sandbox before Phase 2 (autonomous execution) or before exposing any channel.**

El plan abre un canal de Telegram (M12) que hace `execFile` de un proceso que sostiene la **contraseña del portal en el entorno**, con 27 controles de seguridad y 17 hitos, y **ninguno es el sandbox**. Choca de frente con `CLAUDE.md §8.1-8.2` («habilita sandboxing antes de dar acceso amplio», `mode="all"` o al menos `"non-main"`) y con `§16` (checklist bloqueante: «Sandbox enabled for tool execution»).

`openclaw security audit --deep` da **0 critical hoy con el sandbox apagado** (runbook:74-80), así que la verificación de M16 (`0 critical`) **no detecta esto**.

**Corrección:** un hito propio antes de M12 — reactivar el sandbox o documentar por escrito la excepción con su compensación, y añadir a M16 un chequeo explícito: `openclaw config get agents.defaults.sandbox.mode | grep -qv off`. Reusar el `0 critical` como prueba de que el sandbox está bien es exactamente el «contarlo dos veces» que el propio plan denuncia en §7.

---

## 6. Ollama **local** se asume instalado; nunca lo estuvo, y el argumento de privacidad de T2 depende de él

Todo lo configurado y probado en este proyecto es **ollama-cloud**: `openclaw models set ollama-cloud/gpt-oss:120b`, `https://ollama.com` (`docs/openclaw-phase1-runbook.md:25-42, :102`). `127.0.0.1:11434` aparece únicamente como *propuesta* en `specs/006-preguntas-gcp/_diseno-sintesis.md:483`, jamás en el runbook de estado.

El plan pone en T2 un modelo local ≤4B con la justificación fuerte de que **«cada pregunta lleva nombres de sede y de empresa: el texto no sale del box»** — y el box tiene **8 GB** (runbook:91). S5 sólo pide `ollama list`, que asume que el demonio existe. Si no existe, T2 cae a T3 (ollama-cloud) y **el texto sí sale**, silenciosamente, sin que ningún control lo advierta.

**Corrección:** M0 debe verificar tres cosas, no una: `curl -sf http://127.0.0.1:11434/api/tags` (¿hay demonio?), `ollama list` (¿qué modelo?), y `free -m` + `ollama ps` durante una inferencia (¿cabe en 8 GB junto al gateway?). Y en el código: si T2 no está disponible, **el router no debe escalar a T3 en silencio** — debe declararlo en la respuesta igual que hace con el presupuesto agotado (`degradado_motivo='sin modelo local: la pregunta salió del box'`). Hoy el plan sólo declara la degradación por dinero, no por privacidad.

---

## 7. `registerCommand()` nunca se ha verificado que exista, y M0 no lo comprueba

Toda la vía determinista (M12, y con ella pedido (a)) descansa en `api.registerCommand()`. Su única evidencia en el repo es una **recomendación de trabajo futuro**: `specs/002-estado-etl-pull/tasks.md:108-112` («A reliable interactive `/estado` needs either a deterministic OpenClaw plugin (`registerCommand()`, no model) or a paid model — both are future work») y `docs/openclaw-phase1-runbook.md:217`. Nadie ha instalado un plugin.

S5 lo lista («existencia de `registerCommand` en ella»), pero las verificaciones ejecutables de M0 son `openclaw --version`, `ollama list`, el FTS5 y los `grep` del `.env` — **ninguna toca la API de plugins**. El supuesto se declara y no se cierra. Si `registerCommand` no existe o cambió de firma en la versión instalada, M12 se cae entero y con él el argumento de «cero tokens, nada que inyectar».

**Corrección:** M0 debe ejecutar `openclaw plugins --help`, `openclaw plugins validate` sobre un plugin de tres líneas que sólo registre `/ayuda`, e instalarlo con `-l`. Cinco minutos. Y si no existe: la salida es MCP + `/p` por el modelo, que es la vía que ya falló en la 002 — hay que saberlo **antes** de M1, no en M12.

---

## 8. La «memoria persistente real» (pedido d) es **de sólo escritura**: nada la lee

`§6` diseña `incident_note`, `operator_pref`, `ask_intent_miss`, `ask_feedback` con sus CHECK y su trigger. Pero:

- No hay **ningún intent, ninguna herramienta y ningún peldaño del router** que consulte `incident_note` u `operator_pref`. La pregunta *«¿qué pasó la última vez con este job?»* —que es la que justifica tener memoria— no tiene camino.
- El plan **eliminó `incident_note_fts`**, que sí estaba en el diseño previo (`specs/006-preguntas-gcp/_diseno-sintesis.md`, tabla `incident_note_fts` con `tokenize='unicode61 remove_diacritics 2'`). Conservó el FTS de `ask_intent_miss`. Es decir: se puede buscar en las preguntas que **fallaron**, no en lo que el operador **aprendió**.
- Los únicos lectores nombrados son offline: `ask_report.py` y `ask_backlog.py` (M16).

**Corrección:** un intent `historial_incidente` (dominio `meta`, sin salida al portal, `cut_off = incident.last_seen_at`) que lea `incident` + `incident_note` por `job_id`, y devolver `incident_note_fts`. Y un criterio en M16: al menos una pregunta del set de evals se responde **desde memoria y sin tocar el portal**. Sin eso, `state.db` es un log de auditoría, no memoria — y el pedido (d) queda a medias.

---

## 9. El presupuesto de turno (30 s) hace **imposible** cualquier intent `heavy`

Cuatro números del propio plan que no encajan:

| Dónde | Valor |
|---|---|
| `defaults.timeout_seconds` (§4) | 20 |
| `httpx.Timeout(read=20)` (§3) | 20 |
| presupuesto de turno con reloj monótono (§3) | 30 s |
| reintentos ante *timeout de lectura* (§3, tabla) | **0** |

Y del portal: `DB_STATEMENT_TIMEOUT_MS` por defecto **800 000 ms** (`src/lib/db/index.ts:107`), `maxDuration=120` en informe-variacion, `HEAVY_MODE` en margenes/data, y `analisis-de-inventario` con un 504 explícito propio.

Consecuencia: `margen_sede` y `sobrestock_di` (los dos intents pesados de M11) **nunca pueden completarse** si tardan >20 s, y con 0 reintentos el veredicto será `error_portal` permanente. Además el plan late (`heartbeat`) *antes y después* de un `heavy: true` — un latido después de un timeout de 20 s es inútil, la petición ya murió.

**Corrección:** `heavy: true` debe implicar un presupuesto propio (p. ej. `read=45`, turno 60 s) declarado en el catálogo y validado al cargar (`timeout_seconds <= turn_budget`), o esos intents se marcan como **asíncronos**: el agente responde «lo estoy calculando» y entrega por Telegram al terminar. Elegir una de las dos, pero no dejar tres timeouts que se contradicen.

---

## 10. El coste de T4 («$24/mes») no está respaldado: el caché de Anthropic no sobrevive a un proceso por turno

El plan afirma en §5: *«~4-6k tokens de entrada (≈90 % cacheados) + ~500 de salida ≈ $0,02–0,03»* → `OS_ASK_T4_DAILY_MAX_USD = 0.80` ≈ **$24/mes**.

Pero §1 fija la arquitectura: **«Un proceso por turno, sin demonios nuevos»**. El caché de prompt de Anthropic es del lado servidor con TTL de 5 minutos y la *escritura* cuesta más que el input normal. Preguntas de Telegram separadas por más de 5 minutos —que es el caso normal— pagan **escritura de caché en cada turno**, no lectura. El 90 % cacheado es la excepción, no la media, y el propio test de M14 (`cache_read_input_tokens > 0` en el segundo turno) sólo pasa si los dos turnos van seguidos: **verifica el mejor caso y se leerá como si verificara el caso típico**.

**Corrección:** o medir el coste real durante la semana en sombra antes de fijar el tope (10.8 se aprueba *después* de M16, no antes de M14), o eliminar el prefijo cacheado del cálculo y presupuestar sobre el peor caso. Y cambiar el test de M14 a lo que importa: `budget_report --desde -7d | jq -e '.t4.costo_usd_medio_por_turno <= 0.05'` con turnos reales espaciados.

---

## 11. Hitos cuya «verificación ejecutable» no verifica lo que dice

- **M2:** `assert len(yaml.safe_load(open('redaction_cases.yaml')))>=20`. Cuenta **entradas del fichero**, no cobertura ni aciertos. Veinte casos triviales pasan. Y falta el caso que yo mismo comprobé como roto: `redact('{"password": "hunter2xyz"}')` devuelve el texto **intacto** (ejecutado contra `src/os_system_agent/redaction.py:26-29`; `Cookie: vp_session=…` y `Set-Cookie:` tampoco se enmascaran — `x-csrf-token:` sí, por casualidad, porque el nombre contiene «token»). **Corrección:** el criterio debe ser `pytest -k redaction` con un caso *por patrón nuevo nombrado*, no un conteo.
- **M4:** *«prueba de revocación cronometrada: desactivar en /admin/usuarios y confirmar 401 en <10 s»*. No hay comando. Es el control #27, el kill-switch, y su verificación es prosa. **Corrección:** `scripts/verify_portal_user.py --watch-401 --timeout 15` que haga polling a un endpoint de datos e imprima el delta en segundos; exit≠0 si supera 10.
- **M13:** `adversarial_probe --expect-obeyed 0 --expect-exfil 0`. El plan lo llama «criterio observable, no interpretativo», pero **la herramienta que define «obeyed» la escribimos nosotros en el mismo hito**. Es autorreferencial. **Corrección:** el criterio observable de verdad es el que el propio plan enuncia y luego no usa: **cero peticiones HTTP fuera del `endpoint_id` del intent esperado**, medible desde `http_budget`/`ask_turn` sin juicio semántico.
- **M16:** `cobertura >= 0.80`. El denominador no está definido en ninguna parte. ¿Preguntas totales? ¿Preguntas distintas? ¿Excluyendo repetidas? **Corrección:** definirlo en el esquema — `cobertura = turnos con verdict IN ('ok','cached') / turnos totales excluyendo 'fuera_de_alcance'` — o el número se negocia el día que se mide.

---

## 12. Modo de fallo no contemplado: el portal responde **200 con datos parciales** por recorte silencioso de ventana

El plan cubre 200-con-`error` (R8), 200-con-`message` (sin dato) y `updatedAt: null` (indeterminado). No cubre el tercer 200 mentiroso: **`/api/rotacion` recorta a 93 días en silencio** (R17, `src/app/api/rotacion/route.ts:521`) y devuelve `meta.effectiveRange` distinto del pedido. La regla #10 del loader (`max_span_days > defaults.max_window_days` → `CatalogError`) previene que *nosotros* pidamos de más, pero no cubre el caso general: cualquier endpoint que devuelva un `range`/`effectiveRange` propio puede haber servido menos de lo pedido, y el agente reportaría el agregado como si fuera del rango completo.

Nótese que `/api/ventas-x-item/v2` **también devuelve `range: {start, end}`** en las tres ramas (`:503-504`, `:384`, `:452`) — es decir, la información para detectarlo está ahí y el plan no la usa.

**Corrección:** un campo `response.served_range` obligatorio (como `cut_off`) cuando el endpoint lo expone, y una regla en `ground.py` hermana del cruce rango-vs-corte: **si `served_range != rango pedido`, la respuesta declara el rango realmente servido o rehúsa**. Es tres líneas y cierra una familia entera de números falsos.

---

## 13. La frontera de seguridad no es tan uniforme como afirma §7 control #1

El plan sostiene que negar subtableros hace que *«es el portal quien responde 403, no nuestro código»*. Es cierto para los que verifiqué con gate de subsección (`analisis-de-inventario:178-183`, `participacion-comercial:59-64`, `ventas-x-item/v2:118-125`, `margenes/data:362-364`, `margenes/meta:67-69`) — pero **no es universal**:

- `/api/informe-variacion/meta` sólo llama `requireAuthSession()`; no hay `canAccessPortalSubsection` en el fichero (`src/app/api/informe-variacion/meta/route.ts:35`). Conceder o negar el subtablero `informe-variacion` **no cambia nada** ahí.
- `/api/portal/freshness` igual (`:11`), lo cual está bien pero conviene saberlo.

O sea: la afirmación «un allowlist de método en nuestro cliente es un `if`; una cuenta sin esa sección es una negativa del servidor» es verdadera por endpoint, no por diseño. Y `src/proxy.ts:109` confirma que no hay chokepoint donde apoyarse (R18, verificado).

**Corrección:** `verify_portal_user.py --assert-denied` debe ampliarse a **una ruta por cada subtablero negado** (no sólo las cuatro de admin/PII) y correr en cada arranque. Lo que no aparezca ahí como 403 comprobado, no se puede contar como «impuesto por el servidor» en la aprobación de Fase 1.

---

## 14. Dependencias asumidas sin verificar (más allá de S1-S8)

- **Migraciones del portal aplicadas en el destino.** M4 es `high` + aprobación humana y crea la cuenta con `allowedEmpresas` y `portalProfile`. El `POST /api/admin/users` hace detección de columnas en caliente y responde 400 pidiendo la migración si falta (`src/app/api/admin/users/route.ts:655-695`, tres INSERT alternativos según qué columnas existan). El plan no verifica `20260723_dinastia_tenant_tables.sql` ni `20260704_app_users_portal_profile.sql` antes del hito de riesgo alto. **Añadir a M0:** `\dt`/`\d app_users` y comprobar `allowed_empresas`, `portal_profile`, `password_changed_at`.
  *(Nota buena: `password_changed_at` tiene `SET DEFAULT now()` — `db/migrations/20260701_app_users_password_policy.sql:7` — así que la cuenta recién creada **no** nace en `passwordChangeRequired`, siempre que esa migración esté aplicada. Si no lo está, el bot no autentica nunca y el síntoma es un 401 indistinguible.)*
- **`httpx` — esta sí está verificada y el plan acierta:** `uv.lock:263-274` la resuelve en 0.28.1 vía `mcp` (`uv.lock:407-413`), con `anyio`/`httpcore` ya presentes. Cero árbol nuevo. Confirmado.
- **`mypy` no lo invoca ningún step de CI** — confirmado leyendo `.github/workflows/ci.yml` (ruff check, ruff format, pytest, gitleaks; nada más), con `[tool.mypy] files=["src"]` configurado en `pyproject.toml:78-87`. El plan lo corrige en M1; correcto.
- **`var/` no está en `.gitignore` hoy** (`audit-ledger.jsonl` sí, por patrón sin barra). M1 lo cubre; verificado que hace falta.

---

## 15. Lo que del pedido queda sin cubrir, en una línea cada uno

| Pedido | Estado |
|---|---|
| **(a)** OpenClaw responde preguntas del portal | **En riesgo** — depende entera de `registerCommand()` no verificado (#7) y del sandbox apagado (#5) |
| **(b)** Telegram ya existe | **Cubierto** — `notify.py` + `config/openclaw.example.json` reutilizados sin tocar; el plan acierta |
| **(c)** LLMs de distintos calibres | **Parcial** — T3/T4 (ollama-cloud, Anthropic) son reales; **T2 local no está instalado** (#6) |
| **(d)** Memoria persistente real | **Parcial** — el esquema es sólido, pero **nadie la lee** (#8) |
| **(e)** Que el agente no pueda borrar nada | **Parcial** — bien acotado en el carril del portal, pero el agente **conserva SSH al 232** (`src/os_system_agent/ssh_client.py`, allowlist read-only) y corre **sin sandbox**; el plan no vuelve a declarar esa superficie |

---

**Los tres que arreglaría antes de escribir una línea de código:** el `und_acum = 0` (#1), porque invalida el ejemplo canónico y demuestra que los controles verifican presencia y no semántica; el 403 de órdenes de compra (#2), porque es una contradicción interna entre §2 y §4 que sale a la luz como fallo de arranque; y la zona horaria (#4), porque no está en ninguna parte del documento y contamina fechas, presupuesto y retención a la vez.