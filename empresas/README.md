# `empresas/` — código de ETL por empresa

Este repo es **uno solo** a propósito: el agente de monitoreo y los ETLs que
vigila viven juntos, así cada box hace **un `git pull`** y trae todo lo suyo.

> **El repo es PRIVADO.** Aquí hay nombres de cliente, bases y tablas del ERP y
> lógica de negocio. No lo hagas público ni copies estos archivos a un repo
> público.

## Qué hay aquí y qué no

| Carpeta | Qué es | Dónde corre |
|---|---|---|
| `dinastia/` | Los 3 ETLs de Dinastia (ventas, margen, rotación) + la dimensión de tipos de documento | box de Dinastia, en `/opt/dinastia-{ventas,margen,rotacion}` |
| `mercamio/etl-rotacion/` | ETL de rotación de Mercamio | server232, en `/opt/etl_rotacion` |

**No está aquí** el resto de los ETLs de Mercamio (`visor-etl-margen`,
`visor-etl-ventas-item`, `visor-etl-sync`, `ventas-pipeline-*`): viven en el repo
`visor-productividad`, que tiene su propio despliegue en server232. Duplicarlos
crearía dos fuentes de verdad para el mismo código. Si algún día se consolidan,
que sea moviéndolos, no copiándolos.

La raíz del repo (`src/`, `scripts/`, `config/`, `specs/`, `docs/`) es el
**agente de monitoreo**, que es de **solo lectura**: observa y reporta, nunca
escribe en una base. El código de `empresas/` sí escribe — son ETLs. Esa frontera
es intencional; ver `CLAUDE.md` §4.

## Nunca se versiona

Lo cubre `.gitignore`, pero conviene tenerlo presente:

- `**/config/pipeline_config.yaml` y `**/config/rotacion.env` — llevan credenciales
  del ERP y de GCP. Solo se versionan los `*.example`.
- `.venv/`, `logs/`, `__pycache__/`
- Volcados de esquema del ERP (`schema_*.sql`, `esquema.sql`) — pesados y con
  detalle interno del cliente.

## Desplegar un cambio

El repo **no se clona encima** de `/opt/<pipeline>`: ahí viven el `config/` real y
el `.venv`, que no están versionados y un clone los borraría. Se clona aparte y se
copian los archivos:

```bash
# una vez
sudo -u <usuario> git clone <repo> /opt/os-system-agent-repo

# cada cambio
sudo -u <usuario> git -C /opt/os-system-agent-repo pull
sudo cp /opt/os-system-agent-repo/empresas/dinastia/ventas/etl/<archivo>.py \
        /opt/dinastia-ventas/etl/
sudo chown <usuario>:<usuario> /opt/dinastia-ventas/etl/<archivo>.py
```

No hace falta `daemon-reload`: el Python se lee en cada corrida. Solo se recarga
systemd si cambian archivos `.service` / `.timer`.

Los runbooks operativos de cada empresa están en su carpeta
(`dinastia/RUNBOOK.md`, `mercamio/etl-rotacion/deploy/DEPLOY_DEBIAN12.md`).
