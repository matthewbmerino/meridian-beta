"""Principled position sizing.

Answers the question: given a Risk x Momentum score, current holdings,
per-name volatility, and a target risk budget — how much (in dollars) should
the user add or trim on each position?

Framework (synthesized from established literature):

    w_target_i = clip( k * edge_calibrated_i
                       * (sigma_target / sigma_i_floored) * tier_mult
                       , 0 , w_cap )

- Kelly (1956) / Thorp (1969): the edge term is now derived from a
  *calibrated* win probability and payoff ratio via the proper Kelly
  formula: edge = b*p - q, where p is win probability, q = 1-p, and
  b = avg_win / avg_loss.  The raw composite score (0-100) is transformed
  into calibrated (p, b, q) via a logistic/sigmoid mapping (default),
  empirical bucket statistics (when historical data is available), or a
  Bayesian hybrid of both.  See analysis/calibration.py for details.
  Fractional Kelly (k ~ 0.17) for robustness to estimation error.
- Risk parity (Qian 2005): the `sigma_target / sigma_i` term scales inversely
  with each name's realized vol so each position contributes comparable risk.
- Grinold-Kahn (1999): tier multipliers express conviction — Strong Buy sizes
  near the cap, Buy mid, Hold freezes at current weight, Trim halves,
  Reduce goes to zero.
- Markowitz (1952) with practitioner clips: hard `w_cap`, sector cap, and
  cash floor enforce diversification.
- Leland (1999): a no-trade band avoids churning on <1% drifts.
- Michaud (1989): the low-vol trap guard floors sigma at 0.5x median so a
  value-trap stock's suppressed volatility doesn't inflate its weight.
- Moreira-Muir (2017): ex-post portfolio vol check with rho_bar ~ 0.4
  rescales if the naive inverse-vol allocation would exceed the vol target.
- Practitioner overlay: a 25 bps / $100 friction floor suppresses
  microscopic rebalance trades that lose to commissions + slippage, and a
  tax-deferral rule holds short-term gains for the LTCG threshold.

Edge calibration (added 2026-05):

  The previous formula used ``edge = max(0, (score - 50) / 50)`` which
  conflated signal strength with win probability.  A score of 80 yielded
  edge = 0.60, but in practice a strong composite signal does not imply a
  60% expected return — it implies a *higher probability of a positive
  return* with some empirical payoff ratio.  The calibration layer
  (analysis/calibration.py) separates these concerns:

  1. Score -> p(win) via a logistic sigmoid (tunable floor/ceiling/steepness)
  2. Payoff ratio b from empirical data or a configurable default (1.15)
  3. Kelly edge = b*p - q, which is the theoretically correct quantity

  This eliminates the contradiction where a STRONG BUY name could receive a
  TRIM sizing action because the naive edge formula disagreed with the tier
  mapping on what the score means.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

import numpy as np

from analysis.calibration import (
    BucketStats,
    CalibrationConfig,
    calibrate_edge,
    calibration_config_from_prefs,
)


@dataclass
class SizingConfig:
    # --- Core portfolio risk parameters ---
    # Growth-tilted defaults: stay invested, size into conviction, but respect
    # vol + concentration limits. Tune knobs in data/prefs.json per taste.
    w_cap: float = 0.09               # max single-name weight
    sigma_target: float = 0.16        # annualized portfolio vol target
    k: float = 0.17                   # fractional-Kelly conviction coefficient

    # Cash management — a TARGET + a FLOOR (not the same thing):
    target_gross: float = 0.92        # aim to be ~92% invested (8% cash)
    cash_floor: float = 0.03          # hard floor — never drop cash below 3%

    sector_cap: float = 0.35          # max aggregate weight per sector
    corr_assumed: float = 0.40        # avg pairwise correlation for vol check
    vol_overshoot_limit: float = 1.20 # scale down if sigma_P > this * target

    # --- Friction / execution thresholds ---
    trade_band: float = 0.01          # no-trade band (|delta_w| must exceed)
    friction_bps: float = 25.0        # minimum trade as bps of equity
    min_dollar_trade: float = 100.0   # absolute min trade in USD

    # --- Safety guards ---
    min_position_weight: float = 0.01 # below this, close rather than hold a stub
    low_vol_floor_multiplier: float = 0.5  # floor sigma at this * median(sigma)
    default_sigma: float = 0.30       # fallback vol if unavailable

    # When deploying cash toward target_gross, only scale up these tiers:
    deployable_tiers: tuple[str, ...] = ("Strong Buy", "Buy")

    # Edge calibration — transforms raw composite score into a principled
    # Kelly edge (b*p - q) instead of the naive (score-50)/50.
    # See analysis/calibration.py for parameter docs.
    calibration: CalibrationConfig = field(default_factory=CalibrationConfig)


@dataclass
class PositionInput:
    symbol: str
    score: float                      # RxM score 0-100
    current_weight: float             # fraction of portfolio equity
    sigma: float | None = None     # annualized volatility; falls back to default
    sector: str = ""
    holding_days: int | None = None      # for tax-deferral rule
    unrealized_pct: float | None = None  # unrealized gain % (for tax rule)


@dataclass
class SizingResult:
    symbol: str
    tier: str
    current_weight: float
    target_weight: float
    delta_weight: float               # target - current
    delta_dollars: float              # positive = add, negative = trim
    action: str                       # "ADD" | "TRIM" | "HOLD" | "CLOSE"
    rationale: str
    # Audit trail: the binding constraint that produced this target.
    # One of "signal" | "cap" | "sector" | "gross" | "vol" | "conviction"
    # | "deploy" | "no_trade" | "tax" | "at_target" | "none"
    constraint: str = "none"
    # Diagnostics
    raw_weight: float = 0.0           # pre-constraint weight
    tier_mult: float = 0.0
    sigma_used: float = 0.0
    tax_deferred: bool = False


@dataclass
class _PositionState:
    """Mutable per-position pipeline state threaded through the seven stages.

    Replaces the ad-hoc string-keyed dicts the old pipeline used: type-safe,
    and each stage mutates only the fields it owns. ``reduction`` records the
    largest constraint imposed so far so ``_finalize`` can report the binding
    driver honestly.
    """
    pos: PositionInput
    tier: str
    w_raw: float
    sigma_used: float
    tier_mult: float
    constraint: str = "none"
    reduction: float = 0.0
    w_clipped: float = 0.0


# ---------------------------------------------------------------------------
# Tier mapping (matches analysis/stbr and pages/opportunities/positions tiers)
# ---------------------------------------------------------------------------

def tier_of(score: float) -> str:
    if score >= 80:
        return "Strong Buy"
    if score >= 60:
        return "Buy"
    if score >= 40:
        return "Hold"
    if score >= 20:
        return "Trim"
    return "Reduce"


_TIER_MULT = {
    "Strong Buy": 1.5,    # size toward cap
    "Buy":        1.0,    # above-average, below cap
    "Hold":       None,   # freeze at current weight
    "Trim":      -0.5,    # halve current weight
    "Reduce":    -1.0,    # zero target
}


def _stage1_raw_targets(positions, cfg, bucket_stats, sigma_floor, operator_bucket_stats=None) -> list[_PositionState]:
    """Stage 1-2: score-to-tier + per-position raw weight.

    The formula is Kelly-calibrated edge (b*p - q) x inverse-vol x tier
    conviction. The reverse-alpha guard: a high-conviction name above its
    formula optimum is never trimmed — signal-driven trims only come from
    the Trim/Reduce tiers; the w_cap + cash-floor rescale still prevent
    overweight.

    operator_bucket_stats: the operator's own Decision Book buckets — blended
    into the edge when the sample is sufficient (hybrid calibration).
    """
    states: list[_PositionState] = []
    for p in positions:
        tier = tier_of(p.score)
        sigma_i = p.sigma if (p.sigma and p.sigma > 0) else cfg.default_sigma
        sigma_used = max(sigma_i, sigma_floor)

        mult = _TIER_MULT[tier]
        if mult is None:
            # Hold — no-trade zone, target = current
            w_raw = p.current_weight
            tier_mult_numeric = 0.0
        elif mult > 0:
            # Strong Buy / Buy — conviction × inverse-vol × tier. See the
            # reverse-alpha note in the module docstring.
            edge = calibrate_edge(p.score, cfg.calibration, bucket_stats, operator_bucket_stats)
            w_formula = cfg.k * edge * (cfg.sigma_target / sigma_used) * mult
            w_raw = max(w_formula, p.current_weight)
            tier_mult_numeric = mult
        elif mult == -0.5:
            # Trim — halve current (signal-driven, always fires)
            w_raw = 0.5 * p.current_weight
            tier_mult_numeric = mult
        else:
            # Reduce — target zero (signal-driven exit, always fires)
            w_raw = 0.0
            tier_mult_numeric = mult

        states.append(_PositionState(
            pos=p, tier=tier, w_raw=w_raw,
            sigma_used=sigma_used, tier_mult=tier_mult_numeric,
        ))
    return states


def _stage3_cap(states, cfg) -> None:
    """Stage 3: clip each weight to [0, w_cap]."""
    for r in states:
        w_clip = min(max(r.w_raw, 0.0), cfg.w_cap)
        if w_clip < r.w_raw - 1e-9:
            red = r.w_raw - w_clip
            if red > r.reduction:
                r.constraint, r.reduction = "cap", red
        r.w_clipped = w_clip


def _stage4_sector_cap(states, cfg) -> None:
    """Stage 4: scale down over-weighted sectors to sector_cap."""
    sector_totals: dict[str, float] = {}
    for r in states:
        sec = (r.pos.sector or "").strip() or "_unknown"
        sector_totals[sec] = sector_totals.get(sec, 0.0) + r.w_clipped
    for sec, total in list(sector_totals.items()):
        if sec == "_unknown":
            continue
        if total > cfg.sector_cap and total > 0:
            scale = cfg.sector_cap / total
            for r in states:
                if ((r.pos.sector or "").strip() or "_unknown") == sec:
                    before = r.w_clipped
                    r.w_clipped *= scale
                    red = before - r.w_clipped
                    if red > r.reduction:
                        r.constraint, r.reduction = "sector", red


def _stage4b_deploy_cash(states, cfg, bucket_stats, operator_bucket_stats=None) -> None:
    """Stage 4b: deploy idle cash toward target gross exposure.

    Growth-mindset rule: cash above the target level is redirected into the
    highest-conviction names (Strong Buy / Buy) up to the per-name cap,
    proportionally to each candidate's calibrated edge x inverse-vol score.
    """
    gross_after = sum(r.w_clipped for r in states)
    if gross_after >= cfg.target_gross:
        return
    deployable = [
        r for r in states
        if r.tier in cfg.deployable_tiers
        and r.w_clipped < cfg.w_cap - 1e-6
    ]
    if not deployable:
        return

    weights = []
    for r in deployable:
        edge = calibrate_edge(r.pos.score, cfg.calibration, bucket_stats, operator_bucket_stats)
        weights.append(edge * (cfg.sigma_target / r.sigma_used))
    remaining_gap = cfg.target_gross - gross_after
    # Iteratively distribute gap, respecting per-name cap.
    for _ in range(4):  # a few passes to handle cap overflow
        if remaining_gap <= 1e-6:
            break
        still_open = [i for i, r in enumerate(deployable) if r.w_clipped < cfg.w_cap - 1e-6]
        if not still_open:
            break
        open_weight_sum = sum(weights[i] for i in still_open) or 1.0
        new_gap = 0.0
        for i in still_open:
            r = deployable[i]
            alloc = remaining_gap * (weights[i] / open_weight_sum)
            new_w = r.w_clipped + alloc
            if new_w > cfg.w_cap:
                new_gap += (new_w - cfg.w_cap)
                r.w_clipped = cfg.w_cap
            else:
                r.w_clipped = new_w
            # Deployment added weight — record it as the driver unless a
            # cap clip overrode it (the cap reduction is larger).
            if alloc > r.reduction:
                r.constraint, r.reduction = "deploy", alloc
        remaining_gap = new_gap


def _stage5_cash_floor(states, cfg) -> None:
    """Stage 5: hard cash floor via gross-exposure rescale."""
    gross = sum(r.w_clipped for r in states)
    max_gross = 1.0 - cfg.cash_floor
    if gross > max_gross and gross > 0:
        scale = max_gross / gross
        for r in states:
            before = r.w_clipped
            r.w_clipped *= scale
            red = before - r.w_clipped
            if red > r.reduction:
                r.constraint, r.reduction = "gross", red


def _stage6_vol_check(states, cfg, covariance) -> None:
    """Stage 6: ex-post portfolio vol sanity.

    sigma_P^2 = rho * (sum w_i*sigma_i)^2 + (1-rho) * sum w_i^2 * sigma_i^2.
    When a factor-aware covariance matrix is provided, use sigma_P =
    sqrt(w^T * Sigma * w) instead — the blanket rho assumption is replaced
    by the factor model's implied correlation structure.
    """
    vol_ceiling = cfg.vol_overshoot_limit * cfg.sigma_target
    if covariance is not None and not covariance.empty:
        cov_syms = [r.pos.symbol for r in states if r.pos.symbol in covariance.index]
        if len(cov_syms) >= 2:
            w_cov = np.array([r.w_clipped for r in states if r.pos.symbol in covariance.index])
            w_cov = w_cov / w_cov.sum() if w_cov.sum() > 0 else w_cov
            sub = covariance.loc[cov_syms, cov_syms].values
            sigma_P = float(np.sqrt(max(w_cov @ sub @ w_cov, 0.0)))
            if sigma_P > vol_ceiling > 0:
                scale = vol_ceiling / sigma_P
                for r in states:
                    before = r.w_clipped
                    r.w_clipped *= scale
                    red = before - r.w_clipped
                    if red > r.reduction:
                        r.constraint, r.reduction = "vol", red
            return
    rho = cfg.corr_assumed
    s_sum = sum(r.w_clipped * r.sigma_used for r in states)
    s_sq_sum = sum((r.w_clipped ** 2) * (r.sigma_used ** 2) for r in states)
    sigma_P = (rho * (s_sum ** 2) + (1 - rho) * s_sq_sum) ** 0.5
    if sigma_P > vol_ceiling > 0:
        scale = vol_ceiling / sigma_P
        for r in states:
            before = r.w_clipped
            r.w_clipped *= scale
            red = before - r.w_clipped
            if red > r.reduction:
                r.constraint, r.reduction = "vol", red


def compute_target_weights(
    positions: list[PositionInput],
    portfolio_equity: float,
    cfg: SizingConfig | None = None,
    bucket_stats: dict[int, BucketStats] | None = None,
    covariance=None,
    operator_bucket_stats: dict[int, BucketStats] | None = None,
) -> list[SizingResult]:
    """Produce a principled target weight + action per position.

    The algorithm follows the seven-stage pipeline (each stage is a named
    helper operating on a typed _PositionState):
      1. Score-to-tier + tier multiplier
      2. Per-position raw weight from the Kelly x inverse-vol x conviction
         formula, using *calibrated* edge (b*p - q) instead of raw
         (score-50)/50.  The calibration layer (analysis/calibration.py)
         maps the composite score to a win probability via logistic sigmoid,
         empirical bucket stats, or a Bayesian hybrid.
      3. Clip each weight to [0, w_cap]
      4. Sector cap (scale down over-weighted sectors)
      4b. Deploy idle cash toward target gross (conviction-weighted)
      5. Cash floor (scale all down if gross exposure > 1 - cash_floor)
      6. Ex-post portfolio vol check (scale down if >1.2x target). Uses the
         factor-aware covariance matrix when provided (analysis/factors.py),
         falling back to the blanket rho=cfg.corr_assumed assumption.
      7. Delta -> dollars, then no-trade band, friction floor, tax deferral

    covariance: optional pd.DataFrame indexed/columned by symbol — the
    factor-aware covariance (B·Σ_f·Bᵀ + D). When present, stage 6 computes
    sigma_P = sqrt(wᵀ·Σ·w) instead of the rho formula, so two names with
    shared factor exposure are correctly implied to be correlated.

    operator_bucket_stats: the operator's own resolved-outcome buckets from
    the Decision Book (analysis/outcomes.py). The hybrid calibration blends
    them with the logistic prior when the sample is sufficient — the Kelly
    edge on every surface becomes the operator's measured base rate.
    """
    cfg = cfg or SizingConfig()
    if portfolio_equity <= 0 or not positions:
        return []

    sigmas_present = [p.sigma for p in positions if p.sigma and p.sigma > 0]
    sigma_median = median(sigmas_present) if sigmas_present else cfg.default_sigma
    sigma_floor = cfg.low_vol_floor_multiplier * sigma_median

    states = _stage1_raw_targets(positions, cfg, bucket_stats, sigma_floor, operator_bucket_stats)
    _stage3_cap(states, cfg)
    _stage4_sector_cap(states, cfg)
    _stage4b_deploy_cash(states, cfg, bucket_stats, operator_bucket_stats)
    _stage5_cash_floor(states, cfg)
    _stage6_vol_check(states, cfg, covariance)

    return _finalize(states, positions, portfolio_equity, cfg)


def _finalize(states, positions, portfolio_equity, cfg):
    """Stage 7: per-position action, deltas, and friction filters."""
    results: list[SizingResult] = []
    for r in states:
        pos7: PositionInput = r.pos
        w_target = r.w_clipped

        # Close-out rule — if target is positive but below the "keep it a real
        # position" floor, zero it rather than hold a stub.
        if 0.0 < w_target < cfg.min_position_weight and pos7.current_weight < cfg.min_position_weight:
            w_target = 0.0

        delta_w = w_target - pos7.current_weight
        delta_dollars = delta_w * portfolio_equity

        tax_deferred = False
        action = "HOLD"
        rationale_parts: list[str] = []
        constraint = r.constraint
        tier = r.tier

        # The tier's own signal: Trim/Reduce tiers demand the change directly.
        if tier in ("Trim", "Reduce"):
            constraint = "signal"

        # No-trade band — suppresses *near-zero* drifts.
        if abs(delta_w) < cfg.trade_band:
            if delta_w != 0:
                rationale_parts.append("inside no-trade band")
                constraint = "no_trade"
            delta_w, delta_dollars = 0.0, 0.0
        else:
            # Friction floor
            friction_dollars = max(cfg.friction_bps / 1e4 * portfolio_equity, cfg.min_dollar_trade)
            if abs(delta_dollars) < friction_dollars:
                delta_w, delta_dollars = 0.0, 0.0
                rationale_parts.append("below friction floor")
                constraint = "no_trade"

        # Tax deferral: don't realize short-term gains to trim a Hold-ish name
        if (
            delta_dollars < 0
            and pos7.holding_days is not None and pos7.holding_days < 365
            and pos7.unrealized_pct is not None and pos7.unrealized_pct > 0
        ):
            # Only Trim tier is worth deferring; Reduce is urgent enough to eat the tax
            if tier == "Trim":
                delta_w, delta_dollars = 0.0, 0.0
                tax_deferred = True
                rationale_parts.append(
                    f"deferred — short-term gain ({365 - pos7.holding_days}d to LTCG)"
                )
                constraint = "tax"

        # Classify action post-filters
        if delta_dollars > 0:
            action = "ADD"
        elif delta_dollars < 0:
            action = "CLOSE" if w_target == 0.0 else "TRIM"
        else:
            action = "HOLD"

        # Compose rationale — lead with the actual binding constraint, not a guess.
        if action == "ADD":
            if constraint == "deploy":
                lead = f"{tier} — deployed idle cash toward {w_target*100:.1f}%"
            else:
                lead = f"{tier} — deploy toward {w_target*100:.1f}%"
        elif action == "TRIM":
            if constraint == "cap":
                lead = f"{tier} — cap-driven (over per-name limit {cfg.w_cap*100:.0f}%)"
            elif constraint == "sector":
                lead = f"{tier} — sector cap (sector over {cfg.sector_cap*100:.0f}%)"
            elif constraint == "gross":
                lead = f"{tier} — gross rescale to respect cash floor"
            elif constraint == "vol":
                lead = f"{tier} — vol-constrained (portfolio vol above target)"
            elif constraint == "signal":
                lead = f"{tier} — signal-driven reduce to {w_target*100:.1f}%"
            elif constraint == "none" and tier in ("Strong Buy", "Buy") and w_target >= cfg.w_cap - 1e-6:
                lead = f"{tier} — cap-driven (over per-name limit {cfg.w_cap*100:.0f}%)"
            else:
                lead = f"{tier} — reduce to {w_target*100:.1f}%"
        elif action == "CLOSE":
            lead = f"{tier} — exit position (signal-driven)"
        else:
            # HOLD — distinguish reasons
            if constraint == "tax":
                lead = f"{tier} — " + "; ".join(rationale_parts)
            elif constraint == "no_trade":
                lead = f"{tier} — " + "; ".join(rationale_parts)
            elif tier == "Hold":
                lead = "Hold tier — no-trade zone"
                if constraint == "none":
                    constraint = "no_trade"
            elif tier in ("Strong Buy", "Buy"):
                # High-conviction and within cap: conviction confirms current
                lead = f"{tier} — conviction confirms current weight"
                if constraint == "none":
                    constraint = "conviction"
            else:
                lead = f"{tier} — at target"
        rationale = lead

        results.append(SizingResult(
            symbol=pos7.symbol,
            tier=tier,
            current_weight=pos7.current_weight,
            target_weight=w_target,
            delta_weight=delta_w,
            delta_dollars=delta_dollars,
            action=action,
            rationale=rationale,
            constraint=constraint,
            raw_weight=r.w_raw,
            tier_mult=r.tier_mult,
            sigma_used=r.sigma_used,
            tax_deferred=tax_deferred,
        ))

    return results


def compute_post_trade_weight(
    current_mv: float,
    trade_dollars: float,
    nav: float,
    side: str = "BUY",
) -> float:
    """Weight of a position *after* a hypothetical trade.

    Pure weight math used by the sizing-preview endpoint: the position's
    would-be market value (existing + signed trade) divided by the post-trade
    NAV. For a new name current_mv is 0. A SELL beyond the position would make
    the weight negative — callers validate before use.
    """
    if nav <= 0:
        return 0.0
    signed = trade_dollars if side.upper() == "BUY" else -trade_dollars
    post_mv = current_mv + signed
    post_nav = nav + signed
    if post_nav <= 0:
        return 0.0
    return post_mv / post_nav


def portfolio_sigma(
    weights: list[float],
    sigmas: list[float],
    rho: float = 0.40,
) -> float:
    """Ex-post portfolio volatility under an assumed pairwise correlation.

    sigma_P^2 = rho * (sum w_i*sigma_i)^2 + (1-rho) * sum w_i^2 * sigma_i^2.
    Mirrors the stage-6 vol check inside compute_target_weights.
    """
    if len(weights) != len(sigmas) or not weights:
        return 0.0
    s_sum = sum(w * s for w, s in zip(weights, sigmas))
    s_sq_sum = sum((w ** 2) * (s ** 2) for w, s in zip(weights, sigmas))
    return (rho * (s_sum ** 2) + (1 - rho) * s_sq_sum) ** 0.5


def config_from_prefs(prefs_dict: dict) -> SizingConfig:
    """Build a SizingConfig from the user-prefs dict. Missing keys fall back
    to SizingConfig defaults."""
    defaults = SizingConfig()
    return SizingConfig(
        w_cap=float(prefs_dict.get("sizing_w_cap", defaults.w_cap)),
        sigma_target=float(prefs_dict.get("sizing_sigma_target", defaults.sigma_target)),
        k=float(prefs_dict.get("sizing_k", defaults.k)),
        target_gross=float(prefs_dict.get("sizing_target_gross", defaults.target_gross)),
        cash_floor=float(prefs_dict.get("sizing_cash_floor", defaults.cash_floor)),
        sector_cap=float(prefs_dict.get("sizing_sector_cap", defaults.sector_cap)),
        corr_assumed=float(prefs_dict.get("sizing_corr_assumed", defaults.corr_assumed)),
        trade_band=float(prefs_dict.get("sizing_trade_band", defaults.trade_band)),
        friction_bps=float(prefs_dict.get("sizing_friction_bps", defaults.friction_bps)),
        min_position_weight=float(prefs_dict.get("sizing_min_position_weight", defaults.min_position_weight)),
        calibration=calibration_config_from_prefs(prefs_dict),
    )
