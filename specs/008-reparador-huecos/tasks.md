# Tasks — 008 Reparador de huecos

## T1 — Reglas puras de decisión ✅ hecho (2026-09-21)

**Objetivo:** clasificar cada día y planificar rangos, sin tocar I/O.
**Archivos:** `src/os_system_agent/backfill.py`
**Comandos:** `uv run pytest tests/test_backfill.py`, `uv run ruff check`, `uv run mypy`
**Verificación:** 26 tests verdes; ruff y mypy limpios.
**Riesgo:** `low` (no escribe nada) · **Aprobación humana:** no

## T2 — Tests de las reglas ✅ hecho (2026-09-21)

**Objetivo:** cubrir cada veredicto, el agrupamiento y el incidente real.
**Archivos:** `tests/test_backfill.py`
**Verificación:** `test_the_2026_09_21_dinastia_case` reproduce el estado de ese
día y exige el plan `20260827..20260909` con 19 días bloqueados.
**Riesgo:** `low` · **Aprobación humana:** no

## T3 — Herramienta de operación ✅ hecho (2026-09-21)

**Objetivo:** cablear las reglas con el ERP, GCP y los runners desplegados.
**Archivos:** `empresas/dinastia/tools/dinastia_ops.py`
**Comandos:** `python dinastia_ops.py estado --dias 60`
**Verificación:** argparse validado en local; falta la prueba contra las bases
reales (T6).
**Riesgo:** `medium` (puede invocar cargas) · **Aprobación humana:** sí, para `--aplicar`

## T4 — Menú interactivo ✅ hecho (2026-09-21)

**Objetivo:** operar sin recordar sintaxis; rebajarse a `osagent` si lo lanza root.
**Archivos:** `empresas/dinastia/tools/menu.sh`
**Comandos:** `bash -n menu.sh`
**Verificación:** sintaxis validada; la opción 3 simula antes de preguntar.
**Riesgo:** `medium` · **Aprobación humana:** sí, para ejecutar el plan

## T5 — Documentar en el RUNBOOK ⬜ pendiente

**Objetivo:** que alguien que no estuvo hoy sepa usarlo.
**Archivos:** `empresas/dinastia/RUNBOOK.md`
**Riesgo:** `low` · **Aprobación humana:** no

## T6 — Validar en el box, en solo lectura ⬜ pendiente

**Objetivo:** confirmar contra las bases reales que `estado` cuadra con lo que ya
sabemos, y que el tramo 28-jul..20-ago sale bloqueado.
**Comandos:** los de "Verificación manual" del `plan.md`, pasos 1 a 3.
**Verificación:** el plan sobre 60 días debe salir vacío (todo está cargado hoy),
y el tramo perdido debe dar 24 días `source_empty`.
**Riesgo:** `low` (no escribe) · **Aprobación humana:** no

## T7 — Arreglar `_iso` en `post_run` ⬜ pendiente

**Objetivo:** que el refresco mande el rango en el formato que la función espera.
Hoy manda ISO (`2026-08-27`) a funciones que comparan contra `YYYYMMDD`; glibc
ignora los guiones y lo disimula, pero **el primer día de cada rango nunca entra
al rollup**. Detectado y parcheado a mano el 2026-09-21 en `margen_dinastia_roll`.
**Archivos:** `empresas/dinastia/*/common/post_run.py` (los tres), + test.
**Enfoque:** mirar `proargtypes` en `pg_proc` y mandar `date` o `text` según el
tipo declarado, en vez de asumir.
**Riesgo:** `medium` (afecta a todos los refrescos) · **Aprobación humana:** sí

## T8 — Timer diario que solo avisa si hay huecos ⬜ pendiente

**Objetivo:** que nadie tenga que acordarse de mirar. `reparar --si` sobre 60
días, reparando lo aditivo y avisando por Telegram **solo** cuando haya días
bloqueados o algo falle.
**Dependencias:** T6 y T7 cerradas, y una semana de uso manual.
**Riesgo:** `high` (escribe sin humano delante) · **Aprobación humana:** sí, explícita
