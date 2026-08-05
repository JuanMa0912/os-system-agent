# Configuracion del ETL de rotacion

Esta carpeta es para credenciales y variables de entorno. No pongas claves dentro
del archivo Python.

## Windows PowerShell

1. Copia `rotacion.ps1.example` como `rotacion.ps1`.
2. Llena las claves y datos faltantes en `rotacion.ps1`.
3. Carga las variables antes de ejecutar:

```powershell
. .\config\rotacion.ps1
.\.venv\Scripts\python.exe .\etl_rotacion_diaria_sede_3bd_auto.py --check-only
```

## Debian 12 / Linux

1. Copia `rotacion.env.example` como `rotacion.env`.
2. Llena las claves y datos faltantes en `rotacion.env`.
3. Protege el archivo:

```bash
chmod 600 config/rotacion.env
```

4. Carga las variables antes de ejecutar:

```bash
set -a
. ./config/rotacion.env
set +a
python3 etl_rotacion_diaria_sede_3bd_auto.py --check-only
```

Si una clave tiene caracteres especiales como `$`, usa comillas simples en Linux
cuando llenes el valor.
