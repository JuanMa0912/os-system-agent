# Spec 006 — El agente responde preguntas de negocio contra GCP

> Estado: **borrador para aprobación**. Diseñada a partir de un reconocimiento de 5 agentes
> sobre el código real, un panel de 3 arquitecturas independientes y dos críticos
> adversariales. El expediente completo está en `_diseno-sintesis.md`,
> `_critica-seguridad.md` y `_critica-completitud.md` de esta misma carpeta.

## 1. Problema

Hoy el agente responde **una** pregunta: *«¿corrió el job?»*. La señal es systemd, y el
reporte llega a Telegram. Pero el operador no pregunta eso: pregunta *«¿cuánto vendimos ayer
en tal sede?»*, *«¿qué margen dejó la categoría X?»*, *«¿por qué cayó la rotación?»* — y para
saberlo entra al portal, o entra al servidor.

El dato para responder ya existe: vive en el PostgreSQL de Cloud SQL que alimenta el portal
`visor-productividad`. Lo que falta es un camino **seguro** entre una pregunta en Telegram y
ese dato.

Hay un antecedente que esta spec tiene que respetar: el pull interactivo `/estado` se intentó
y se **parqueó** (`specs/002-estado-etl-pull/tasks.md`). No falló por falta de ganas — falló
porque `command-dispatch` no alcanzaba las herramientas MCP y el modelo gratuito se colgaba,
pedía parámetros o se disparaba. Repetir el mismo intento con otra pregunta daría el mismo
resultado. Por eso el núcleo de esta spec es **determinista**: el modelo no escribe SQL.

## 2. Actores

| Actor | Papel |
| --- | --- |
| Operador humano | Pregunta por Telegram. Único destinatario allowlisted en la v1. |
| OS_SYSTEM_AGENT | Traduce la pregunta a una consulta del catálogo, la ejecuta, redacta la respuesta. |
| OpenClaw Gateway | Canal y orquestación. Corre en el app-server, no en un portátil. |
| Cloud SQL (GCP) | Origen del dato de negocio. Producción compartida con el portal. |
| app-server (`232`) | Hogar del agente. Su IP ya está autorizada en Cloud SQL. |
| DBA | **Único** que ejecuta DDL. El agente jamás lo hace. |

## 3. Journeys

**J1 — Pregunta cerrada (el 80 %).** El operador escribe `/p venta ayer <sede>`. El router
determinista resuelve el intent, compila el SQL desde el catálogo, lo ejecuta contra una vista
de solo lectura y responde con cifras y su fecha de corte. Sin modelo en el lazo.

**J2 — Pregunta abierta.** El operador escribe en lenguaje natural. Un modelo traduce la
frase a `(intent, parámetros)` — **nunca a SQL** — y a partir de ahí el camino es el de J1.
Si no hay intent que encaje, el agente lo dice y registra la falla en `ask_intent_miss`.

**J3 — El dato no está.** La ventana pedida no tiene carga. El agente responde *«no hay dato
para ese rango»* con la fecha del último corte, en vez de un número pequeño y falso.

**J4 — Escalada.** Ninguna pregunta puede provocar escritura. Cualquier operación que cambie
estado sigue el flujo de aprobación de `CLAUDE.md §17`, fuera de esta spec.

## 4. Requisitos funcionales

- **RF-01** El agente responde preguntas de negocio desde Cloud SQL vía Telegram.
- **RF-02** El SQL **no lo escribe el modelo**. Sale de un catálogo declarativo
  (`{column, op, param}`) compilado con `psycopg.sql`; los parámetros van ligados, nunca
  interpolados.
- **RF-03** Toda consulta lleva `LIMIT` y un techo de tiempo. Sin excepción.
- **RF-04** Toda respuesta declara **fecha de corte del dato**. Un número sin su fecha es un
  número engañoso.
- **RF-05** Si el rango pedido no tiene dato, se dice explícitamente (J3). No se devuelve un
  agregado parcial como si fuera completo.
- **RF-06** Ninguna cifra puede aparecer en la prosa si no está en el resultado de la consulta
  (verificación mecánica, no confianza en el modelo).
- **RF-07** Multi-modelo por escalones: lo determinista primero; el modelo solo para entender
  la frase. El escalón de pago tiene tope **en dólares**, no en número de turnos.
- **RF-08** Memoria persistente en SQLite (`var/state.db`): incidentes con historia,
  preguntas fallidas, presupuesto consumido y un ledger append-only.
- **RF-09** Todo lo que se escriba a memoria pasa por `redact()` **al escribir y al leer**.

## 5. Requisitos no funcionales

- **RNF-01** Latencia objetivo J1 < 5 s.
- **RNF-02** El agente nunca abre más de 3 conexiones concurrentes a producción.
- **RNF-03** Toda invocación deja rastro de auditoría con quién, qué intent y qué costo.
- **RNF-04** Fail-closed: sin catálogo, sin credenciales o sin recibo de privilegios válido,
  el agente **no arranca**.

## 6. Frontera de seguridad

El requisito del operador es literal: **no puede borrar nada**. Eso se garantiza con capas,
no con instrucciones al modelo — un prompt no es un control.

| Capa | Control |
| --- | --- |
| Sistema operativo | Usuario Linux propio, sin `sudo`, sin escritura fuera de su carpeta. |
| systemd | `ProtectSystem=strict`, `ReadWritePaths` solo su carpeta, `NoNewPrivileges`, `PrivateTmp`. |
| Base de datos | Rol `os_agent_ro`: `SELECT` **solo sobre vistas `agente_ro.v_*`**, nunca sobre tablas base. |
| Base de datos | `default_transaction_read_only=on`, `statement_timeout`, `idle_in_transaction_session_timeout`, `CONNECTION LIMIT 3`, a nivel de **rol**. |
| Aplicación | El modelo no emite SQL. Catálogo declarativo + `psycopg.sql`. |
| Canal | `allowFrom` de un solo operador; `requireMention` en grupo. |

### 6.1 Hallazgo bloqueante: las funciones `refresh_*` son ejecutables por cualquiera

Verificado con comando sobre el repo del portal:

- **Cero `REVOKE`** en todo `db/`. PostgreSQL otorga `EXECUTE` a `PUBLIC` por defecto al
  crear una función, así que basta con tener `CONNECT` para poder invocarlas.
- **12 migraciones** fijan `SET statement_timeout = 0` dentro de funciones `refresh_*`
  (por ejemplo `db/migrations/20260618_rotacion_refresh_timeouts.sql:9`). Ese `SET` de nivel
  de función **sobrescribe** el timeout del rol.

Consecuencia: un rol «de solo lectura» podría llamar `SELECT refresh_...()` y consumir CPU de
producción **sin techo de tiempo**, contra la misma instancia que sirve el portal, hasta
fallar en el primer `INSERT` por falta de privilegio. El daño ya está hecho para entonces.

Un test que solo comprueba «la función lanza excepción» **pasa en verde** y no detecta nada.

**Control exigido, antes de crear el rol del agente:**

```sql
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION <cada refresh_*> TO <rol_del_sync>;
```

Y la verificación debe (a) exigir **cero filas** en `information_schema.routine_privileges`
para el rol del agente, y (b) medir **tiempo transcurrido** (`elapsed < 1s`), no solo que se
lance la excepción.

## 7. Criterios de aceptación

- **CA-01** `UPDATE ... WHERE false` con el rol del agente lanza `25006`, **y** vuelve a
  fallar (`42501`) tras `SET default_transaction_read_only = off` — sin esta segunda mitad la
  prueba es decorativa.
- **CA-02** `SELECT` sobre una tabla base falla; sobre la vista correspondiente, pasa.
- **CA-03** `SELECT refresh_*()` falla **en menos de 1 segundo**.
- **CA-04** `pg_has_role(current_user, 'pg_read_all_data', 'member')` es falso.
- **CA-05** Un parámetro con intento de inyección es rechazado por el compilador, no
  escapado.
- **CA-06** Una pregunta sobre un rango sin dato devuelve J3, no un agregado parcial.
- **CA-07** Ninguna respuesta contiene una cifra ausente del resultado de la consulta.
- **CA-08** El reporte diario actual no cambia ni un carácter: `diff` vacío antes y después.
- **CA-09** `uv run pytest` verde, incluidos los 124 tests existentes.

## 8. Fuera de alcance

- Ejecución de ETLs desde la conversación (es Fase 2, `specs/004`).
- Escritura en cualquier base de datos.
- WhatsApp.
- Cualquier dato personal nominal: cédula, nombre, cargo o incidencia de un empleado. La
  productividad se expone **agregada** por sede y departamento, con umbral mínimo de grupo.
- Búsqueda semántica sobre la memoria en la v1.

## 9. Supuestos a confirmar

| # | Supuesto | Cómo se confirma |
| --- | --- | --- |
| S1 | El app-server alcanza Cloud SQL con el proxy | `gcloud sql instances describe` + un `cloud-sql-proxy` con usuario IAM desechable |
| S2 | La instancia usa IP pública con redes autorizadas, no IP privada/VPC | igual que S1 |
| S3 | Versión de OpenClaw instalada y modelos disponibles | `openclaw --version`, `ollama list` |
| S4 | `memory.search` está apagado | `openclaw config get memory.search` |
| S5 | La contraseña filtrada de `produ` fue rotada | consulta al DBA |

**S1 y S2 son bloqueantes y se verifican en M0.5, antes de escribir una línea de compilador.**
Si la instancia es de IP privada, la sección de acceso a GCP cambia por completo.
