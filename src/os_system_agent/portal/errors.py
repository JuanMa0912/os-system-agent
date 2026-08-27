"""Error taxonomy for the portal client (spec 006 §3, plan 006 §2.2 and §9).

Every answer the portal can give us is mapped to exactly one :class:`Outcome`,
and each outcome states whether it is **terminal** — whether trying again could
ever help. That distinction is the whole point of this module: the agent box
shares an outbound IP with the offices, so a retry loop against a wrong password
would lock real people out of the portal for fifteen minutes.

Two rules hold for every message built here, and each has a test:

1. **No secrets.** An exception message never contains a cookie, a password, an
   authentication header or a full URL. It carries the method, the allowlisted
   path and the status code — enough to debug, nothing to leak. Exception text
   ends up in logs and in Telegram.
2. **Fail closed.** A status or body shape we do not recognise is
   :attr:`Outcome.UNEXPECTED` and terminal, never "probably fine, retry".
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, ClassVar

# The portal's own rate limit: 10 failed logins per IP in a 15-minute window
# (`src/app/api/auth/login/route.ts`). Every backoff decision here and the login
# budget in `budget.py` derive from this single constant, so there is one number
# to change if the portal ever changes its own.
RATE_LIMIT_WINDOW_MINUTES = 15
RATE_LIMIT_WINDOW_SECONDS = RATE_LIMIT_WINDOW_MINUTES * 60

RETRY_AFTER_HEADER = "retry-after"

# A data endpoint never answers with megabytes. A 200 bigger than this is not
# parsed at all: it is treated as a contract break, so a runaway or hostile
# response cannot make us spend memory parsing it.
MAX_BODY_BYTES = 1_000_000


class Outcome(StrEnum):
    """What a portal answer means for the agent."""

    OK = "OK"
    NO_DATA = "NO_DATA"
    TERMINAL_AUTH = "TERMINAL_AUTH"
    ACCOUNT_DISABLED = "ACCOUNT_DISABLED"
    RATE_LIMITED = "RATE_LIMITED"
    TRANSIENT = "TRANSIENT"
    CONTRACT = "CONTRACT"
    UNEXPECTED = "UNEXPECTED"

    @property
    def is_terminal(self) -> bool:
        """True when retrying cannot help, so the agent must stop and report.

        ``TRANSIENT`` is the only failure that may be retried (with a long
        backoff). ``OK``/``NO_DATA`` are not failures at all: both are valid
        answers, and ``NO_DATA`` means the portal answered correctly that the
        requested range has no data (RF-05).
        """
        return self not in (Outcome.OK, Outcome.NO_DATA, Outcome.TRANSIENT)


class PortalError(RuntimeError):
    """Base class for every portal client failure.

    Subclasses fix :attr:`outcome`; ``is_terminal`` is derived from it so the
    two can never disagree.
    """

    outcome: ClassVar[Outcome] = Outcome.UNEXPECTED

    def __init__(
        self,
        message: str,
        *,
        method: str = "GET",
        path: str = "",
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.path = path
        self.status = status

    @property
    def is_terminal(self) -> bool:
        """Whether retrying this failure could ever succeed."""
        return self.outcome.is_terminal


class PortalConfigError(PortalError):
    """Raised when the client is configured with something unusable (fail closed)."""


class MethodNotAllowedError(PortalError):
    """Raised when a non-``GET`` method is attempted (RF-03, CA-01).

    The portal's permission model does not distinguish reading from writing, so
    this allowlist is the only thing standing between the agent's cookie and a
    write it is technically able to perform. It raises *before* any request is
    emitted.
    """


class PathNotAllowedError(PortalError):
    """Raised when a path is malformed or is not in the exact-match allowlist."""


class InvalidParameterError(PortalError):
    """Raised when a query parameter name or value is not an accepted shape."""


class TerminalAuthError(PortalError):
    """HTTP 401 — wrong credentials, or the 30-day password expiry hit.

    Terminal: a bad password does not fix itself, and every retry burns a slot
    of a rate-limit window shared with the offices.
    """

    outcome: ClassVar[Outcome] = Outcome.TERMINAL_AUTH


class AccountDisabledError(PortalError):
    """HTTP 403 — the account was deliberately revoked. Terminal by definition."""

    outcome: ClassVar[Outcome] = Outcome.ACCOUNT_DISABLED


class RateLimitedError(PortalError):
    """HTTP 429 — the rate limit is already spent. Terminal for this window."""

    outcome: ClassVar[Outcome] = Outcome.RATE_LIMITED

    def __init__(
        self,
        message: str,
        *,
        method: str = "GET",
        path: str = "",
        status: int | None = 429,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message, method=method, path=path, status=status)
        self.retry_after_seconds = retry_after_seconds

    @property
    def effective_retry_after_seconds(self) -> int:
        """Seconds to wait: the server's ``Retry-After``, or the full window.

        Falling back to the whole window is the conservative choice — waiting
        too long costs us a late answer; waiting too little costs other people
        their access to the portal.
        """
        if self.retry_after_seconds is None:
            return RATE_LIMIT_WINDOW_SECONDS
        return self.retry_after_seconds


class TransientError(PortalError):
    """Portal down, timeout or DNS/transport failure — retryable with backoff.

    Does not consume login budget when it happens outside the login route
    (plan §2.2): the portal being down must not cost the shared rate limit.
    """

    outcome: ClassVar[Outcome] = Outcome.TRANSIENT


class ResponseContractError(PortalError):
    """The portal answered 200 with a body that is not the agreed contract.

    Terminal: answering with garbage is worse than not answering (plan §9), so
    the agent refuses and reports the broken intent instead of retrying.
    """

    outcome: ClassVar[Outcome] = Outcome.CONTRACT


class UnexpectedStatusError(PortalError):
    """A status we do not model (3xx not followed, 404, ...). Terminal, fail closed."""

    outcome: ClassVar[Outcome] = Outcome.UNEXPECTED


class PreflightFailedError(PortalError):
    """``GET /api/health`` was not OK, so no login was attempted (plan §2.3).

    Not a login failure: no budget was spent, because the request never reached
    the login route.
    """

    outcome: ClassVar[Outcome] = Outcome.TRANSIENT


class LoginBudgetExhaustedError(PortalError):
    """The agent's own login budget for this window is spent (plan §2.1)."""

    outcome: ClassVar[Outcome] = Outcome.RATE_LIMITED

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: int,
    ) -> None:
        super().__init__(message, method="POST", path="<login>", status=None)
        self.retry_after_seconds = retry_after_seconds


class LoginLockedError(PortalError):
    """Another process already holds the single-flight login lock (RNF-02)."""

    outcome: ClassVar[Outcome] = Outcome.RATE_LIMITED


class BudgetStateError(PortalError):
    """The persisted budget file is missing a valid shape, or is used unlocked.

    Fail closed and loud: a budget we cannot read is a budget we cannot trust,
    and continuing would mean logging in with no counter at all.
    """


def normalize_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Lowercase header names so lookups are case-insensitive."""
    return {str(name).lower(): str(value) for name, value in headers.items()}


def retry_after_seconds(headers: Mapping[str, str]) -> int | None:
    """Parse ``Retry-After`` as a non-negative number of seconds, else ``None``.

    The HTTP-date form is deliberately not parsed: callers fall back to the full
    rate-limit window, which is never shorter than the date would have been.
    """
    raw = normalize_headers(headers).get(RETRY_AFTER_HEADER)
    if raw is None:
        return None
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    return value if value >= 0 else None


def _parse_json(body: str) -> Any | None:
    """Parse ``body`` as JSON, returning ``None`` when it is not valid JSON."""
    if len(body.encode("utf-8", errors="ignore")) > MAX_BODY_BYTES:
        return None
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return None


def _declares_no_data(payload: Mapping[str, Any]) -> bool:
    """True when the payload's cut-off date is null/empty — i.e. no data (plan §9).

    Checked at the top level and one level into ``data``, which are the two
    shapes the portal uses. A number without its cut-off date misleads (RF-04),
    so an absent date is an explicit "no data", never a zero.
    """
    scopes: list[Mapping[str, Any]] = [payload]
    nested = payload.get("data")
    if isinstance(nested, Mapping):
        scopes.append(nested)
    for scope in scopes:
        if "updatedAt" not in scope:
            continue
        value = scope["updatedAt"]
        if value is None or (isinstance(value, str) and not value.strip()):
            return True
    return False


def _classify_ok_body(body: str) -> Outcome:
    """Classify a 200 by its body: HTTP success is not business success."""
    payload = _parse_json(body)
    if payload is None:
        # A 200 that is not JSON is usually an HTML login page or an error page
        # served with the wrong status. Never treat it as data.
        return Outcome.CONTRACT
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if error is not None and error is not False and error != "":
            return Outcome.CONTRACT
        if payload.get("ok") is False:
            return Outcome.CONTRACT
        if _declares_no_data(payload):
            return Outcome.NO_DATA
    return Outcome.OK


def classify(status: int, headers: Mapping[str, str], body: str) -> Outcome:
    """Map one HTTP answer to its :class:`Outcome`.

    Pure and side-effect free, so the golden cases in
    ``evals/cases/portal_errors_cases.yaml`` exercise exactly the code that runs
    in production.
    """
    if status == 401:
        return Outcome.TERMINAL_AUTH
    if status == 403:
        return Outcome.ACCOUNT_DISABLED
    if status == 429:
        return Outcome.RATE_LIMITED
    if 500 <= status <= 599:
        return Outcome.TRANSIENT
    if status != 200:
        # Includes 3xx: redirects are never followed (a redirect can leave the
        # allowlisted host and path), so seeing one means something changed.
        return Outcome.UNEXPECTED
    return _classify_ok_body(body)


def error_for(
    status: int,
    headers: Mapping[str, str],
    body: str,
    *,
    method: str,
    path: str,
) -> PortalError | None:
    """Build the exception for an answer, or ``None`` when it is not a failure.

    ``OK`` and ``NO_DATA`` return ``None``: "there is no data for that range" is
    a correct answer the caller must render, not an error to raise.

    The message is assembled from the method, the allowlisted path and the
    status only — never from ``headers`` or ``body``, which are exactly where a
    cookie or a password would be.
    """
    outcome = classify(status, headers, body)
    where = f"{method} {path}"
    if outcome in (Outcome.OK, Outcome.NO_DATA):
        return None
    if outcome is Outcome.TERMINAL_AUTH:
        return TerminalAuthError(
            f"{where}: 401 unauthenticated — bad credentials or expired password; "
            "terminal, no retry",
            method=method,
            path=path,
            status=status,
        )
    if outcome is Outcome.ACCOUNT_DISABLED:
        return AccountDisabledError(
            f"{where}: 403 forbidden — the agent account looks disabled or out of scope; "
            "terminal, no retry",
            method=method,
            path=path,
            status=status,
        )
    if outcome is Outcome.RATE_LIMITED:
        seconds = retry_after_seconds(headers)
        wait = seconds if seconds is not None else RATE_LIMIT_WINDOW_SECONDS
        return RateLimitedError(
            f"{where}: 429 rate limited — wait {wait}s before any further attempt",
            method=method,
            path=path,
            status=status,
            retry_after_seconds=seconds,
        )
    if outcome is Outcome.TRANSIENT:
        return TransientError(
            f"{where}: {status} — the portal is unavailable; retryable with backoff",
            method=method,
            path=path,
            status=status,
        )
    if outcome is Outcome.CONTRACT:
        return ResponseContractError(
            f"{where}: 200 with a body that does not match the expected contract; "
            "refusing to answer with it",
            method=method,
            path=path,
            status=status,
        )
    return UnexpectedStatusError(
        f"{where}: unexpected status {status}; failing closed",
        method=method,
        path=path,
        status=status,
    )


def map_transport_error(exc: Exception, *, method: str, path: str) -> PortalError:
    """Map a transport-level failure (timeout, DNS, TLS, refused) to the taxonomy.

    Only the exception's **class name** is used. Its message is discarded on
    purpose: httpx puts the full request URL in some of them, and that URL can
    carry query parameters — or, if someone ever misconfigures a base URL with
    userinfo, a password.
    """
    return TransientError(
        f"{method} {path}: transport failure ({type(exc).__name__}); retryable with backoff",
        method=method,
        path=path,
        status=None,
    )
