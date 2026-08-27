#!/usr/bin/env python3
"""portal_probe.py — probe the portal's public health endpoint (spec 006, T3).

Answers one question: **is the portal reachable and healthy from this box?**
It exercises DNS, TLS, the timeouts and the error taxonomy end to end while
touching **no credential at all** — ``GET /api/health`` is public, unauthenticated
and outside the login rate limit, so running this in a loop cannot cost anyone
their access to the portal.

It is also the preflight the login budget depends on (plan §2.3): if this says
not-ok, the agent must not attempt a login.

Usage::

    uv run python scripts/portal_probe.py --health --json
    OS_PORTAL_BASE_URL=https://portal.example uv run python scripts/portal_probe.py --health

Exit codes: ``0`` healthy · ``1`` unhealthy or unreachable · ``2`` misconfigured.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime

from os_system_agent.portal.client import (
    HEALTH_PATH,
    PortalClient,
    build_httpx_fetcher,
    check_health,
    make_httpx_client,
    normalize_base_url,
)
from os_system_agent.portal.errors import PortalConfigError
from os_system_agent.redaction import redact

BASE_URL_ENV_VAR = "OS_PORTAL_BASE_URL"

EXIT_OK = 0
EXIT_UNHEALTHY = 1
EXIT_MISCONFIGURED = 2


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe the portal's public /api/health endpoint. Sends no credentials.",
    )
    parser.add_argument(
        "--health",
        action="store_true",
        default=True,
        help="Check /api/health (the only supported mode; no other path is reachable).",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get(BASE_URL_ENV_VAR, ""),
        help=f"Portal base URL, https only (default: ${BASE_URL_ENV_VAR}).",
    )
    parser.add_argument("--json", action="store_true", help="Emit one JSON object on stdout.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        # Validate the URL before opening a socket, so a misconfiguration is a
        # clear message and not a confusing connection error.
        base_url = normalize_base_url(args.base_url)
    except PortalConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        print(
            f"set {BASE_URL_ENV_VAR} or pass --base-url (https only, no credentials in the URL)",
            file=sys.stderr,
        )
        return EXIT_MISCONFIGURED

    with make_httpx_client() as http_client:
        # The allowlist is left at its default, which holds only the public
        # health path: this CLI cannot be pointed at a data endpoint.
        probed = PortalClient(base_url=base_url, fetcher=build_httpx_fetcher(http_client))
        # No `headers=`: this probe never carries a cookie or an auth header.
        result = check_health(probed)

    payload = {
        "ok": result.ok,
        "status": result.status,
        "db": result.db,
        "latency_ms": result.latency_ms,
        "host": probed.host,
        "path": HEALTH_PATH,
        "checked_at": datetime.now(UTC).isoformat(),
        "detail": result.detail,
    }

    if args.json:
        print(redact(json.dumps(payload, sort_keys=True)))
    else:
        state = "OK" if result.ok else "NOT OK"
        print(redact(f"portal {probed.host}{HEALTH_PATH}: {state} (status={result.status})"))
        print(redact(f"  db={result.db} latency_ms={result.latency_ms} detail={result.detail}"))

    return EXIT_OK if result.ok else EXIT_UNHEALTHY


if __name__ == "__main__":
    raise SystemExit(main())
