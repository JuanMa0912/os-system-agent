# Spec — 005 Señal de destino: ¿llegó el dato?

## Problem

Fase 1 monitorea **systemd**: responde *"¿corrió el job y con qué exit code?"*. No
responde *"¿llegó el dato?"*. Esas dos preguntas se separan más seguido de lo
cómodo, y cuando se separan el monitoreo actual dice **verde**.

Caso real, verificado en server232 el 2026-08-03/04 — `visor-etl-sync.service`
refresca los rollups de margen en GCP así:

```bash
"${GCP_PSQL[@]}" -c "SELECT refresh_margen_final_roll('$DESDEC','$HASTAC');" >/dev/null 2>&1 \
  || { log "WARN: refresh de margen_final_roll fallo; el tablero de margenes puede quedar atrasado."; return 0; }
```

Se traga el error y **devuelve 0**. systemd reporta `Result=success`, el agente
reporta OK, y el tablero de márgenes puede quedar con datos viejos sin que nadie
se entere. Agravante: el único rastro sería `/var/log/visor-etl-sync.log`, que es
`root:root 0644` mientras el servicio corre como `prodapp` — no puede escribirlo, y
no tiene una línea desde 2026-07-03.

El mismo patrón aplica a cualquier ETL que termine en 0 habiendo cargado 0 filas,
o que cargue el día equivocado.

## Actors

- **Human operator (Juan)** — recibe el reporte/alerta; decide si se re-corre.
- **OS_SYSTEM_AGENT** — corre en MMAUTOML01, ya monitorea systemd por SSH.
- **produXdia @ server232** — Postgres 18.3, destino local de los ETLs.
- **GCP (produxdia)** — destino final del `sync`; alimenta los tableros.
- **Telegram** — mismo canal y mismo formato que hoy.

## User journeys

1. **El job corrió y el dato llegó.** systemd OK **y** la tabla destino tiene el
   día esperado → INFO, una sola línea, como hoy.
2. **El job corrió pero el dato NO llegó.** systemd `success` pero la tabla sigue
   en un día viejo → **CRITICAL**, diciendo explícitamente que la unit reportó
   éxito. Es el caso que hoy pasa callado.
3. **El dato llegó pero está flaco.** La tabla tiene el día esperado con un conteo
   muy por debajo de lo normal (p.ej. el POS no cerró y cargó 0 filas) → WARNING.
4. **El destino no se puede consultar.** Timeout/credenciales → **una** alerta
   distinta de "no puedo verificar el destino", nunca un falso CRITICAL por job.

## Functional requirements

- **FR-1** Un job del catálogo puede declarar una **verificación de destino**: qué
  tabla, qué columna de fecha, y qué día se espera (típicamente D-1).
- **FR-2** El estado final de un job combina las dos señales. La regla es
  conservadora: **gana la peor**. systemd OK + destino atrasado = CRITICAL.
- **FR-3** La evidencia debe nombrar las dos señales por separado, para que el
  reporte diga *por qué* está mal. Ej:
  `visor-etl-sync.service: success, last run 08:11 · margen_final_roll: max
  fecha_dcto=20260721, se esperaba 20260803 (13 días atrás)`.
- **FR-4** Verificación de volumen **opcional** por job: mínimo de filas para el
  día esperado; por debajo → WARNING.
- **FR-5** Un fallo al consultar el destino es su propia condición, **no** se
  traduce a CRITICAL por cada job (evitar el "7 criticals confusos" de la 003).
- **FR-6** Los jobs sin verificación declarada siguen funcionando exactamente
  igual que hoy (solo systemd). La adopción es incremental, job por job.
- **FR-7** El destino puede ser el Postgres local (232) o el de GCP; un job puede
  declarar verificaciones en ambos (el `sync` es justamente el que las necesita).

## Non-functional requirements

- **NFR-1 Solo lectura, y demostrable.** Únicamente `SELECT`. La sesión se abre
  con `default_transaction_read_only=on`, de modo que un `INSERT`/`UPDATE` falle
  en el motor aunque el código tuviera un bug.
- **NFR-2 Sin superusuario.** Rol dedicado de solo lectura con `SELECT` sobre las
  tablas del catálogo y nada más. Hoy el único rol con acceso amplio es
  `postgres`; el agente **no** debe usarlo.
- **NFR-3 Barato y acotado.** Consultas indexadas (`ORDER BY ... DESC LIMIT 1`,
  no `max()` sobre 31 GB), `statement_timeout` explícito, una conexión por
  corrida. `margen_final` tiene 56 M de filas: un scan completo no es aceptable.
- **NFR-4 Fail closed.** Sin credenciales o sin poder conectar, el agente lo dice;
  nunca asume que el destino está bien.
- **NFR-5 Determinista y testeable.** La evaluación es una función pura sobre
  `(día_esperado, día_observado, conteo)`; el I/O se inyecta, igual que hoy con
  el runner de SSH.
- **NFR-6 Sin secretos.** Credenciales por entorno; nunca en el catálogo, el
  repo, los logs ni los mensajes.

## Security boundaries

- El catálogo (`alert-rules.yml`) lleva **nombres de tabla y columna**, nunca
  credenciales ni cadenas de conexión.
- Rol de BD dedicado, solo `CONNECT` + `SELECT` sobre lo listado. Sin `postgres`.
- `pg_hba` acotado al host del agente. Sin exposición entrante nueva.
- Sigue siendo Fase 1: **detecta y reporta, no ejecuta nada**. Ninguna re-corrida.

## Acceptance criteria

- **AC-1** Un job con systemd `success` y tabla destino atrasada se reporta
  CRITICAL, y la evidencia menciona explícitamente que la unit dijo éxito.
- **AC-2** Un job con las dos señales sanas se reporta INFO, igual que hoy.
- **AC-3** Un job sin verificación de destino se comporta idéntico a antes
  (ninguna regresión en los 12 jobs actuales).
- **AC-4** El día esperado con conteo bajo el mínimo produce WARNING, no CRITICAL.
- **AC-5** Con el destino inalcanzable sale **una** condición de "no verificable",
  y ningún job cambia a CRITICAL por esa causa.
- **AC-6** Un intento de escritura por la conexión del agente falla en el motor.
- **AC-7** Ningún secreto aparece en reportes, alertas ni logs.
- **AC-8** Reproducir el caso del `margen_final_roll` congelado en 20260721
  produce CRITICAL (hoy produce OK).

## Out of scope

- Re-correr o reparar ETLs (Fase 2, con aprobación).
- Detección de anomalías / bandas estadísticas de volumen (Fase 3). Aquí solo un
  mínimo fijo por job.
- Cuadrar cifras de negocio entre origen y destino (row-count reconciliation).
- Leer el journal para extraer los `WARN` que el sync ya imprime — es una señal
  complementaria y va aparte; `etl_monitor` ya está en `systemd-journal`, así que
  no necesita cambios de permisos.
