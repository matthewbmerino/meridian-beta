"""Regime analogues — what does the market's present state resemble, and what
happened next?

Pure computation. No fetching, no stores, no side effects: callers pass price and
macro Series in and get a study back.

## The question this answers

"What is today like, and what did 'like today' pay?" is a nearest-neighbour
question, and it fails in three specific ways that this module is built to
avoid.

1. **Look-ahead in the yardstick.** Every feature is standardised against its own
   EXPANDING mean and standard deviation, so a 1988 observation is judged only
   against what was knowable in 1988. A full-sample z-score would make the 1970s
   look extreme because of the 2020s.

2. **Overlapping analogues counted as independent evidence.** Monthly
   observations from one episode are almost identical, so the raw nearest-20 are
   typically five months from 2007 and five from 2018. Reporting them as twenty
   samples inflates confidence in whatever those two periods did. ``nearest``
   therefore enforces a minimum spacing so each analogue is a DISTINCT episode,
   and the study reports the count it actually used.

3. **No base rate.** A forward-return median means nothing on its own. Every
   study reports the SAME statistics over ALL available months, so the reader can
   see whether the analogue set moves the distribution at all. Frequently it does
   not, and saying so is the honest result.

## Features are levels, differences and ranks — never a price

Each feature is a state variable that is comparable across decades: trend
distance, realised volatility, drawdown, time since the last bear market, real
rates, credit spreads, curve slope, and a valuation rank. The valuation feature
is the one that needs care — see ``valuation_z``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# The state vector, in the order the surface prints. Every feature must be
# available for the whole comparison window, or an analogue is chosen on the
# features it happens to have (which is how a 1974 month with no credit spread
# becomes "closest" to a modern one).
STATE_FEATURES: tuple[str, ...] = (
    "sp_12m",           # index 12-month return
    "relative",         # growth index minus broad index, 12-month
    "dist_200",         # price vs its 200-session mean
    "pct_above_200",    # share of the last quarter spent above the 200-session mean
    "vol21",            # realised vol, 21-session, annualised
    "dd_ath",           # drawdown from the running all-time high
    "months_since_20dd",
    "real10y",          # 10-year yield minus trailing CPI
    "baa",              # BAA credit spread over the 10-year
    "curve",            # 10-year minus 2-year
    "cape_z",           # valuation, standardised (see valuation_z)
)

SESSIONS_PER_MONTH = 21
SESSIONS_PER_YEAR = 252


def _monthly(series: pd.Series) -> pd.Series:
    """Month-end last value. A partial current month keeps its own label."""
    return series.resample("M").last()


def monthly_state(
    broad: pd.Series,
    growth: pd.Series | None = None,
    *,
    dgs10: pd.Series | None = None,
    cpi: pd.Series | None = None,
    baa: pd.Series | None = None,
    curve: pd.Series | None = None,
    cape: pd.Series | None = None,
) -> pd.DataFrame:
    """Build the monthly state frame from daily series.

    ``broad`` is the market (S&P 500); ``growth`` is the leadership index
    (Nasdaq Composite) and is optional — without it there is no ``relative``
    feature and the caller should drop it from ``STATE_FEATURES``. Macro inputs
    are forward-filled onto the month grid: CPI is published monthly with a lag
    and a spread is quoted daily, so the last observation is the one a reader
    would have had.
    """
    f = pd.DataFrame(index=_monthly(broad).index)
    f["sp_12m"] = _monthly(broad).pct_change(12)
    if growth is not None and len(growth):
        g12 = _monthly(growth).pct_change(12)
        f["nq_12m"] = g12
        f["relative"] = (1 + g12) / (1 + f["sp_12m"]) - 1

    sma200 = broad.rolling(200).mean()
    f["dist_200"] = ((broad / sma200) - 1).resample("M").last()
    f["pct_above_200"] = (broad > sma200).rolling(63).mean().resample("M").last()
    f["vol21"] = (
        broad.pct_change().rolling(21).std() * np.sqrt(SESSIONS_PER_YEAR)
    ).resample("M").last()

    dd = broad / broad.cummax() - 1
    f["dd_ath"] = dd.resample("M").last()
    sessions = pd.Series(np.arange(len(broad)), index=broad.index)
    last_bear = pd.Series(
        np.where((dd <= -0.20).to_numpy(), np.arange(len(dd)), np.nan), index=dd.index
    ).ffill()
    f["months_since_20dd"] = ((sessions - last_bear) / SESSIONS_PER_MONTH).resample("M").last()

    f["cpi_yoy"] = (
        cpi.pct_change(12).reindex(f.index, method="ffill") if cpi is not None and len(cpi) else np.nan
    )
    if dgs10 is not None and len(dgs10):
        f["real10y"] = dgs10.resample("M").last().reindex(f.index, method="ffill") - f["cpi_yoy"]
    for name, ser in (("baa", baa), ("curve", curve)):
        if ser is not None and len(ser):
            f[name] = ser.resample("M").last().reindex(f.index, method="ffill")
    if cape is not None and len(cape):
        c = cape.copy()
        # Shiller's series is monthly but stamped on the 1st; align it to the
        # month it describes, not the month it is labelled with.
        c.index = c.index + pd.offsets.MonthEnd(0)
        c = c[~c.index.duplicated(keep="last")]
        f["cape"] = c.reindex(f.index, method="ffill")
        f["cape_z"] = valuation_z(f["cape"])
    return f


def valuation_z(cape: pd.Series, min_obs: int = 120) -> pd.Series:
    """Standardise a valuation level against its OWN history so far.

    A raw percentile of CAPE is not comparable across eras: the ratio has drifted
    structurally upward for a century, so a plain rank just says "recent" and
    would call 1996 and 2021 equally extreme when they are not. Standing versus
    the expanding mean is the question an allocator is actually asking — "is this
    expensive FOR ITS OWN TIME" — and it is the only form that can be compared
    with 1996 without a look-ahead.
    """
    mu = cape.expanding(min_obs).mean()
    sd = cape.expanding(min_obs).std()
    return (cape - mu) / sd


def expanding_z(df: pd.DataFrame, features: tuple[str, ...] | list[str], min_obs: int = 60) -> pd.DataFrame:
    """Per-feature expanding z-score. Returns a frame with the same index."""
    out = pd.DataFrame(index=df.index)
    for c in features:
        if c not in df.columns:
            continue
        mu = df[c].expanding(min_obs).mean()
        sd = df[c].expanding(min_obs).std()
        out[c] = (df[c] - mu) / sd
    return out


def _fwd(series: pd.Series, ts: pd.Timestamp, months: int) -> float:
    i = series.index.searchsorted(ts)
    j = i + int(months * SESSIONS_PER_MONTH)
    if i >= len(series) or j >= len(series):
        return float("nan")
    return float(series.iloc[j] / series.iloc[i] - 1)


def _worst_dd(series: pd.Series, ts: pd.Timestamp, months: int) -> float:
    i = series.index.searchsorted(ts)
    j = min(i + int(months * SESSIONS_PER_MONTH), len(series))
    w = series.iloc[i:j]
    if len(w) < 2:
        return float("nan")
    return float((w / w.cummax() - 1).min())


def _dd20(series: pd.Series, ts: pd.Timestamp, months: int) -> float:
    i = series.index.searchsorted(ts)
    j = min(i + int(months * SESSIONS_PER_MONTH), len(series))
    w = series.iloc[i:j]
    if len(w) < 2:
        return float("nan")
    return float(((w / w.cummax() - 1) <= -0.20).any())


def outcomes(
    series: pd.Series,
    ts: pd.Timestamp,
    horizons: tuple[int, ...] = (3, 6, 12, 24),
    dd_months: int = 24,
) -> dict:
    """Forward return at each horizon plus the path risk over ``dd_months``.

    Path risk travels with the point returns on purpose: a state can carry a
    positive median 12-month return and still have cost most allocators their
    mandate on the way there, which is the only thing a drawdown number is for.
    """
    out: dict = {"as_of": str(pd.Timestamp(ts).date())}
    for h in horizons:
        out[f"f{h}"] = _fwd(series, ts, h)
    out["maxdd"] = _worst_dd(series, ts, dd_months)
    out["dd20"] = _dd20(series, ts, dd_months)
    out["dd_months"] = dd_months
    return out


def nearest(
    z: pd.DataFrame,
    as_of: pd.Timestamp,
    *,
    k: int = 15,
    min_gap_months: int = 18,
    exclude_years: int = 3,
) -> list[dict]:
    """Nearest DISTINCT states to ``as_of`` by RMS distance in sigma units.

    Greedy by distance with a spacing rule: once a month is picked, everything
    within ``min_gap_months`` is skipped. Without this the same episode is
    counted many times and the set is not a sample of anything (the raw top-20
    is routinely 2007 five times and 2018 five times).

    ``exclude_years`` keeps the recent past out: a state six months ago is not a
    historical analogue, it is the present.
    """
    vec = z.loc[as_of]
    dist = np.sqrt(((z - vec) ** 2).mean(axis=1)).dropna()
    dist = dist[dist.index <= pd.Timestamp(as_of) - pd.DateOffset(years=exclude_years)]
    picked: list[dict] = []
    used: list[pd.Timestamp] = []
    for ts, d in dist.sort_values().items():
        if any(abs((ts - u).days) < int(min_gap_months * 30.44) for u in used):
            continue
        used.append(ts)
        picked.append({"as_of": str(ts.date()), "distance": float(d)})
        if len(picked) >= k:
            break
    return picked


def study(
    broad: pd.Series,
    *,
    growth: pd.Series | None = None,
    dgs10: pd.Series | None = None,
    cpi: pd.Series | None = None,
    baa: pd.Series | None = None,
    curve: pd.Series | None = None,
    cape: pd.Series | None = None,
    features: list[str] | None = None,
    k: int = 15,
    horizons: tuple[int, ...] = (3, 6, 12, 24),
    dd_months: int = 24,
    named_dates: dict[str, str] | None = None,
) -> dict:
    """The whole study. Returns a JSON-ready dict, or ``{"available": False}``.

    Requires every feature it uses to be present for the whole comparison
    window, so the panel starts when the LAST input begins — a study that
    silently varies its feature set by date is not a study.
    """
    frame = monthly_state(
        broad, growth, dgs10=dgs10, cpi=cpi, baa=baa, curve=curve, cape=cape
    )
    cols = [c for c in (features or STATE_FEATURES) if c in frame.columns]
    df = frame.dropna(subset=cols)
    if len(df) < 60:
        return {
            "available": False,
            "note": (
                f"Deep history covers only {len(df)} usable months; the study needs 60. "
                "Run scripts/collect_deep_history.py."
            ),
        }
    z = expanding_z(df, cols).dropna()
    df = df.loc[z.index]
    if len(df) < 60:
        return {"available": False, "note": "Not enough history after standardisation."}

    as_of = df.index[-1]
    dist = np.sqrt(((z - z.loc[as_of]) ** 2).mean(axis=1)).dropna()
    picks = nearest(z, as_of, k=k)

    for p in picks:
        p.update({k2: v for k2, v in outcomes(broad, pd.Timestamp(p["as_of"]), horizons, dd_months).items()
                  if k2 != "as_of"})
        p["cape"] = _safe(df, p["as_of"], "cape")

    # The base rate: the SAME statistics over every month in the panel that has
    # a complete forward window. This is the number the analogue set must beat to
    # mean anything.
    base_dates = [t for t in df.index if broad.index.searchsorted(t) + dd_months * SESSIONS_PER_MONTH < len(broad)]
    base = [outcomes(broad, t, horizons, dd_months) for t in base_dates]

    def _vals(rows: list[dict], key: str) -> list[float]:
        # Narrow to floats in one place: a plain comprehension leaves the
        # element type as Any | None, which numpy's medians reject.
        out: list[float] = []
        for row in rows:
            v = row.get(key)
            if v is None or (isinstance(v, float) and np.isnan(v)):
                continue
            out.append(float(v))
        return out

    def med(rows: list[dict], key: str) -> float | None:
        vals = _vals(rows, key)
        return float(np.median(vals)) if vals else None

    def share(rows: list[dict], key: str) -> float | None:
        vals = _vals(rows, key)
        return float(np.mean(vals)) if vals else None

    analog_med = {f"f{h}": med(picks, f"f{h}") for h in horizons}
    base_med = {f"f{h}": med(base, f"f{h}") for h in horizons}

    named = {}
    for label, iso in (named_dates or DEFAULT_NAMED_DATES).items():
        ts = pd.Timestamp(iso)
        if ts not in z.index or ts not in dist.index:
            continue
        named[label] = {
            "as_of": str(ts.date()),
            "distance": float(dist[ts]),
            "state": {c: _safe(df, ts, c) for c in cols},
            **{k2: v for k2, v in outcomes(broad, ts, horizons, dd_months).items() if k2 != "as_of"},
        }

    return {
        "available": True,
        # Two dates, on purpose: the state is measured at the last SESSION, and
        # the monthly panel row carrying it is labelled with that month's end.
        # Printing the month label alone dates today's reading to a day that has
        # not happened.
        "as_of": str(broad.index[-1].date()),
        "state_month": str(as_of.date()),
        "panel_start": str(df.index[0].date()),
        "n_months": int(len(df)),
        "features": list(cols),
        "state": {c: {"value": _safe(df, as_of, c), "z": _safe(z, as_of, c)} for c in cols},
        "analogues": picks,
        "n_analogues": len(picks),
        "analogue_medians": analog_med,
        "analogue_p_dd20": share(picks, "dd20"),
        "base_rate": {
            "n": len(base),
            "medians": base_med,
            "p_dd20": share(base, "dd20"),
        },
        "named_periods": named,
        "horizons": list(horizons),
        "dd_months": dd_months,
        "note": (
            "Analogues are distinct episodes at least 18 months apart, chosen on the state "
            "vector alone. Compare every analogue figure with base_rate: the set only carries "
            "information where the two differ."
        ),
    }


DEFAULT_NAMED_DATES: dict[str, str] = {
    "1999-12 (bubble)": "1999-12-31",
    "2000-03 (peak)": "2000-03-31",
    "2007-10 (peak)": "2007-10-31",
    "2021-11 (peak)": "2021-11-30",
}


def _safe(df: pd.DataFrame, ts, col: str) -> float | None:
    try:
        v = df.at[pd.Timestamp(ts), col]
    except (KeyError, TypeError):
        return None
    return None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)


def breadth_divergence(
    close: pd.DataFrame,
    *,
    index: pd.Series | None = None,
    near_ath: float = 0.02,
    min_symbols: int = 200,
) -> dict:
    """How much of the market is being left behind while the index sits at a high.

    ``close`` is sessions x symbols. The measure is the share of constituents
    more than 20% below their OWN 52-week high, alongside the share below their
    200-session mean. That pair is the internal read of a headline high: an index
    can make a record on a handful of names while most of the market is in a
    drawdown, and the two are invisible in the index alone.

    When ``index`` is given, the current reading is placed against the readings
    from months when the index was within ``near_ath`` of its own record — that
    is the comparison that matters, because breadth is always worse during a
    drawdown and saying "breadth is bad" without conditioning on the index level
    says nothing.
    """
    if close is None or close.empty or close.shape[1] < min_symbols:
        return {
            "available": False,
            "note": f"Need at least {min_symbols} symbols to measure breadth.",
        }
    c = close.apply(pd.to_numeric, errors="coerce")
    # A FULL window or nothing. With `min_periods=120` the first year of a frame
    # has no 52-week high yet, `off <= -0.20` is False for every name, and the
    # month reports 0% breadth — perfect breadth, fabricated, and it then drags
    # the at-record median down and flatters every comparison against it. The
    # partial month is DROPPED, never averaged in.
    hi = c.rolling(SESSIONS_PER_YEAR, min_periods=SESSIONS_PER_YEAR).max()
    sma = c.rolling(200, min_periods=200).mean()
    off = c / hi - 1
    below = c < sma
    w52 = off.notna()
    w200 = sma.notna() & c.notna()
    n52 = w52.sum(axis=1)
    n200 = w200.sum(axis=1)
    # Guard the denominator, don't clamp it: before the first full window there
    # are ZERO valid names and `x / 0` raises rather than reporting a share.
    d52 = n52.replace(0, np.nan)
    d200 = n200.replace(0, np.nan)
    monthly = pd.DataFrame({
        "off10": (off <= -0.10).where(w52).sum(axis=1) / d52,
        "off20": (off <= -0.20).where(w52).sum(axis=1) / d52,
        "off30": (off <= -0.30).where(w52).sum(axis=1) / d52,
        "below200": below.where(w200).sum(axis=1) / d200,
        "n": n52,
        "n_warm": n200,
    })
    monthly = monthly[(monthly["n"] >= min_symbols) & (monthly["n_warm"] >= min_symbols)]
    monthly = monthly.dropna(subset=["off20", "below200"])
    if monthly.empty:
        return {"available": False, "note": "Not enough overlapping history to measure breadth."}
    mm = monthly.resample("M").last().dropna(subset=["off20"])

    at_record = None
    pctile = None
    worst_rank = None
    median_at_record = None
    if index is not None and len(index):
        idx = index.dropna()
        ath = idx / idx.cummax() - 1
        near = (ath > -abs(near_ath))
        nr = near.groupby(near.index.to_period("M")).max()
        mm = mm.copy()
        mm["at_record"] = [bool(nr.get(p, False)) for p in mm.index.to_period("M")]
        at_record = bool(mm["at_record"].iloc[-1])
        when = mm[mm["at_record"]]
        if len(when) >= 6:
            cur = float(mm["off20"].iloc[-1])
            median_at_record = float(when["off20"].median())
            pctile = float((when["off20"] < cur).mean())
            worst_rank = int((when["off20"] > cur).sum()) + 1

    last = mm.iloc[-1]
    return {
        "available": True,
        # The last SESSION the frame actually covers, not the month label the
        # series rows carry — a partial month is labelled with a month-end that
        # has not happened yet, and an as-of that reads as the future is worse
        # than no as-of at all.
        "as_of": str(pd.Timestamp(c.index[-1]).date()),
        "last_month": str(mm.index[-1].date()),
        "n_symbols": int(last["n"]),
        "off_10_pct": float(last["off10"]),
        "off_20_pct": float(last["off20"]),
        "off_30_pct": float(last["off30"]),
        "below_200_pct": float(last["below200"]),
        "at_record": at_record,
        "months_at_record": int(mm["at_record"].sum()) if "at_record" in mm else 0,
        "median_off20_at_record": median_at_record,
        "percentile_vs_at_record": pctile,
        "worst_rank_at_record": worst_rank,
        "series": [
            {
                "date": str(pd.Timestamp(ix).date()),
                "off20": float(r["off20"]),
                "below200": float(r["below200"]),
                "at_record": bool(r.get("at_record", False)),
                "n": int(r["n"]),
            }
            for ix, r in mm.iterrows()
        ],
    }
