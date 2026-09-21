# Spec — 008 Reparador de huecos: cargar lo que falta sin borrar lo que hay

## Problem

El 2026-09-21, en Dinastia, se descubrieron **14 días seguidos** (27-ago a 9-sep)
que el ERP tenía y GCP no: ~$6.113 millones de venta que el tablero nunca vio.
Nadie se enteró en tres semanas, y no por descuido:

1. **Ninguna ventana automática llega tan atrás.** `daily` carga D-4..D-1,
   `weekly` 8 días, `monthly` el mes en curso. Cuando el ERP repuso el dato
   tarde, ya no quedaba ningún mecanismo que volviera por él. Un hueco de más
   de 8 días es permanente.
2. **Cargar cero no es fallar.** Las corridas de esos días terminaron en verde
   con `RECORDS_LOADED: 0` y mandaron su ✅ por Telegram.
3. **No hay nadie mirando.** En `servidorUAID` no corre ningún agente de
   monitoreo: el clon del repo es código parado.

Y el mismo día se descubrió lo contrario: el ERP había **perdido 24 días**
(28-jul a 20-ago) que solo sobreviven en GCP. Recargar "los dos últimos meses"
a ciegas —que es exactamente lo que se pidió al principio— los habría borrado,
porque `replace_by_date` borra el día y reinserta lo que el origen devuelva.
Lo único que lo impidió fue comparar antes.

De ahí el problema a resolver: **hace falta una forma de cargar lo que falta que
sea incapaz de borrar lo que ya está**, usable a mano cuando el automático falle
y automatizable después.

## Actors

- **Operador humano** — entra al box, quiere reponer datos sin estudiarse el
  modelo de escritura de cada ETL.
- **OS_SYSTEM_AGENT** — provee las reglas y la herramienta.
- **ETLs desplegados** (`/opt/dinastia-{ventas,margen,rotacion}`) — quienes de
  verdad cargan; la herramienta los invoca, no los reimplementa.
- **ERP Siesa/Biable** (MySQL, solo lectura) — el origen, que puede estar
  incompleto, vacío o haber perdido datos.
- **GCP Cloud SQL** — el destino, que a veces es la única copia.

## User journeys

1. *"¿Cómo va Dinastia?"* → `estado`: una línea por día, ERP contra GCP, con el
   veredicto de cada día y el resumen. No escribe nada.
2. *"Faltan días, repáralos"* → `reparar`: enseña el plan (qué rangos cargaría y
   qué días se niega a tocar). Sin `--aplicar` se detiene ahí.
3. *"Carga este rango exacto"* → `cargar`: puerta de atrás explícita, con aviso
   de que no comprueba el origen.
4. *"El dato está pero el tablero no lo ve"* → `refrescar`.
5. Desde el menú, cualquiera de las anteriores sin recordar sintaxis.

## Functional requirements

- **FR1** Comparar, por día, origen contra destino: filas, sedes presentes y una
  medida (venta).
- **FR2** Clasificar cada día en un veredicto y decidir a partir de él:
  - `missing` (origen con datos, destino vacío) → cargable, solo suma.
  - `hollow` (destino con filas pero medida cero mientras el origen mide) →
    cargable; cubre el punto ciego de rotación, que es una rejilla y devuelve
    ~10.600 filas diarias aunque no haya una sola venta.
  - `both_empty` (festivo) → nada que hacer.
  - `ok` → ya está.
  - `source_empty`, `risky_sede`, `risky_measure` → **bloqueado**: recargar
    perdería datos.
- **FR3** Agrupar los días cargables en el menor número de rangos contiguos.
  Un rango **nunca** puede contener un día bloqueado, porque cargarlo lo
  re-extraería del origen y lo sustituiría.
- **FR4** No escribir nada sin `--aplicar`, y confirmar de forma interactiva
  salvo `--si` (para timers).
- **FR5** Delegar la carga en el `run_pipeline.py` desplegado, que ya refresca
  vistas y reporta a Telegram. No duplicar la lógica de carga.
- **FR6** Registrar cada acción (quién, qué, cuándo, qué rango) en un JSONL.
- **FR7** Menú interactivo sobre los mismos subcomandos, que se rebaja a
  `osagent` si lo lanza root.

## Non-functional requirements

- Las **reglas de decisión** viven en `src/os_system_agent/`, con tests: son lo
  único que puede causar una pérdida de datos. `empresas/**` está excluido de
  ruff y del leak guard, así que lo que viva solo ahí no lo revisa nadie.
- Sin dependencias nuevas: corre con el intérprete del despliegue.
- El nombre de tabla se lee de la config desplegada, no se escribe en el código.
- Fail-closed: sin entorno cargado, las consultas fallan; no se adivina nada.

## Security boundaries

- Solo lectura contra el ERP.
- Escritura en GCP **únicamente** a través del runner ya desplegado y auditado.
- Se ejecuta como `osagent`, nunca como root.
- Sin secretos en el código: todo sale de `/etc/dinastia/dinastia.env`.
- La herramienta no borra ni trunca nada por su cuenta; el único borrado posible
  es el `replace_by_date` del ETL, y solo sobre días que ya se verificaron vacíos
  o huecos.

## Acceptance criteria

- **AC1** Con el estado del 2026-09-21, `reparar` propone exactamente
  `20260827..20260909` y bloquea los 19 días que solo existen en GCP.
  *(Cubierto por `test_the_2026_09_21_dinastia_case`.)*
- **AC2** Un día bloqueado entre dos cargables parte el plan en dos rangos y no
  aparece en ninguno.
- **AC3** Un festivo entre dos cargables no parte el rango.
- **AC4** Una tirada de solo festivos no genera rango.
- **AC5** Un día con sede de menos en el origen se bloquea aunque el conteo de
  filas parezca razonable.
- **AC6** Sin `--aplicar`, ninguna ejecución escribe.
- **AC7** `estado` sobre un rango sano no propone nada.

## Out of scope

- Reparar Mercamio (esta versión es solo Dinastia).
- Decidir por su cuenta sobre días bloqueados: eso siempre es humano.
- Sustituir el health-check de `post_run.py`, que sigue siendo quien reporta
  cada corrida.
- El timer diario que llamaría a `reparar --si`: va en una fase posterior, una
  vez que la parte manual se haya usado de verdad.
