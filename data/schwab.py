"""Schwab Trader API — OAuth, token store, and market-data adapters.

WHY THIS EXISTS
---------------
Meridian's equity feed is Massive (paid Stocks tier). The Schwab Trader API
serves the same market data — quotes, daily price history, instrument search,
fundamentals — and its market-data endpoints cost nothing on a funded account.
This module is the swap-in source, sitting beside ``data/massive.py`` with the
SAME return shapes so ``data/market.py`` can route to either one without a
consumer change.

THE PRICE OF THIS SOURCE: A SEVEN-DAY CLOCK
-------------------------------------------
Access tokens live 30 minutes and are refreshed silently here. The REFRESH
token hard-expires SEVEN DAYS after the original authorization, and refreshing
does NOT move that wall — Schwab documents no way to extend it. So the operator
re-authorizes weekly. This module makes that cheap (``scripts/schwab_auth.py``
prints the URL; one command exchanges the callback) and LOUD
(``token_status()`` reports days_left; an expired grant raises
``SchwabAuthExpired`` rather than serving last week's prices).

A refresh token is ONE-TIME-USE: every refresh spends it and returns a
replacement. Losing the replacement costs a human at a browser, so
``_refresh_locked`` serialises refreshes and persists the result before the
token is used — two threads refreshing at once would invalidate one another.

WHAT IS NOT HERE
----------------
Option chains. Massive's plan 403s options and the app already falls back to
yfinance (``data/options_yf.py``), so a Schwab chain is a shape-parity project
of its own, not part of the quote/history swap. Crypto has no Schwab product;
``data/crypto_assets.py`` owns that.

SHAPE CONTRACT
--------------
Every adapter mirrors its ``data/massive.py`` sibling. Where Schwab genuinely
lacks a field (employees, homepage, logo, SIC, shares outstanding) the value is
None — never a plausible-looking number. Two deliberate divergences, both
documented at their site: 52-week extremes ARE populated (Schwab carries them,
Massive does not), and ``preMarketChange`` is 0.0 when Schwab has no
extended-hours delta rather than a guessed move.
"""

from __future__ import annotations

import base64
import datetime
import functools
import logging
import secrets
import threading
import time
import urllib.parse
from collections import deque
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from config import SCHWAB_APP_KEY, SCHWAB_APP_SECRET, SCHWAB_CALLBACK_URL
from data.cache import ttl_cache
from data.crypto import load_json_store, write_json

# One definition of the US session clock: massive.market_session is pure and
# needs no key, so importing it never depends on the Massive plan.
from data.massive import market_session
from data.session import data_root

log = logging.getLogger(__name__)

_API = "https://api.schwabapi.com"
_AUTHORIZE_URL = f"{_API}/v1/oauth/authorize"
_TOKEN_URL = f"{_API}/v1/oauth/token"
_ET = ZoneInfo("America/New_York")

_TOKEN_FILE = "schwab_tokens.json"
_TOKEN_IDENTITY = "schwab_tokens"

#: The refresh token's hard wall, counted from the ORIGINAL authorization.
_REFRESH_LIFETIME_SECONDS = 7 * 24 * 3600
#: Refresh this many seconds before the access token lapses.
_ACCESS_SKEW_SECONDS = 60

_TOKEN_STATES = ("connected", "expired", "disconnected")

#: Serialises every read-modify-write of the token store.
#:
#: ``_mark`` runs on EVERY Schwab call that finds the grant expired, so while the
#: grant is dead this store is rewritten every few seconds. Each write mints a
#: fresh salt and therefore pays a 600k-iteration PBKDF2 (~200ms), which makes
#: the read -> mutate -> write span wide: a ``_mark`` that read before
#: ``begin_authorization`` wrote can land its stale copy AFTER it and silently
#: restore the previous ``pending_state``. The operator then completes the login
#: perfectly and the callback answers "authorization state mismatch".
_store_lock = threading.RLock()


def _store_serialized(fn):
    """Hold the token-store lock across a whole read-modify-write."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _store_lock:
            return fn(*args, **kwargs)

    return wrapper


class SchwabError(RuntimeError):
    """Any Schwab API failure that is not an expired authorization."""


class SchwabNotConfigured(SchwabError):
    """SCHWAB_APP_KEY / SCHWAB_APP_SECRET are unset — nothing can be called."""


class SchwabAuthExpired(SchwabError):
    """The grant is gone (7-day wall, revoked, or never connected).

    Callers must degrade to an explicit empty/stale state and surface a
    reconnect prompt. Retrying cannot fix this — a human has to re-authorize.
    """


# ---------------------------------------------------------------------------
# Token store (encrypted at rest, like every other PII/credential store)
# ---------------------------------------------------------------------------


def _token_path() -> Path:
    """Resolved through data_root() at CALL time so tests isolate per fixture."""
    return data_root() / _TOKEN_FILE


def configured() -> bool:
    """True when the OAuth app credentials are present."""
    return bool(SCHWAB_APP_KEY and SCHWAB_APP_SECRET)


def _float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _normalize_token(raw) -> dict:
    """Every key always present, including on legacy/pre-field rows.

    A row written before a field existed is missing the KEY, not holding None,
    so a consumer doing ``tok["state"]`` would raise on exactly the rows that
    matter. Normalise at read; derive state only when it is absent.
    """
    raw = raw if isinstance(raw, dict) else {}
    now = time.time()
    access = str(raw.get("access_token") or "")
    refresh = str(raw.get("refresh_token") or "")
    access_expires = _float(raw.get("access_expires_at"))
    refresh_expires = _float(raw.get("refresh_expires_at"))

    state = raw.get("state")
    if state not in _TOKEN_STATES:
        if refresh and refresh_expires and refresh_expires <= now:
            state = "expired"
        elif refresh:
            state = "connected"
        else:
            state = "disconnected"

    return {
        "access_token": access,
        "refresh_token": refresh,
        "access_expires_at": access_expires,
        "refresh_expires_at": refresh_expires,
        "issued_at": _float(raw.get("issued_at")),
        "last_refresh_at": _float(raw.get("last_refresh_at")),
        "state": state,
        "last_error": raw.get("last_error"),
        # Single-use CSRF state for the OAuth redirect; cleared on completion.
        "pending_state": str(raw.get("pending_state") or ""),
        "pending_state_at": _float(raw.get("pending_state_at")),
    }


def _read_token() -> dict:
    return _normalize_token(load_json_store(_token_path(), _TOKEN_IDENTITY, {}))


def _write_token(token: dict) -> dict:
    clean = _normalize_token(token)
    write_json(_token_path(), _TOKEN_IDENTITY, clean)
    return clean


def _mark(state: str, error: str | None = None) -> None:
    with _store_lock:
        token = _read_token()
        new_state = state if state in _TOKEN_STATES else "expired"
        # Never rewrite an unchanged mark. This is called by every failed quote
        # call, and each write costs a PBKDF2 plus the read-modify-write window
        # that can swallow a freshly minted OAuth state.
        if token["state"] == new_state and token["last_error"] == error:
            return
        token["state"] = new_state
        token["last_error"] = error
        _write_token(token)


def token_status() -> dict:
    """Operator-facing status. Never returns a token value."""
    token = _read_token()
    now = time.time()
    days_left = 0.0
    if token["refresh_expires_at"]:
        days_left = max(0.0, (token["refresh_expires_at"] - now) / 86400.0)
    return {
        "configured": configured(),
        "state": token["state"],
        "connected": token["state"] == "connected" and bool(token["refresh_token"]),
        "days_left": round(days_left, 1),
        "refresh_expires_at": (
            datetime.datetime.fromtimestamp(token["refresh_expires_at"], datetime.timezone.utc).isoformat()
            if token["refresh_expires_at"]
            else None
        ),
        "last_refresh_at": (
            datetime.datetime.fromtimestamp(token["last_refresh_at"], datetime.timezone.utc).isoformat()
            if token["last_refresh_at"]
            else None
        ),
        "last_error": token["last_error"],
    }


def disconnect() -> dict:
    """Forget the grant. The file stays (audit trail); the tokens do not."""
    with _store_lock:
        token = _read_token()
        token.update(
            access_token="",
            refresh_token="",
            access_expires_at=0.0,
            refresh_expires_at=0.0,
            state="disconnected",
            last_error=None,
        )
        return _write_token(token)


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


def _basic_auth() -> dict[str, str]:
    if not configured():
        raise SchwabNotConfigured(
            "SCHWAB_APP_KEY / SCHWAB_APP_SECRET are not set — register an app at "
            "developer.schwab.com and add them to /etc/meridian/env"
        )
    raw = f"{SCHWAB_APP_KEY}:{SCHWAB_APP_SECRET}".encode()
    return {
        "Authorization": f"Basic {base64.b64encode(raw).decode()}",
        "Content-Type": "application/x-www-form-urlencoded",
    }


def authorize_url(state: str = "meridian") -> str:
    """The URL the operator opens to grant read-only access."""
    if not configured():
        raise SchwabNotConfigured("SCHWAB_APP_KEY / SCHWAB_APP_SECRET are not set")
    params = {
        "response_type": "code",
        "client_id": SCHWAB_APP_KEY,
        "redirect_uri": SCHWAB_CALLBACK_URL,
        "scope": "readonly",
        "state": state,
    }
    return f"{_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


#: How long an authorize URL stays usable before the state is stale.
_PENDING_STATE_TTL_SECONDS = 600


def begin_authorization() -> str:
    """Mint a single-use state for the OAuth redirect and remember it.

    The callback is the ONE unauthenticated endpoint in this flow (Schwab
    redirects a plain browser there), so the state is what proves the callback
    belongs to an authorization this app started. It is single-use and expires.
    """
    state = secrets.token_urlsafe(24)
    with _store_lock:
        token = _read_token()
        token["pending_state"] = state
        token["pending_state_at"] = time.time()
        _write_token(token)
    return state


def complete_authorization(code: str, state: str) -> dict:
    """Verify the redirect's state, then exchange the code. Never reuses state."""
    with _store_lock:
        token = _read_token()
        expected = token["pending_state"]
        issued = token["pending_state_at"]
        if not expected:
            raise SchwabError("no authorization in flight — start from the Reconnect button")
        if not secrets.compare_digest(str(state or ""), expected):
            raise SchwabError("authorization state mismatch — start the connect flow again")
        if not issued or time.time() - issued > _PENDING_STATE_TTL_SECONDS:
            raise SchwabError("authorization expired before the callback arrived — start again")

        # Spend it before the exchange: a callback replayed after this point fails.
        token["pending_state"] = ""
        token["pending_state_at"] = 0.0
        _write_token(token)
    # Outside the lock: the exchange is a network round trip and must not block
    # the quote threads behind it.
    return exchange_code(code)


@_store_serialized
def _store_token_response(body: dict, *, is_refresh: bool) -> dict:
    """Persist a token response, preserving the original 7-day wall on refresh."""
    now = time.time()
    previous = _read_token()
    access_token = str(body.get("access_token") or "")
    refresh_token = str(body.get("refresh_token") or "")

    if is_refresh:
        # A refresh returns a rotated refresh token but does NOT extend the
        # wall. Keep the original issued_at; fall back to the stored expiry,
        # then to now (which under-reports remaining life rather than inventing it).
        issued_at = previous["issued_at"] or (
            previous["refresh_expires_at"] - _REFRESH_LIFETIME_SECONDS if previous["refresh_expires_at"] else now
        )
        if not refresh_token:
            # Schwab occasionally omits the replacement — keep the spent one's
            # value rather than blanking the grant.
            refresh_token = previous["refresh_token"]
    else:
        issued_at = now

    token = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "access_expires_at": now + _float(body.get("expires_in") or 1800),
        "refresh_expires_at": issued_at + _REFRESH_LIFETIME_SECONDS,
        "issued_at": issued_at,
        "last_refresh_at": now,
        "state": "connected" if (access_token or refresh_token) else "expired",
        "last_error": None,
    }
    return _write_token(token)


def exchange_code(code: str) -> dict:
    """Exchange an authorization code (or the whole callback URL) for a grant."""
    raw = (code or "").strip()
    if "code=" in raw:
        # The operator pastes the callback URL; the code inside is percent-encoded
        # once more than the body expects.
        parsed = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query)
        values = parsed.get("code") or []
        raw = urllib.parse.unquote(values[0]) if values else raw
    if not raw:
        raise SchwabError("no authorization code supplied")

    resp = requests.post(
        _TOKEN_URL,
        data={"grant_type": "authorization_code", "code": raw, "redirect_uri": SCHWAB_CALLBACK_URL},
        headers=_basic_auth(),
        timeout=30,
    )
    if resp.status_code != 200:
        raise SchwabError(f"token exchange failed: HTTP {resp.status_code} {resp.text[:300]}")
    return _store_token_response(resp.json(), is_refresh=False)


_refresh_lock = threading.Lock()


def _refresh_locked(token: dict, *, force: bool = False) -> dict:
    """Spend the refresh token and persist its replacement. Serialised.

    ``force`` skips the "another thread already refreshed" shortcut only when
    the stored token is UNCHANGED: the server rejected it (401), so a token that
    merely looks fresh must not be reused — but a token another thread just
    minted must.
    """
    now = time.time()
    if token["refresh_expires_at"] and now >= token["refresh_expires_at"]:
        _mark("expired", "refresh token hit the 7-day wall")
        raise SchwabAuthExpired(
            "Schwab's 7-day refresh window has lapsed — run scripts/schwab_auth.py url and re-authorize"
        )
    if not token["refresh_token"]:
        _mark("disconnected", "no refresh token on file")
        raise SchwabAuthExpired("Schwab is not connected — run scripts/schwab_auth.py url")

    with _refresh_lock:
        # Re-read inside the lock: another thread may have refreshed while we
        # waited, and spending an already-spent token kills the grant.
        current = _read_token()
        if force:
            if current["access_token"] and current["access_token"] != token["access_token"]:
                return current
        elif current["access_token"] and current["access_expires_at"] - time.time() > _ACCESS_SKEW_SECONDS:
            return current
        if not current["refresh_token"]:
            _mark("disconnected", "no refresh token on file")
            raise SchwabAuthExpired("Schwab is not connected — run scripts/schwab_auth.py url")

        resp = requests.post(
            _TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": current["refresh_token"]},
            headers=_basic_auth(),
            timeout=30,
        )
        if resp.status_code in (400, 401, 403):
            _mark("expired", f"refresh rejected: HTTP {resp.status_code} {resp.text[:200]}")
            raise SchwabAuthExpired(
                f"Schwab rejected the refresh (HTTP {resp.status_code}) — the 7-day grant has lapsed; re-authorize"
            )
        if resp.status_code != 200:
            raise SchwabError(f"token refresh failed: HTTP {resp.status_code} {resp.text[:300]}")
        return _store_token_response(resp.json(), is_refresh=True)


def access_token(*, force: bool = False) -> str:
    """A live access token, refreshed when within the skew window."""
    token = _read_token()
    if not token["refresh_token"] and not token["access_token"]:
        raise SchwabAuthExpired("Schwab is not connected — run scripts/schwab_auth.py url")
    if not force and token["access_token"] and token["access_expires_at"] - time.time() > _ACCESS_SKEW_SECONDS:
        return token["access_token"]
    return _refresh_locked(token, force=force)["access_token"]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


_session = None
_session_lock = threading.Lock()


def _http():
    """A kept-alive requests Session (TLS handshake is ~70ms of every call).

    Returns None when the module's ``requests`` reference has no ``Session``
    (the test double), so ``_request`` falls back to a plain get.
    """
    global _session
    if _session is not None:
        return _session
    with _session_lock:
        if _session is None:
            try:
                sess = requests.Session()
                adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=32, max_retries=0)
                sess.mount("https://", adapter)
                _session = sess
            except AttributeError:
                return None
    return _session


class _RateLimiter:
    """Sliding-window limiter; Schwab's ceiling is ~120 requests/minute."""

    def __init__(self, max_calls: int = 100, window_seconds: float = 60.0):
        self._max_calls = max_calls
        self._window = window_seconds
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self._window:
                    self._calls.popleft()
                if len(self._calls) < self._max_calls:
                    self._calls.append(now)
                    return waited
                sleep_for = self._window - (now - self._calls[0])
            time.sleep(max(sleep_for, 0.05))
            waited += max(sleep_for, 0.05)


#: Two lanes over one 120/min API ceiling. Interactive requests (a page the
#: operator is waiting on) get 80/min and NEVER queue behind background work;
#: warmers and scans get their own 40/min lane. Sharing one queue meant a
#: 70-symbol page-cache warm, fired every five minutes, could make a click wait
#: a full 60s window — measured, not theorised.
_limiter = _RateLimiter(max_calls=80, window_seconds=60.0)
_bg_limiter = _RateLimiter(max_calls=40, window_seconds=60.0)

_bg_mode = threading.local()


class background_priority:
    """Mark this thread's calls as background (its own rate-limit lane)."""

    def __enter__(self):
        _bg_mode.on = True
        return self

    def __exit__(self, *exc):
        _bg_mode.on = False
        return False


def _is_background() -> bool:
    return bool(getattr(_bg_mode, "on", False))


def _request(path: str, params: dict | None = None, timeout: int = 20):
    """GET a Schwab market-data path with auth refresh, pacing and 429 backoff."""
    token = access_token()
    for attempt in (1, 2):
        waited = (_bg_limiter if _is_background() else _limiter).acquire()
        if waited > 1.0:
            log.warning("Schwab rate limiter queued %s for %.1fs", path, waited)
        started = time.monotonic()
        sess = _http()
        do_get = sess.get if sess is not None else requests.get
        resp = do_get(
            f"{_API}{path}",
            params=params or {},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=timeout,
        )
        elapsed = time.monotonic() - started
        if elapsed > 2.0:
            log.warning("Schwab %s took %.1fs", path, elapsed)
        if resp.status_code == 401 and attempt == 1:
            token = access_token(force=True)
            continue
        if resp.status_code == 429 and attempt == 1:
            try:
                delay = float(resp.headers.get("Retry-After") or 1.0)
            except (TypeError, ValueError):
                delay = 1.0
            time.sleep(min(max(delay, 0.25), 5.0))
            continue
        if resp.status_code != 200:
            raise SchwabError(f"GET {path} -> HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()
    raise SchwabError(f"GET {path} failed after retry")


# ---------------------------------------------------------------------------
# Quotes — same shape as massive.get_quotes_schwab_compat
# ---------------------------------------------------------------------------


def _quote_row(payload: dict, session: str) -> dict:
    """Reshape one Schwab quote payload into the Schwab-compatible dict.

    Schwab already speaks (almost) the shape the rest of the app consumes, so
    this is a normaliser, not a translation. Deliberate rules:

      * ``lastPrice`` prefers the regular-session print, then the live quote
        (extended hours), then the mark — the same precedence Massive's shim
        uses, so ``extract_live_price`` behaves identically on both feeds.
      * No price at all returns ``lastPrice: None`` with ``stale: True``. A
        zeroed quote would freeze NAV at a fake $0 instead of greying the row.
      * ``regular.regularMarketLastPrice`` is set to the same ``lastPrice``
        Massive emits, NOT Schwab's raw field, because consumers read that
        field first and the two feeds must not disagree on what "current" means.
    """
    quote = payload.get("quote") or {}
    regular = payload.get("regular") or {}

    last_price = regular.get("regularMarketLastPrice") or quote.get("lastPrice") or quote.get("mark")
    net_change = regular.get("regularMarketNetChange")
    if net_change is None:
        net_change = quote.get("netChange")
    net_pct = regular.get("regularMarketPercentChange")
    if net_pct is None:
        net_pct = quote.get("netPercentChange")

    stale = last_price is None

    # Schwab has no pre-market delta field. 0.0 means "no extended-hours move
    # to show" — consumers render nothing for zero; it is never a guessed move.
    extended_change = quote.get("postMarketChange") or 0.0
    extended_pct = quote.get("postMarketPercentChange") or 0.0

    return {
        "quote": {
            "lastPrice": last_price,
            "bidPrice": quote.get("bidPrice", 0),
            "askPrice": quote.get("askPrice", 0),
            "bidSize": quote.get("bidSize", 0),
            "askSize": quote.get("askSize", 0),
            "netChange": net_change if net_change is not None else 0,
            "netPercentChangeInDouble": net_pct if net_pct is not None else 0,
            "openPrice": quote.get("openPrice", 0),
            "highPrice": quote.get("highPrice", 0),
            "lowPrice": quote.get("lowPrice", 0),
            "closePrice": quote.get("closePrice", 0),
            "totalVolume": quote.get("totalVolume", 0),
            "mark": last_price,
            "stale": stale,
            "session": session,
            "preMarketChange": extended_change,
            "preMarketPercentChange": extended_pct,
            # Schwab DOES carry these (Massive's feed does not) — pass them
            # through rather than nulling a field we actually have.
            "52WkHigh": quote.get("52WkHigh"),
            "52WkLow": quote.get("52WkLow"),
        },
        "regular": {
            "regularMarketLastPrice": last_price,
            "regularMarketNetChange": net_change if net_change is not None else 0,
            "regularMarketPercentChange": net_pct if net_pct is not None else 0,
        },
        "fundamental": {},
    }


#: Quotes are cached PER SYMBOL, not per call signature. Caching the tuple
#: meant every page with a different symbol subset re-fetched the whole batch;
#: at Schwab's ~340ms per call that turned ordinary navigation into seconds.
_QUOTE_TTL_SECONDS = 30.0
_quote_cache: dict[str, tuple[float, dict]] = {}
_quote_lock = threading.Lock()


def _fetch_quotes(symbols: tuple[str, ...]) -> dict:
    """One batched quote request for exactly these symbols (uncached)."""
    data = _request(
        "/marketdata/v1/quotes",
        {"symbols": ",".join(symbols), "indicative": "false"},
    )
    session = market_session()
    out: dict[str, dict] = {}
    for sym in symbols:
        payload = data.get(sym)
        if not isinstance(payload, dict):
            # Missing symbol: emit an explicit stale row rather than dropping it,
            # so the consumer can grey it instead of silently keeping an import price.
            out[sym] = _quote_row({}, session)
            continue
        out[sym] = _quote_row(payload, session)
    return out


def get_quotes_schwab_compat(symbols: tuple[str, ...], max_age: float | None = None) -> dict:
    """Batch quotes in the Schwab-compatible shape, served per symbol.

    ``max_age`` overrides the 30s page-load TTL for callers that need live
    motion: the websocket stream asks for 2s, which is what turns a table of
    static numbers into ticking prices. Subscribers still share one upstream
    fetch — whoever refreshes first resets the stamp and the rest hit the short
    cache instead of each firing their own request.

    Only symbols not already fresh in the per-symbol cache cost a request, and
    they go out in ONE batch. An auth failure propagates (the operator must see
    it); any other failure returns what the cache had, so consumers take their
    existing "feed down → mark every row stale" path instead of a fake price.
    """
    if not symbols:
        return {}
    syms = tuple(sorted({s.upper() for s in symbols if s}))

    ttl = _QUOTE_TTL_SECONDS if max_age is None else max(0.0, float(max_age))
    now = time.time()
    out: dict[str, dict] = {}
    missing: list[str] = []
    with _quote_lock:
        for sym in syms:
            hit = _quote_cache.get(sym)
            if hit is not None and now - hit[0] < ttl:
                out[sym] = hit[1]
            else:
                missing.append(sym)

    if not missing:
        return out

    try:
        fresh = _fetch_quotes(tuple(missing))
    except SchwabAuthExpired:
        raise
    except Exception as exc:
        log.warning("Schwab batch quote failed for %d symbols: %s", len(missing), exc)
        return out

    stamp = time.time()
    with _quote_lock:
        for sym, payload in fresh.items():
            _quote_cache[sym] = (stamp, payload)
    out.update(fresh)
    return out


# ---------------------------------------------------------------------------
# Price history — same DataFrame contract as massive.get_price_history
# ---------------------------------------------------------------------------

#: Schwab accepts these period values for daily bars; pick the smallest that
#: covers the request, then trim client-side to the exact window.
_PERIOD_STEPS = ((365, 1), (730, 2), (1095, 3), (1825, 5), (3650, 10), (5475, 15), (99999, 20))


def _period_for_days(days: int) -> int:
    for limit, period in _PERIOD_STEPS:
        if days <= limit:
            return period
    return 20


@ttl_cache(ttl_seconds=3600)
def _history_period(symbol: str, period: int) -> pd.DataFrame:
    """Daily bars for a PERIOD in years, cached by period.

    Keyed on the period rather than the requested day count so a 30-day chart,
    a 90-day scan and a 365-day page share one fetch — the old key made every
    distinct window its own ~320ms Schwab call.
    """
    data = _request(
        "/marketdata/v1/pricehistory",
        {
            "symbol": symbol,
            "periodType": "year",
            "period": period,
            "frequencyType": "daily",
            "frequency": 1,
            "needExtendedHoursData": "false",
        },
    )
    candles = data.get("candles") or []
    if not candles:
        return pd.DataFrame()

    df = pd.DataFrame(candles)
    # Schwab timestamps daily candles at the session open (ms). Convert to the
    # EXCHANGE calendar date, not the UTC one: tz-naive dates at UTC-midnight are
    # what every consumer (and Massive's own shim) indexes by.
    dates = (
        pd.to_datetime(df["datetime"], unit="ms", utc=True)
        .dt.tz_convert(_ET)
        .dt.normalize()
        .dt.tz_localize(None)
    )
    df.index = pd.DatetimeIndex(dates)
    df.index.name = "date"
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    return df[keep].sort_index()


def get_price_history(symbol: str, days: int = 365) -> pd.DataFrame:
    """Daily OHLCV for *symbol*, shaped exactly like massive.get_price_history."""
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 365
    sym = (symbol or "").strip().upper()
    if not sym or days <= 0:
        return pd.DataFrame()
    try:
        return _history_period(sym, _period_for_days(days)).tail(days)
    except SchwabAuthExpired:
        raise
    except Exception as exc:
        log.warning("Schwab price history failed for %s: %s", sym, exc)
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Instrument search / fundamentals
# ---------------------------------------------------------------------------


@ttl_cache(ttl_seconds=300)
def search_instruments(query: str) -> dict:
    """Prefix symbol search, reshaped to ``{"instruments": [...]}``."""
    q = (query or "").strip().upper()
    if not q:
        return {"instruments": []}
    rows = _request("/marketdata/v1/instruments", {"symbol": q, "projection": "symbol-search"})
    instruments = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not row.get("symbol"):
            continue
        instruments.append(
            {
                "symbol": row.get("symbol", ""),
                "description": row.get("description", ""),
                "assetType": row.get("assetType") or "EQUITY",
                "exchange": row.get("exchange", ""),
            }
        )
    return {"instruments": instruments[:20]}


@ttl_cache(ttl_seconds=300)
def get_fundamentals(symbol: str) -> dict:
    """Instrument fundamentals in massive.get_fundamentals' shape.

    Schwab's FUNDAMENTAL projection carries ratios (peRatio, eps, divYield) but
    not headcount/homepage/logo/SIC/shares-outstanding. Those stay None — the
    equity shape is preserved, the values are not invented.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"instruments": []}
    rows = _request("/marketdata/v1/instruments", {"symbol": sym, "projection": "fundamental"})
    row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
    fundamental = row.get("fundamental") or {}
    out = {
        "symbol": row.get("symbol", sym),
        "description": row.get("description", ""),
        "marketCap": fundamental.get("marketCap"),
        "sharesOutstanding": None,
        "sector": fundamental.get("sector") or None,
        "exchange": row.get("exchange", ""),
    }
    for key in ("peRatio", "pegRatio", "pbRatio", "eps", "divYield", "industry"):
        if fundamental.get(key) is not None:
            out[key] = fundamental[key]
    return {
        "instruments": [
            {
                "symbol": row.get("symbol", sym),
                "description": row.get("description", ""),
                "fundamental": out,
            }
        ]
    }


# ---------------------------------------------------------------------------
# Accounts — the positions CSV, automated (READ-ONLY)
# ---------------------------------------------------------------------------

#: Canonical position columns, in the order data/chase._build_row emits them.
_POSITION_COLUMNS = [
    "symbol", "description", "asset_type", "quantity", "avg_cost",
    "market_value", "current_price", "day_pl", "day_pl_pct", "total_pl",
    "underlying", "put_call", "strike", "expiration", "option_multiplier",
    "source",
]


def get_accounts(fields: str = "positions") -> list[dict]:
    """Every linked account with balances and positions.

    Read-only: nothing here can place, modify or cancel an order.

    Requires the app's **Accounts and Trading Production** product. An app
    registered for market data alone answers ``401 Client not authorized``
    here while every quote call stays healthy — so callers must read that as
    "not entitled", never as "the account holds nothing".
    """
    return _request("/trader/v1/accounts", {"fields": fields}, timeout=30)


def _position_row(position: dict, source: str = "Schwab") -> dict | None:
    """One Schwab position → the canonical row, or None when it carries no shares."""
    instrument = position.get("instrument") or {}
    symbol = str(instrument.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    quantity = _float(position.get("longQuantity")) - _float(position.get("shortQuantity"))
    if quantity == 0:
        return None

    average_price = abs(_float(position.get("averagePrice")))
    market_value = _float(position.get("marketValue"))
    if market_value == 0 and average_price:
        market_value = quantity * average_price
    current_price = market_value / quantity if quantity else 0.0

    total_pl = _float(position.get("longOpenProfitLoss") or position.get("shortOpenProfitLoss"))
    if total_pl == 0 and average_price:
        total_pl = market_value - (average_price * quantity)

    option_fields = None
    if str(instrument.get("assetType") or "").upper() == "OPTION":
        option_fields = (
            str(instrument.get("underlyingSymbol") or "").upper(),
            str(instrument.get("putCall") or "").upper(),
            _float(instrument.get("strikePrice")),
            str(instrument.get("expirationDate") or "")[:10],
        )

    from data.chase import build_position_row

    return build_position_row(
        symbol,
        str(instrument.get("description") or symbol),
        quantity,
        current_price,
        market_value,
        _float(position.get("currentDayProfitLoss")),
        _float(position.get("currentDayProfitLossPercentage")),
        total_pl,
        average_price,
        source,
        option_fields,
    )


def positions_from_accounts(accounts) -> tuple[pd.DataFrame, dict]:
    """Map a ``/trader/v1/accounts`` payload onto (positions frame, summary).

    The summary prefers Schwab's own ``currentBalances`` — the broker's
    liquidation value is the number that must agree with the statement — and
    falls back to building it from the rows only when a payload carries no
    balances at all. Cash sums across accounts so two Schwab accounts land as
    one Schwab snapshot, matching a single positions CSV.
    """
    rows: list[dict] = []
    cash = liquidation = long_mv = short_mv = buying_power = 0.0
    saw_balances = False

    for account in accounts or []:
        securities = (account or {}).get("securitiesAccount") or (account or {})
        for position in securities.get("positions") or []:
            row = _position_row(position)
            if row is not None:
                rows.append(row)
        balances = securities.get("currentBalances") or {}
        if balances:
            saw_balances = True
            cash += _float(balances.get("cashBalance"))
            liquidation += _float(balances.get("liquidationValue"))
            long_mv += _float(balances.get("longMarketValue"))
            short_mv += _float(balances.get("shortMarketValue"))
            buying_power += _float(balances.get("buyingPower"))

    frame = pd.DataFrame(rows, columns=_POSITION_COLUMNS)
    day_pl = round(float(frame["day_pl"].sum()), 2) if len(frame) else 0.0

    if saw_balances:
        # Prefer the broker's own liquidation value; a payload carrying cash but
        # no liquidation figure still gets an honest NAV instead of zero.
        nav = liquidation if liquidation else (long_mv + short_mv + cash)
        summary = {
            "net_liquidation": round(nav, 2),
            "cash_balance": round(cash, 2),
            "buying_power": round(buying_power, 2),
            "long_market_value": round(long_mv, 2),
            "short_market_value": round(short_mv, 2),
            "day_pl": day_pl,
        }
    else:
        summary = {
            "net_liquidation": round(long_mv + short_mv + cash, 2),
            "cash_balance": round(cash, 2),
            "buying_power": 0,
            "long_market_value": round(long_mv, 2),
            "short_market_value": round(short_mv, 2),
            "day_pl": day_pl,
        }
    return frame, summary
