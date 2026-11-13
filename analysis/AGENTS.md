# Analysis Layer — Computation Engine

Read root `AGENTS.md` first for product vision, metrics framework, verification gates, and the Autonomy Contract.

Pure functions, no side effects, no data fetching. If a dashboard number is wrong, the bug is likely here.

## Invariants

1. **Pure functions only.** No `import data.*`, no `import backend.*`, no network, no disk I/O (except composites hysteresis state). Accept DataFrames/Series/dicts, return dicts/DataFrames.
2. **No NaN in output dicts.** Use `None` for missing values; wrap numpy scalars with `float()`. NaN leaks silently break frontend JSON.
3. **Type hints on all public functions.** Internal helpers can be untyped.
4. **Sortino over Sharpe.** See root `AGENTS.md`.
5. **Degrade, don't fabricate.** Insufficient input returns `None` or low-confidence — never interpolate or default to zero silently.
6. **Unit testable with synthetic data.** No live API required.

## Modules

| Module | Purpose | Key Functions |
|--------|---------|---------------|
| `risk.py` | Portfolio risk analytics | `daily_returns`, `portfolio_beta`, `correlation_matrix`, `historical_var`, `conditional_var`, `sortino_ratio`, `sharpe_ratio`, `max_drawdown`, `max_drawdown_detail` |
| `signals.py` | Technical indicators | `rsi`, `sma`, `macd`, `bollinger_bands`, `unusual_volume`, `compute_composite_score`, `generate_signals` |
| `stbr.py` | Short-Term Bubble Risk | `calculate_stbr`, `stbr_zscore`, `stbr_velocity`, `current_risk_label` |
| `portfolio.py` | Allocation analytics | `compute_allocation`, `allocation_by_type`, `herfindahl_index`, `top_holdings` |
| `options.py` | Options analytics | `aggregate_greeks`, `expiry_timeline`, `pl_scenario` |
| `sizing.py` | Fractional-Kelly sizing | `compute_target_weights`, `tier_of`, `config_from_prefs` |
| `calibration.py` | Edge calibration | `calibrate_edge`, `logistic_edge`, `empirical_edge`, `hybrid_edge` |
| `composites.py` | Layer-4 forecasting composites | `portfolio_weather`, `risk_regime`, `momentum_breadth`, `capital_at_risk`, `all_composites` |
| `position_intel.py` | Per-position intelligence | `ma_distances`, `relative_strength`, `vol_regime`, `liquidity_score`, `position_intelligence` |
| `stress.py` | Stress testing | `parametric_stress`, `monte_carlo_var` |
| `relationships.py` | Cross-asset discovery | `cross_correlation`, `find_lead_lag_pairs`, `correlation_clusters`, `correlation_network`, `cointegration_pairs` |
| `sectors.py` | Sector intelligence | `sector_returns`, `sector_momentum`, `sector_rotation_signals`, `sector_breadth`, `market_regime_breadth` |
| `factors.py` | Factor model | `factor_returns`, `factor_exposures`, `portfolio_factor_exposures`, `factor_covariance` |
| `attribution.py` | Brinson attribution | `brinson_attribution` |
| `performance.py` | **The Performance page's daily P&L, decomposed so a transfer is not a trading day.** `build_days` collapses the intraday snapshot feed to one close per New York calendar day and splits each close per position into `market = qty_prev × Δprice` and `capital = Δqty × price`; summed across the book, `ΔNAV = Σmarket + Σcapital + Δcash`, so `flow = Δcash + Σcapital` is money that arrived or left and `pnl = Σmarket` is what the book earned. **Never diff net liquidation to get a day's P&L** — it is a balance, so every deposit reads as a gain (live: a $620,000 house-proceeds transfer was the best "day" on record and its withdrawal the worst, with max drawdown −66%). Measured on the BROKERAGE book (buying power + market value), never combined net liquidation, whose hand-entered external balances would turn every edit of one into a return. A position that appears is opened at today's price, one that disappears is closed at yesterday's, a split books nothing, and a close more than `MAX_SPAN_DAYS` after the previous observation compounds the index but is never ranked as a best/worst day. Known limit: cash income (interest, dividends) arrives with no trade and is therefore classified as flow — a few hundred dollars a year, and the alternative would be guessing which cash is income. | `build_days`, `decompose`, `slice_range`, `summarize` |
| `pretrade.py` | Pre-trade impact | `pretrade_impact` |
| `themes.py` | Theme aggregation | `theme_allocation`, `theme_concentration`, `theme_pnl`, `intra_theme_correlation` |
| `regime.py` | The multi-cycle regime study behind `/history`. Builds a monthly state vector (trend, extension, realised vol, drawdown, time since the last bear market, real rates, credit spread, curve, valuation) from deep history; every feature is standardised against its own **EXPANDING** mean and sd, so a 1988 month is judged only on what was knowable in 1988. `nearest()` picks the closest states on the RMS distance in those sigma units and enforces a minimum spacing — without it the raw top-20 is five months from 2007 and five from 2018, counted as twenty samples. `study()` returns the analogue set, the forward outcomes, and the SAME statistics over every month on record, because a median from a clustered set means nothing without the base rate beside it. `breadth_divergence()` is the internal read of a headline high: the share of constituents 20% below their OWN 52-week high, placed against every earlier month the index sat within 2% of its record. **A month needs a FULL 252-session window or it is dropped** — a lazy `min_periods` makes the comparison False for every name and the month reports *perfect* breadth, which then drags the at-record median down and flatters everything measured against it (found live: the first five months of a two-year frame served 0.0%). | `monthly_state`, `valuation_z`, `expanding_z`, `nearest`, `outcomes`, `study`, `breadth_divergence`, `STATE_FEATURES` |
| `outcomes.py` | Decision Book scorecard | `scorecard`, `operator_bucket_stats` |
| `compute_index.py` | Compute Index composite | `compute_index`, `compute_index_series`, `tightness_composite` |
| `compute_exposure.py` | Book compute exposure | `compute_exposure`, `index_drop` |
| `discovery.py` | Opportunity discovery ranking | `score_setup`, `rank_candidates` |
| `engagement.py` | Desk status scoring | `discipline_score`, `active_streak`, `desk_rank`, `milestones_due`, `desk_status` |
| `cash_position.py` | The cash page's engine: reconstructed balance series, allocation, forward interest run-rate, and the **treasury view**. `treasury_view` answers three questions and nothing else — what the book earns after tax against a benchmark, how fast it can be spent, and which moves are worth making. **One sign convention on both axes**: `apy − benchmark` and `amount × (apy − benchmark)`, so negative always means "earning less than the risk-free". The after-tax leg is asymmetric on purpose — Treasury interest escapes the state/local rate, bank interest does not, which is why a lower bill can beat a higher CD in New York. `days_to_cash` measures ACCESS without penalty (a bill is T+1 because selling it is free of penalty; a term CD resolves from its maturity, and an undated one is `None` = unknown lock, never a flattering zero). A missing benchmark or an unstated tax rate degrades the derived figure to `None` — never a default. Actions rank by DOLLARS, not rate, and a position with no recorded rate is its own action kind rather than a zero. | `running_series`, `allocation`, `interest_summary`, `days_to_cash`, `band_for`, `treasury_view` |
| `liabilities.py` | Debt amortization + obligation rollup + the debt-analysis page engine. Debt and commitments are NEVER summed: `total`/`current`/`long_term`/`monthly_debt_service`/`annual_interest`/`weighted_apr` cover DEBT rows only, so a monthly bill cannot inflate the balance owed or shrink net worth; commitments report `monthly_commitments` and `commitments_next_12` (recurring × 12 + one-offs). `cash_coverage` reserves BOTH — debt by its current portion, bills by what they claim over the same window — so "how much of my cash is actually available?" has one answer. `debt_analysis` runs carry/priority/projection/timeline over debt rows only (a bill has nothing to pay down); `data_quality_flags` sees every row. | `amortize`, `monthly_rate`, `next_due_date`, `liability_row`, `liability_rows`, `rollup`, `net_worth`, `projection`, `payoff_timeline`, `priority_ranking`, `carry_analysis`, `data_quality_flags`, `debt_analysis` |

## Composite System

Layer-4 composites return `{value, confidence, inputs[], change_reason, is_predictive: true, trend?}`. State persists to `data/composite_state.json`; hysteresis prevents flickering. `all_composites()` is the single aggregator.

Current: **Portfolio Weather**, **Risk Regime**, **Momentum Breadth**, **Capital at Risk**. Build more only when the operator reaches for the insight.

## Edge Cases That Have Bitten

- Autocorrelation on constant series → NaN; guard with `pd.notna()` fallback.
- Efficient frontier with <3 symbols → degenerate; return early.
- VaR on <30 obs → low confidence.
- Drawdown on flat NAV → cummax division by zero; guard with `replace(0, np.nan)`.
- Empty options chain → zeroed dict, not crash.

## Verification

```bash
venv/bin/python -m pytest tests/ -q   # must be green
venv/bin/ruff check .                 # must exit 0
curl -s http://127.0.0.1:8001/api/meta/data-health
```

**Rules:** run the full suite; add a test for new functions; fix the code when a test fails; verify every metric to the formula level.

## Cross-Reference

- Data sources: `data/AGENTS.md`
- Endpoints: `backend/AGENTS.md`
- Tools: `agent/AGENTS.md`
- History: `journal/analysis.md`

## Recent Changes

Full dated history: `journal/analysis.md`. Read when you need context, not every session.
