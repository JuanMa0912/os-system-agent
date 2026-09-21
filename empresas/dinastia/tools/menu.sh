#!/usr/bin/env bash
# menu.sh — la cara interactiva de dinastia_ops, para operar sin recordar sintaxis.
#
# Se ejecuta SIEMPRE como osagent: el .env es 640 root:osagent, los timers corren
# con ese usuario y el ETL no necesita privilegios. Si lo lanza root, se rebaja
# solo — asi los logs no quedan con dueno equivocado y un error de dedo no tiene
# alcance de root.
set -euo pipefail

USUARIO_ETL="${DINASTIA_USER:-osagent}"
ENV_FILE="${DINASTIA_ENV_FILE:-/etc/dinastia/dinastia.env}"
DEPLOY_BASE="${DINASTIA_DEPLOY_BASE:-/opt}"

if [ "$(id -u)" -eq 0 ]; then
  echo "(root detectado: reejecutando como ${USUARIO_ETL})"
  exec sudo -u "$USUARIO_ETL" "$0" "$@"
fi

AQUI="$(cd "$(dirname "$0")" && pwd)"
OPS="${AQUI}/dinastia_ops.py"
PY="${DEPLOY_BASE}/dinastia-ventas/.venv/bin/python"

[ -x "$PY" ] || { echo "ERROR: no encuentro el interprete ${PY}"; exit 1; }
[ -f "$OPS" ] || { echo "ERROR: no encuentro ${OPS}"; exit 1; }

# El entorno trae ERP + GCP + Telegram. Sin el, todo falla cerrado.
if [ -r "$ENV_FILE" ]; then
  set -a; . "$ENV_FILE"; set +a
else
  echo "AVISO: no puedo leer ${ENV_FILE} — las consultas van a fallar."
fi

ops() { "$PY" "$OPS" "$@"; }

pedir_rango() {
  read -r -p "  dias hacia atras [60]: " DIAS
  DIAS="${DIAS:-60}"
}

pausa() { read -r -p "  (enter para volver al menu) " _; }

while true; do
  cat <<'MENU'

  ============================================================
   OS_SYSTEM_AGENT — Dinastia (servidorUAID)
  ============================================================
   1) Estado            comparar ERP vs GCP, las 3 tablas
   2) Estado detallado  una linea por dia
   3) Reparar huecos    carga SOLO lo que falta (simula primero)
   4) Cargar un rango   fechas a mano (puerta de atras)
   5) Refrescar vistas  sin cargar
   6) Ultimas corridas  journal de los timers
   7) Ver el registro   que se ha ejecutado desde aqui
   0) Salir
  ============================================================
MENU
  read -r -p "  opcion: " OPCION
  case "$OPCION" in
    1) pedir_rango; ops estado --dias "$DIAS"; pausa ;;
    2) pedir_rango
       read -r -p "  pipeline [todos]: " P
       ops estado --dias "$DIAS" --pipeline "${P:-todos}" --detalle; pausa ;;
    3) pedir_rango
       read -r -p "  pipeline [todos]: " P
       P="${P:-todos}"
       echo "  --- simulacion (no escribe) ---"
       ops reparar --dias "$DIAS" --pipeline "$P"
       read -r -p "  ejecutar este plan de verdad? [s/N]: " R
       if [ "${R:-n}" = "s" ] || [ "${R:-n}" = "S" ]; then
         ops reparar --dias "$DIAS" --pipeline "$P" --aplicar
       else
         echo "  no se ejecuto nada."
       fi
       pausa ;;
    4) read -r -p "  pipeline (ventas|margen|rotacion): " P
       read -r -p "  desde (YYYYMMDD): " D
       read -r -p "  hasta (YYYYMMDD): " H
       ops cargar "$P" "$D" "$H"; pausa ;;
    5) read -r -p "  pipeline (ventas|margen|rotacion): " P
       ops refrescar "$P"; pausa ;;
    6) systemctl list-timers 'dinastia-*' --all --no-pager || true
       for U in ventas margen rotacion; do
         echo "--- dinastia-${U}-daily ---"
         journalctl -u "dinastia-${U}-daily.service" -n 5 --no-pager || true
       done
       pausa ;;
    7) tail -n 20 "${DINASTIA_OPS_LOG:-$HOME/dinastia-ops.jsonl}" 2>/dev/null \
         || echo "  (todavia no hay registro)"
       pausa ;;
    0) exit 0 ;;
    *) echo "  opcion no valida." ;;
  esac
done
