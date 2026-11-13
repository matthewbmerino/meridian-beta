"""Analyst price targets for a symbol — Yahoo Finance, cached.

A consensus target is a THIRD-PARTY AVERAGE, not a fact about the company: it is a
forward 12-month estimate from a set of sell-side analysts whose number and identity
change over time. So the payload carries its own provenance (source, the contributing
analyst count, the fetch date) and refuses to render a number without it, and a symbol
with no coverage returns ``available=False`` plus a reason rather than a zeroed shell
that would read as "analysts think it is worth nothing".

Provider and caching mirror ``data/earnings.py``: Yahoo, with ``ttl_cache`` bounding
the fetch (targets move slowly and Yahoo rate-limits aggressively).
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime, timezone

import pandas as pd

from data.analyst_institutions import VETTED_TIERS, criteria, resolve, tier_label
from data.cache import ttl_cache

log = logging.getLogger(__name__)

SOURCE = "Yahoo Finance analyst price targets"
BASIS = "12-month forward price targets, averaged across the contributing analysts"
CAVEAT = (
    "A consensus is an average of estimates, not a call meridian endorses, and the "
    "analyst count moves as coverage changes."
)


def _num(raw: dict | None, key: str) -> float | None:
    """Pull a finite positive number out of Yahoo's loosely-typed dict.

    Yahoo returns ``None``, ``'Infinity'`` and occasionally a string; anything that
    is not a usable number becomes ``None`` so the caller decides, rather than a
    NaN propagating into the chart's price scale.
    """
    if not raw:
        return None
    try:
        value = raw.get(key)
    except AttributeError:
        return None
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return number


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _positive(value: float | None) -> float | None:
    """A non-positive price target is a missing value, not an estimate."""
    if value is None or value <= 0:
        return None
    return value


# Rating words differ per house ("Outperform" vs "Overweight" vs "Buy"); the buckets are
# what make a rating MIX comparable across firms.
_BULLISH = {
    "strong buy", "buy", "outperform", "overweight", "add", "accumulate", "positive",
    "top pick", "conviction buy", "market outperform", "sector outperform", "speculative buy",
}
_NEUTRAL = {
    "hold", "neutral", "market perform", "sector perform", "equal-weight", "equal weight",
    "in-line", "inline", "peer perform", "sector weight", "market weight", "average",
    "fair value", "market perform ",
}
_BEARISH = {
    "sell", "underperform", "underweight", "reduce", "negative", "strong sell",
    "market underperform", "sector underperform", "below average",
}

# A target older than roughly two quarters describes a different regime, so it is counted
# in the consensus but reported as stale rather than presented as today's view.
STALE_DAYS = 180
FRESH_DAYS = 90


def _rating_bucket(grade) -> str | None:
    g = " ".join(str(grade or "").lower().split())
    if not g:
        return None
    if g in _BULLISH:
        return "bullish"
    if g in _NEUTRAL:
        return "neutral"
    if g in _BEARISH:
        return "bearish"
    # Unknown wording: fall back to a token test rather than dropping the row, so an
    # unlisted synonym shows up in the mix instead of vanishing.
    if any(w in g for w in ("buy", "outperform", "overweight", "accumulate", "positive")):
        return "bullish"
    if any(w in g for w in ("sell", "underperform", "underweight", "reduce", "negative")):
        return "bearish"
    return "neutral"


def _action_bucket(action, prior, target) -> str | None:
    a = " ".join(str(action or "").lower().split())
    if "rais" in a:
        return "raised"
    if "lower" in a:
        return "lowered"
    if "maintain" in a or "reit" in a or a in ("main", "init"):
        return "maintained"
    # No action string: infer from the numbers rather than claiming "maintained".
    if prior is not None and target is not None:
        if target > prior:
            return "raised"
        if target < prior:
            return "lowered"
        return "maintained"
    return None


@ttl_cache(ttl_seconds=21600)  # 6 hours, same window as the aggregate consensus
def get_institutional_targets(symbol: str) -> dict | None:
    """Per-house targets from the revision feed, filtered to vetted institutions.

    One row per FIRM (its most recent publication — the feed is a history, not a
    snapshot), each resolved through the institution registry. Returns None only when the
    provider carries no firm-level history at all, so the caller can say the filter could
    not be applied instead of implying a vetted consensus exists.
    """
    sym = (symbol or "").upper().strip()
    if not sym:
        return None
    try:
        import yfinance as yf
        ud = yf.Ticker(sym).upgrades_downgrades
    except Exception as exc:
        log.warning("institutional targets failed for %s: %s", sym, exc)
        return None
    if ud is None or not len(ud):
        return None
    try:
        df = ud.reset_index()
    except Exception as exc:
        log.warning("institutional targets unusable for %s: %s", sym, exc)
        return None

    if "Firm" not in df.columns:
        return None
    date_col = "GradeDate" if "GradeDate" in df.columns else df.columns[0]
    df = df.dropna(subset=["Firm"]).copy()
    df["_date"] = pd.to_datetime(df[date_col], errors="coerce", utc=True)
    df = df.dropna(subset=["_date"]).sort_values("_date", ascending=False)
    latest = df.drop_duplicates(subset=["Firm"], keep="first")
    now = pd.Timestamp.now(tz="UTC")

    rows = []
    for _, r in latest.iterrows():
        inst = resolve(str(r.get("Firm")))
        # A price target must be a POSITIVE price. The feed writes 0.0 (and occasionally
        # 0.01) for "no target published"; averaging that in as an estimate is how a
        # consensus silently becomes nonsense.
        target = _positive(_num({"v": r.get("currentPriceTarget")}, "v"))
        prior = _positive(_num({"v": r.get("priorPriceTarget")}, "v"))
        days = int((now - r["_date"]).days)
        rows.append({
            "firm": str(r.get("Firm")),
            "institution": inst.canonical,
            "tier": inst.tier,
            "tier_label": tier_label(inst.tier),
            "vetted": inst.vetted,
            "listed": inst.known,
            "grade": None if pd.isna(r.get("ToGrade")) else str(r.get("ToGrade")),
            "rating": _rating_bucket(r.get("ToGrade")),
            "action": _action_bucket(r.get("priceTargetAction"), prior, target),
            "target": _round(target),
            "prior_target": _round(prior),
            "date": r["_date"].date().isoformat(),
            "age_days": days,
            "stale": days > STALE_DAYS,
        })
    rows.sort(key=lambda x: (not x["vetted"], x["tier"], x["institution"]))

    def _stats(subset: list[dict]) -> dict | None:
        vals = sorted(x["target"] for x in subset if x["target"] is not None)
        if not vals:
            return None
        mean = sum(vals) / len(vals)
        return {
            "count": len(subset),
            "with_target": len(vals),
            "mean": round(mean, 2),
            "median": round(statistics.median(vals), 2),
            "high": round(vals[-1], 2),
            "low": round(vals[0], 2),
            "dispersion_pct": round((vals[-1] - vals[0]) / mean * 100, 1) if mean else None,
        }

    # The vetted consensus is recency-bounded: a house that has not published inside the
    # window is not part of today's view. Those houses are NAMED in ``vetted_stale_firms``
    # rather than quietly dropped, because "who stopped publishing" is itself information.
    vetted_current = [x for x in rows if x["vetted"] and x["age_days"] <= STALE_DAYS]
    vetted_stale = [x for x in rows if x["vetted"] and x["age_days"] > STALE_DAYS]
    other = [x for x in rows if not x["vetted"]]
    vetted = vetted_current
    fresh = [x for x in vetted if x["age_days"] <= FRESH_DAYS]
    rated = [x for x in vetted if x["rating"]]
    mix = {b: sum(1 for x in rated if x["rating"] == b) for b in ("bullish", "neutral", "bearish")}
    share = (mix["bullish"] / len(rated)) if rated else None
    consensus = None
    if share is not None:
        consensus = "overweight" if share >= 0.7 else "underweight" if share <= 0.3 else "neutral"
    revisions = {b: sum(1 for x in vetted if x["action"] == b and x["age_days"] <= 30)
                 for b in ("raised", "lowered", "maintained")}

    return {
        "vetted": _stats(vetted),
        "vetted_count": len(vetted),
        "vetted_fresh": _stats(fresh),
        "fresh_count": len(fresh),
        "rating_mix": mix,
        "rated_count": len(rated),
        "consensus_rating": consensus,
        "revisions_30d": revisions,
        "stale_count": len(vetted_stale),
        "vetted_stale_firms": sorted(
            f"{x['institution']} ({x['age_days']}d)" for x in vetted_stale
        ),
        "vetted_coverage": len(vetted_current) + len(vetted_stale),
        "window_days": STALE_DAYS,
        "oldest_days": max((x["age_days"] for x in vetted), default=None),
        "newest_days": min((x["age_days"] for x in vetted), default=None),
        "total_firms": len(rows),
        "excluded_count": len(other),
        "excluded_firms": sorted({x["institution"] for x in other}),
        "tier_counts": {str(tier): sum(1 for x in rows if x["tier"] == tier) for tier in (1, 2, 3)},
        "criteria": criteria(),
        "vetted_tiers": list(VETTED_TIERS),
        "rows": rows,
    }


@ttl_cache(ttl_seconds=21600)  # 6 hours
def get_analyst_targets(symbol: str) -> dict:
    """Return the sell-side price-target consensus for *symbol*.

    Always returns a dict. ``available`` is False with a ``reason`` when the symbol
    has no sell-side coverage, and the numbers are only ever the ones the provider
    returned — nothing is inferred or defaulted.
    """
    sym = (symbol or "").upper().strip()
    if not sym:
        return {"symbol": symbol, "available": False, "reason": "no symbol given"}

    from data.crypto_assets import is_crypto

    if is_crypto(sym):
        return {
            "symbol": sym,
            "available": False,
            "reason": "Analyst price targets are an equity concept; a crypto asset "
                      "has no sell-side coverage.",
        }

    try:
        import yfinance as yf
    except Exception as exc:  # pragma: no cover - import guard
        log.warning("yfinance unavailable: %s", exc)
        return {"symbol": sym, "available": False,
                "reason": "The analyst-target provider is not installed."}

    targets = None
    info: dict = {}
    targets_error = None
    try:
        ticker = yf.Ticker(sym)
        try:
            targets = ticker.analyst_price_targets
        except Exception as exc:
            # A raised provider error means "ask again later"; an empty payload means
            # "nobody covers this". They must not collapse into one message.
            targets_error = exc.__class__.__name__
            log.warning("analyst_price_targets failed for %s: %s", sym, exc)
        try:
            info = ticker.info or {}
        except Exception as exc:
            # The analyst count and the recommendation live only in info; losing it
            # costs provenance, not the targets, so degrade instead of failing.
            log.warning("yfinance info failed for %s: %s", sym, exc)
            info = {}
    except Exception as exc:
        log.warning("Analyst target fetch failed for %s: %s", sym, exc)
        return {"symbol": sym, "available": False,
                "reason": f"Target fetch failed ({exc.__class__.__name__})."}

    mean = _num(targets, "mean")
    high = _num(targets, "high")
    low = _num(targets, "low")
    median = _num(targets, "median")
    current = _num(targets, "current")
    if mean is None:
        # Fall back to the same field out of info (older yfinance shapes put it there)
        mean = _num(info, "targetMeanPrice")
    if high is None:
        high = _num(info, "targetHighPrice")
    if low is None:
        low = _num(info, "targetLowPrice")
    if current is None:
        current = _num(info, "currentPrice")

    if mean is None or mean <= 0:
        if targets_error:
            return {
                "symbol": sym,
                "available": False,
                "reason": f"Target fetch failed ({targets_error}) with no value to fall back on.",
            }
        return {
            "symbol": sym,
            "available": False,
            "reason": "No sell-side price target returned for this symbol.",
        }

    analysts = _num(info, "numberOfAnalystOpinions")
    recommendation = info.get("recommendationKey")
    if recommendation is not None and not isinstance(recommendation, str):
        recommendation = None

    # A range without both ends is not a range; drop the half rather than plot a
    # line from a guess, and say so in the payload.
    partial_range = (high is None) != (low is None)
    if partial_range:
        high = low = None

    upside_pct = ((mean - current) / current * 100.0) if (current and current > 0) else None
    spread_pct = ((high - low) / mean * 100.0) if (high is not None and low is not None) else None

    count = None if analysts is None else int(analysts)
    basis = BASIS if count is None else f"{BASIS} ({count} analysts)"

    notes = []
    if count is None:
        notes.append("The contributing analyst count was not reported.")
    if high is None or low is None:
        notes.append("No high/low range was reported, so only the mean level is available.")
    if (mean - current) / current > 1.0 if (current and current > 0) else False:
        notes.append("The mean sits more than 100% above the last price — verify coverage.")

    out = {
        "symbol": sym,
        "available": True,
        "mean": _round(mean),
        "median": _round(median),
        "high": _round(high),
        "low": _round(low),
        "current": _round(current),
        "upside_pct": _round(upside_pct),
        "spread_pct": _round(spread_pct),
        "analysts": count,
        "recommendation": recommendation,
        "as_of": datetime.now(timezone.utc).date().isoformat(),
        "source": SOURCE,
        "basis": basis,
        "caveat": CAVEAT,
        "notes": notes,
    }

    # The vetted consensus is the professional read; the aggregate stays available as the
    # unfiltered comparison. If the provider has no firm-level history the block is absent
    # and a note says the filter could not be applied — never a silent fallback to
    # "everyone", which would misrepresent the sample.
    institutional = get_institutional_targets(sym)
    if institutional and institutional.get("vetted"):
        out["institutional"] = institutional
        out["vetted_mean"] = institutional["vetted"]["mean"]
        if institutional["excluded_count"]:
            out["notes"].append(
                f"{institutional['excluded_count']} of {institutional['total_firms']} firms "
                "publishing on this name are outside the vetted list; they are named in the "
                "institutional block."
            )
    else:
        out["institutional_unavailable"] = (
            "No firm-level revision history was returned, so the institutional filter "
            "could not be applied — the figures above are the unfiltered provider average."
        )
    return out
