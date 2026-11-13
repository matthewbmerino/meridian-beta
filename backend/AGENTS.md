# Backend Layer — FastAPI API Server

Read root `AGENTS.md` first for product vision, verification gates, and the Autonomy Contract.

The bridge between computation and display. If a number is correct in `analysis/` but wrong in the UI, serialization or routing is the likely bug.

## Application Structure

| File | Purpose |
|------|---------|
| `main.py` | App factory, CORS, `AuthGuardMiddleware`, lifespan (scheduler + SQLite re-encrypt), router registration |
| `services/scheduler.py` | APScheduler: sonar 15m, alerts 15m (weekday market hours), market panel 60m (no-op when current) |
| `services/compass_session.py` | Thread-local context for Compass chat sessions |
| `services/sizing_book.py` | Shared sizing book for `/sizing`, `/pretrade-impact`, Compass sizing tools |

## Endpoint Map

| Router | Prefix | Notes |
|--------|--------|-------|
| accounts | `/api/accounts` | summary, positions, realized (incl. external), external |
| market | `/api/market` | quotes, prices, options, fundamentals, search, insider, signals, score-breakdown, stbr, stbr-extended, details, news, iv-rank, sectors, earnings |
| analysis | `/api/analysis` | correlation, var, frontier, sizing, sizing-preview, pretrade-impact, factors, factor-covariance, backtest, blend, drawdown-detail, attribution, greeks, position-intel, composites, stress, outcomes |
| snapshots | `/api/snapshots` | portfolio history, position history, count |
| sonar | `/api/sonar` | scan, universes |
| compass | `/api/compass` | chat (SSE), chats, save, delete |
| fred | `/api/fred` | environment, yield-curve, series |
| chart-state | `/api/chart-state` | per-symbol chart + STBR state |
| gpu | `/api/gpu` | metrics, trends, offers, index |
| watchlist | `/api/watchlist` | CRUD + reorder |
| imports | `/api/imports` | list, upload, toggle, delete |
| prefs | `/api/prefs` | get/update |
| external-cash | `/api/external-cash` | CRUD |
| liabilities | `/api/liabilities` | CRUD + `POST /reorder` (persist the manual row order) + amortization rollup, cash coverage, and `net_worth` (the ONE place debt meets `net_liquidation`) + `GET /analysis` (paydown projection, payoff order, priority ranking, cost of carry vs the FRED risk-free rate, record-quality flags; `?include_projection=false` for agent consumers). Coverage is measured against `cash_balance` — the same spendable cash the Overview shows — falling back to external balances only if the summary cannot be computed. |
| intelligence | `/api/intelligence` || leaderboard | `/api/leaderboard` | `meta` (panel coverage + available periods + universes), `board` (ranked rows, every period and calendar year on the row, `q`/`sector`/`signal` filters, `sort`/`asc`, `include_suspect`), `sectors` (period roll-up), `years` (calendar-year matrix by sector). Composed from the market panel and TTL-cached 15m at the composition layer. `movers` reads the SAME composed frame **before any page limit** and returns its two tails — the biggest gainers and decliners over the window — because the board's worst decliner is normally past the page edge (the score punishes exactly the move the operator wants to see). Suspects are set aside exactly as on the board, and any set-aside name that would have placed is named in `excluded_suspect` with its side; `include_suspect` re-admits them with `suspect`/`suspect_note` on the row. |
 sector rotation/dispersion/breadth, network, lead-lag, pairs, portfolio relationships |
| history | `/api/history` | `status` (deep-history store contents + build time), `regime` (today's state vector, the nearest DISTINCT historical episodes at least 18 months apart, their forward outcomes, and the unconditional base rate beside them), `breadth` (share of the S&P 500 in its own drawdown while the headline sits within 2% of a record, placed against every earlier month it did). Reads only the optional deep-history store + the panel; TTL-cached 1h; degrades to `available:false` + a reason. |
| research | `/api/research` | run (SSE), reports, types |
| alerts | `/api/alerts` | list, unread, mark read, stream |
| theses | `/api/theses` | CRUD + review |
| decisions | `/api/decisions` | list, log |
| themes | `/api/themes` | theme exposure |
| pipeline | `/api/pipeline` | idea pipeline + commit-to-book |
| discovery | `/api/discovery` | opportunity feed |
| compute | `/api/compute` | compute exposure |
| engagement | `/api/engagement` | desk status + milestones |
| notes | `/api/notes` | capture inbox |
| meta | `/api/meta` | model-info, data-health, version, methodology, metrics |
| settings | `/api/settings` | ollama-probe |
| auth | `/api/auth` | status, login, logout |

**Total: ~120 endpoints + WebSocket `/ws/quotes` (live quotes, 5s poll).**

## The leaderboard's board is composed ONCE (backend/routers/leaderboard.py)

`_assemble()` produces every window for every symbol in one pass; the endpoints filter, sort and
page the result. `assemble_cached` is built at MODULE scope — calling `ttl_cache(...)` inside an
endpoint returns a brand-new wrapper per request, so the decorator caches nothing while reading
exactly like it does. The panel only changes once a day, so 15 minutes is the TTL.

Two shape rules this router exists to honour:

- **Anchors resolve against the CALENDAR, not the frame.** The router reads ~35 anchor dates and
  asks `period_returns` for "21 sessions back". Resolved against those 35 rows that becomes
  "21 rows back" and `1M`/`1Y` silently vanish or measure the wrong window — so the full session
  index is passed in explicitly.
- **A filter never acts silently.** Rows whose own price history contains an impossible
  single-session move are set aside with the count, the symbol and the reason in the payload; the
  operator can re-include them with `include_suspect=true`. Same for the liquidity floor and the
  common-stock filter on `All US`.
- **No bare NaN in JSON.** Missing values serialise as `null` (`_num`).

## History reads a store the quote feed cannot fill (backend/routers/history.py)

`/api/history/*` exists because the entitlement floor makes two-cycle questions unanswerable from
the panel — see `data/AGENTS.md`. Three rules:

- **`/regime` returns the base rate in the SAME payload as the analogues.** Every analogue figure
  is meaningless without the unconditional figure beside it, and measured on this data the
  analogue set frequently cannot beat it. The endpoint composes both from one `analysis.regime.study()`
  call, so the pair can never drift apart.
- **`/breadth` walks the panel one YEAR at a time.** Reading five years of a 14M-row panel to
  pivot 500 columns exhausts memory; per-year reads keep only the matching row groups live, and
  the frame is filtered to the universe before it is concatenated. The index series rides in on
  `SPY`, and the comparison is only reported once at least six at-record months exist — below
  that the endpoint stays available but leaves the percentile `None` rather than computing a
  percentile of four points.
- **A failure names itself.** A missing store, a missing series and a study that raised are three
  different `note` strings; none of them is an empty chart. A `503` mid-read degrades the same
  way, because a 500 on this page would blank the whole route.

## Conventions

- **Auth (load-bearing):** Single-operator. `AuthGuardMiddleware` gates every `/api/*` except `/api/auth` and `/api/health` (fail-closed: no password → 403). Valid credential = login cookie OR service bearer token (`Authorization: Bearer <MERIDIAN_API_TOKEN>`). Crown-jewel routers additionally use `Depends(require_auth)`. Any new global-store endpoint must be gated.
- **Router file:** `backend/routers/{domain}.py`; register in `main.py`.
- **Lazy imports** inside endpoint functions — avoids circular imports and speeds startup.
- **NaN-safe serialization:** use `_safe_float()` pattern from `routers/accounts.py`. Guard `isfinite()` on derived floats; a bad history row must degrade, never 500.
- **Error handling:** catch exceptions, return `{"error": "message"}` with appropriate status. Never leak tracebacks.
- **Response shape:** always return a top-level dict (never a raw list).
- **Frontend contract:** new endpoint requires matching `frontend/src/lib/api.ts` method + `frontend/src/lib/types.ts` interface. Shipping one without the other is incomplete.
- **Caching:** `data/cache.py` for deterministic heavy computations. Session-dependent/stress stays uncached.
- **Route order matters.** Static paths must register before parameterized ones (e.g. `/earnings/portfolio` before `/earnings/{symbol}`), or the static segment is captured as a path param.
- **`/api/health`** is the unauthenticated liveness probe; keep it free of operator data.

## Known Pitfalls

- A `@router.get`-decorated function called directly keeps its `Query(...)` defaults, and `Query(False)` is truthy. Test route handlers through `fastapi.testclient.TestClient` against a minimal app, or use plain defaults on shared helpers.
- When `pytest` patches a function that uses lazy imports, patch the **source module**, not the backend module.
- A multi-source feed must not return a bare empty list on total failure; carry provenance (`requested`, `resolved`, `unresolved`, `degraded`, `note`).
- `get_combined_positions()` returns a DataFrame — `df or {}` raises; use `positions.to_dict("records")` with `is not None`.
- FastAPI route order is registration order — static before parameterized.
- Pydantic silently drops undeclared fields; verify round-trip when a field "doesn't stick".
- POST collectors need method+body; a GET-default helper silently returns empty for POST routes.

## Verification

```bash
venv/bin/python -m pytest tests/ -q   # must be green
venv/bin/ruff check .                 # must exit 0
curl -s http://127.0.0.1:8001/api/meta/data-health
cd frontend && npx tsc --noEmit
cd frontend && npm run lint
```

**Rules:** every new endpoint needs a `tests/test_backend.py` smoke test (200 + JSON-serializable, no NaN/Infinity). New endpoint needs `api.ts` + `types.ts`. Fix code when a test fails.

## Cross-Reference

- Computation: `analysis/AGENTS.md`
- Frontend: `frontend/AGENTS.md`
- Compass tools: `agent/AGENTS.md`
- History: `journal/backend.md`

## Recent Changes

Full dated history: `journal/backend.md`. Read when you need context, not every session.
## Decision journal endpoints

- `GET /api/decisions` — list, newest first. Query params: `limit`, `symbol`,
  `status`, `exclude_closed`. With `symbol_history=true` and a `symbol`, returns
  chronological `entries`, `current_status`, and `counts_by_status`.
- `POST /api/decisions` — log a new decision (status derived from action).
- `PATCH /api/decisions/{id}/status` — body `{status, reason?, source?}`.
  Status codes: 200 ok, 404 unknown id, 409 same status, 422 invalid status.

`GET /api/engagement/status` passes `exclude_closed=True` to `list_decisions`
(lifecycle change 2026-10-07), so closed decisions remain in the journal but no
longer surface as open desk items.

## Credit endpoints (`backend/routers/credit.py`)

`GET /api/credit` → `{observations, rollup, integrity, error}`; `POST /api/credit/scores`;
`POST /api/credit/scores/bulk` (the backfill — idempotent, reports `added`/`skipped`);
`DELETE /api/credit/scores/{id}`. All gated by `require_auth`. Validation failures return
`{"error": ...}` with **HTTP 200** (this codebase's convention) — read the BODY, not the status.

The read contract is per-SERIES (model + bureau). Do not add an endpoint returning "the credit
score": two models are two numbers on the same day, so one aggregated field would be a fiction.
Scores publish monthly at best, so a consumer must render `as_of` and the staleness flag rather
than implying the number is current.

