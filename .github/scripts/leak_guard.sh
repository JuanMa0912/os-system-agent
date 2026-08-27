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

# --self-test: comprueba que el guardia DE VERDAD detecta algo antes de que se
# confie en su "limpio". El modo de fallo peligroso de un escaner no es dar un
# falso positivo, es pasar en verde sin haber mirado. La sonda vive aqui y no en
# el workflow para que el YAML del CI no tenga que llevar una IP privada dentro.
if [ "${1:-}" = "--self-test" ]; then
  SONDA_DIR="docs/_leak_guard_probe"
  mkdir -p "$SONDA_DIR"
  printf 'host 10.1.2.3\n' > "$SONDA_DIR/sonda.md"
  git add -N "$SONDA_DIR/sonda.md" >/dev/null 2>&1 || true
  if "$0" >/dev/null 2>&1; then
    rm -rf "$SONDA_DIR"
  # `git add -N` deja la intencion de anadir en el indice; sin este reset la
  # sonda queda como un borrado fantasma en `git status`.
  git reset -q -- "$SONDA_DIR" >/dev/null 2>&1 || true
    echo "FALLO: el guardia no detecto la sonda — esta roto" >&2
    exit 1
  fi
  rm -rf "$SONDA_DIR"
  # `git add -N` deja la intencion de anadir en el indice; sin este reset la
  # sonda queda como un borrado fantasma en `git status`.
  git reset -q -- "$SONDA_DIR" >/dev/null 2>&1 || true
  echo "self-test: el guardia detecta la sonda"
  exit 0
fi

# `evals` entra porque `.gitleaks.toml` deja de mirar sus casos dorados: alguien
# tiene que seguir vigilando esa carpeta, aunque sea con otras reglas.
RUTAS=(src scripts tests config docs evals .github)
FALLOS=0

# Rutas excluidas del barrido. Ojo con el formato: `git grep -n` emite
# `ruta:linea:contenido`, asi que el patron tiene que anclar en `ruta:` — un `$`
# tras la ruta no casa nunca, y el guardia terminaria denunciandose a si mismo.
#
# Se excluye este propio script porque CONTIENE los patrones que busca; es la
# unica forma de que un detector de cadenas pueda declarar las cadenas.
#
# `evals/cases/redaction_cases.yaml` es la OTRA excepcion, y la unica que
# tampoco mira gitleaks (ver .gitleaks.toml). Es inevitable: son los casos
# dorados que prueban que `redact()` tapa cada forma de secreto, asi que el
# fichero tiene que contenerlas. La proteccion de ese fichero no es un escaner
# sino su tamano y su revision: si alguna vez crece o deja de ser obviamente
# falso, la excepcion deja de estar justificada.
EXCLUIR='^(specs/[^/]+/_|\.github/scripts/leak_guard\.sh:|evals/cases/redaction_cases\.yaml:)'

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
