"""Leaderboard — cross-sectional ranking, period comparison, capital flow.

Pure computation. No fetching, no stores, no side effects. Accepts a panel
slice (sessions x symbols close + volume frames) and returns ranked rows.

## Period definitions are SESSIONS, not calendar days

A "3M" return is the return over 63 TRADING SESSIONS, not 90 calendar days.
Calendar arithmetic over a session index silently produces a different window
every time (holidays, weekends, half-days), so the same label would mean a
different thing on two dates and no two rows would be comparable. Every period
therefore carries its own ``basis`` and the ``base_session`` it measured from,
and the surface prints both. The session count IS the definition.

``YTD`` is the one exception: it is measured from the last session of the prior
calendar year, because that is what the label means to anyone reading it.

## Never fabricate a return

A period return exists only when the symbol has a usable close at BOTH the base
session and the latest session. Anything else is NaN and the row is dropped from
ranking for that period — never 0.0 (which would rank a newly-listed name as
flat and a suspended name as flat, both wrong), and never carried forward from
an earlier session (which would invent a price that never traded).

## The composite is a RANK COMPOSITE, not a model

``leaderboard_score`` blends four percentile ranks with published weights. Every
component is itself a percentile of an observable, so the score can always be
decomposed back to the formula and the row's own inputs — see ``reasons``. It is
deliberately NOT a forecast and NOT the app's STBR (which is a per-symbol
time-series statistic computed elsewhere); ``extension_z`` here is this module's
own panel-vectorized overstretch measure and is named differently on purpose.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Sessions per period. The count is the definition; the label is a convenience.
PERIODS: dict[str, dict] = {
    "1D": {"sessions": 1, "label": "1 Day", "basis": "1 trading session"},
    "1W": {"sessions": 5, "label": "1 Week", "basis": "5 trading sessions"},
    "1M": {"sessions": 21, "label": "1 Month", "basis": "21 trading sessions"},
    "3M": {"sessions": 63, "label": "3 Months", "basis": "63 trading sessions"},
    "6M": {"sessions": 126, "label": "6 Months", "basis": "126 trading sessions"},
    "1Y": {"sessions": 252, "label": "1 Year", "basis": "252 trading sessions"},
    "2Y": {"sessions": 504, "label": "2 Years", "basis": "504 trading sessions"},
    "3Y": {"sessions": 756, "label": "3 Years", "basis": "756 trading sessions"},
    "5Y": {"sessions": 1260, "label": "5 Years", "basis": "1260 trading sessions"},
    "YTD": {"sessions": None, "label": "Year to Date", "basis": "prior calendar year end"},
}

DEFAULT_PERIOD = "YTD"

# Trailing window for the "normal" flow baseline and the overstretch measure.
FLOW_BASELINE_SESSIONS = 252
EXTENSION_MA_SESSIONS = 126

# A symbol needs this many sessions of history before it is rankable at all.
MIN_SESSIONS_FOR_RANK = 60

# Composite weights. Must sum to 1.0; published on the surface.
SCORE_WEIGHTS = {
    "momentum": 0.40,
    "relative_strength": 0.20,
    "consistency": 0.20,
    "flow": 0.20,
}

# Signals. These are the actionable buckets: each one names the tension between
# where PRICE is and where DOLLARS are, which is the whole point of a flow read.
SIGNALS = {
    "LEADING": "Top-decile price with dollars still arriving — leaders under accumulation.",
    "CROWDED": "Top-decile price with dollars leaving — strength being distributed into.",
    "EMERGING": "Unremarkable price with dollars arriving — flow leading price.",
    "FADING": "Weak price with dollars leaving — no bid.",
    "NEUTRAL": "Price and flow are not saying anything unusual.",
}


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    """Replace non-finite values with NaN so every downstream comparison is sane."""
    if df is None or df.empty:
        return pd.DataFrame()
    return df.replace([np.inf, -np.inf], np.nan)


# ── Anchors ──────────────────────────────────────────────────────────


def session_anchors(sessions: pd.DatetimeIndex) -> dict[str, dict]:
    """Resolve each period label to the session it measures FROM.

    Returns ``{period: {base_session, sessions_used, basis, available}}``. A
    period whose window reaches past the panel's own history reports
    ``available: False`` rather than clamping silently to a shorter window —
    a 5Y return computed over 14 months is not a 5Y return.
    """
    idx = pd.DatetimeIndex(sorted(pd.DatetimeIndex(sessions).unique()))
    out: dict[str, dict] = {}
    if len(idx) == 0:
        return out
    last = idx[-1]
    prior_year_end = idx[idx < pd.Timestamp(year=last.year, month=1, day=1)]

    for name, spec in PERIODS.items():
        want = spec["sessions"]
        if want is None:
            if len(prior_year_end) == 0:
                out[name] = {
                    "base_session": None, "sessions_used": None,
                    "basis": spec["basis"], "available": False,
                    "note": f"No session before {last.year}-01-01 in the panel.",
                }
                continue
            base = prior_year_end[-1]
            used = int(len(idx[idx > base]))
            out[name] = {
                "base_session": base.date().isoformat(), "sessions_used": used,
                "basis": spec["basis"], "available": used > 0,
                "note": None,
            }
            continue

        if want >= len(idx):
            out[name] = {
                "base_session": None, "sessions_used": None,
                "basis": spec["basis"], "available": False,
                "note": (
                    f"Needs {want} sessions; the panel holds {len(idx)}. "
                    "Not clamped — a shorter window is a different period."
                ),
            }
            continue
        base = idx[-(want + 1)]
        out[name] = {
            "base_session": base.date().isoformat(), "sessions_used": want,
            "basis": spec["basis"], "available": True, "note": None,
        }
    return out


# ── Returns ─────────────────────────────────────────────────────────


def period_returns(
    close: pd.DataFrame, sessions: pd.DatetimeIndex | None = None
) -> pd.DataFrame:
    """Total return per symbol for every available period.

    Rows are period labels; columns are the UNION of symbols seen in any period,
    because a DataFrame built from per-period Series aligns on the union. A
    symbol without a usable close at both ends of a period therefore reads NaN
    in that cell — never 0.0, which would rank a newly-listed name as flat and a
    suspended name as flat, both wrong. Consumers filter with ``.notna()``.

    ``sessions`` is the FULL trading calendar. Pass it whenever ``close`` holds
    only a subset of sessions (which is the normal case — the caller reads a
    handful of anchor dates, not the whole panel). Anchors resolved against a
    sparse index measure "21 rows back" instead of "21 sessions back" and
    silently produce windows of the wrong length, or none at all.
    """
    close = _clean(close)
    if close.empty:
        return pd.DataFrame()
    anchors = session_anchors(close.index if sessions is None else sessions)
    last = close.index[-1]
    latest = close.loc[last]

    rows: dict[str, pd.Series] = {}
    for name, a in anchors.items():
        if not a["available"]:
            continue
        base = pd.Timestamp(a["base_session"])
        if base not in close.index:
            continue
        b = close.loc[base]
        both = b.notna() & latest.notna() & (b > 0)
        r = (latest[both] / b[both] - 1.0) * 100.0
        rows[name] = r.astype("float64")
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).T


def calendar_year_returns(
    close: pd.DataFrame,
    years: list[int] | None = None,
    sessions: pd.DatetimeIndex | None = None,
) -> pd.DataFrame:
    """Total return per symbol for each CALENDAR year.

    The prior year's final close is the base, so the first year in the panel
    reports no return (it has no in-panel base) instead of a partial one that
    would read as a bad year.

    ``sessions`` is the full calendar, for the same reason as in
    :func:`period_returns` — the year's TRUE last session must come from the
    calendar, not from whichever anchor dates the caller happened to read.
    """
    close = _clean(close)
    if close.empty:
        return pd.DataFrame()
    idx = close.index if sessions is None else pd.DatetimeIndex(sorted(sessions))
    if years is None:
        years = sorted({d.year for d in idx})
        years = [y for y in years if y > idx[0].year]

    out: dict[int, pd.Series] = {}
    for y in years:
        in_year = idx[idx.year == y]
        if len(in_year) == 0:
            continue
        prior = idx[idx < pd.Timestamp(year=y, month=1, day=1)]
        if len(prior) == 0:
            continue
        # The base must actually BE in the frame the caller supplied. Reading a
        # date that is not there raises KeyError, which in a request path is a
        # 500 — degrade to "this year is not measurable here" instead.
        base_date = prior[-1]
        if base_date not in close.index:
            continue
        base = close.loc[base_date]
        end = close.loc[in_year[-1]]
        both = base.notna() & end.notna() & (base > 0)
        out[y] = ((end[both] / base[both] - 1.0) * 100.0).astype("float64")
    if not out:
        return pd.DataFrame()
    df = pd.DataFrame(out)
    df.columns = [str(c) for c in df.columns]
    return df.T


def monthly_returns(close: pd.DataFrame, months: int = 12) -> pd.Series:
    """Share of the last *months* monthly windows that were positive, per symbol.

    A count, not a model: it answers "how often did this name go up", which is
    the consistency half of the composite. Symbols with fewer than half the
    windows recorded are dropped rather than scored on a handful of months.

    Month-ends come from a group-by on the frame's own ROW index rather than
    ``resample``: the resample month alias was renamed between pandas 2.1 ("M")
    and 2.2 ("ME"), and this app must run on both. Melting the frame into long
    form to do the same thing costs ~2.4s at 380 x 15k cells; grouping the rows
    is a 380-row operation.
    """
    close = _clean(close)
    if close.empty:
        return pd.Series(dtype="float64")
    me = close.groupby(close.index.to_period("M")).last()
    if len(me) < 2:
        return pd.Series(dtype="float64")
    # fill_method=None is a CORRECTNESS setting, not a lint fix: the default
    # "pad" carries a month with no print forward, turning "we have no close for
    # this name that month" into a fabricated flat month that then counts as
    # "not positive" in the consistency ratio.
    rets = me.pct_change(fill_method=None).iloc[-months:]
    valid = rets.notna().sum()
    share = (rets > 0).sum() / valid.replace(0, np.nan)
    share[valid < months * 0.5] = np.nan
    return share.astype("float64")


# ── Flow ────────────────────────────────────────────────────────────


def flow_profile(volume: pd.DataFrame, close: pd.DataFrame,
                 recent: int = 21, baseline: int = FLOW_BASELINE_SESSIONS) -> pd.DataFrame:
    """Dollar-volume flow per symbol: recent activity vs its own baseline.

    ``flow_ratio`` = mean daily dollar volume over the last *recent* sessions
    divided by the mean over the trailing *baseline*. >1 means dollars are
    arriving faster than this name's normal; <1 means they are leaving.

    Ratios are computed against the symbol's OWN history, never against other
    symbols, so a small cap that is finally being noticed is comparable to a
    megacap. Symbols without a full baseline window are dropped rather than
    scored against a stub.
    """
    volume = _clean(volume)
    close = _clean(close)
    if volume.empty or close.empty:
        return pd.DataFrame()
    dv = (close * volume).astype("float64")
    if len(dv) < baseline + 1:
        return pd.DataFrame()

    recent_avg = dv.iloc[-recent:].mean()
    baseline_avg = dv.iloc[-baseline:].mean()
    ratio = recent_avg / baseline_avg.replace(0, np.nan)
    out = pd.DataFrame({
        "dollar_volume_avg": recent_avg,
        "dollar_volume_baseline": baseline_avg,
        "flow_ratio": ratio,
    })
    out["flow_ratio"] = out["flow_ratio"].replace([np.inf, -np.inf], np.nan)
    return out


# ── Stretch and drawdown ─────────────────────────────────────────────


def extension_z(close: pd.DataFrame, window: int = EXTENSION_MA_SESSIONS) -> pd.Series:
    """How far above its own trend a name is trading, in standard deviations.

    This module's own overstretch measure (NOT the app's STBR, which is a
    separate per-symbol statistic). Take ``close / MA(window) - 1``, then express
    that ratio's latest value as a z-score against its own trailing distribution.
    """
    close = _clean(close)
    if close.empty or len(close) < window + 20:
        return pd.Series(dtype="float64")
    ratio = close / close.rolling(window, min_periods=window).mean() - 1.0
    mean = ratio.mean()
    std = ratio.std().replace(0, np.nan)
    return ((ratio.iloc[-1] - mean) / std).astype("float64")


def drawdown_from_high(close: pd.DataFrame, window: int = FLOW_BASELINE_SESSIONS) -> pd.Series:
    """Current close versus the highest close in the trailing *window*, in percent."""
    close = _clean(close)
    if close.empty:
        return pd.Series(dtype="float64")
    w = close.iloc[-window:]
    hi = w.max()
    last = close.iloc[-1]
    return (((last / hi.replace(0, np.nan)) - 1.0) * 100.0).astype("float64")


# ── Ranking + composite ─────────────────────────────────────────────


def _pct_rank(s: pd.Series) -> pd.Series:
    """Percentile in [0, 1]. NaN stays NaN — an unrankable row is not a zero."""
    if s.empty or s.notna().sum() == 0:
        return pd.Series(np.nan, index=s.index, dtype="float64")
    return s.rank(pct=True, na_option="keep").astype("float64")


def score_board(rows: pd.DataFrame, period: str) -> pd.DataFrame:
    """Blend the composite and label each row.

    ``rows`` must carry ``ret_period``, ``rs_period``, ``consistency`` and
    ``flow_ratio``. Any missing input degrades that component to NaN and the
    score is renormalised over the components that ARE present, with the
    shortfall named in ``reasons`` — never defaulted to a neutral value that
    would look like agreement.
    """
    if rows.empty:
        return rows
    out = rows.copy()

    inputs = {
        "momentum": _pct_rank(out.get("ret_period", pd.Series(dtype="float64"))),
        "relative_strength": _pct_rank(out.get("rs_period", pd.Series(dtype="float64"))),
        "consistency": _pct_rank(out.get("consistency", pd.Series(dtype="float64"))),
        "flow": _pct_rank(out.get("flow_ratio", pd.Series(dtype="float64"))),
    }

    weighted = pd.Series(0.0, index=out.index, dtype="float64")
    weight_present = pd.Series(0.0, index=out.index, dtype="float64")
    missing: list[pd.Series] = []
    for key, weight in SCORE_WEIGHTS.items():
        col = inputs[key]
        have = col.notna()
        weighted = weighted.add(col.fillna(0.0) * weight, fill_value=0.0)
        weight_present = weight_present.add(have.astype("float64") * weight, fill_value=0.0)
        missing.append(~have)

    score = (weighted / weight_present.replace(0, np.nan)) * 100.0

    # A falling knife is not an opportunity: a name in a deep drawdown that is
    # ALSO down over the period cannot score as a leader, however cheap it looks.
    dd = out.get("drawdown_pct", pd.Series(np.nan, index=out.index))
    ret = out.get("ret_period", pd.Series(np.nan, index=out.index))
    knife = (dd < -30.0) & (ret < 0)
    score = score.where(~knife, score.clip(upper=50.0))

    out["score"] = score.astype("float64")
    pct = _pct_rank(out.get("ret_period", pd.Series(dtype="float64")))

    flow = out.get("flow_ratio", pd.Series(np.nan, index=out.index))
    top_price = pct >= 0.80
    low_price = pct <= 0.50
    weak_price = pct <= 0.30
    dollars_in = flow >= 1.0
    dollars_out = flow < 0.80
    dollars_surging = flow >= 1.30

    signal = pd.Series("NEUTRAL", index=out.index, dtype="object")
    signal = signal.mask(top_price & dollars_in, "LEADING")
    signal = signal.mask(top_price & dollars_out, "CROWDED")
    signal = signal.mask(low_price & dollars_surging, "EMERGING")
    signal = signal.mask(weak_price & dollars_out, "FADING")
    out["signal"] = signal
    out["signal_note"] = signal.map(SIGNALS)

    out["reasons"] = [
        _reasons(
            row,
            missing_kinds=[k for k, m in zip(SCORE_WEIGHTS, missing, strict=True) if bool(m.loc[idx])],
        )
        for idx, row in out.iterrows()
    ]
    return out


def _fmt_pct(v) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "n/a"
    return f"{v:+.1f}%"


def _reasons(row: pd.Series, missing_kinds: list[str]) -> list[str]:
    """Short operator-facing strings. Every claim is one of the row's own inputs."""
    out: list[str] = []
    ret = row.get("ret_period")
    if ret is not None and not pd.isna(ret):
        out.append(f"{_fmt_pct(ret)} over the period")
    rs = row.get("rs_period")
    if rs is not None and not pd.isna(rs):
        out.append(
            f"{'ahead of' if rs >= 0 else 'behind'} the benchmark by {abs(rs):.1f}pp"
        )
    fr = row.get("flow_ratio")
    if fr is not None and not pd.isna(fr):
        out.append(f"dollar volume at {fr:.2f}x its own baseline")
    cons = row.get("consistency")
    if cons is not None and not pd.isna(cons):
        out.append(f"{cons * 100:.0f}% of the last year's months positive")
    ext = row.get("extension_z")
    if ext is not None and not pd.isna(ext) and abs(ext) >= 2:
        out.append(f"{'stretched' if ext > 0 else 'compressed'} {abs(ext):.1f} sigma vs trend")
    dd = row.get("drawdown_pct")
    if dd is not None and not pd.isna(dd) and dd <= -20:
        out.append(f"{dd:.0f}% below its 52-week high")
    if missing_kinds:
        out.append(f"score renormalised — no {', '.join(missing_kinds)} input")
    return out


# ── Aggregation ─────────────────────────────────────────────────────


def rollup(rows: pd.DataFrame, group_map: dict[str, str], key: str = "sector") -> pd.DataFrame:
    """Aggregate per-symbol rows to a group level (sector by default).

    Median return, median flow ratio and the count of names per group. The
    MEDIAN is deliberate: one 400% mover must not define a sector's year.
    """
    if rows.empty:
        return pd.DataFrame()
    df = rows.copy()
    df[key] = [group_map.get(str(s).upper(), "Unclassified") for s in df.index]
    grp = df.groupby(key, dropna=False)
    out = pd.DataFrame({
        "n": grp.size(),
        "median_return": grp["ret_period"].median(),
        "median_flow_ratio": grp["flow_ratio"].median() if "flow_ratio" in df else np.nan,
        "advancers_pct": grp["ret_period"].apply(
            lambda s: float((s > 0).sum()) / float(s.notna().sum()) * 100.0
            if s.notna().sum() else np.nan
        ),
        "median_market_value": grp["market_value"].median() if "market_value" in df else np.nan,
    })
    return out.sort_values("median_return", ascending=False)


def breadth(rows: pd.DataFrame) -> dict:
    """Advance/decline and dispersion for the ranked set. Explicit about n."""
    if rows.empty or "ret_period" not in rows:
        return {"available": False, "note": "No rankable rows for this period."}
    s = rows["ret_period"].dropna()
    if s.empty:
        return {"available": False, "note": "No rankable rows for this period."}
    up = int((s > 0).sum())
    return {
        "available": True,
        "n": int(len(s)),
        "advancing": up,
        "declining": int(len(s) - up),
        "advancing_pct": round(up / len(s) * 100.0, 1),
        "median_return": round(float(s.median()), 2),
        "mean_return": round(float(s.mean()), 2),
        "dispersion_std": round(float(s.std()), 2),
        "best": round(float(s.max()), 2),
        "worst": round(float(s.min()), 2),
    }
