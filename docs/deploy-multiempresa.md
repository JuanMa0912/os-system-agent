# Despliegue y actualización por empresa (spec 004)

Guía operativa para levantar el agente de cada empresa y **actualizarlo en el PC**.
Aplica a Mercamio (ahora) y Dinastia (después): mismos pasos, distinto `empresa`.
El **bot de Telegram es compartido** (`cortana`): un solo bot entrega los reportes de
todas las empresas, distinguidos por la etiqueta `Reporte empresa <X>`.

## Modelo

Un agente por empresa. El monitoreo puede ser de dos formas:

- **Remoto (por SSH):** el agente corre en un box (p.ej. MMAUTOML01) y llega al
  servidor de ETLs por SSH (alias `server232`). **Así está Mercamio hoy.**
- **Co-ubicado (local):** el agente corre EN el servidor de ETLs y monitorea local
  con `--server-alias local` (sin SSH). Opción para Dinastia si el agente vive en su
  propio servidor.

Punto común: **un grupo de Telegram** vía **un bot compartido (`cortana`)**. Cada
agente manda su `Reporte empresa <X>` por cortana al mismo grupo.

```
  Mercamio:  agente@MMAUTOML01 ──SSH──► server232 (ETLs)
  Dinastia:  agente@servidor-dinastia ──local──► ETLs
        │                                   │
        └──────── cortana (bot) ──► Grupo Telegram "ETL Reports" ──► 📱 tú
                  "Reporte empresa Mercamio" / "Reporte empresa Dinastia"
```

## Lo que cambia por empresa (nunca se commitea)

| Qué | Dónde | Mercamio |
|-----|-------|----------|
| `empresa` + jobs | `config/alert-rules.yml` (gitignored) | `empresa: Mercamio S.A` |
| Fuente de monitoreo | `--server-alias` del ExecStart | `server232` (SSH, remoto) |
| Id del grupo | `OS_TELEGRAM_TARGET` en la unit systemd | id del grupo compartido |

Bot: **compartido (`cortana`)**; su token vive en la config de OpenClaw de cada box.
El **código** (este repo) es idéntico en todos lados; solo la config difiere.

## Prerrequisitos por box

- Ubuntu, Python 3.11+, **`uv`** (NO `pip`/`venv` a mano; el venv de uv no trae `pip`),
  OpenClaw gateway local.
- **Salida HTTPS (443) a `api.telegram.org`**.
- Monitoreo remoto: alias SSH `server232` funcionando (`ssh server232 hostname`).
- Usuario de solo lectura en el server de ETLs (`etl_monitor`). `etl_runner` → Fase 2.

## Despliegue (ejemplo: Mercamio — remoto desde MMAUTOML01)

```bash
# 1) Código
git clone <repo> ~/os-system-agent && cd ~/os-system-agent
uv sync

# 2) Catálogo real (gitignored). Registra cada ETL a medida que lo creas.
cp config/alert-rules.example.yml config/alert-rules.yml
#   edita: empresa: Mercamio S.A  +  tus jobs (id, systemd_unit, freshness, paths)
```

3. **Telegram (bot compartido `cortana`):** cortana ya está configurado en OpenClaw.
   Agrega cortana al grupo y obtén el **chat id del grupo** (número negativo) → va en
   `OS_TELEGRAM_TARGET`. Cómo obtenerlo: `@myidbot` → `/getgroupid` en el grupo, o
   `getUpdates` con el token de cortana.
4. **systemd:** copia los `.example` a `~/.config/systemd/user/` y en cada `.service`
   pon `WorkingDirectory`, `PATH`/openclaw, `OS_TELEGRAM_TARGET=<id grupo>` y
   `--catalog config/alert-rules.yml` en el ExecStart.
   - **Remoto (Mercamio):** deja el `--server-alias` por defecto (`server232`). **NO**
     pongas `--server-alias local`.
   - **Co-ubicado:** añade `--server-alias local` al ExecStart.
   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now os-system-agent-daily.timer os-system-agent-alerts.timer
   loginctl enable-linger "$USER"
   ```

## Actualizar en el PC (código o catálogo)

`config/alert-rules.yml` y `.env` están **gitignored**: `git pull` **no los toca**.

```bash
cd ~/os-system-agent
git pull
uv sync                 # SOLO si cambiaron dependencias (no aplica a cambios de solo código)
uv run pytest -q        # opcional: sanity
systemctl --user restart os-system-agent-daily.timer os-system-agent-alerts.timer
```

> Con `uv` el paquete queda editable, así que un cambio de **solo código** ya queda
> activo con el `git pull` — no necesitas `uv sync` ni reinstalar.

## Verificación

```bash
# a) Dry-run (no envía) — confirma la etiqueta:
uv run python scripts/send_daily_report.py --catalog config/alert-rules.yml
#    → "Reporte empresa Mercamio S.A"

# b) Estado real por SSH a server232 (remoto), sin enviar:
uv run python scripts/send_daily_report.py --live --catalog config/alert-rules.yml
#    (co-ubicado sería:  --live --server-alias local)

# c) Prueba de canal — envío verde por cortana (sin --live):
OS_TELEGRAM_TARGET="<id_grupo>" \
  uv run python scripts/send_daily_report.py --send --catalog config/alert-rules.yml
```

Checklist: (1) el reporte dice `Reporte empresa <X>`; (2) llega al grupo por cortana;
(3) el agente no tiene config de otra empresa; (4) ningún secreto en el mensaje.

## Dinastia (co-ubicado + entrega directa, sin OpenClaw)

Dinastia corre el agente **EN su propio servidor de ETLs**, así que monitorea
**local** (`--server-alias local`, sin SSH) y entrega por Telegram **directo**
(`--direct`, POST a `api.telegram.org` con el bot cortana) — **no necesita el
gateway de OpenClaw**. Mismo bot `cortana` y mismo grupo que Mercamio.

> Dos familias de units, no confundir:
> - **Units que CORREN los ETLs** (viven en `dinastia-etl/`): `dinastia-ventas-daily.service`,
>   `dinastia-etl-margen.service`, `dinastia-rotacion-daily.service`. Corren como
>   usuario de sistema (`User=dinastia…`) en `/etc/systemd/system/`.
> - **Units del AGENTE de monitoreo** (este repo, `config/systemd/dinastia-agent-*`):
>   `systemctl --user`, solo LEEN `systemctl status` de las anteriores y mandan Telegram.

```bash
# 1) Código
git clone <repo> ~/os-system-agent && cd ~/os-system-agent
uv sync

# 2) Catálogo de Dinastia (gitignored). Ya trae los 3 ETLs registrados.
cp config/alert-rules.dinastia.example.yml config/alert-rules.yml
#   verifica: los `systemd_unit` coinciden con las units ETL instaladas en el box.

# 3) systemd del agente (co-ubicado + directo). USER services:
mkdir -p ~/.config/systemd/user
cp config/systemd/dinastia-agent-daily.service.example  ~/.config/systemd/user/dinastia-agent-daily.service
cp config/systemd/dinastia-agent-daily.timer.example    ~/.config/systemd/user/dinastia-agent-daily.timer
cp config/systemd/dinastia-agent-alerts.service.example ~/.config/systemd/user/dinastia-agent-alerts.service
cp config/systemd/dinastia-agent-alerts.timer.example   ~/.config/systemd/user/dinastia-agent-alerts.timer
#   edita en cada .service: <UV_PATH>, OS_TELEGRAM_TARGET=<id grupo>,
#   TELEGRAM_BOT_TOKEN=<token cortana>  (SECRETO — solo en el box, nunca en git)

systemctl --user daemon-reload
systemctl --user enable --now dinastia-agent-daily.timer dinastia-agent-alerts.timer
loginctl enable-linger "$USER"
```

Verificación (co-ubicado, entrega directa):

```bash
# a) Dry-run (no envía) — confirma la etiqueta "Reporte empresa Dinastia":
uv run python scripts/send_daily_report.py --catalog config/alert-rules.yml

# b) Estado real LOCAL (sin enviar):
uv run python scripts/send_daily_report.py --live --server-alias local \
    --catalog config/alert-rules.yml

# c) Prueba de canal — envío verde DIRECTO por cortana:
OS_TELEGRAM_TARGET="<id_grupo>" TELEGRAM_BOT_TOKEN="<token_cortana>" \
  uv run python scripts/send_daily_report.py --send --direct \
    --catalog config/alert-rules.yml
```

## Seguridad

- Nunca commitear `config/alert-rules.yml`, `.env`, ni llaves (ya gitignored).
- **Bot compartido (`cortana`):** su token vive en la config de OpenClaw de cada box.
  Se eligió simplicidad sobre aislamiento; si algún día quieres aislar, se puede pasar
  a un bot por empresa (un token por server).
- **Ejecución (Fase 2) deshabilitada:** estos timers son **solo lectura**.
