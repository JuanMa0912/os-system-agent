"""Read-only portal API access (spec 006).

The agent holds **no database credentials**. Everything it knows about the
business it reads over HTTPS from the portal API, as a dedicated account, and
every request leaves through :class:`~os_system_agent.portal.client.PortalClient`
— ``GET`` only, exact-path allowlist, one connection, no retries.

Three modules, three jobs:

* :mod:`~os_system_agent.portal.errors` — the outcome taxonomy: what each answer
  means and whether retrying could ever help.
* :mod:`~os_system_agent.portal.client` — the only outbound HTTP door.
* :mod:`~os_system_agent.portal.budget` — the persisted login budget and the
  cross-process lock that keep a bug here from locking the offices out of the
  portal they share an IP with.
"""

from __future__ import annotations

from os_system_agent.portal.budget import (
    LOGIN_FAILURE_WINDOW_MINUTES,
    MAX_LOGIN_FAILURES_PER_WINDOW,
    BudgetState,
    LoginBudget,
    default_budget_path,
    login_guard,
)
from os_system_agent.portal.client import (
    ALLOWED_METHODS,
    DEFAULT_ALLOWED_PATHS,
    HEALTH_PATH,
    EndpointCall,
    Fetcher,
    HealthResult,
    HttpResponse,
    PortalClient,
    PortalResponse,
    Timeouts,
    build_httpx_fetcher,
    check_health,
    log_event,
    make_httpx_client,
)
from os_system_agent.portal.errors import (
    AccountDisabledError,
    BudgetStateError,
    InvalidParameterError,
    LoginBudgetExhaustedError,
    LoginLockedError,
    MethodNotAllowedError,
    Outcome,
    PathNotAllowedError,
    PortalConfigError,
    PortalError,
    PreflightFailedError,
    RateLimitedError,
    ResponseContractError,
    TerminalAuthError,
    TransientError,
    UnexpectedStatusError,
)

__all__ = [
    "ALLOWED_METHODS",
    "DEFAULT_ALLOWED_PATHS",
    "HEALTH_PATH",
    "LOGIN_FAILURE_WINDOW_MINUTES",
    "MAX_LOGIN_FAILURES_PER_WINDOW",
    "AccountDisabledError",
    "BudgetState",
    "BudgetStateError",
    "EndpointCall",
    "Fetcher",
    "HealthResult",
    "HttpResponse",
    "InvalidParameterError",
    "LoginBudget",
    "LoginBudgetExhaustedError",
    "LoginLockedError",
    "MethodNotAllowedError",
    "Outcome",
    "PathNotAllowedError",
    "PortalClient",
    "PortalConfigError",
    "PortalError",
    "PortalResponse",
    "PreflightFailedError",
    "RateLimitedError",
    "ResponseContractError",
    "TerminalAuthError",
    "Timeouts",
    "TransientError",
    "UnexpectedStatusError",
    "build_httpx_fetcher",
    "check_health",
    "default_budget_path",
    "log_event",
    "login_guard",
    "make_httpx_client",
]
