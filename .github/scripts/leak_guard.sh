#!/usr/bin/env bash
# leak_guard.sh — impide que vuelvan a entrar datos internos al repositorio.
#
# gitleaks detecta CREDENCIALES (tokens, llaves, contrasenas). No detecta lo que
# de verdad se filtro en este proyecto: topologia de red interna, nombres de base
# de datos y nombres de cliente. Este guardia cubre ese hueco.
#
# Alcance: src/ scripts/ tests/ config/ docs/ .github/
#   NO vigila `empresas/`: es codigo ETL de produccion que llego de otros repos y
#   referencia hosts reales por necesidad; misma exclusion que hace ruff en
#   pyproject.toml. Tampoco vigila `specs/*/_*.md`, que son expedientes de
#   analisis donde citar una ruta o un patron es justamente el trabajo.
#
# Salida: 0 si esta limpio, 1 si encuentra algo. Imprime archivo:linea y el
# motivo, pero NUNCA el valor completo de lo que encontro.
set -euo pipefail

RUTAS=(src scripts tests config docs .github)
FALLOS=0

# Rutas que se excluyen del barrido, como regex de ruta.
EXCLUIR='^(specs/[^/]+/_|\.github/scripts/leak_guard\.sh$)'

# Ejecuta un patron y reporta. $1 = etiqueta, $2 = regex extendida, $3 = regex de
# excepciones permitidas (se filtra de los resultados).
revisar() {
  local etiqueta="$1" patron="$2" permitido="${3:-}"
  local hits
  hits="$(git grep -nIE "$patron" -- "${RUTAS[@]}" 2>/dev/null || true)"
  [ -n "$hits" ] || return 0

  hits="$(printf '%s\n' "$hits" | grep -vE "$EXCLUIR" || true)"
  [ -n "$hits" ] || return 0

  if [ -n "$permitido" ]; then
    hits="$(printf '%s\n' "$hits" | grep -viE "$permitido" || true)"
  fi
  [ -n "$hits" ] || return 0

  echo "FALLO [$etiqueta]:"
  # Solo archivo:linea y los primeros 60 caracteres, para no reimprimir el dato.
  printf '%s\n' "$hits" | cut -c1-60 | sed 's/^/  /'
  echo
  FALLOS=1
}

# 1) IPs privadas RFC1918. Se permiten las de documentacion y las locales.
revisar "IP privada" \
  '\b(10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|192\.168\.[0-9]{1,3}\.[0-9]{1,3}|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3})\b' \
  '0\.0\.0\.0|127\.0\.0\.1|10\.255\.255\.254|<[A-Z_]+>|x\.x\.x|example|placeholder'

# 2) Nombres de cliente y de sistemas internos.
revisar "nombre interno" \
  '\b(BD_BIABLE[0-9]*|produXdia|servidorUAID|etl_monitor@|prodapp)\b' \
  '<[A-Z_]+>|placeholder'

# 3) Material criptografico pegado por error.
revisar "llave privada" \
  'BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY'

# 4) JSON de service account de GCP.
revisar "service account GCP" \
  '"private_key_id"|"client_email"[[:space:]]*:[[:space:]]*"[^"]+iam\.gserviceaccount'

# 5) Correos corporativos nominales (los genericos y de ejemplo se permiten).
revisar "correo corporativo" \
  '\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.(com|com\.co|co)\b' \
  '@example\.|noreply@|@anthropic|@users\.noreply|<[A-Z_]+>|dominio\.'

if [ "$FALLOS" -ne 0 ]; then
  cat <<'MSG'
--------------------------------------------------------------------
El guardia bloqueo el cambio: hay datos internos en rutas vigiladas.

Que hacer:
  - Sustituye el valor real por un marcador: <IP_SERVIDOR>, <NOMBRE_BD>.
  - Si es documentacion, usa los rangos reservados para ejemplos
    (RFC 5737: 192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24) y el
    dominio example.com (RFC 2606).
  - Si el hallazgo es legitimo y no hay alternativa, amplia la lista de
    excepciones de este script EN EL MISMO PR, para que quede revisado.

Esto no reemplaza a gitleaks: gitleaks busca credenciales, este guardia
busca topologia interna y nombres de cliente.
--------------------------------------------------------------------
MSG
  exit 1
fi

echo "leak_guard: limpio (${#RUTAS[@]} rutas vigiladas)"
exit 0
