# Despliegue y actualización por empresa (spec 004)

Guía operativa para levantar **un agente por empresa** y para **actualizarlo en el
PC** cada vez que cambie el código o el catálogo. Aplica igual a Mercamio (ahora)
y a Dinastia (después): son los mismos pasos con distinto `empresa` y bot.

## Modelo (recordatorio)

Un agente **co-ubicado** en el servidor de cada empresa. Cada agente solo toca su
propio servidor (monitoreo **local**, sin SSH cruzado). El único punto común es un
**grupo de Telegram**, al que cada agente llega por salida a internet. **Un bot por
empresa**; si un servidor se compromete, solo se filtra el token de esa empresa.

```
  Server Mercamio ──(salida 443)──┐
                                  ├──►  Grupo Telegram "ETL Reports"  ──► 📱 tú
  Server Dinastia ──(salida 443)──┘      "Reporte empresa Mercamio"
                                          "Reporte empresa Dinastia"
```

## Lo que cambia por empresa (nunca se commitea)

| Qué | Dónde | Ejemplo Mercamio |
|-----|-------|------------------|
| `empresa` + jobs | `config/alert-rules.yml` (gitignored) | `empresa: Mercamio` |
| Token del bot | config de OpenClaw del server (`~/.openclaw`) | `bot_mercamio` |
| Id del grupo | `OS_TELEGRAM_TARGET` en la unit systemd | id del grupo compartido |
| Fuente de monitoreo | `--server-alias` en el ExecStart | `local` (co-ubicado) |

El **código** (este repo) es idéntico en todos los servidores. Solo la config difiere.

## Prerrequisitos por servidor

- Ubuntu (WSL2 o nativo), Python 3.11+, **`uv`** (gestor de entorno/dependencias —
  este proyecto NO usa `pip`/`venv` a mano; el venv de uv ni siquiera trae `pip`),
  OpenClaw gateway local.
- **Salida HTTPS (443) a `api.telegram.org`** (confírmalo con IT de la empresa).
- Usuario `etl_monitor` (solo lectura). `etl_runner` no-root se agrega en Fase 2.

## Despliegue inicial (ejemplo: Mercamio)

```bash
# 1) Código (uv gestiona el entorno; NO uses pip/venv a mano)
git clone <repo> ~/os-system-agent && cd ~/os-system-agent
uv sync

# 2) Catálogo real (gitignored). Registra aquí cada ETL a medida que lo creas.
cp config/alert-rules.example.yml config/alert-rules.yml
#   edita: empresa: Mercamio  +  tus jobs (id, systemd_unit, freshness, paths)
```

3. **Bot + grupo de Telegram:**
   - Con `@BotFather` crea `bot_mercamio` y copia su token.
   - Crea (o usa) el grupo **"ETL Reports"**, agrega el bot y a ti, y obtén el
     **chat id del grupo** (ese id va en `OS_TELEGRAM_TARGET`).
4. **OpenClaw:** canal `telegram` con el token de `bot_mercamio`, `allowFrom` = tu
   id, `groupPolicy: allowlist`. Corre `openclaw security audit`.
5. **systemd (co-ubicado):** copia los ejemplos y **añade `--server-alias local`**
   al `ExecStart` (el agente corre EN el server; sin esto intentaría SSH a
   `server232`):

   ```bash
   mkdir -p ~/.config/systemd/user
   cp config/systemd/os-system-agent-daily.service.example  ~/.config/systemd/user/os-system-agent-daily.service
   cp config/systemd/os-system-agent-daily.timer.example    ~/.config/systemd/user/os-system-agent-daily.timer
   cp config/systemd/os-system-agent-alerts.service.example ~/.config/systemd/user/os-system-agent-alerts.service
   cp config/systemd/os-system-agent-alerts.timer.example   ~/.config/systemd/user/os-system-agent-alerts.timer
   #   edita cada .service: WorkingDirectory, PATH/openclaw, OS_TELEGRAM_TARGET=<id grupo>,
   #   y en el ExecStart agrega:  --server-alias local
   systemctl --user daemon-reload
   systemctl --user enable --now os-system-agent-daily.timer os-system-agent-alerts.timer
   loginctl enable-linger "$USER"
   ```

6. **Verifica** (ver sección Verificación).

## Actualizar en el PC (código o catálogo)

`config/alert-rules.yml` y `.env` están **gitignored**, así que `git pull` **no los
toca** — tus jobs reales quedan intactos.

```bash
cd ~/os-system-agent
git pull
uv sync                           # SOLO si cambiaron dependencias (no aplica a cambios de solo código)
uv run pytest -q                  # opcional: sanity
# si vas a agregar/editar ETLs, edita config/alert-rules.yml (no lo hace git)
systemctl --user restart os-system-agent-daily.timer os-system-agent-alerts.timer
```

> Nota: con `uv` el paquete queda instalado en modo editable, así que un cambio de
> **solo código** (como este) queda activo con el `git pull` — **no** necesitas
> `uv sync` ni reinstalar. `uv sync` es solo cuando cambian dependencias.

## Verificación

```bash
# Dry-run local (no envía): debe encabezar con "Reporte empresa Mercamio"
uv run python scripts/send_daily_report.py --server-alias local --catalog config/alert-rules.yml

# Alertas en dry-run (no envía):
uv run python scripts/alert_incidents.py --server-alias local --catalog config/alert-rules.yml

# Envío real de prueba (usa el grupo): agrega --send con OS_TELEGRAM_TARGET puesto.
```

Checklist: (1) el reporte dice `Reporte empresa <X>`; (2) llega al grupo correcto;
(3) el agente **no** tiene config de otra empresa; (4) ningún secreto en el mensaje.

## Luego: PC de Dinastia

Repite **exactamente** los mismos pasos en el servidor de Dinastia con:
`empresa: Dinastia` en su `config/alert-rules.yml`, `bot_dinastia` (agregado al
**mismo** grupo), y `OS_TELEGRAM_TARGET` = el id de ese mismo grupo. **Nada** de
Mercamio se copia a ese servidor: son instancias independientes que solo coinciden
en el grupo de Telegram.

## Seguridad

- Nunca commitear `config/alert-rules.yml`, `.env`, ni llaves (ya gitignored).
- El token del bot vive en la config de OpenClaw del server, no en el repo.
- **Ejecución (Fase 2) sigue deshabilitada:** estos timers son de **solo lectura**
  (monitorean y reportan; no corren ni cambian nada en el servidor).
