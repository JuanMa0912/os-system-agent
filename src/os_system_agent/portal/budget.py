"""Persisted login budget and single-flight lock (spec 006 RNF-02, plan 006 §2.1).

**Why this file exists.** The agent box and the offices leave to the internet
through the same IP, and the portal blocks an IP after 10 failed logins in 15
minutes. So a retry loop here does not degrade the agent: it locks the people in
the offices out of their own portal for a quarter of an hour — and if the portal
has no ``AUDIT_IP_HMAC_SECRET`` defined, the block key is a ``/24`` and reaches
254 addresses.

Three properties make that structurally impossible rather than carefully avoided:

1. **The counter is on disk, not in memory.** A crash loop is exactly the
   situation that produces repeated failures; an in-memory counter would reset
   on every restart and hammer the portal.
2. **The counter is incremented before the request leaves**, not after it fails.
   If the process dies mid-flight the attempt still counts — the pessimistic
   direction is the safe one.
3. **A cross-process ``O_EXCL`` lock** guards both the counter and the login
   itself, so two agent processes cannot log in at once. That also protects the
   human: ``createSessionReplacingOthers`` revokes previous sessions, so a
   second concurrent login would evict the first.

The preflight against the public ``/api/health`` closes the loop: the most
common failure (portal down) costs zero budget, because no login is attempted.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from os_system_agent.portal.client import HealthResult, log_event
from os_system_agent.portal.errors import (
    RATE_LIMIT_WINDOW_MINUTES,
    BudgetStateError,
    LoginBudgetExhaustedError,
    LoginLockedError,
    PreflightFailedError,
)

# The portal allows 10 failed logins per IP per window. We take 2 and leave 8 for
# the people who share the IP. Two is enough to survive one stale-cookie relogin
# plus one genuine retry, and small enough that a bug here cannot lock anyone out.
PORTAL_IP_FAILURE_LIMIT = 10
MAX_LOGIN_FAILURES_PER_WINDOW = 2
LOGIN_FAILURE_WINDOW_MINUTES = RATE_LIMIT_WINDOW_MINUTES
LOGIN_FAILURE_WINDOW = timedelta(minutes=LOGIN_FAILURE_WINDOW_MINUTES)

# Where the counter lives. Absolute by construction: a relative path would mean
# "one budget per working directory", which is no budget at all.
BUDGET_ENV_VAR = "OS_PORTAL_BUDGET_PATH"
BUDGET_FILENAME = "portal-login-budget.json"
BUDGET_DIRNAME = "var"

STATE_VERSION = 1
BUDGET_FILE_MODE = 0o600
BUDGET_DIR_MODE = 0o700

LOCK_SUFFIX = ".lock"
# A lock older than this belonged to a process that died holding it. Reclaiming
# it is safe because it is far longer than any login round trip, and refusing to
# reclaim would wedge the agent permanently after a single crash.
LOCK_STALE_SECONDS = 120

Clock = Callable[[], datetime]
HealthCheck = Callable[[], HealthResult]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def default_budget_path() -> Path:
    """Absolute path of the budget file, honouring :data:`BUDGET_ENV_VAR`.

    Fails closed when the environment override is relative: silently resolving
    it against the current directory would create a second, independent counter.
    """
    override = os.environ.get(BUDGET_ENV_VAR)
    if override:
        candidate = Path(override)
        if not candidate.is_absolute():
            raise BudgetStateError(
                f"{BUDGET_ENV_VAR} must be an absolute path; a relative one would "
                "create one budget per working directory"
            )
        return candidate
    # src/os_system_agent/portal/budget.py -> repo root
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / BUDGET_DIRNAME / BUDGET_FILENAME


@dataclass(frozen=True)
class BudgetState:
    """The persisted counter for the current window."""

    window_started_at: datetime
    failures: int
    last_attempt_at: datetime | None = None

    @property
    def remaining(self) -> int:
        """Login attempts still allowed in this window."""
        return max(0, MAX_LOGIN_FAILURES_PER_WINDOW - self.failures)

    @property
    def exhausted(self) -> bool:
        """True when no further login may be attempted in this window."""
        return self.remaining <= 0

    def seconds_until_window_ends(self, now: datetime) -> int:
        """Whole seconds left before the counter resets (never negative)."""
        ends_at = self.window_started_at + LOGIN_FAILURE_WINDOW
        return max(0, int((ends_at - now).total_seconds()))

    def to_json(self) -> dict[str, Any]:
        """Serialize to the on-disk shape."""
        return {
            "version": STATE_VERSION,
            "window_started_at": self.window_started_at.isoformat(),
            "failures": self.failures,
            "last_attempt_at": (self.last_attempt_at.isoformat() if self.last_attempt_at else None),
        }


def _parse_dt(value: Any, *, field_name: str) -> datetime:
    """Parse a stored ISO timestamp, failing closed on anything unexpected."""
    if not isinstance(value, str):
        raise BudgetStateError(f"budget file field {field_name!r} is not a timestamp string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise BudgetStateError(
            f"budget file field {field_name!r} is not a valid ISO timestamp"
        ) from exc
    # A naive timestamp would compare wrongly against an aware `now` and could
    # silently reset the window; treat it as UTC, which is what we always write.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class LoginBudget:
    """Cross-process login budget backed by one small JSON file.

    Reads are lock-free; every mutation requires the lock held via :meth:`lock`,
    and :meth:`reserve` refuses to run without it. That refusal is deliberate: a
    counter incremented outside the lock is a counter two processes can race.
    """

    def __init__(self, path: Path | None = None, *, clock: Clock = _utcnow) -> None:
        resolved = Path(path) if path is not None else default_budget_path()
        if not resolved.is_absolute():
            raise BudgetStateError(f"budget path must be absolute, got {resolved}")
        self._path = resolved
        self._lock_path = resolved.with_name(resolved.name + LOCK_SUFFIX)
        self._clock = clock
        self._locked = False

    @property
    def path(self) -> Path:
        """Absolute path of the persisted counter."""
        return self._path

    @property
    def lock_path(self) -> Path:
        """Absolute path of the single-flight lock file."""
        return self._lock_path

    @property
    def is_locked(self) -> bool:
        """Whether *this* instance currently holds the lock."""
        return self._locked

    # ---- reading ---------------------------------------------------------

    def snapshot(self) -> BudgetState:
        """Current state with window expiry already applied.

        A missing file is a legitimate first run and yields a fresh window; a
        file we cannot parse raises, because a budget we cannot read is a budget
        we cannot trust.
        """
        now = self._clock()
        if not self._path.exists():
            return BudgetState(window_started_at=now, failures=0)

        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise BudgetStateError(
                f"could not read the login budget file: {type(exc).__name__}"
            ) from exc
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise BudgetStateError(
                "login budget file is not valid JSON; refusing to log in"
            ) from exc
        if not isinstance(data, dict):
            raise BudgetStateError("login budget file must contain a JSON object")
        if data.get("version") != STATE_VERSION:
            raise BudgetStateError(
                f"login budget file has version {data.get('version')!r}, expected {STATE_VERSION}"
            )
        failures = data.get("failures")
        if isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
            raise BudgetStateError("login budget file has an invalid 'failures' counter")

        state = BudgetState(
            window_started_at=_parse_dt(
                data.get("window_started_at"), field_name="window_started_at"
            ),
            failures=failures,
            last_attempt_at=(
                _parse_dt(data.get("last_attempt_at"), field_name="last_attempt_at")
                if data.get("last_attempt_at") is not None
                else None
            ),
        )
        if now - state.window_started_at >= LOGIN_FAILURE_WINDOW:
            return BudgetState(window_started_at=now, failures=0)
        return state

    def remaining(self) -> int:
        """Login attempts still allowed in the current window."""
        return self.snapshot().remaining

    # ---- writing ---------------------------------------------------------

    def _write(self, state: BudgetState) -> None:
        """Persist ``state`` atomically with ``0600`` permissions, then fsync.

        The fsync is what makes property 2 real: the counter must be on the
        platter before the login request goes out, not merely in a buffer that a
        crash would discard.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=BUDGET_DIR_MODE)
        tmp = self._path.with_name(self._path.name + f".{os.getpid()}.tmp")
        payload = json.dumps(state.to_json(), separators=(",", ":"), sort_keys=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, BUDGET_FILE_MODE)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            with suppress(OSError):
                os.unlink(tmp)
            raise
        os.replace(tmp, self._path)
        # os.replace does not carry the mode on every platform; set it again so
        # the counter is never world-readable.
        with suppress(OSError):
            os.chmod(self._path, BUDGET_FILE_MODE)

    def _require_lock(self, action: str) -> None:
        if not self._locked:
            raise BudgetStateError(
                f"refusing to {action} without the single-flight lock held; "
                "call LoginBudget.lock() first"
            )

    def reserve(self) -> BudgetState:
        """Count one login attempt **before** it is sent, or refuse.

        Returns the state after the increment. The caller may only send the
        login request after this returns.
        """
        self._require_lock("reserve a login attempt")
        now = self._clock()
        state = self.snapshot()
        if state.exhausted:
            raise LoginBudgetExhaustedError(
                f"login budget spent: {MAX_LOGIN_FAILURES_PER_WINDOW} attempts in "
                f"{LOGIN_FAILURE_WINDOW_MINUTES} min, leaving the rest of the shared "
                f"limit of {PORTAL_IP_FAILURE_LIMIT} for people",
                retry_after_seconds=state.seconds_until_window_ends(now),
            )
        reserved = replace(state, failures=state.failures + 1, last_attempt_at=now)
        self._write(reserved)
        log_event(
            {
                "portal": "login_reserved",
                "failures": reserved.failures,
                "remaining": reserved.remaining,
                "window_minutes": LOGIN_FAILURE_WINDOW_MINUTES,
            }
        )
        return reserved

    def record_success(self) -> None:
        """Clear the counter after a login that actually succeeded."""
        self._require_lock("record a successful login")
        now = self._clock()
        self._write(BudgetState(window_started_at=now, failures=0, last_attempt_at=now))
        log_event({"portal": "login_success", "remaining": MAX_LOGIN_FAILURES_PER_WINDOW})

    def record_rate_limited(self, retry_after_seconds: int | None = None) -> None:
        """Burn the whole window after a 429: the limit is already spent.

        Cheaper to sit out a window we might not owe than to discover we owed it
        by locking someone else out.
        """
        self._require_lock("record a rate limit")
        now = self._clock()
        self._write(
            BudgetState(
                window_started_at=now,
                failures=MAX_LOGIN_FAILURES_PER_WINDOW,
                last_attempt_at=now,
            )
        )
        log_event(
            {
                "portal": "login_rate_limited",
                "retry_after_s": retry_after_seconds,
                "remaining": 0,
            }
        )

    # ---- locking ---------------------------------------------------------

    def _acquire(self) -> int:
        """Create the lock file exclusively, reclaiming it only if it is stale."""
        self._lock_path.parent.mkdir(parents=True, exist_ok=True, mode=BUDGET_DIR_MODE)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            return os.open(self._lock_path, flags, BUDGET_FILE_MODE)
        except FileExistsError:
            pass

        age = self._lock_age_seconds()
        if age is None or age < LOCK_STALE_SECONDS:
            raise LoginLockedError(
                "another agent process holds the portal login lock; "
                "refusing a concurrent login (it would revoke the other session)"
            )
        # Stale: the holder died. Reclaim once — if we lose that race, the
        # winner is a live holder and we back off exactly as above.
        with suppress(OSError):
            os.unlink(self._lock_path)
        try:
            return os.open(self._lock_path, flags, BUDGET_FILE_MODE)
        except FileExistsError as exc:
            raise LoginLockedError(
                "another agent process reclaimed the portal login lock first"
            ) from exc

    def _lock_age_seconds(self) -> float | None:
        try:
            return max(0.0, time.time() - self._lock_path.stat().st_mtime)
        except OSError:
            # It vanished between the failed create and this stat: the holder
            # released it. Treat as fresh so we back off and the caller retries.
            return None

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Hold the cross-process single-flight lock for the duration of a login."""
        if self._locked:
            raise BudgetStateError("the login lock is already held by this instance")
        fd = self._acquire()
        try:
            with suppress(OSError):
                os.write(fd, str(os.getpid()).encode("ascii"))
            self._locked = True
            yield
        finally:
            self._locked = False
            with suppress(OSError):
                os.close(fd)
            with suppress(OSError):
                os.unlink(self._lock_path)


@contextmanager
def login_guard(budget: LoginBudget, *, health_check: HealthCheck) -> Iterator[BudgetState]:
    """Everything that must happen around a login, in the only correct order.

    1. take the cross-process lock — one login at a time, ever;
    2. preflight ``GET /api/health`` — if the portal is down, **no budget is
       spent** and no login is attempted;
    3. reserve one attempt on disk **before** the caller sends anything;
    4. on a clean exit, clear the counter; on any exception, leave the attempt
       counted.

    Usage::

        with login_guard(budget, health_check=lambda: check_health(client)):
            do_login()   # must raise on failure
    """
    with budget.lock():
        health = health_check()
        if not health.ok:
            log_event(
                {
                    "portal": "login_skipped",
                    "reason": "preflight_not_ok",
                    "health_status": health.status,
                    "budget_spent": False,
                }
            )
            raise PreflightFailedError(
                f"portal health check is not ok (status={health.status}); "
                "skipping login so no shared rate-limit budget is spent",
                method="GET",
                path="/api/health",
                status=health.status,
            )
        state = budget.reserve()
        yield state
        budget.record_success()
