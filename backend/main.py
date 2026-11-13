"""FastAPI application factory and lifespan."""

import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from backend.services import scheduler  # noqa: E402

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("meridian.startup")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        scheduler.start()
    except Exception as e:
        log.error("Scheduler failed to start: %s", e)
    yield
    # Shutdown: re-encrypt the SQLite stores so no plaintext survives at rest.
    try:
        from data.gpu import reencrypt as gpu_reencrypt
        from data.snapshots import reencrypt as snapshots_reencrypt
        gpu_reencrypt()
        snapshots_reencrypt()
    except Exception as e:
        log.warning("At-rest re-encrypt on shutdown failed: %s", e)
    try:
        scheduler.stop()
    except Exception as e:
        log.warning("Scheduler stop failed: %s", e)


app = FastAPI(title="Meridian", lifespan=lifespan)


def _enforce_data_dir_mode(data_dir=None) -> None:
    """Keep data/ owner-only: it holds the encryption secret and every store.

    SECURITY.md promises 700/600, but the only code that applied it was
    ``scripts/deploy.sh`` — which ``make deploy`` does not run. Measured at 755
    on the live host. Applying it here makes the mode a property of the app, not
    of which deploy path someone used.
    """
    from pathlib import Path

    data_dir = Path(data_dir) if data_dir is not None else Path(__file__).resolve().parent.parent / "data"
    try:
        mode = data_dir.stat().st_mode & 0o777
    except OSError:
        return
    if not mode & 0o077:
        return
    try:
        data_dir.chmod(0o700)
        log.info("data/ mode %03o -> 700 (owner-only)", mode)
    except OSError as exc:
        log.warning(
            "data/ is mode %03o (group/world bits set) and could not be tightened: %s. "
            "Run: chmod 700 %s",
            mode, exc, data_dir,
        )


_enforce_data_dir_mode()


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Log every request with method, path, status, and duration.

    Health checks are excluded to keep the log signal-to-noise high. Paths
    only — never query strings, headers, or bodies.
    """

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/api/health":
            return await call_next(request)
        start = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("Request failed: %s %s", request.method, request.url.path)
            raise
        duration_ms = (time.monotonic() - start) * 1000
        log.info(
            "%s %s -> %s (%.1fms)",
            request.method, request.url.path, response.status_code, duration_ms,
        )
        try:
            from backend.services.metrics import record
            record(request.method, request.url.path, response.status_code, duration_ms)
        except Exception as e:
            # Metrics recording must never break a request; but stay audible.
            log.warning("Metrics record failed for %s: %s", request.url.path, e)
        return response


class AuthGuardMiddleware(BaseHTTPMiddleware):
    """Single-operator gate: every /api/* request needs a valid credential.

    Exemptions: /api/auth/* (login itself) and /api/health. Fail-closed: if no
    password is configured, the API is locked (403); otherwise a missing or
    invalid credential is 401. This is the outer lock — per-router require_auth
    dependencies remain as defense-in-depth for the crown-jewel stores.

    Credentials: the operator's HttpOnly login cookie, or the service bearer
    token (Authorization: Bearer <MERIDIAN_API_TOKEN>) for agents/scripts.
    """

    _EXEMPT_PREFIXES = ("/api/auth", "/api/health", "/api/broker/schwab/callback")
    # Public product-report intake: no credentials, but bounded and redacted.
    # The list endpoint stays behind the middleware and require_auth.
    _EXEMPT_EXACT = ("/api/desk/report",)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if (not path.startswith("/api") or path in self._EXEMPT_EXACT
                or path.startswith(self._EXEMPT_PREFIXES)):
            return await call_next(request)

        from data import auth

        cookie = request.cookies.get("meridian_auth")
        if not auth.is_password_set():
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=403,
                content={"detail": "Private data is locked — no password configured"},
            )
        if cookie and auth.validate_cookie(cookie):
            return await call_next(request)
        authorization = request.headers.get("authorization", "")
        if authorization.startswith("Bearer "):
            token = authorization.removeprefix("Bearer ").strip()
            if auth.verify_api_token(token):
                return await call_next(request)
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required"},
        )


app.add_middleware(RequestLogMiddleware)
app.add_middleware(AuthGuardMiddleware)

_CORS_ORIGINS = [
    o.strip() for o in os.environ.get(
        "MERIDIAN_CORS_ORIGINS",
        "http://localhost:3000,https://meridian.matthewmerino.com",
    ).split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Routers ---
from backend.routers import accounts as accounts_router  # noqa: E402
from backend.routers import alerts as alerts_router  # noqa: E402
from backend.routers import alternatives as alternatives_router  # noqa: E402
from backend.routers import analysis as analysis_router  # noqa: E402
from backend.routers import auth as auth_router  # noqa: E402
from backend.routers import broker_schwab as broker_schwab_router  # noqa: E402
from backend.routers import cash_movements as cash_movements_router  # noqa: E402
from backend.routers import chart_state as chart_state_router  # noqa: E402
from backend.routers import compass as compass_router  # noqa: E402
from backend.routers import compute_exposure as compute_exposure_router  # noqa: E402
from backend.routers import credit as credit_router  # noqa: E402
from backend.routers import desk_reports as desk_reports_router  # noqa: E402
from backend.routers import discovery as discovery_router  # noqa: E402
from backend.routers import engagement as engagement_router  # noqa: E402
from backend.routers import external_cash as external_cash_router  # noqa: E402
from backend.routers import fred as fred_router  # noqa: E402
from backend.routers import gpu as gpu_router  # noqa: E402
from backend.routers import history as history_router  # noqa: E402
from backend.routers import imports as imports_router  # noqa: E402
from backend.routers import intelligence as intel_router  # noqa: E402
from backend.routers import leaderboard as leaderboard_router  # noqa: E402
from backend.routers import liabilities as liabilities_router  # noqa: E402
from backend.routers import market as market_router  # noqa: E402
from backend.routers import meta as meta_router  # noqa: E402
from backend.routers import notes as notes_router  # noqa: E402
from backend.routers import performance as performance_router  # noqa: E402
from backend.routers import pipeline as pipeline_router  # noqa: E402
from backend.routers import prefs as prefs_router  # noqa: E402
from backend.routers import research as research_router  # noqa: E402
from backend.routers import settings as settings_router  # noqa: E402
from backend.routers import snapshots as snapshots_router  # noqa: E402
from backend.routers import sonar as sonar_router  # noqa: E402
from backend.routers import themes as themes_router  # noqa: E402
from backend.routers import theses as theses_router  # noqa: E402
from backend.routers import watchlist as watchlist_router  # noqa: E402
from backend.routers import websocket as ws_router  # noqa: E402
from backend.routers.cash_movements import position_router as cash_position_router  # noqa: E402

app.include_router(accounts_router.router)
app.include_router(alternatives_router.router)
app.include_router(auth_router.router)
app.include_router(broker_schwab_router.router)
app.include_router(chart_state_router.router)
app.include_router(market_router.router)
app.include_router(watchlist_router.router)
app.include_router(imports_router.router)
app.include_router(external_cash_router.router)
# Cash movements — the append-only ledger beside the balance stores.
app.include_router(cash_movements_router.router)
app.include_router(cash_position_router)

app.include_router(liabilities_router.router)
app.include_router(prefs_router.router)
app.include_router(snapshots_router.router)
app.include_router(performance_router.router)
app.include_router(fred_router.router)
app.include_router(gpu_router.router)
app.include_router(analysis_router.router)
app.include_router(sonar_router.router)
app.include_router(compass_router.router)
app.include_router(ws_router.router)
app.include_router(meta_router.router)
app.include_router(notes_router.router)
app.include_router(intel_router.router)
app.include_router(leaderboard_router.router)
# History — the multi-cycle read the 5-year quote feed cannot give.
app.include_router(history_router.router)
app.include_router(research_router.router)
app.include_router(settings_router.router)
app.include_router(alerts_router.router)
app.include_router(theses_router.router)
app.include_router(themes_router.router)
app.include_router(pipeline_router.router)
app.include_router(discovery_router.router)
app.include_router(engagement_router.router)
app.include_router(compute_exposure_router.router)
app.include_router(credit_router.router)
app.include_router(desk_reports_router.router)


# Resolve the commit ONCE, at import time. This describes the code THIS PROCESS
# loaded. It must NOT be re-resolved per request: doing that returned the working
# tree's current HEAD, so `make smoke` compared two calls to the same command and
# passed unconditionally — it could never detect a stale server, and a production
# restart on a feature branch looked fresh the moment the tree moved to master.
_RUNNING_COMMIT = "unknown"
_RUNNING_COMMIT_SOURCE = "unknown"
_PROCESS_STARTED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")
try:
    import subprocess as _sp

    _RUNNING_COMMIT = _sp.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, timeout=5,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ).stdout.strip() or "unknown"
    # Capture the branch too: "prod is serving a non-master branch" is the failure
    # mode that actually bit, and it is invisible from a bare SHA.
    _RUNNING_COMMIT_SOURCE = _sp.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, timeout=5,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ).stdout.strip() or "unknown"
except Exception:
    pass


@app.get("/api/health")
def health():
    """Unauthenticated liveness probe.

    `commit` is the commit captured WHEN THIS PROCESS STARTED, not the working
    tree's current HEAD — so `make smoke` can genuinely detect a stale server.
    If you merge or switch branches without restarting, this value stays put and
    smoke fails, which is the point.
    """
    return {
        "status": "ok",
        "commit": _RUNNING_COMMIT,
        "branch": _RUNNING_COMMIT_SOURCE,
        "started_at": _PROCESS_STARTED_AT,
        "pid": os.getpid(),
    }
