# Acceso remoto a los boxes del agente

Cómo se llega, desde el equipo de desarrollo, a las dos máquinas que participan en la
operación del agente. Sin secretos: los valores reales (IPs, usuarios, claves) van en
`~/.ssh/config` y en el gestor de credenciales de cada equipo, nunca aquí.

> Convención de este documento: `<IP_AUTOML>` y `<IP_232>` son marcadores. Reemplázalos
> en tu `~/.ssh/config` local; no los escribas en ningún archivo versionado.

## 0. Mapa

| Box | SO | Rol | Cómo se entra |
| --- | --- | --- | --- |
| Equipo de desarrollo | Windows 11 + WSL2 (Ubuntu 26.04) | Donde se escribe el código | — |
| Box con WSL (`MMAUTOML01`) | Windows + WSL2 (Ubuntu 24.04) | Instalación previa de OpenClaw, a retirar | OpenSSH de Windows → `wsl.exe` |
| **app-server (`232`)** | Debian 12 | **Hogar definitivo del agente** | SSH directo (alias `server232`) |

El agente vive en el **232** porque su IP de salida ya está autorizada en Cloud SQL, está
siempre encendido y no se duerme. Ver `docs/agente-232-hardening.md` para el modelo de
permisos allí.

## 1. Hechos de red verificados

Compruébalos antes de diagnosticar nada; ahorran tiempo:

- El 232 está en otra subred que el equipo de desarrollo, y **es alcanzable**:
  `Test-NetConnection -ComputerName <IP_232> -Port 22` → `TcpTestSucceeded: True`.
- **El ping ICMP al 232 falla, y eso es normal.** No concluyas "no hay ruta" por un ping
  fallido; prueba siempre el puerto TCP.
- `MMAUTOML01` **no resuelve por DNS ni NetBIOS** desde la red de desarrollo. Usa su IP.

## 2. El box con WSL: OpenSSH de Windows, no sshd dentro de WSL

### Por qué no se expone el sshd de WSL directamente

Se probó de verdad, no se supuso. Con `networkingMode=mirrored` activo, un listener dentro
de WSL **no** se alcanza desde la LAN:

```powershell
# Con un listener real escuchando en WSL:28080
Test-NetConnection -ComputerName <IP_LAN_DEL_HOST> -Port 28080
# -> TcpTestSucceeded : False
```

La causa no es WSL sino el firewall de Hyper-V:

```powershell
Get-NetFirewallHyperVVMSetting -PolicyStore ActiveStore |
  Select-Object DefaultInboundAction
# -> Block
```

Exponerlo exigiría `New-NetFirewallHyperVRule` con administrador. Además el firewall de estos
equipos lo administra IT de forma central (hay reglas de Kaspersky Administration Kit en la
lista), así que abrir un puerto entrante ahí es una decisión de política, no solo técnica.

**Camino elegido:** el OpenSSH Server que trae Windows — servicio firmado por Microsoft, con
una única regla en el firewall del *host* — y desde ahí se salta a Ubuntu con `wsl.exe`.

### 2.1 Instalar y arrancar (en el box con WSL, PowerShell como administrador)

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Set-Service -Name sshd -StartupType Automatic
Start-Service sshd
```

### 2.2 Acotar el firewall a la LAN

El instalador crea una regla abierta a cualquier origen. Ciérrala a tu subred:

```powershell
# <SUBRED_LAN> = la subred de tu oficina en notacion CIDR, p.ej. 203.0.113.0/24
Set-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' `
  -RemoteAddress <SUBRED_LAN> -Enabled True
Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' |
  Get-NetFirewallAddressFilter | Select-Object RemoteAddress   # verificar
```

### 2.3 Solo clave pública, nunca contraseña

En el equipo de desarrollo:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/os_agent_automl -C "os_system_agent->automl"
```

En el box con WSL, edita `C:\ProgramData\ssh\sshd_config`:

```text
PubkeyAuthentication yes
PasswordAuthentication no
```

y reinicia: `Restart-Service sshd`.

> **Tropiezo 1 — el más común.** Si la cuenta con la que entras pertenece al grupo
> **Administradores**, sshd **ignora** `C:\Users\<usuario>\.ssh\authorized_keys` y lee
> `C:\ProgramData\ssh\administrators_authorized_keys`. Ese archivo además exige una ACL
> restringida o sshd lo rechaza en silencio:
>
> ```powershell
> $f = "C:\ProgramData\ssh\administrators_authorized_keys"
> icacls $f /inheritance:r
> icacls $f /grant "Administrators:F" /grant "SYSTEM:F"
> ```
>
> Si la autenticación por clave "no funciona sin razón", es esto el 90 % de las veces.
> Lo más limpio es entrar con una cuenta **no administradora** y usar su
> `~/.ssh/authorized_keys` normal.

### 2.4 Alias en `~/.ssh/config` del equipo de desarrollo

```text
Host automl
  HostName <IP_AUTOML>
  User <usuario_no_admin>
  IdentityFile ~/.ssh/os_agent_automl
  IdentitiesOnly yes
  ServerAliveInterval 30
  ServerAliveCountMax 3
```

### 2.5 Saltar a Ubuntu

```bash
ssh automl "wsl -d Ubuntu -- bash -lc 'hostname; uptime'"
```

> **Tropiezo 2 — el shell por defecto.** Si el `DefaultShell` de OpenSSH quedó en `cmd.exe`,
> el entrecomillado se rompe de formas raras. Fíjalo a PowerShell una vez:
>
> ```powershell
> New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell `
>   -Value "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" `
>   -PropertyType String -Force
> ```

> **Tropiezo 3 — systemd de usuario bajo SSH.** OpenClaw corre como servicio
> `systemctl --user`. Una sesión de `wsl.exe` lanzada por SSH **no** es una sesión de login,
> así que `systemctl --user` puede responder "Failed to connect to bus" aunque el servicio
> esté bien. La cura, una sola vez dentro de la Ubuntu de ese box:
>
> ```bash
> loginctl enable-linger "$USER"
> ```
>
> Y si aun así falla, exporta el bus explícitamente en el comando remoto:
> `export XDG_RUNTIME_DIR=/run/user/$(id -u)`.

## 3. El 232: SSH directo

El 232 es Debian, así que no hay salto por `wsl.exe`. Se usa el patrón que ya define
`CLAUDE.md §10` y que consume `src/os_system_agent/ssh_client.py`:

```text
Host server232
  HostName <IP_232>
  User etl_monitor
  IdentityFile ~/.ssh/os_system_agent_server232
  IdentitiesOnly yes
  ServerAliveInterval 30
  ServerAliveCountMax 3
```

Comprobación:

```bash
ssh server232 'hostname; date; uptime'
```

El usuario `etl_monitor` es **de solo lectura**. El agente nunca entra como root ni con una
cuenta con `sudo`; los permisos y el porqué están en `docs/agente-232-hardening.md`.

## 4. Qué NO se hace

- No se expone ningún puerto de estos boxes fuera de la LAN.
- No se guarda ninguna clave privada dentro del repositorio (`CLAUDE.md §4`).
- No se habilita autenticación por contraseña en ninguno de los dos.
- No se usa el mismo par de claves para el box con WSL y para el 232: si una se compromete,
  la otra sigue en pie.
