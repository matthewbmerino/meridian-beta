"""Methodology reference for every surfaced metric.

Pure data — no computation, no side effects. Each entry documents the formula
exactly as implemented in the analysis layer, so the operator can verify any
number to the formula level (root AGENTS.md: "every metric must be defensible
to the formula level"). Served by GET /api/meta/methodology/{metric}.

Every entry carries the same shape so the frontend can render it uniformly:
    name, formula, description, inputs, interpretation, source.
"""

from __future__ import annotations

METHODOLOGY: dict[str, dict] = {
    "data_imports": {
        "name": "Import pipeline",
        "formula": "CSV/PDF → parse → merge into positions, realized lots, or external P&L",
        "description": (
            "Every uploaded file is one IMPORT record, not a second copy of the book: it merges into "
            "the position/realized/external ledgers and can be hidden (excluded from every total) "
            "without being deleted. Position files are merged with the live Schwab data; realized "
            "files populate the Performance page's YTD realized P&L; PDFs and statements are extracted "
            "by the LLM into external P&L and folded into the realized totals by source. An import the "
            "LLM cannot reconcile is flagged for review rather than accepted silently."
        ),
        "inputs": ["Broker CSVs (Chase, Fidelity, Schwab)", "Statement PDFs (Fanduel, prediction markets)"],
        "interpretation": "A hidden import still exists — it is excluded from totals, not deleted.",
        "source": "data/imports.py; backend/routers/imports.py",
    },
    "sortino": {
        "name": "Sortino Ratio",
        "formula": "Sortino = (R̄ − Rf) / DD × √252,  DD = √(mean(min(R − Rf, 0)²))",
        "description": (
            "Risk-adjusted return that penalizes only downside volatility. "
            "Upside volatility is not penalized — it is exactly what an investor wants."
        ),
        "inputs": ["Daily portfolio returns", "Risk-free rate (default 0%)"],
        "interpretation": "<0 negative · 0–1 weak · 1–2 adequate · >2 strong",
        "source": "Sortino & Price (1994); analysis/risk.py sortino_ratio",
    },
    "sharpe": {
        "name": "Sharpe Ratio",
        "formula": "Sharpe = (R̄ − Rf) / σ × √252",
        "description": (
            "Risk-adjusted return penalizing total volatility (upside and downside). "
            "Computed internally; never the headline metric — Sortino is primary."
        ),
        "inputs": ["Daily portfolio returns", "Risk-free rate (default 0%)"],
        "interpretation": "Higher is better; penalizes upside vol, so Sortino is preferred",
        "source": "Sharpe (1966); analysis/risk.py sharpe_ratio",
    },
    "var": {
        "name": "Value at Risk (95%)",
        "formula": "VaR₉₅ = percentile(R, 5%)",
        "description": (
            "Historical VaR: the 5th-percentile daily return over the sample. "
            "A negative number representing the loss threshold not exceeded 95% of days."
        ),
        "inputs": ["Daily portfolio returns (sample window)"],
        "interpretation": "More negative = more risk; the loss expected 1 day in 20",
        "source": "analysis/risk.py historical_var",
    },
    "cvar": {
        "name": "Conditional VaR (Expected Shortfall)",
        "formula": "CVaR₉₅ = mean(R | R ≤ VaR₉₅)",
        "description": (
            "The average loss in the worst 5% of days — the expected loss beyond VaR. "
            "More actionable than VaR: it tells you the expected loss in the tail."
        ),
        "inputs": ["Daily portfolio returns (sample window)"],
        "interpretation": "More negative = deeper tail risk than VaR alone suggests",
        "source": "analysis/risk.py conditional_var",
    },
    "hhi": {
        "name": "Herfindahl-Hirschman Index",
        "formula": "HHI = Σ (wᵢ × 100)²,  wᵢ = |MVᵢ| / Σ|MV|",
        "description": (
            "Position concentration. Ranges from 0 (perfectly diversified) to "
            "10,000 (single holding). Uses absolute market values so shorts count."
        ),
        "inputs": ["Position market values"],
        "interpretation": "<1000 diversified · 1000–1800 moderate · >1800 concentrated",
        "source": "analysis/portfolio.py herfindahl_index",
    },
    "max_drawdown": {
        "name": "Max Drawdown",
        "formula": "MDD = min((NAV − running_max(NAV)) / running_max(NAV))",
        "description": "The largest peak-to-trough decline in portfolio value over the window.",
        "inputs": ["Portfolio NAV time series"],
        "interpretation": "More negative = deeper historical loss; context for sizing conviction",
        "source": "analysis/risk.py max_drawdown",
    },
    "stbr": {
        "name": "Short-Term Bubble Risk",
        "formula": "STBR = Price / SMA₁₄₀",
        "description": (
            "Price relative to its 140-day moving average. Bands: <0.75 Bearish, "
            "0.75–1 Neutral, 1–1.25 Normal, 1.25–1.5 Heating Up, 1.5–1.75 Risky, "
            "1.75–2 Super Risky, >2 Bubble Pop."
        ),
        "inputs": ["Daily close prices (140d SMA)"],
        "interpretation": ">1.25 overextended; >2 bubble territory — trim candidates",
        "source": "analysis/stbr.py calculate_stbr",
    },
    "composite_score": {
        "name": "Composite Signal Score",
        "formula": "Score = 50 + 0.5 × Σ wᵢ·cᵢ,  cᵢ ∈ [−1, +1]",
        "description": (
            "0–100 blend of technical indicators, each weighted by predictive "
            "reliability: RSI 25% (mean-reversion polarity), Trend/SMA50 20%, "
            "MACD 20%, Bollinger %B 15%, Volume context 10%, Volatility regime 10%."
        ),
        "inputs": ["RSI", "SMA50 distance + crossover", "MACD histogram", "Bollinger %B", "Volume ratio", "HV30"],
        "interpretation": "≥65 BUY · 35–65 NEUTRAL · <35 SELL",
        "source": "analysis/signals.py compute_composite_score",
    },
    "kelly_edge": {
        "name": "Calibrated Kelly Edge",
        "formula": "edge = b·p − q,  floored at 0",
        "description": (
            "The expected-value term of the Kelly criterion. p = calibrated win "
            "probability (logistic sigmoid, empirical buckets, or hybrid), "
            "b = payoff ratio (avg win / avg loss), q = 1 − p. Replaces the naive "
            "(score−50)/50 proxy."
        ),
        "inputs": ["Composite score", "Calibration method (logistic/empirical/hybrid)", "Historical forward returns"],
        "interpretation": ">0 positive expected value; drives fractional-Kelly sizing",
        "source": "Kelly (1956); analysis/calibration.py calibrate_edge",
    },
    "portfolio_weather": {
        "name": "Portfolio Weather",
        "formula": "Weighted score: Vol 25% + VIX 20% + Drawdown 20% + Breadth 10% + Credit 15% + Yield curve 10%",
        "description": (
            "Layer-4 forecasting composite. Synthesizes realized portfolio vol, VIX, "
            "drawdown, sector breadth, credit spreads, and yield-curve spread into "
            "Calm / Watchful / Elevated / Storm. Hysteresis prevents flickering."
        ),
        "inputs": [
            "Portfolio vol", "VIX", "Drawdown", "Breadth (% above SMA50)",
            "Credit spread vs 2y avg", "10Y−2Y spread",
        ],
        "interpretation": "Elevated/Storm → de-risk; Calm/Watchful → maintain",
        "source": "analysis/composites.py portfolio_weather",
    },
    "risk_regime": {
        "name": "Risk Regime",
        "formula": (
            "60d return 20% + vol 20% + skew 15% + autocorr 10% + %down days 10% "
            "+ credit 10% + yield curve 8% + VIX 7%"
        ),
        "description": (
            "Layer-4 forecasting composite. Classifies market state from 60-day "
            "return characteristics plus cross-asset confirmation: Trending / Choppy / "
            "Euphoric / Distribution / Panic-Rebound. Hysteresis prevents flickering."
        ),
        "inputs": [
            "60d return", "Annualized vol", "Skewness", "Autocorrelation",
            "% down days", "Credit spread", "Yield curve", "VIX",
        ],
        "interpretation": "Distribution/Panic-Rebound → defensive; Trending → risk-on",
        "source": "analysis/composites.py risk_regime",
    },
    "capital_at_risk": {
        "name": "Capital at Risk",
        "formula": "CaR = |VaR₅d,₉₅| × NAV",
        "description": "Dollar amount at risk over the next 5 trading days at 95% confidence.",
        "inputs": ["5-day 95% VaR", "Portfolio NAV", "5-day 95% CVaR (optional)"],
        "interpretation": "The dollar loss not exceeded 95% of 5-day windows",
        "source": "analysis/composites.py capital_at_risk",
    },
    "avg_correlation": {
        "name": "Average Pairwise Correlation",
        "formula": "ρ̄ = mean over pairs of corr(Rᵢ, Rⱼ)",
        "description": "Mean of the off-diagonal correlation matrix entries across holdings.",
        "inputs": ["Daily returns per holding (aligned to common dates)"],
        "interpretation": ">0.7 high (concentrated in risk space) · 0.4–0.7 moderate · <0.4 low",
        "source": "analysis/risk.py correlation_matrix",
    },
    "portfolio_beta": {
        "name": "Portfolio Beta",
        "formula": "β = Σ wᵢ·βᵢ,  βᵢ = cov(Rᵢ, R_SPY) / var(R_SPY)",
        "description": "Weighted-average sensitivity of the portfolio to the market (SPY).",
        "inputs": ["Per-holding returns", "SPY returns", "Position weights"],
        "interpretation": ">1.2 aggressive · 0.8–1.2 market · <0.8 defensive",
        "source": "analysis/risk.py portfolio_beta, weighted_portfolio_beta",
    },
    "factor_exposure": {
        "name": "Factor Exposure",
        "formula": "βᵢ,ⱼ = OLS(Rᵢ, Fⱼ),  F ∈ {market, size, value, momentum, quality}",
        "description": (
            "Per-asset sensitivity to each factor, estimated by OLS regression of "
            "the asset's returns on the factor return series. Factor returns are "
            "built Fama-French style from the cross-section: market (index), "
            "size (SMB), value (HML), momentum (WML), quality (high-minus-low)."
        ),
        "inputs": ["Per-holding returns", "Market returns", "Market caps", "Value metrics", "Quality metrics"],
        "interpretation": "|β|>1 high exposure; portfolio exposure = weight-weighted average",
        "source": "Fama & French (1993); analysis/factors.py factor_exposures",
    },
    "factor_covariance": {
        "name": "Factor-Aware Covariance",
        "formula": "Σ = B·Σ_f·Bᵀ + D",
        "description": (
            "Covariance matrix implied by the factor model: B is the exposure "
            "matrix, Σ_f the factor covariance, D the diagonal idiosyncratic "
            "variance. Replaces the blanket ρ̄=0.4 assumption — two names with "
            "shared factor exposure are correctly implied to be correlated."
        ),
        "inputs": ["Factor exposures", "Factor return covariance", "Residual volatility"],
        "interpretation": "Drives the ex-post portfolio vol check in sizing",
        "source": "analysis/factors.py factor_covariance",
    },
    "compute_exposure": {
        "name": "Compute Exposure",
        "formula": (
            "exposure_weight = Σ market_value(compute-linked holdings) / NAV; "
            "drop = (peak(index) − current(index)) / peak(index)"
        ),
        "description": (
            "The operator's capital exposed to the price of compute, mapped "
            "against the Compute Index trend (Layer-5 relationship "
            "intelligence). A holding is compute-linked when its recorded "
            "thesis sets compute_linked. The index drop is reported with a "
            "reliability flag — the index is an available-offer spot index, "
            "not a full-market index, so below " + str(90) + " snapshots the "
            "drop is informational only and never gates capital (backlog #10)."
        ),
        "inputs": ["Compute-linked theses (data/theses)", "Position market values + NAV", "Compute Index series"],
        "interpretation": "Higher weight = more of the book priced by compute; drop is context, not a signal",
        "source": "analysis/compute_exposure.py compute_exposure / index_drop",
    },
    "compute_index": {
        "name": "Compute Index",
        "formula": (
            "index_t = 100 × Σ(w_i × price_median_i,t) / Σ(w_i × price_median_i,base), "
            "w_i = tflops_i / Σ(tflops_j)"
        ),
        "description": (
            "The spot price of compute as a financialized commodity. A "
            "TFLOPS-weighted basket of median GPU rental prices (Vast.ai "
            "marketplace) normalized to 100 at the base date. A GPU that "
            "delivers more compute per dollar gets more weight — the index "
            "measures the price of compute, not the price of a GPU box. "
            "Prices come from the marketplace's available offers (search "
            "offers API); a GPU class that is fully rented out drops out of "
            "the sample, so the index is an available-offer spot price index, "
            "not a full-market index."
        ),
        "inputs": [
            "Per-GPU median $/GPU-hr (Vast.ai available offers)",
            "GPU TFLOPS ratings (FP8)",
            "Base-date prices",
        ],
        "interpretation": "Rising = compute getting more expensive; falling = depreciation/oversupply",
        "source": "analysis/compute_index.py compute_index",
    },
    "brinson_attribution": {
        "name": "Brinson Attribution",
        "formula": (
            "Allocationᵢ = (w_p,i − w_b,i)·R_b,i · Selectionᵢ = w_b,i·(R_p,i − R_b,i) "
            "· Interactionᵢ = (w_p,i − w_b,i)·(R_p,i − R_b,i)"
        ),
        "description": (
            "Decomposes the portfolio's excess return vs a benchmark into "
            "allocation (weighting decisions), selection (stock-picking within "
            "a sector), and interaction (the joint effect)."
        ),
        "inputs": ["Position snapshot history", "Benchmark returns (SPY)", "Sector map"],
        "interpretation": "Positive allocation = right sector bets; positive selection = right names",
        "source": "Brinson, Hood & Beebower (1986); analysis/attribution.py brinson_attribution",
    },
    "thesis_review": {
        "name": "Thesis Review",
        "formula": (
            "status = target_hit if (price ≥ target_price); "
            "invalidated if (STBR ∈ {Bubble, Bubble Pop, Overextended}); "
            "at_risk if (earnings ≤ 5d) or (insider selling, conviction ≥ 4) or "
            "(compute-linked and index −10% from peak); stale if unreviewed > 7d"
        ),
        "description": (
            "Evaluates a recorded position thesis against live triggers. "
            "The operator's discipline: exits are upside objectives, not "
            "downside stops — a price at/above target means the thesis played "
            "out (an inform signal to consider taking profit, never a forced "
            "loss). Hard triggers (STBR bubble) invalidate; warning triggers "
            "(earnings proximity, insider selling, compute-index drop) mark "
            "at-risk; an unreviewed thesis goes stale. The review is a "
            "heuristic, not a certainty — it surfaces when the operator should "
            "re-examine a position, never auto-executes."
        ),
        "inputs": [
            "Thesis record (target_price, conviction, invalidation_triggers, compute_linked)",
            "Live price history",
            "STBR (140d SMA ratio)",
            "Earnings calendar",
            "SEC Form 4 insider activity",
            "Compute Index history",
        ],
        "interpretation": "invalidated = re-examine now; at_risk = watch; stale = review overdue",
        "source": "backend/services/theses.py evaluate_thesis_review",
    },
    "theme_concentration": {
        "name": "Theme Concentration (HHI)",
        "formula": "HHI_theme = Σ(w_t)² × 10000,  w_t = theme market value / total market value",
        "description": (
            "Herfindahl-Hirschman Index across themes, not positions. A book "
            "that is 75% one theme is a single decision, not many — theme "
            "concentration makes that visible the same way single-name HHI "
            "makes position concentration visible. The symbol→theme registry "
            "is the theses store; positions without a recorded theme roll into "
            "No Thesis (and dilute the concentration measure honestly)."
        ),
        "inputs": ["Position market values", "Symbol→theme map (theses store)"],
        "interpretation": "0 = diversified across themes; 10000 = fully one theme",
        "source": "analysis/themes.py theme_concentration",
    },
    "win_rate": {
        "name": "Decision Win Rate",
        "formula": (
            "win_rate = wins / n;  long/add win = forward_ret > 0;  "
            "trim/sell/close win = forward_ret < SPY forward_ret"
        ),
        "description": (
            "The share of resolved decisions marked a win. Two-sided, as a desk "
            "books it: a long wins when the forward return is positive, a trim/"
            "sell/close wins when the forward return is below the SPY benchmark "
            "(value booked by dodging the decline). Reported per horizon "
            "(1d/5d/20d/60d). Raw returns are always stored; win/loss is derived."
        ),
        "inputs": [
            "Resolved decisions (data/outcomes.py)",
            "Forward symbol + SPY returns",
            "Decision action/direction",
        ],
        "interpretation": "Compare to 0.5; small samples are marked unverified, never trusted",
        "source": "data/outcomes.py _is_win; analysis/outcomes.py scorecard",
    },
    "information_coefficient": {
        "name": "Information Coefficient (IC)",
        "formula": "IC = Pearson corr(decision anchor score, forward return)",
        "description": (
            "Grinold-Kahn skill measure: the linear correlation between the "
            "decision's score at decision time (the anchor snapshot) and the "
            "subsequent return. Positive IC means higher-scored decisions did "
            "better — the operator's signal has value. Degenerates to None "
            "below 5 pairs or when either series has zero variance."
        ),
        "inputs": ["Decision anchor score at decision time", "Forward returns", "Resolved sample"],
        "interpretation": ">0.05 meaningful; <0 the score is inversely predictive; thin samples unverified",
        "source": "analysis/outcomes.py _safe_ic (Grinold & Kahn 1999)",
    },
    "wilson_lower": {
        "name": "Wilson Lower Bound",
        "formula": "LB = (p + z²/2n − z·√(p(1−p)/n + z²/4n²)) / (1 + z²/n),  z = 1.96, p = wins/n",
        "description": (
            "The conservative lower end of the win-rate confidence interval. "
            "Used to gate the desk rank: a high win rate on a small sample "
            "cannot rank up, because the lower bound stays close to chance. "
            "This is the anti-cheat — logging filler decisions cannot inflate "
            "measured skill."
        ),
        "inputs": ["Win count", "Resolved sample size", "z = 1.96 (95%)"],
        "interpretation": "Rises with both win rate and sample size; the honest floor on skill",
        "source": "Wilson (1927); analysis/outcomes.py _wilson_lower_bound",
    },
    "decision_sharpe": {
        "name": "Decision Sharpe",
        "formula": "decision_sharpe = mean(excess) / std(excess),  excess = forward ret − SPY forward ret",
        "description": (
            "Risk-adjusted consistency of the decision set's forward excess "
            "returns (vs SPY). Reported per horizon; the decision-book analogue "
            "of the portfolio's Sharpe. The headline skill measures remain win "
            "rate and IC, which are less sensitive to a single outlier."
        ),
        "inputs": ["Resolved decision forward returns", "SPY forward return (benchmark)"],
        "interpretation": "Higher = more consistent decision edge; outlier-sensitive",
        "source": "analysis/outcomes.py scorecard",
    },
    "momentum_breadth": {
        "name": "Momentum Breadth",
        "formula": "score = 0.60·(% above 50d SMA) + 0.40·(% above 200d SMA), renormalized when one is missing",
        "description": (
            "Layer-4 continuous composite (0–100): broad participation in the "
            "trend. Uses the portfolio/universe share of names above the 50d and "
            "200d moving averages, weighted 60/40. High = most names participating "
            "(healthy); low = narrow leadership (fragile). Returns a score, not a "
            "raw percentage."
        ),
        "inputs": ["% of universe above 50d SMA", "% above 200d SMA"],
        "interpretation": "≥75 broad · 50–75 moderate · 30–50 narrow · <30 fragile",
        "source": "analysis/composites.py momentum_breadth",
    },
    "portfolio_greeks": {
        "name": "Portfolio Greeks",
        "formula": "greek_total = Σ option_position (greek × quantity × option_multiplier)",
        "description": (
            "First- and second-order option sensitivities aggregated across the "
            "book: delta (directional), gamma (delta's rate of change), theta "
            "(time decay), vega (vol sensitivity). Scaled by quantity and the "
            "contract multiplier. Only option positions contribute."
        ),
        "inputs": ["Option positions", "Per-contract delta/gamma/theta/vega", "Option multiplier (default 100)"],
        "interpretation": "Signed net exposure — the book's options risk, not equity risk",
        "source": "analysis/options.py aggregate_greeks",
    },
    "monte_carlo_var": {
        "name": "Monte Carlo VaR",
        "formula": "VaR₉₅ = percentile(simulated horizon returns, 5%); block-bootstrap of actual daily returns",
        "description": (
            "Value at Risk by simulation rather than the historical percentile. "
            "Bootstraps `horizon` consecutive days of the actual return matrix "
            "many times (preserving cross-asset correlation), compounds each "
            "path, and takes the 5th percentile. When no seed is passed the "
            "result varies run to run — compare to historical VaR."
        ),
        "inputs": ["Position weights", "Joint daily-return matrix", "Horizon (days)", "Simulation count"],
        "interpretation": "More negative = larger simulated tail loss; CVaR₉₅ is the mean of the worst 5%",
        "source": "analysis/stress.py monte_carlo_var",
    },
    "analyst_targets": {
        "name": "Analyst Price Target Consensus",
        "formula": "mean = \u03a3(target_i) / N  \u00b7  band = max(target) \u2212 min(target)",
        "description": (
            "The sell-side consensus 12-month price target: the average of the published "
            "targets of every covering analyst, plus the highest and lowest of them. It is a "
            "THIRD-PARTY AVERAGE, not a modelled fair value, and it behaves backwards: the "
            "mean moves when analysts revise, and revisions historically FOLLOW the price "
            "rather than lead it. The band width is the disagreement \u2014 a wide band means "
            "the street cannot agree on the earnings path, so the same mean with a 60% band "
            "is a weaker statement than with a 15% band. "
            "The VETTED consensus is the professional read: it averages only named institutions "
            "on the registry (tier 1 global/bulge-bracket desks and tier 2 major independent and "
            "regional research houses), and only those that have published inside the recency "
            "window. Two exclusions are always disclosed by name: houses outside the registry, "
            "and registry houses whose latest target is older than the window. An unfiltered "
            "average mixes a desk that carries the name for years with a shop that upgrades "
            "everything, and the mean of those two is not a view of anything."
        ),
        "interpretation": (
            "Mean above price = the street expects upside. Read the band WITH it: >60% of the "
            "mean is wide disagreement, <30% is tight agreement. Under ~5 analysts a single "
            "revision moves the mean. It is not a forecast meridian endorses and not a stop. "
            "Since the VETTED cut exists, the headline is the vetted mean: a house that is not "
            "on the registry, or that has not published inside the window, is excluded and "
            "NAMED, never anonymous."
        ),
        "inputs": [
            "Per-house 12-month targets and ratings from the revision feed",
            "The institution registry (tier 1 global desks, tier 2 major/regional research)",
            "Publication date per house (the consensus is recency-bounded)",
            "Last price (for the upside and the falsifier)",
        ],
        "source": "data/analyst_targets.py get_analyst_targets + get_institutional_targets "
                  "(Yahoo Finance); registry in data/analyst_institutions.py",
    },
    "stbr_zscore": {
        "name": "STBR Z-Score",
        "formula": "z = (STBR_now − mean(STBR, 252d)) / std(STBR, 252d)",
        "description": (
            "How unusual the current bubble-risk reading is relative to its own "
            "history. STBR is price / 140d SMA; the z-score says whether today's "
            "extension is typical or extreme *for this name*, which the raw ratio "
            "alone cannot. Returns 0.0 when the series is too short (<20) or flat."
        ),
        "inputs": ["STBR series", "Lookback (252d default)"],
        "interpretation": ">2 unusually extended vs own history; <−2 unusual compression",
        "source": "analysis/stbr.py stbr_zscore",
    },
    "stbr_velocity": {
        "name": "STBR Velocity",
        "formula": "velocity = (STBR_now / STBR_{t−20} − 1) × (252/20)",
        "description": (
            "The annualized rate at which bubble risk is building or unwinding "
            "over a 20-day window. A high STBR that is flat is different from one "
            "that is accelerating — velocity turns a level into a warning. "
            "Returns 0.0 when the series is too short."
        ),
        "inputs": ["STBR series", "Window (20d default)"],
        "interpretation": "Strongly positive = risk building fast; negative = cooling",
        "source": "analysis/stbr.py stbr_velocity",
    },
    "discipline_score": {
        "name": "Discipline Score",
        "formula": (
            "score = Σ (component × weight) / Σ weight, over the components present: "
            "Review Freshness 0.40 + Forecast Precision 0.35 + Research Discipline 0.25"
        ),
        "description": (
            "Process quality, not outcomes — the Desk Loop rewards the craft, "
            "never P&L. Review Freshness = theses reviewed within the interval; "
            "Forecast Precision = decisions carrying both a direction and a "
            "horizon (a scoreable forecast, not a bare note); Research "
            "Discipline = committed ideas backed by a thesis or linked report. "
            "A component with no input is absent, not zero; the score "
            "renormalizes over what is present and is None when nothing is."
        ),
        "inputs": [
            "Theses + last-review times",
            "Decisions (direction, horizon)",
            "Pipeline items (stage, thesis_ref, report_ids)",
        ],
        "interpretation": (
            "0 = no process discipline · 100 = every obligation met; "
            "confidence rises with components present"
        ),
        "source": "analysis/engagement.py discipline_score",
    },
    "desk_rank": {
        "name": "Desk Rank",
        "formula": (
            "rank from the Wilson lower bound of the resolved win rate: "
            "Developing ≥ 0.50, Consistent ≥ 0.55, Proven ≥ 0.60 — gated on "
            "n ≥ min_obs AND IC > 0"
        ),
        "description": (
            "The operator's measured-skill tier. Gated on the honesty "
            "contract: with too few resolved decisions, a non-positive "
            "information coefficient, or no resolved win rate, the rank stays "
            "Unrated and states the gate. Logging filler decisions cannot rank "
            "up — only a win-rate's Wilson lower bound that clears the rung, "
            "on a sample large enough to mean something."
        ),
        "inputs": [
            "Resolved decision scorecard",
            "Wilson lower bound",
            "Information coefficient",
            "Resolved sample size",
        ],
        "interpretation": "Unrated → Developing → Consistent → Proven; the gate is shown, never hidden",
        "source": "analysis/engagement.py desk_rank; Wilson (1927), Grinold & Kahn (1999)",
    },
    "active_streak": {
        "name": "Active Streak",
        "formula": "consecutive days ending today or yesterday with ≥ 1 logged decision",
        "description": (
            "The number of consecutive days the operator logged at least one "
            "decision, counted backward from today (or yesterday, so an "
            "unlogged morning doesn't break it). Rewards the daily ritual, not "
            "the outcome."
        ),
        "inputs": ["Logged decisions (timestamps)"],
        "interpretation": "0 = no active streak; higher = a sustained daily process",
        "source": "analysis/engagement.py active_streak",
    },
    "hv_rank": {
        "name": "Historical Volatility Rank",
        "formula": "HV_rank = (HV_now − HV_52w_low) / (HV_52w_high − HV_52w_low) × 100,  HV = 30d realized vol",
        "description": (
            "Where today's 30d realized volatility sits within the past year's "
            "range (0 = 52w low, 100 = 52w high). A premium-capture proxy used "
            "when the options chain IV rank is unavailable (feed 403): high HV "
            "rank means the name is moving more than usual, so option premium is "
            "relatively rich."
        ),
        "inputs": ["Current 30d historical volatility", "Trailing 252d HV high/low"],
        "interpretation": ">70 rich (premium-selling candidate) · <30 compressed",
        "source": "analysis/signals.py iv_rank (applied to HV); backend/routers/market.py iv_rank",
    },
}


def get_methodology(metric: str) -> dict | None:
    """Return the methodology entry for a metric, or None if unknown."""
    return METHODOLOGY.get(metric)


def list_metrics() -> list[str]:
    """Return the available methodology metric names."""
    return sorted(METHODOLOGY.keys())
