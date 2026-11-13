# Data Layer — Feeds, Persistence, Caching

Read root `AGENTS.md` first for product vision, verification gates, and the Autonomy Contract.

This is the top of the dependency tree. Nothing in `data/` imports `analysis/`, `backend/`, or `agent/`. Every number above originates here. If a feed is stale or silently empty, the stack displays garbage with confidence.

## Invariants

1. **No fabricating.** Return `None` or an explicit empty state on failure. Never interpolate or default silently.
2. **Massive is the broker API.** Responses are reshaped to Schwab-compatible schemas (`get_quotes_schwab_compat`) before leaving the layer.
3. **At-rest encryption is load-bearing.** Every PII store (imports, prefs, watchlist, alerts, chats, findings, reports, theses, decisions, outcomes, pipeline, milestones, briefing, snapshots, GPU DB) uses `data/crypto.py`. Never write PII to plaintext disk.
4. **Single operator.** No per-visitor sessions. All stores resolve to fixed paths under `data/` via `data.session.data_root()`.
5. **Public/private boundary.** Anything consumed cross-layer must be public (no `_` prefix). Don't import another layer's underscore-prefixed names.

## Feeds

| Module | Feed | Purpose | Key Functions |
|--------|------|---------|---------------|
| `massive.py` | Massive | Primary market: quotes, OHLCV, options, fundamentals, search. Quotes carry `stale`, `session`, `preMarketChange`/`preMarketPercentChange`. | `get_snapshot`, `get_price_history`, `get_option_chain`, `get_fundamentals`, `get_quotes_schwab_compat`, `market_session` |
| `schwab.py` | Schwab Trader API | The equity-feed swap-in: quotes, daily OHLCV, instrument search and fundamentals in the SAME shapes as `massive.py`, so `data/market.py` routes to either. OAuth app credentials plus a hard 7-day refresh wall (`SchwabAuthExpired`); no options, no crypto. SHADOW MODE until `scripts/schwab_parity.py` passes — nothing routes to it yet. | `get_quotes_schwab_compat`, `get_price_history`, `search_instruments`, `get_fundamentals`, `authorize_url`, `exchange_code`, `token_status` |
| `reference.py` | Polygon | Ticker details, news, sector maps, market caps | `get_ticker_details`, `get_ticker_news`, `get_sector_map`, `get_market_caps` |
| `fred.py` | FRED | Macro: yield curve, credit spreads, fed funds, VIX; `FRED_SERIES` catalog. `risk_free_rate()` is the risk-free BENCHMARK for the debt carry analysis — `{rate, as_of, series, label}` or None (never a default), cached 1h. `cash_curve()` is the same contract per tenor for the cash page's spread (1M/3M/6M/1Y, each point carrying its own observation date; `[]` when FRED is unavailable). | `get_series`, `get_yield_curve`, `get_credit_spread`, `market_environment`, `risk_free_rate`, `cash_curve` |
| `edgar.py` | EDGAR | SEC Form 4 insider filings | `get_insider_activity` |
| `earnings.py` | StockAnalysis | Earnings calendar + prior EPS (two-tier source) | `get_portfolio_earnings`, `get_ticker_earnings` |
| `universe.py` | Wikipedia + StockAnalysis | Index constituents — pulled LIVE. The S&P 500 comes from Wikipedia's list article; Nasdaq-100 / Dow 30 come from StockAnalysis's per-index list pages, after Wikipedia dropped BOTH constituent tables (Oct 2026: a scrape with no table returns an empty list, so the two universes silently resolved to nothing on every surface that reads them). Every list parse is range-checked — a reflowed page degrades to an explicit empty, never a partial board. Index sector maps are the S&P 500 GICS subset (DJIA 30/30, NDX 86/101; uncovered names stay blank, never guessed from another classification). | `sp500`, `nasdaq100`, `djia` |
| `gpu.py` | Vast.ai | Per-GPU median $/hr, percentiles, TFLOPS, available-offer counts. SQLite history self-accumulates hourly. | `get_gpu_metrics`, `record_snapshot`, `load_history` |
| `document_extract.py` | LLM extractor || `market_panel.py` | Massive grouped daily | The market-wide daily panel: one close+volume per (session, symbol) for the WHOLE US market. `N` sessions cost `N` calls, not `N` x universe — the primitive behind `/leaderboard`. Plaintext + gitignored (a rebuildable cache, like `sonar_cache.json`), never a source of truth. | `load_panel`, `frames`, `session_index`, `update`, `compact`, `refresh_meta`, `panel_meta`, `scan_discontinuities`, `load_reference` |
| `deep_history.py` | yfinance + FRED + multpl | The multi-decade store behind `/history`. One parquet per series under `data/deep_history/{prices,macro}` (gitignored, built by `scripts/collect_deep_history.py`). OPTIONAL: every reader returns an empty Series and a reason when the collector has not run. | `prices`, `price_frame`, `macro`, `manifest`, `available`, `status`, `store_path` |
| `deep_history.py` | yfinance + FRED + multpl | The multi-decade store behind `/history`. One parquet per series under `data/deep_history/{prices,macro}` (gitignored, built by `scripts/collect_deep_history.py`). OPTIONAL: every reader returns an empty Series and a reason when the collector has not run. | `prices`, `price_frame`, `macro`, `manifest`, `available`, `status`, `store_path` |
 PDFs/statements/unrecognized CSVs → structured records via schema from `extract_schemas.py` | `extract_financial_document`, `content_to_text` |
| `extract_schemas.py` | Extraction contracts | `sportsbook`, `prediction_market`, `staking`, `casino`, `external_other` | `infer_schema`, `get_schema`, `EXTRACT_SCHEMAS` |

## Portfolio & Persistence

| Module | Purpose | Key Functions |
|--------|---------|---------------|
| `account.py` | Combined positions/summary + per-location cash breakdown | `get_combined_positions`, `get_combined_summary`, `get_account_breakdown` |
| `imports.py` | CSV imports (Chase/Schwab) + external P&L imports | `add_import`, `list_imports`, `get_visible_imports_with_summary`, `get_all_visible_positions`, `get_all_activity_realized_pl`, `add_external_import`, `get_all_external_pl` |
| `chase.py` | Chase CSV parsing | `parse_chase_csv` |
| `realized.py` | Schwab/Chase realized P&L parsing | `parse_realized_csv`, `get_combined_realized_pl` |
| `snapshots.py` | SQLite snapshot DB (5m scheduler), encrypted at rest (`portfolio.db.enc`) | `record_snapshot`, `maybe_snapshot`, `load_portfolio_history`, `load_position_history` |
| `watchlist.py` | Watchlist symbols, encrypted at rest | `get_watchlist`, `add_symbol`, `remove_symbol`, `reorder` |
| `chart_state.py` | Per-symbol chart state (global, namespaced `chart` vs `stbr`) | `get_chart_state`, `save_chart_state` |
| `theses.py` | Position theses (theme registry, compute flags), encrypted | `get_theses`, `get_thesis`, `upsert_thesis`, `delete_thesis`, `mark_reviewed` |
| `decisions.py` | Decision journal (append-only, arms outcomes) | `log_decision`, `list_decisions` |
| `outcomes.py` | Outcome library resolving decisions | `arm_outcome`, `resolve_outcomes`, `list_outcomes` |
| `pipeline.py` | Idea Pipeline (global, Desk Loop spine) | `get_pipeline`, `upsert_item`, `advance_stage`, `link_decision`, `remove_item` |
| `milestones.py` | Earned milestones (append-only, idempotent) | `earn`, `get_earned`, `has_earned` |
| `prefs.py` | User preferences (incl. `llm_provider`) | `get_prefs`, `get`, `update_prefs` |
| `external_cash.py` | External balances at other institutions, liquidity-kind split (in NAV, not `buying_power`; only kind=`bank` is cash) | `list_external_cash`, `add_external_cash(kind)`, `update_external_cash(kind)`, `remove_external_cash`, `get_cash_totals`, `get_external_cash_total`, `normalize_kind` |
| `cash_yields.py` | Stated TERMS per cash position — the rate AND what the balance is, because 3.5% means different things at 0 days and 365. `INSTRUMENTS` is the single vocabulary: `days` to cash without penalty (0 same-day, 1 T+1, 7 no-penalty CD, `None` = a term CD resolved from its maturity, 999 no defined exit) and `state_exempt` (Treasury interest escapes state/local tax, bank interest does not — the flag that lets a 4.21% bill beat a 4.40% CD in New York). The operator's marginal tax rates live here too, as a STATED input: absent, every after-tax figure is `None`, never a guessed bracket. v1 wrote a bare `{label: apy}` map, so the read normalizes every missing key — a legacy row must never KeyError or be silently dropped. Encrypted at rest like `external_cash` (institutions are PII). | `INSTRUMENTS`, `DEFAULT_INSTRUMENT`, `list_positions`, `list_yields`, `get_assumptions`, `instrument_catalog`, `upsert_position`, `set_assumptions` |
| `liabilities.py` | The operator's obligations, in two families. DEBT — credit cards (kind=`revolving`) and installment loans (kind=`installment`): a BALANCE owed and repaid over time. COMMITMENTS — a repeating bill (kind=`recurring`) and a single payment (kind=`oneoff`): a cash CLAIM, never a balance, never amortized. For debt rows `balance` is owed; for commitment rows it is the claim (monthly for `recurring`, once for `oneoff`). `is_debt` is derived on every read so a consumer never branches on the kind list. Installments require a `payment` (no payment → no schedule → write refused); a `revolving` card marked `autopay_full` stores no payment (paying the statement IS the service); a commitment REFUSES `apr`/`term_months`/`originated_at` (a rate on something that is not a balance contradicts its own kind). Unknown kind reads as `installment` (conservative). Entries carry a manual-order `position`: new rows append, `_ordered()` puts positioned rows first and unarranged rows after in the historical newest-first order, and an edit PRESERVES the position (order is changed only through `reorder_liabilities`). Writes return `public_entry()` so a POST carries the same `is_debt`/`position` contract a GET does. A PARALLEL ledger: never subtracted from `net_liquidation`. | `list_liabilities`, `add_liability`, `update_liability`, `remove_liability`, `reorder_liabilities`, `get_liabilities_total`, `normalize_kind`, `is_debt_kind`, `public_entry`, `store_path` |
| `external_pl_manual.py` | Manual external-P&L entries — operator-asserted, no document (bank interest, bonuses); signed money, provenance mandatory; merges into `get_all_external_pl` | `add_manual_external_pl`, `list_manual_external_pl`, `remove_manual_external_pl`, `manual_pl_rows` |
| `alternatives.py` | Illiquid real-asset positions (watches/graded cards/other) + append-only provenance-backed marks; stakes roll up read-only. NOT in net_liquidation | `list_items`, `add_item`, `update_item`, `remove_item`, `add_valuation`, `list_valuations`, `latest_valuations`, `rollup` |
| `theme_registry.py` | THEME_ALIASES alias->canonical map + canonical_theme() resolution (write-time in theses store, group-time in analysis) | `canonical_theme`, `canonical_theme_set`, `THEME_ALIASES` |
| `alternatives.py` | Illiquid real-asset positions (watches/graded cards/other) + append-only provenance-backed marks; stakes roll up read-only. NOT in net_liquidation | `list_items`, `add_item`, `update_item`, `remove_item`, `add_valuation`, `list_valuations`, `latest_valuations`, `rollup` |
| `theme_registry.py` | THEME_ALIASES alias->canonical map + canonical_theme() resolution (write-time in theses store, group-time in analysis) | `canonical_theme`, `canonical_theme_set`, `THEME_ALIASES` |
| `crypto.py` | AES-256-GCM at-rest encryption for all PII stores + SQLite lifecycle | `read_json`, `write_json`, `read_bytes_store`, `write_bytes_store`, `decrypt_sqlite_to_runtime`, `encrypt_sqlite_from_runtime` |
| `session.py` | Data root + shared secret | `data_root`, `_secret`, `derive_encryption_key` |
| `auth.py` | Bcrypt password (12 rounds) + signed HttpOnly cookie + brute-force backoff; also service bearer token `verify_api_token` | `is_password_set`, `set_password`, `verify_password`, `issue_cookie`, `validate_cookie`, `verify_api_token` |

## Agent Memory

| Module | Purpose | Key Functions |
|--------|---------|---------------|
| `compass_chats.py` | Chat history (JSON index + per-chat files) | `list_chats`, `load_chat`, `save_chat`, `create_chat`, `delete_chat`, `auto_title` |
| `findings.py` | Bridges Journalist conclusions into Compass context | `add_finding`, `recent_findings`, `findings_context` |
| `research_reports.py` | Saved research reports | `save_report`, `load_report`, `list_reports`, `delete_report` |
| `alerts.py` | Alert store (composites shifts, STBR warnings, earnings proximity) | `add_alert`, `list_alerts`, `unread_count`, `mark_read`, `last_scan` |

## Caching

`data/cache.py` provides `ttl_cache(ttl_seconds=60)` — thread-safe, LRU, deep-copy values. Used in `data/` (market, earnings, universe, reference) and backend deterministic heavy computations (frontier cloud, correlation matrix, factor model in `backend/routers/analysis/_common.py`).

## Conventions

- **Keys** live in `.env` (dev, `chmod 600`) or `/etc/meridian/env` (prod, root-only `600`), resolved via `config.py`. Never commit keys.
- **Feed failures degrade.** Return `None`/explicit empty state; callers grey metrics.
- **Cross-layer public API** has no `_` prefix.
- **SQLite lifecycle:** `decrypt_sqlite_to_runtime` on open, `encrypt_sqlite_from_runtime` on shutdown.

## Verification

```bash
venv/bin/python -m pytest tests/ -q   # must be green
venv/bin/ruff check .                 # must exit 0
curl -s http://127.0.0.1:8001/api/meta/data-health
```

**Rules:** run the full suite; add a test for new functions; fix the code when a test fails.

## Cross-Reference

- Computation consumers: `analysis/AGENTS.md`
- Endpoints: `backend/AGENTS.md`
- Compass tools: `agent/AGENTS.md`
- History: `journal/data.md`

## Recent Changes

Full dated history: `journal/data.md`. Read when you need context, not every session.
## The market panel is a PANEL, not a quote feed (data/market_panel.py)

Any question of the form "who is up the most this year / this quarter / since 2022?" needs one
close per (session, symbol) for a whole universe. `massive.get_price_history` is per-symbol and
capped at 1,254 daily rows, so 503 names x N periods is 503 x N calls. Massive's **grouped
daily** endpoint returns every US-listed ticker's OHLCV for ONE date in a single call
(~11k-12.6k rows, measured 0.26s), so sessions cost calls, not names. A five-year backfill
(`--backfill`, ~1,300 sessions) runs in ~2 minutes; the daily increment is one call.

Measured behaviour that the code depends on, verified live:

- **Entitlement floor is 2021-10-15** (`ENTITLEMENT_FLOOR`). Earlier dates return 403 and so does
  a FUTURE date (`last_completed_session()` resolves in America/New_York and waits for the 16:30
  ET settle — the box is UTC, so `date.today()` there is already tomorrow during the US evening).
- **A market holiday is 200-with-zero-rows; a 403 is out-of-window.** `_fetch_batch` returns
  `(rows, closed, errors)` and never merges the last two: "the market was shut" and "we failed to
  ask" are different instructions to the operator.
- **`closed_sessions` counts as COVERED.** A holiday never lands in the panel, so without this a
  run re-asks the feed for all ~50 holidays every time and can never report "already current".
- **`errors` is self-healing.** A session that failed once and succeeded on a retry is dropped
  from the list; a union-forever error list advertises a defect the panel has already fixed.
  `refresh_meta()` is a full RECONCILIATION (counts, first/last session, errors) against the file
  on disk, not a field patch — a sidecar that disagrees with its own data is worse than none.
- **Memory is the constraint, not time.** The full panel is ~14M rows. `update()` writes
  per-batch Parquet parts and compacts, so a crash at minute four keeps the first four minutes
  and a re-run resumes; `load_panel()` reads through Parquet filters, and `frames()` pivots a
  WINDOW. Nothing should ever load the whole panel to answer one question.

**`scan_discontinuities()` is a data-quality gate, not a cleanup.** A ticker reassignment or an
unadjusted split appears as one enormous single-session return and corrupts every period spanning
it — measured live, BNY printed **+1,262% on 2026-05-21** and META **+1,394.7% on 2022-06-09** (the
FB->META change), while the largest genuine one-day gap in the same universe was a biotech
trial-result move of +177%. `_DISCONTINUITY_PCT` (400%) separates the two. The map is stored in
the coverage sidecar and surfaced per row; the feed's history is **detected, never rewritten**.

`market_reference.json` (13k instruments, `name` + `type`) rides along on the same update: the
grouped endpoint returns tickers and no names, and it returns ETFs, warrants, units and
preferreds alongside common stock, so a board built on it unfiltered would rank a 3x leveraged
ETF against a business.

## A five-year quote feed cannot answer a two-cycle question (data/deep_history.py)

Massive caps daily aggregates at **exactly five years** — verified 2026-10-15: `SPY` returns 1254
rows from 2021-10-15 whatever `from` date is requested, and the index endpoints (`I:SPX`) answer
`NOT_AUTHORIZED` on this plan. That is why `ENTITLEMENT_FLOOR` is 2021-10-15 and why the market
panel spans a single cycle. Any question with a two-cycle prerequisite — "is this 1999?", "what
happened after the last time valuation was here?" — is therefore **unanswerable from the panel**,
and answering it needs a second, longer feed.

**The store.** `data/deep_history/{prices,macro}/<name>.parquet`, one file per series, plus
`manifest.json` (rows, first/last observation, build time). Built by
`venv/bin/python scripts/collect_deep_history.py` from **yfinance** (equities: `^GSPC` back to
1927, `^IXIC` to 1971) and **FRED** (macro: `NCBEILQ027S` market value of corporate equities to
1945, `BAA10Y` to 1986, `DGS10` to 1962). One series is neither: **`SHILLER_CAPE`**, scraped from
multpl.com, because FRED carries no price/earnings series long enough to reach 1999 and the
regime study's valuation feature needs one that does. Its `source` is recorded in the manifest.

- **The store is OPTIONAL and gitignored**, exactly like the market panel: derived, rebuildable in
  ~2 minutes, and licensed third-party data. `available()` is False when the collector has not run
  and `status()` returns the reason plus the collector's name — a surface must render "run the
  collector", never an empty chart that reads as "there is no history".
- **`^` and `-` both map to `_`** in the file name (`^GSPC` -> `_GSPC.parquet`). The mapping is not
  reversed anywhere: the manifest is the only place a name is resolved back, and callers always
  pass the provider's own symbol.
- **`price_frame()` OMITS a symbol it cannot find** rather than inner-joining, which would
  silently shrink the frame to the shortest series. Callers that need alignment reindex
  explicitly.
- **Neither feed is used for quotes.** Massive remains the broker API and the panel remains the
  cross-sectional source; this store exists only for the regime study, and nothing in `data/`
  trades against it.

**Pitfall — the providers do not agree on a series.** `SP500` on FRED is only a 10-year series
(2512 rows) while yfinance's `^GSPC` reaches 1927, and `NASDAQCOM` (FRED) and `^IXIC` (yfinance)
are the same index settling a day apart. Pick one per study and record which, or the same
"Nasdaq" will mean two different series on two surfaces.

## Decision lifecycle

Decisions carry a mutable `status` (`open`, `pending`, `closed`) and an append-only
`status_events[]` audit trail. New decisions start with explicit status; legacy
entries are backfilled by action string:

- `OPEN` → `open`
- `STAGE_ENTRY` / `STAGE_HOLD` → `pending`
- `STAGE_CANCELLED` → `closed`
- anything else → `open`

Transition via `set_status(id, to_status, reason=None, source="ui")`. It raises
`KeyError` on unknown id, `ValueError` on invalid or same-status target. The
mutator only touches the decision record; outcome records are unchanged.

`list_decisions(limit=100, symbol=None, status=None, exclude_closed=False)` keeps
legacy defaults; opt-in filters only when requested.

## Credit scores (`data/credit_scores.py`)

One store for the operator's bureau score readings. Two rules are structural, not stylistic:

- **A series is identified by (model, bureau), never by "my credit score".** `series_key()` is the
  only grouping a consumer may use, the API returns one row PER SERIES, and there is no single
  "score" field anywhere. A FICO 8 from Experian and a VantageScore 3.0 from TransUnion are
  different models over different data; they disagree by tens of points on the same day.
- **Observations are append-only with mandatory provenance** (model, bureau, source, as_of). A
  typo'd model is REFUSED because it would split one series in two and quietly shorten the history;
  a score outside 300-900 is refused as a unit error; `add_scores()` is idempotent on
  (model, bureau, as_of) so the backfill is re-runnable.

Staleness (>45 days, `stale_series()`) and a recorded `change` that disagrees with the arithmetic
delta (`series_integrity()`) come back as FLAGS, never silently current. The store resolves through
`data_root()`, so the suite's `_ROOT` patch isolates it — do NOT convert it to a module-level
`_PATH`, which would need its own conftest line and could otherwise write production.

Not `net_liquidation`, not sizing, not the external-P&L ledger: a credit score is a lagging
DESCRIPTION of the book, never an input to it. `data/credit_scores.json` is gitignored like the
other operator-PII stores.

## A promo card balance is a CARD, not a loan (learned the hard way, 2026-10-13)

A store-card promo balance — 0% for N months, amortising on a schedule — is modelled as
`kind=revolving` **with** a `payment` and a `credit_limit`, and **without** `term_months`.
That is the only combination that gets all four things right at once:

- the balance counts in REVOLVING UTILIZATION, which is the entire point of a credit line;
- it still amortises to a payoff date, because the row carries a payment;
- the store accepts a `credit_limit` on it (and correctly REFUSES one on an `installment` — a
  loan's "limit" is its original amount, a different field);
- no `card_with_term` flag fires, because `term_months` is exactly what makes a card row read
  as a loan.

Recording a promo balance as `installment` silently EXCLUDES it from utilization and makes the
ratio flatter than reality. Measured live the day this was fixed: aggregate read 21.9% instead
of 34.1%, with the two largest balances missing from the denominator. Put the term in `note`.


## The vetted-institution registry (data/analyst_institutions.py)

An unfiltered consensus averages everyone who publishes, which is a mix of desks that carry
a name for years and shops that upgrade everything. `analyst_institutions.resolve()` is the
ONLY way a provider firm string becomes a tier — a bare dict lookup misses the real
spellings in the feed ("JP Morgan", "B of A Securities", "Keybanc", "Cowen & Co." beside
"TD Cowen", "SunTrust Robinson Humphrey" for Truist, "Exane BNP Paribas" for BNPP,
"HC Wainwright & Co."). Three rules:

- **Unknown means unvetted, never dropped.** An unlisted house comes back tier 3 with its own
  name, so `excluded_firms` can list it. Never assume a firm is reputable because it is not
  in the table; never hide it either.
- **The consensus is RECENCY-BOUNDED.** A house that has not published inside the window
  (`STALE_DAYS`, 180) is not part of today's view — it is counted, named in
  `vetted_stale_firms`, and left out of the mean. Live: Credit Suisse at 1,329 days and
  Edward Jones at 1,713 days were being averaged into CEG's "current" consensus.
- **A price target must be POSITIVE.** The feed writes `0.0` for "no target published";
  averaging that in as an estimate dragged VRT's vetted mean to 263.41 against a real 372.80.
  `_positive()` is the guard, and it belongs on every price field taken from this feed.

The frontend defaults to the vetted levels and LABELS the sample on every number
(`Vetted mean` vs `Mean (unfiltered)`); the panel prints the registry's criteria, the
per-house table, the stale list and the excluded list, so the filter is inspectable rather
than magic. If the provider returns no firm-level history the block is absent and
`institutional_unavailable` says the filter could not be applied — never a silent fallback
to "everyone", which would misrepresent the sample.

## Analyst price targets are a CONSENSUS, not a fact (data/analyst_targets.py)

`get_analyst_targets(symbol)` returns Yahoo's sell-side price-target consensus — the first
third-party AVERAGE the app renders as though it were a level on a chart, so three rules
apply:

- **Provenance travels with the number.** Every available payload carries `source`,
  `as_of`, the contributing analyst count and a `basis` string, and the chart caption
  prints all of them. A target with no count and no date is not worth plotting.
- **Absence is a reason, never a zero.** No coverage (crypto, most ETFs, thin small caps)
  returns `available=False` plus `reason`; a provider ERROR returns a different reason
  naming the exception, so "nobody covers it" and "ask again later" stay distinguishable.
  A missing mean must never become 0 — that renders as a $0 price target.
- **A half-range is not a range.** If only the high or only the low comes back, BOTH are
  dropped (with a note) rather than plotting one bound and inventing the other. Yahoo also
  returns strings and infinities in this payload; `_num` filters to finite numbers so a
  NaN never reaches the chart's price scale.
