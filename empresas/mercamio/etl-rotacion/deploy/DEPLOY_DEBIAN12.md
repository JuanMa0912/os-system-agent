# Despliegue en Debian 12

Esta guia instala el ETL en `/opt/etl_rotacion` y lo programa con `systemd`.

> **ESTADO REAL EN server232 (verificado 2026-08-03).** Esta guia describia el
> modelo viejo: UNA unit (`etl-rotacion.service` + `.timer`) que procesaba las tres
> empresas en serie, y el script `etl_rotacion_diaria_sede_3bd_auto.py`.
>
> **El 2026-07-31 11:19 se migro a una unit por empresa** (`etl-rotacion@.service`
> con instancias `@mercamio`, `@mtodo`, `@bogota`) y el script en produccion es
> **`etl_rotacion_v3.py`**. Motivo documentado en la propia unit: en serie tardaba
> ~53 min (mercamio 30 + mtodo 18 + bogota 5) y el 2026-07-31 termino 07:53:24 con
> el sync a GCP ya corriendo desde 07:50 — bogota no alcanzo a subir y GCP se quedo
> sin sus 25.693 filas del 30-jul. En paralelo el total baja a ~30 min y el sync
> pudo adelantarse a las 07:35.
>
> Las units viejas quedaron huerfanas en `/etc/systemd/system` (el timer `disabled`,
> el service sin correr nunca) hasta que se removieron el **2026-08-03**; respaldo en
> `/root/systemd-orphans-20260803/`. **No las reinstales.**
>
> **Fuente de verdad de las units diarias:** repo `visor-productividad`, carpeta
> `deploy/systemd/` (ahi viven `etl-rotacion@.service` y los tres `@*.timer`).
> En ESTA carpeta ya solo quedan las del **rolling**, que si siguen vigentes.
> Los `etl-rotacion.service` / `.timer` del modelo viejo se eliminaron el
> 2026-08-05 al consolidar el repo: estaban ahi solo como historia y eran la
> trampa que recreaba las huerfanas. Copia de respaldo en el servidor, en
> `/root/systemd-orphans-20260803/`.

## 1. Crear usuario y carpetas

```bash
sudo useradd --system --create-home --home-dir /opt/etl_rotacion --shell /usr/sbin/nologin etlrotacion
sudo mkdir -p /opt/etl_rotacion
sudo mkdir -p /var/log/etl_rotacion
sudo chown -R etlrotacion:etlrotacion /opt/etl_rotacion /var/log/etl_rotacion
```

## 2. Instalar dependencias del sistema

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
```

## 3. Copiar archivos a `/opt/etl_rotacion`

Copia estos archivos y carpetas al servidor:

```text
etl_rotacion_v3.py                          # el que corre en produccion
requirements.txt
config/rotacion.env.example
deploy/systemd/etl-rotacion-rolling.service
deploy/systemd/etl-rotacion-rolling.timer
```

La estructura final recomendada es:

```text
/opt/etl_rotacion/
  etl_rotacion_v3.py
  requirements.txt
  config/
    rotacion.env.example
    rotacion.env
  deploy/
    systemd/
      etl-rotacion-rolling.service
      etl-rotacion-rolling.timer
```

Las units diarias (`etl-rotacion@.service` + los tres `@*.timer`) salen del repo
`visor-productividad`, no de aqui — ver la nota del encabezado.

Despues de copiar:

```bash
sudo chown -R etlrotacion:etlrotacion /opt/etl_rotacion
```

## 4. Crear entorno virtual

```bash
cd /opt/etl_rotacion
sudo -u etlrotacion python3 -m venv .venv
sudo -u etlrotacion /opt/etl_rotacion/.venv/bin/python -m pip install --upgrade pip
sudo -u etlrotacion /opt/etl_rotacion/.venv/bin/pip install -r requirements.txt
```

## 5. Configurar variables y claves

```bash
sudo mkdir -p /opt/etl_rotacion/config
sudo cp /opt/etl_rotacion/config/rotacion.env.example /opt/etl_rotacion/config/rotacion.env
sudo nano /opt/etl_rotacion/config/rotacion.env
sudo chown etlrotacion:etlrotacion /opt/etl_rotacion/config/rotacion.env
sudo chmod 600 /opt/etl_rotacion/config/rotacion.env
```

En `rotacion.env`, cambia `ETL_LOG_DIR` para que apunte al log del servidor:

```bash
ETL_LOG_DIR=/var/log/etl_rotacion
```

## 6. Probar antes de programar

```bash
sudo -u etlrotacion bash -lc 'set -a; . /opt/etl_rotacion/config/rotacion.env; set +a; /opt/etl_rotacion/.venv/bin/python /opt/etl_rotacion/etl_rotacion_diaria_sede_3bd_auto.py --check-only'
```

Ver que procesaria sin cargar:

```bash
sudo -u etlrotacion bash -lc 'set -a; . /opt/etl_rotacion/config/rotacion.env; set +a; /opt/etl_rotacion/.venv/bin/python /opt/etl_rotacion/etl_rotacion_diaria_sede_3bd_auto.py --dry-run --history-start 20260101 --rolling-days 15'
```

## 7. Instalar systemd

Se instalan **cuatro** timers:

| Timer | Cuando | Que corre |
|---|---|---|
| `etl-rotacion@mercamio.timer` | diario 07:00:00 | `v3 --mode daily --empresas mercamio` (~30 min) |
| `etl-rotacion@mtodo.timer` | diario 07:00:20 | `v3 --mode daily --empresas mtodo` (~18 min) |
| `etl-rotacion@bogota.timer` | diario 07:00:40 | `v3 --mode daily --empresas bogota` (~5 min) |
| `etl-rotacion-rolling.timer` | dias 1, 11, 21 a la 01:00 | `v3 --mode rolling --rolling-days 15` |

El desfase de 20 s entre empresas es solo para no chocar en el
`CREATE INDEX IF NOT EXISTS` de arranque. Corren en paralelo, terminan ~07:30,
antes del sync a GCP de las 07:35.

```bash
# La plantilla + los tres timers salen del repo visor-productividad:
sudo cp <visor-productividad>/deploy/systemd/etl-rotacion@.service        /etc/systemd/system/
sudo cp <visor-productividad>/deploy/systemd/etl-rotacion@mercamio.timer  /etc/systemd/system/
sudo cp <visor-productividad>/deploy/systemd/etl-rotacion@mtodo.timer     /etc/systemd/system/
sudo cp <visor-productividad>/deploy/systemd/etl-rotacion@bogota.timer    /etc/systemd/system/
# El rolling sigue saliendo de este repo:
sudo cp /opt/etl_rotacion/deploy/systemd/etl-rotacion-rolling.service /etc/systemd/system/
sudo cp /opt/etl_rotacion/deploy/systemd/etl-rotacion-rolling.timer   /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now etl-rotacion@mercamio.timer \
                            etl-rotacion@mtodo.timer \
                            etl-rotacion@bogota.timer \
                            etl-rotacion-rolling.timer
```

> **NO instalar** `etl-rotacion.service` ni `etl-rotacion.timer` (modelo viejo,
> removidos del servidor el 2026-08-03).

**Salida 3 = guarda de "cargue sin ventas"** (el POS no cerro el dia). La plantilla
`etl-rotacion@.service` **no** declara `SuccessExitStatus=3` a proposito: asi la
instancia queda `failed` y se ve en `systemctl --failed` en vez de pasar callada.
El monitoreo la reporta como CRITICAL, que es lo correcto.

El timer usa la hora local del servidor. Si el servidor debe correr en hora de
Colombia, verifica la zona horaria:

```bash
timedatectl
sudo timedatectl set-timezone America/Bogota
```

Verificar que quedo programado:

```bash
systemctl list-timers --all --no-pager | grep etl-rotacion
# esperado: @mercamio, @mtodo, @bogota (diarios) + rolling (dias 1/11/21)
```

Ejecutar una empresa manualmente:

```bash
sudo systemctl start etl-rotacion@mercamio.service
```

Ver logs del servicio:

```bash
journalctl -u etl-rotacion@mercamio.service -n 100 --no-pager
tail -n 100 /var/log/etl_rotacion/etl_rotacion_v3_$(date +%Y%m%d).log
```

## Comportamiento diario

Cada instancia ejecuta (una por empresa, en paralelo):

```bash
/opt/etl_rotacion/.venv/bin/python /opt/etl_rotacion/etl_rotacion_v3.py \
    --mode daily --empresas <mercamio|mtodo|bogota> --log-dir /var/log/etl_rotacion
```

Y el rolling de los dias 1, 11 y 21:

```bash
/opt/etl_rotacion/.venv/bin/python /opt/etl_rotacion/etl_rotacion_v3.py \
    --mode rolling --rolling-days 15 --log-dir /var/log/etl_rotacion
```

> Latencia medida: el rolling del 2026-08-01 tardo **10 h 39 min** (01:00 -> 11:39) y
> se solapo con los diarios de las 07:00. Tenerlo presente antes de mover horarios.

Si corre el 15 de abril de 2026 a las 7:00 a. m., sin `--procesar-hoy`,
procesa desde el 31 de marzo de 2026 hasta el 14 de abril de 2026.

En cada repaso:

- Borra y recarga ventas de cada empresa/dia.
- Conserva el inventario historico que ya estaba guardado en destino.
- Si aparece una venta nueva para un item que no tenia snapshot historico, no le
  copia el inventario actual de forma incorrecta.
- Limpia logs de base y archivos locales con mas de 31 dias.
