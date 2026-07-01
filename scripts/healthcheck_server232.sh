#!/usr/bin/env bash
# healthcheck_server232.sh — read-only health snapshot of the target server.
# Phase 1: SKELETON. Wire up once the SSH alias is configured (see docs/).
# Every command here must stay read-only (CLAUDE.md §10).
set -euo pipefail

ALIAS="${1:-server232}"

echo "[os_system_agent] read-only healthcheck for '${ALIAS}' — NOT YET IMPLEMENTED"
echo "Intended read-only checks:"
echo "  ssh ${ALIAS} hostname"
echo "  ssh ${ALIAS} date"
echo "  ssh ${ALIAS} uptime"
echo "  ssh ${ALIAS} df -h"
echo "  ssh ${ALIAS} systemctl status <allowlisted-service> --no-pager"
exit 0
