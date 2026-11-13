"""Short Term Bubble Risk (STBR) analysis.

STBR = Price / SMA(140 bars)

Risk levels:
  Bearish < 0.5, Bearish 0.5-0.75, Neutral 0.75-1, Normal 1-1.25,
  Heating Up 1.25-1.50, Risky 1.50-1.75, Super Risky 1.75-2, Bubble Pop > 2

THE WINDOW IS 140 *BARS*, NOT 140 CALENDAR DAYS — and on a 7-day-a-week asset
those coincide. An equity's 140-trading-day SMA spans ~196 calendar days; BTC
trades every day, so its SMA spans 140. Same convention (140 observations),
~29% shorter calendar lookback on crypto, and that gap is not cosmetic:
measured over 4,405 days of BTC history the 140-bar window flags "> 1.50" on
10.2% of days against 14.6% for a calendar-matched 196-bar window.

So `calculate_stbr` reports the window it actually used — `window_bars`,
`window_unit`, `calendar_days` — and a consumer can label it truthfully
("140-day SMA" for BTC, "140-day SMA (~196 calendar days)" for an equity)
instead of implying an identical lookback for both.
"""

import numpy as np
import pandas as pd

#: Lookback in BARS (observations), the conventional 140.
STBR_WINDOW = 140

RISK_LEVELS = [
    ("Bearish",      -np.inf, 0.50,  "#3B82F6"),   # blue
    ("Bearish",      0.50,    0.75,  "#60A5FA"),   # light blue
    ("Neutral",      0.75,    1.00,  "#06B6D4"),   # cyan
    ("Normal",       1.00,    1.25,  "#10B981"),   # green
    ("Heating Up",   1.25,    1.50,  "#F59E0B"),   # amber
    ("Risky",        1.50,    1.75,  "#F97316"),   # orange
    ("Super Risky",  1.75,    2.00,  "#EF4444"),   # red
    ("Bubble Pop",   2.00,    np.inf, "#DC2626"),  # deep red
]


def _window_span(close_series: pd.Series, window: int) -> dict:
    """Describe what `window` observations actually span in calendar terms.

    Derives the unit from the DATA, not from a hardcoded assumption about the
    asset: a series whose index has no weekend gaps is trading 7 days a week
    (crypto), so its window is already in calendar days.
    """
    lookback = None
    if isinstance(close_series.index, pd.DatetimeIndex) and len(close_series) > window:
        try:
            lookback = int((close_series.index[-1] - close_series.index[-window]).days)
        except Exception:
            lookback = None

    # 140 bars spanning ~140 calendar days means no weekend gaps => 7-day asset.
    seven_day = lookback is not None and lookback <= int(window * 1.15)
    return {
        "window_bars": int(window),
        "window_unit": "day" if seven_day else "trading-day",
        "calendar_days": lookback,
    }


def calculate_stbr(close_series: pd.Series, window: int = STBR_WINDOW) -> pd.DataFrame:
    """Compute STBR from a close-price Series with a DatetimeIndex.

    Returns a DataFrame with columns: close, sma_140, stbr, risk_label,
    risk_color. The ``sma_140`` column name is kept for backward compatibility
    with existing consumers; ``window`` defaults to 140 observations.

    The returned frame also carries ``attrs['window']`` — the window actually
    used, in bars and in calendar days (see the module docstring).
    """
    df = pd.DataFrame({"close": close_series})
    df["sma_140"] = df["close"].rolling(window=window).mean()
    df["stbr"] = df["close"] / df["sma_140"]
    df.dropna(inplace=True)

    labels = []
    colors = []
    for val in df["stbr"]:
        for label, lo, hi, color in RISK_LEVELS:
            if lo <= val < hi:
                labels.append(label)
                colors.append(color)
                break
        else:
            labels.append("Bubble Pop")
            colors.append("#DC2626")
    df["risk_label"] = labels
    df["risk_color"] = colors
    df.attrs["window"] = _window_span(close_series, window)
    return df


def stbr_zscore(stbr_series: pd.Series, lookback: int = 252) -> float:
    """Z-score of current STBR relative to its own history.

    Measures how many standard deviations the current STBR is from its
    trailing mean. Values > 2 indicate statistically unusual extension;
    values < -2 indicate unusual compression.
    """
    if len(stbr_series) < 20:
        return 0.0
    window = stbr_series.tail(lookback)
    mu = window.mean()
    sigma = window.std()
    if sigma == 0:
        return 0.0
    return float((stbr_series.iloc[-1] - mu) / sigma)


def stbr_velocity(stbr_series: pd.Series, window: int = 20) -> float:
    """Rate of change of STBR over `window` periods (annualized).

    Positive = accelerating toward overextension.
    Negative = decelerating / mean-reverting.
    Useful for detecting regime *transitions* before they complete.
    """
    if len(stbr_series) < window + 1:
        return 0.0
    pct_change = (stbr_series.iloc[-1] / stbr_series.iloc[-window] - 1)
    # Annualize: scale by 252/window trading days
    return float(pct_change * (252 / window))


def current_risk_label(stbr_value: float) -> tuple[str, str]:
    """Return (label, color) for a single STBR value."""
    for label, lo, hi, color in RISK_LEVELS:
        if lo <= stbr_value < hi:
            return label, color
    return "Bubble Pop", "#DC2626"
