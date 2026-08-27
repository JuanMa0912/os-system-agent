"""Read-only HTTP client for the portal API (spec 006 RF-03, plan 006 §1, §3.2).

This module is the agent's **only** outbound HTTP door to the portal, and it is
deliberately narrow:

* **``GET`` only.** The portal's permission model cannot express "read only"
  (the same subdashboard permission enables ``GET`` and ``POST``/``PATCH``), so
  the distinction is enforced here. A non-``GET`` raises *before* the request is
  built, so there is no code path that emits one (CA-01).
* **Exact-path allowlist**, never a prefix: ``/api/margenes/data`` is thirteen
  different endpoints behind one path, and some expose customer tax ids and
  salesperson national ids (plan §3.1). Prefix matching would grant them all.
* **One connection, no retries, explicit timeouts on all four httpx phases.**
  The agent shares an outbound IP with the offices; a retrying, concurrent
  client is how you lock people out of their own portal.
* **The fetcher is injectable** (same pattern as ``collector.Runner``), so every
  rule above is tested without a network.

Nothing here knows how to log in. Login and cookies are the session module's
job; this client only carries whatever headers it is handed, and never puts
them in a message, a log line or a ``repr``.
"""

from __future__ import annotations

import json
import re
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from os_system_agent.portal.errors import (
    InvalidParameterError,
    MethodNotAllowedError,
    Outcome,
    PathNotAllowedError,
    PortalConfigError,
    PortalError,
    classify,
    error_for,
    map_transport_error,
)
from os_system_agent.redaction import redact

# The only method the agent may ever emit.
ALLOWED_METHODS = frozenset({"GET"})

# Public, unauthenticated, and outside the login rate limit: the free signal we
# use to know whether the portal is alive before spending anything (plan §2.3).
HEALTH_PATH = "/api/health"

# T3 only reaches the public endpoint. The intent catalog (T6) supplies the rest,
# versioned in the repo so the allowlist is reviewable in a PR diff (plan §3.3).
DEFAULT_ALLOWED_PATHS = frozenset({HEALTH_PATH})

# One request at a time and zero retries: both are safety limits, not tuning.
MAX_CONCURRENCY = 1
MAX_RETRIES = 0

_PARAM_NAME = re.compile(r"\A[A-Za-z][A-Za-z0-9_]{0,39}\Z")
_PARAM_VALUE_MAX = 200
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_DETAIL_MAX = 120


@dataclass(frozen=True)
class Timeouts:
    """Explicit deadlines for the four httpx phases. No phase may be unbounded."""

    connect: float = 5.0
    read: float = 15.0
    write: float = 10.0
    pool: float = 5.0


DEFAULT_TIMEOUTS = Timeouts()


@dataclass(frozen=True, repr=False)
class EndpointCall:
    """One outbound request, already validated against both allowlists.

    ``headers`` is where an authentication cookie would live, so ``__repr__``
    shows header **names** only. A frozen dataclass is repr'd into tracebacks
    and debug logs by default, and that is precisely how a cookie leaks.
    """

    method: str
    path: str
    url: str
    params: tuple[tuple[str, str], ...] = ()
    headers: tuple[tuple[str, str], ...] = ()
    timeouts: Timeouts = DEFAULT_TIMEOUTS

    def __repr__(self) -> str:
        names = ",".join(name for name, _ in self.headers)
        keys = ",".join(key for key, _ in self.params)
        return (
            f"EndpointCall(method={self.method!r}, path={self.path!r}, "
            f"param_keys={keys!r}, header_names={names!r})"
        )


@dataclass(frozen=True, repr=False)
class HttpResponse:
    """What a :data:`Fetcher` returns: a raw HTTP answer, not yet classified.

    ``__repr__`` masks header values for the same reason as
    :class:`EndpointCall`: ``Set-Cookie`` arrives here.
    """

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: str = ""
    elapsed_ms: float = 0.0

    def __repr__(self) -> str:
        names = ",".join(sorted(str(name).lower() for name in self.headers))
        return (
            f"HttpResponse(status={self.status}, header_names={names!r}, "
            f"body_bytes={len(self.body)}, elapsed_ms={self.elapsed_ms:.1f})"
        )


# (call) -> raw response. Injected in tests so the allowlists, the taxonomy and
# the budget are all exercised without touching the network.
Fetcher = Callable[[EndpointCall], HttpResponse]


@dataclass(frozen=True)
class PortalResponse:
    """A classified answer handed back to callers.

    Carries no headers on purpose: nothing downstream (template, model, log,
    memory) needs them, and not carrying them means they cannot leak.
    """

    path: str
    status: int
    outcome: Outcome
    body: str
    elapsed_ms: float

    def json(self) -> Any | None:
        """Parse the body as JSON, or ``None`` when it is not valid JSON."""
        try:
            return json.loads(self.body)
        except (ValueError, TypeError):
            return None


@dataclass(frozen=True)
class HealthResult:
    """Outcome of the public ``/api/health`` probe. Never raises for the caller."""

    ok: bool
    status: int | None
    latency_ms: float | None = None
    db: str | None = None
    detail: str = ""


def log_event(payload: Mapping[str, object]) -> None:
    """Write one structured, redacted JSON line to stderr (mcp_server.py pattern).

    The portal's own audit trail records sessions and logins but **not queries**
    (spec §3), so this line plus the ledger are the only record of what the
    agent looked at. Paths come from the allowlist and only parameter *keys* are
    logged, so no business value or secret rides along.
    """
    print(
        redact(json.dumps(payload, separators=(",", ":"), sort_keys=True)),
        file=sys.stderr,
        flush=True,
    )


def assert_method_allowed(method: str, *, path: str = "") -> str:
    """Return the normalized method, or raise before anything is emitted (CA-01)."""
    normalized = method.strip().upper() if isinstance(method, str) else ""
    if normalized not in ALLOWED_METHODS:
        raise MethodNotAllowedError(
            f"method {method!r} is not allowed: the agent may only issue {sorted(ALLOWED_METHODS)}",
            method=normalized or "?",
            path=path,
        )
    return normalized


def assert_path_allowed(path: str, allowed: frozenset[str]) -> str:
    """Validate ``path``'s shape and require an **exact** allowlist match.

    Shape checks come first so a hostile path is rejected on its form, not only
    on its absence from the list: ``//evil.example`` is protocol-relative and
    would silently change host, and ``..`` can walk out of an allowed prefix.
    """
    if not isinstance(path, str) or not path:
        raise PathNotAllowedError("path must be a non-empty string", path="")
    if not path.startswith("/"):
        raise PathNotAllowedError(f"path must start with '/': {path!r}", path=path)
    if path.startswith("//"):
        raise PathNotAllowedError(f"protocol-relative path would change host: {path!r}", path=path)
    if "://" in path or "\\" in path or ".." in path:
        raise PathNotAllowedError(f"path has an unsafe shape: {path!r}", path=path)
    if any(ch in path for ch in "?#") or _CONTROL_CHARS.search(path) or " " in path:
        raise PathNotAllowedError(
            f"path must carry no query, fragment or whitespace: {path!r}", path=path
        )
    if path not in allowed:
        raise PathNotAllowedError(
            f"path {path!r} is not in the exact-match allowlist ({len(allowed)} allowed paths)",
            path=path,
        )
    return path


def assert_params_allowed(
    params: Mapping[str, str] | None, *, path: str
) -> tuple[tuple[str, str], ...]:
    """Validate query parameters and return them as a stable, ordered tuple.

    Parameters come from the intent catalog, never from a model or a chat
    message (RF-02); these checks make that guarantee enforceable rather than
    merely documented.
    """
    if not params:
        return ()
    validated: list[tuple[str, str]] = []
    for key, value in params.items():
        if not isinstance(key, str) or not _PARAM_NAME.match(key):
            raise InvalidParameterError(f"invalid query parameter name: {key!r}", path=path)
        if not isinstance(value, str):
            raise InvalidParameterError(
                f"query parameter {key!r} must be a string, got {type(value).__name__}",
                path=path,
            )
        if len(value) > _PARAM_VALUE_MAX or _CONTROL_CHARS.search(value):
            raise InvalidParameterError(
                f"query parameter {key!r} has an unacceptable value", path=path
            )
        validated.append((key, value))
    return tuple(sorted(validated))


def normalize_base_url(base_url: str) -> str:
    """Validate the portal base URL and strip its trailing slash (fail closed).

    Refuses HTTP and refuses embedded ``user:password@`` credentials. The error
    messages never echo the URL: the whole point of rejecting userinfo is that
    the string may contain a password.
    """
    if not isinstance(base_url, str) or not base_url.strip():
        raise PortalConfigError("portal base URL is not configured")
    parts = urlsplit(base_url.strip())
    if parts.scheme != "https":
        raise PortalConfigError(
            f"portal base URL must use https (got scheme {parts.scheme or 'none'!r})"
        )
    if "@" in parts.netloc:
        raise PortalConfigError("portal base URL must not embed credentials (user:password@host)")
    if not parts.hostname:
        raise PortalConfigError("portal base URL has no host")
    if parts.query or parts.fragment:
        raise PortalConfigError("portal base URL must carry no query or fragment")
    prefix = parts.path.rstrip("/")
    return f"{parts.scheme}://{parts.netloc}{prefix}"


@dataclass(frozen=True)
class PortalClient:
    """Read-only client bound to one base URL, one allowlist and one fetcher."""

    base_url: str
    fetcher: Fetcher
    allowed_paths: frozenset[str] = DEFAULT_ALLOWED_PATHS
    timeouts: Timeouts = DEFAULT_TIMEOUTS

    def __post_init__(self) -> None:
        # Normalizing here (rather than in a factory) means every construction
        # path — tests included — goes through the same validation.
        object.__setattr__(self, "base_url", normalize_base_url(self.base_url))

    @property
    def host(self) -> str:
        """Host of the configured deployment; useful to confirm which one (S1)."""
        return urlsplit(self.base_url).netloc

    def build_url(self, path: str, params: tuple[tuple[str, str], ...]) -> str:
        """Compose the absolute URL from the validated path and parameters."""
        query = f"?{urlencode(params)}" if params else ""
        return f"{self.base_url}{path}{query}"

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> PortalResponse:
        """Validate, emit and classify one request. Raises on any failure outcome.

        Order matters and is part of the contract: the method allowlist is
        checked first, so a ``POST`` never reaches URL construction, let alone
        the fetcher.
        """
        normalized_method = assert_method_allowed(
            method, path=path if isinstance(path, str) else ""
        )
        allowed_path = assert_path_allowed(path, self.allowed_paths)
        allowed_params = assert_params_allowed(params, path=allowed_path)
        call = EndpointCall(
            method=normalized_method,
            path=allowed_path,
            url=self.build_url(allowed_path, allowed_params),
            params=allowed_params,
            headers=tuple((str(k), str(v)) for k, v in (headers or {}).items()),
            timeouts=self.timeouts,
        )

        response = self.fetcher(call)
        outcome = classify(response.status, response.headers, response.body)
        log_event(
            {
                "portal": "request",
                "method": call.method,
                "path": call.path,
                "param_keys": [key for key, _ in call.params],
                "status": response.status,
                "outcome": outcome.value,
                "terminal": outcome.is_terminal,
                "ms": round(response.elapsed_ms, 1),
            }
        )
        failure = error_for(
            response.status,
            response.headers,
            response.body,
            method=call.method,
            path=call.path,
        )
        if failure is not None:
            raise failure
        return PortalResponse(
            path=call.path,
            status=response.status,
            outcome=outcome,
            body=response.body,
            elapsed_ms=response.elapsed_ms,
        )

    def get(
        self,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> PortalResponse:
        """Issue an allowlisted ``GET``."""
        return self.request("GET", path, params=params, headers=headers)


def make_httpx_client(*, timeouts: Timeouts = DEFAULT_TIMEOUTS) -> httpx.Client:
    """Build the real transport with every safety knob set explicitly.

    * a deadline on each of the four phases — no phase may hang forever;
    * ``retries=0`` — retrying is a decision for the caller with the budget in
      hand, never an invisible transport behaviour;
    * one connection — the agent is never concurrent against the portal;
    * ``follow_redirects=False`` — a redirect could leave the allowlisted host
      and path, which would make both allowlists meaningless;
    * ``trust_env=True`` on purpose: the box sits behind a corporate TLS stack
      and needs ``SSL_CERT_FILE``/proxy settings from the environment to connect
      at all.
    """
    transport = httpx.HTTPTransport(
        retries=MAX_RETRIES,
        limits=httpx.Limits(
            max_connections=MAX_CONCURRENCY,
            max_keepalive_connections=MAX_CONCURRENCY,
        ),
    )
    return httpx.Client(
        timeout=httpx.Timeout(
            connect=timeouts.connect,
            read=timeouts.read,
            write=timeouts.write,
            pool=timeouts.pool,
        ),
        transport=transport,
        follow_redirects=False,
        trust_env=True,
    )


def build_httpx_fetcher(http_client: httpx.Client) -> Fetcher:
    """Adapt an ``httpx.Client`` to the :data:`Fetcher` contract."""

    def fetch(call: EndpointCall) -> HttpResponse:
        started = time.monotonic()
        try:
            response = http_client.request(call.method, call.url, headers=dict(call.headers))
        except httpx.HTTPError as exc:
            # `from None`: httpx puts the full request URL in some messages, and
            # a chained traceback ends up in the logs verbatim. The mapped error
            # keeps the class name, which is what actually helps diagnose.
            raise map_transport_error(exc, method=call.method, path=call.path) from None
        return HttpResponse(
            status=response.status_code,
            headers=dict(response.headers),
            body=response.text,
            elapsed_ms=(time.monotonic() - started) * 1000.0,
        )

    return fetch


def check_health(client: PortalClient) -> HealthResult:
    """Probe the public ``/api/health`` endpoint. Never raises.

    This is the mandatory preflight before any login (plan §2.3): if the portal
    is down, the most common failure mode costs zero login budget instead of
    burning slots that belong to the people in the offices.
    """
    try:
        response = client.get(HEALTH_PATH)
    except PortalError as exc:
        return HealthResult(
            ok=False,
            status=exc.status,
            detail=_short(f"{exc.outcome.value}: {exc}"),
        )

    payload = response.json()
    if not isinstance(payload, Mapping):
        return HealthResult(
            ok=False,
            status=response.status,
            detail="health endpoint returned a body that is not a JSON object",
        )
    db = payload.get("db")
    latency = payload.get("latencyMs")
    return HealthResult(
        ok=payload.get("ok") is True,
        status=response.status,
        latency_ms=float(latency) if isinstance(latency, (int, float)) else None,
        db=_short(str(db)) if db is not None else None,
        detail="ok" if payload.get("ok") is True else "health endpoint reported ok=false",
    )


def _short(text: str) -> str:
    """Redact and truncate free text before it can reach a log or a message."""
    return redact(text)[:_DETAIL_MAX]
