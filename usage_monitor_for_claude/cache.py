"""
Usage Cache
============

Thread-safe cache for API data - single source of truth for all usage
state.  All API refresh requests go through ``UsageCache.update()``,
which uses a lock to prevent concurrent calls and a cooldown to prevent
calls that are too close together (HTTP 429).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from .api import fetch_prepaid_credits, fetch_profile, fetch_usage, read_access_token
from .claude_cli import RefreshResult, refresh_token
from .formatting import merge_scoped_limits
from .settings import MAX_BACKOFF, POLL_FAST, POLL_INTERVAL

__all__ = ['CacheSnapshot', 'UpdateResult', 'UsageCache']

log = logging.getLogger(__name__)

# The prepaid balance changes rarely (only on spend or purchase), so it is
# refreshed far less often than usage to avoid doubling request volume.
_BALANCE_REFRESH_INTERVAL = 900


@dataclass(frozen=True)
class CacheSnapshot:
    """Immutable, consistent snapshot of cache state for the popup."""

    usage: dict[str, Any]
    profile: dict[str, Any] | None
    last_success_time: float | None
    refreshing: bool
    last_error: str | None
    version: int


@dataclass(frozen=True)
class UpdateResult:
    """Result of a ``UsageCache.update()`` call.

    Attributes
    ----------
    data : dict or None
        Raw API response dict, or ``None`` when the call was skipped
        (lock held or cooldown active).
    token_refresh : RefreshResult or None
        Set when a token refresh was attempted after a 401 auth error.
    """

    data: dict[str, Any] | None
    token_refresh: RefreshResult | None = None


class UsageCache:
    """Thread-safe cache managing API data, cooldown, and error state.

    All callers (poll loop, popup) go through ``update()`` instead
    of calling ``fetch_usage()`` directly.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._profile_lock = threading.Lock()
        self._usage: dict[str, Any] = {}
        self._profile: dict[str, Any] | None = None
        self._profile_token: str | None = None
        self._last_success_time: float | None = None
        self._refreshing = False
        self._last_error: str | None = None
        self._version = 0
        self._consecutive_errors = 0
        self._last_failed_token: str | None = None
        self._rate_limit_until: float = 0
        # Last-known prepaid balance in minor units; kept across transient balance-fetch failures.
        self._balance_cents: float | None = None
        self._balance_fetched_at: float = 0

    # Public properties

    @property
    def usage(self) -> dict[str, Any]:
        """Last successful usage data (empty dict before first success)."""
        return self._usage

    @property
    def profile(self) -> dict[str, Any] | None:
        return self._profile

    @property
    def last_success_time(self) -> float | None:
        return self._last_success_time

    @property
    def refreshing(self) -> bool:
        return self._refreshing

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def version(self) -> int:
        """Change counter - incremented on every state change."""
        return self._version

    @property
    def consecutive_errors(self) -> int:
        return self._consecutive_errors

    @property
    def rate_limit_remaining(self) -> float:
        """Seconds remaining in the rate-limit backoff window, or 0."""
        return max(self._rate_limit_until - time.time(), 0)

    @property
    def snapshot(self) -> CacheSnapshot:
        """Return a consistent snapshot for the popup to display."""
        with self._state_lock:
            return CacheSnapshot(
                usage=self._usage,
                profile=self._profile,
                last_success_time=self._last_success_time,
                refreshing=self._refreshing,
                last_error=self._last_error,
                version=self._version,
            )

    # Public methods

    def ensure_profile(self) -> None:
        """Fetch the account profile if not yet loaded, or re-fetch if the access token changed (thread-safe).

        Acquires ``_lock`` around the HTTP call to prevent concurrent
        API requests with ``update()``.  Skips the fetch while a
        rate-limit backoff is active so a failed profile request does not
        keep hammering an already 429-limited endpoint.
        """
        current_token = read_access_token()
        if self._profile is not None and self._profile_token == current_token:
            return

        with self._profile_lock:
            current_token = read_access_token()
            if self._profile is not None and self._profile_token == current_token:
                return

            # Respect the rate-limit backoff window. A failed profile fetch
            # leaves _profile as None, so without this guard every popup open
            # would re-fire a request against an already 429-limited endpoint
            # and could prolong the backoff.
            if time.time() < self._rate_limit_until:
                log.debug('ensure_profile skipped (rate-limit backoff, %.0fs remaining)', self._rate_limit_until - time.time())
                return

            log.info('fetch_profile started')
            with self._lock:
                profile = fetch_profile()
            with self._state_lock:
                self._profile = profile
                self._profile_token = current_token
                self._version += 1
            log.info('fetch_profile -> %s', 'OK' if profile else 'failed')

    def update(self) -> UpdateResult:
        """Fetch usage data with lock and cooldown protection.

        Returns
        -------
        UpdateResult
            Contains the API response dict (``data``), or ``None``
            when the call was skipped.  If a token refresh was
            attempted, ``token_refresh`` carries the outcome.
        """
        if not self._lock.acquire(blocking=False):
            log.debug('update skipped (another update in progress)')
            return UpdateResult(data=None)

        try:
            return self._update_locked()
        finally:
            self._lock.release()

    # Private helpers

    def _update_locked(self) -> UpdateResult:
        """Execute the actual update while holding ``_lock``."""
        if self._last_success_time is not None and time.time() - self._last_success_time < POLL_FAST:
            log.debug('update skipped (cooldown, %.0fs remaining)', POLL_FAST - (time.time() - self._last_success_time))
            return UpdateResult(data=None)

        if time.time() < self._rate_limit_until:
            log.debug('update skipped (rate-limit backoff, %.0fs remaining)', self._rate_limit_until - time.time())
            return UpdateResult(data=None)

        if self._last_failed_token is not None:
            if read_access_token() == self._last_failed_token:
                log.debug('update skipped (token unchanged after auth failure)')
                return UpdateResult(data=None)
            self._last_failed_token = None

        with self._state_lock:
            self._refreshing = True
            self._version += 1

        try:
            return self._fetch_and_process()
        except Exception:
            with self._state_lock:
                self._refreshing = False
                self._version += 1
            raise

    def _fetch_and_process(self) -> UpdateResult:
        """Fetch usage data and process the response."""
        token_before = read_access_token()
        log.info('fetch_usage started')
        data = fetch_usage()

        if 'error' in data:
            self._record_error(data)

            if data.get('rate_limited'):
                retry_after = data.get('retry_after')
                if retry_after is not None and retry_after > 0:
                    delay = min(max(retry_after, POLL_INTERVAL), MAX_BACKOFF)
                else:
                    delay = min(POLL_INTERVAL * (2 ** max(self._consecutive_errors - 1, 0)), MAX_BACKOFF)
                self._rate_limit_until = time.time() + delay
                log.warning('fetch_usage -> rate limited, backoff %.0fs', delay)

            token_refresh = None
            if data.get('auth_error'):
                log.warning('fetch_usage -> auth error, attempting token refresh')
                token_refresh = self._try_token_refresh(token_before)
                if token_refresh is not None and self._last_error is None:
                    # Token refresh succeeded and retry was successful
                    return UpdateResult(data=self._usage, token_refresh=token_refresh)
                if token_refresh is None:
                    # Refresh failed or token unchanged - block this token
                    self._last_failed_token = token_before
            elif not data.get('rate_limited'):
                log.warning('fetch_usage -> error: %s', data['error'])

            with self._state_lock:
                self._refreshing = False
                self._version += 1
            return UpdateResult(data=data, token_refresh=token_refresh)

        data = self._augment_usage(data)
        pct_5h = (data.get('five_hour') or {}).get('utilization')
        pct_7d = (data.get('seven_day') or {}).get('utilization')
        log.info('fetch_usage -> OK (5h: %s%%, 7d: %s%%)', pct_5h if pct_5h is not None else '?', pct_7d if pct_7d is not None else '?')
        self._record_success(data)
        return UpdateResult(data=data)

    def _record_error(self, data: dict[str, Any], *, count: bool = True) -> None:
        """Apply common state updates after a failed API response.

        Parameters
        ----------
        data : dict
            API response containing ``'error'`` and optional ``'server_message'``.
        count : bool
            If True (default), increment ``_consecutive_errors``.
        """
        with self._state_lock:
            if count:
                self._consecutive_errors += 1
            error = data['error']
            server_msg = data.get('server_message')
            if server_msg:
                error += f'\n{server_msg}'
            self._last_error = error

    def _record_success(self, data: dict[str, Any]) -> None:
        """Apply common state updates after a successful API response."""
        # _usage is always reassigned (never mutated in place), so existing
        # CacheSnapshot references remain valid after this update.
        with self._state_lock:
            self._consecutive_errors = 0
            self._last_error = None
            self._last_success_time = time.time()
            self._rate_limit_until = 0
            self._last_failed_token = None
            self._usage = data
            self._refreshing = False
            self._version += 1

    def _augment_usage(self, data: dict[str, Any]) -> dict[str, Any]:
        """Fold model-scoped limits and the prepaid balance into a usage response.

        Model-scoped weekly limits are promoted to top-level fields; the
        prepaid credit balance (fetched separately and cached) is written into
        ``spend.balance`` so the popup's usage-credits section can show it.
        The balance is only fetched when usage credits are enabled - there is
        no point querying a wallet whose value would never be displayed.  The
        input dict is not mutated.
        """
        data = merge_scoped_limits(data)

        extra = data.get('extra_usage')
        if isinstance(extra, dict) and extra.get('is_enabled'):
            self._refresh_balance()
            if self._balance_cents is not None:
                data = dict(data)
                spend = data.get('spend')
                spend = dict(spend) if isinstance(spend, dict) else {}
                spend['balance'] = self._balance_cents
                data['spend'] = spend

        return data

    def _refresh_balance(self) -> None:
        """Best-effort refresh of the cached prepaid balance from the org wallet.

        Throttled to ``_BALANCE_REFRESH_INTERVAL`` (the balance changes rarely)
        and skipped while the shared rate-limit backoff is active.  Reads the
        organization UUID from the cached profile and queries the prepaid-credits
        endpoint.  The last-known balance is retained on any failure so a
        transient problem does not make the balance flicker out of the popup.
        """
        now = time.time()
        if now < self._rate_limit_until:
            return
        if now - self._balance_fetched_at < _BALANCE_REFRESH_INTERVAL:
            return

        profile = self._profile
        org_uuid = (profile.get('organization') or {}).get('uuid') if isinstance(profile, dict) else None
        if not org_uuid:
            return

        # Count the attempt (even on failure) so a failing endpoint is not re-hit every poll.
        self._balance_fetched_at = now
        wallet = fetch_prepaid_credits(org_uuid)
        if not isinstance(wallet, dict):
            return

        amount = wallet.get('amount')
        if not isinstance(amount, bool) and isinstance(amount, (int, float)):
            self._balance_cents = amount

    def reset_balance(self) -> None:
        """Clear the cached prepaid balance so the next poll re-fetches it.

        Called on account switch so a previous account's balance is never shown
        for the new account (and to defeat the refresh throttle after a switch).
        The fields are only read/written during a locked update on the same
        poll thread, so no lock is needed here.
        """
        self._balance_cents = None
        self._balance_fetched_at = 0

    def _try_token_refresh(self, token_before: str | None) -> RefreshResult | None:
        """Attempt to refresh the OAuth token via ``claude update``.

        Parameters
        ----------
        token_before : str or None
            The token that was used for the failed request.  Used to
            detect whether the refresh actually produced a new token.

        Returns
        -------
        RefreshResult or None
            The refresh outcome, or ``None`` if the CLI is not available
            or the token didn't change.
        """
        result = refresh_token()
        if not result.success:
            log.info('token refresh failed: %s', result.error)
            return None

        if read_access_token() == token_before:
            log.info('token refresh succeeded but token unchanged')
            return None

        # Token changed - retry the API call
        log.info('token changed, retrying fetch_usage')
        data = fetch_usage()
        if 'error' not in data:
            log.info('retry -> OK')
            self._record_success(self._augment_usage(data))
            return result

        log.warning('retry -> error: %s', data['error'])
        # Update error message but do not increment _consecutive_errors
        # again (the caller already counted this update cycle as one error).
        self._record_error(data, count=False)

        return result
