"""Technical signals: RSI, moving averages, MACD, Bollinger Bands, volatility."""

import numpy as np
import pandas as pd


def rsi(prices, period=14):
    """Compute RSI from a Series of closing prices using Wilder's smoothing."""
    delta = prices.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    # When avg_loss is 0 (all gains), RSI is 100; avoid NaN
    rs = pd.Series(np.where(avg_loss == 0, np.inf, avg_gain / avg_loss), index=prices.index)
    return 100 - (100 / (1 + rs))


def sma(prices, window):
    """Simple moving average."""
    return prices.rolling(window=window, min_periods=window).mean()


def detect_sma_crossover(prices, short_window=50, long_window=200):
    """Detect SMA crossover signals.

    Returns 'GOLDEN_CROSS', 'DEATH_CROSS', or 'NONE'.
    """
    if len(prices) < long_window + 2:
        return "NONE"
    short_sma = sma(prices, short_window)
    long_sma = sma(prices, long_window)
    prev_diff = short_sma.iloc[-2] - long_sma.iloc[-2]
    curr_diff = short_sma.iloc[-1] - long_sma.iloc[-1]

    if prev_diff <= 0 and curr_diff > 0:
        return "GOLDEN_CROSS"
    elif prev_diff >= 0 and curr_diff < 0:
        return "DEATH_CROSS"
    return "NONE"


def unusual_volume(current_volume, volume_series, lookback=20):
    """Flag unusual volume: current volume vs 20-day average.

    Returns the ratio (e.g., 2.5 means 2.5x average volume).
    """
    avg = volume_series.tail(lookback).mean()
    if avg == 0:
        return 0.0
    return current_volume / avg


def macd(prices, fast=12, slow=26, signal=9):
    """MACD line, signal line, histogram. Returns (macd_line, signal_line, histogram)."""
    ema_fast = prices.ewm(span=fast).mean()
    ema_slow = prices.ewm(span=slow).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger_bands(prices, window=20, num_std=2):
    """Returns (upper, middle, lower) bands."""
    middle = prices.rolling(window).mean()
    std = prices.rolling(window).std()
    return middle + num_std * std, middle, middle - num_std * std


def bollinger_position(prices, window=20, num_std=2):
    """Returns %B: 0 = at lower band, 1 = at upper band, >1 = above upper."""
    upper, middle, lower = bollinger_bands(prices, window, num_std)
    band_width = upper - lower
    pct_b = (prices - lower) / band_width.replace(0, float('nan'))
    return pct_b


def historical_volatility(prices, window=30):
    """Annualized historical volatility over `window` trading days."""
    log_returns = np.log(prices / prices.shift(1))
    return log_returns.rolling(window).std() * np.sqrt(252)


def iv_rank(current_iv, iv_series_252d):
    """IV Rank = (current - 52wk low) / (52wk high - 52wk low) * 100."""
    low = iv_series_252d.min()
    high = iv_series_252d.max()
    if high == low:
        return 50.0
    return float((current_iv - low) / (high - low) * 100)


def iv_percentile(current_iv, iv_series_252d):
    """IV Percentile = % of days in past year where IV was below current."""
    return float((iv_series_252d < current_iv).mean() * 100)


def _score_components(row) -> list[dict]:
    """Compute each component's [-1, +1] contribution to the composite score.

    Returns a list of {name, weight, contrib} dicts. The weighted sum of
    contribs is the raw score in [-100, +100]; the composite score is
    raw/2 + 50. Weights reflect predictive reliability:
      RSI 25%  |  Trend (SMA) 20%  |  MACD 20%  |  Bollinger 15%
      Volume-context 10%  |  Volatility regime 10%
    """
    # --- RSI: continuous [-1, +1], mean-reversion polarity ---
    # Assumes oversold (RSI<30) is bullish and overbought (RSI>70) is bearish.
    # This is a mean-reversion signal, NOT momentum. Appropriate for established
    # holdings where extreme readings predict reversal, not continuation.
    rsi_val = row.get("rsi")
    if rsi_val is not None:
        rsi_centered = (50 - rsi_val) / 20  # 30→+1, 50→0, 70→-1
        rsi_contrib = max(-1.0, min(1.0, rsi_centered))
    else:
        rsi_contrib = 0.0

    # --- Trend: SMA50 distance, continuous [-1, +1] ---
    # Clamped at ±15% distance for full signal
    sma_dist = row.get("sma_50_dist_pct")
    crossover = row.get("sma_crossover", "NONE")
    if sma_dist is not None:
        trend_contrib = max(-1.0, min(1.0, sma_dist / 15.0))
    else:
        trend_contrib = 0.0
    # Crossover bonus: recent golden/death cross adds ±0.3
    if crossover == "GOLDEN_CROSS":
        trend_contrib = min(1.0, trend_contrib + 0.3)
    elif crossover == "DEATH_CROSS":
        trend_contrib = max(-1.0, trend_contrib - 0.3)

    # --- MACD: histogram magnitude, continuous [-1, +1] ---
    macd_hist = row.get("macd_histogram")
    current_price = row.get("current_price")
    if macd_hist is not None and current_price and current_price > 0:
        # Normalize histogram by price for cross-asset comparability
        macd_norm = macd_hist / current_price * 100  # as % of price
        macd_contrib = max(-1.0, min(1.0, macd_norm / 1.0))  # ±1% = full signal
    else:
        macd_sig = row.get("macd_signal", "NEUTRAL")
        macd_contrib = 0.5 if macd_sig == "BULLISH" else -0.5 if macd_sig == "BEARISH" else 0.0

    # --- Bollinger %B: continuous [-1, +1] ---
    # %B 0.5 = neutral, 0.0 = +1 (oversold), 1.0 = -1 (overbought)
    bb_pos = row.get("bb_position")
    if bb_pos is not None:
        bb_contrib = max(-1.0, min(1.0, (0.5 - bb_pos) * 2))
    else:
        bb_contrib = 0.0

    # --- Volume context: amplifies existing direction ---
    vol_ratio = row.get("volume_ratio")
    if vol_ratio is not None and vol_ratio > 1.5:
        # High volume amplifies the current directional bias
        direction = rsi_contrib + trend_contrib + macd_contrib
        vol_amplifier = min(1.0, (vol_ratio - 1.0) / 2.0)  # 1.0→0, 3.0→+1
        vol_contrib = vol_amplifier * (1.0 if direction > 0 else -1.0 if direction < 0 else 0.0)
    else:
        vol_contrib = 0.0

    # --- Volatility regime: high vol = uncertainty penalty ---
    hv_30 = row.get("hv_30")
    if hv_30 is not None:
        # HV30 above 40% penalizes conviction; below 15% adds confidence
        vol_regime = max(-1.0, min(1.0, (25 - hv_30) / 25))
        # Only amplifies existing signal direction, doesn't create one
        existing_direction = 1.0 if (rsi_contrib * 25 + trend_contrib * 20 + macd_contrib * 20
                                     + bb_contrib * 15 + vol_contrib * 10) > 0 else -1.0
        vol_regime_contrib = vol_regime * abs(existing_direction) * 0.5
    else:
        vol_regime_contrib = 0.0

    return [
        {"name": "RSI", "weight": 25, "contrib": rsi_contrib},
        {"name": "Trend", "weight": 20, "contrib": trend_contrib},
        {"name": "MACD", "weight": 20, "contrib": macd_contrib},
        {"name": "Bollinger", "weight": 15, "contrib": bb_contrib},
        {"name": "Volume", "weight": 10, "contrib": vol_contrib},
        {"name": "Vol Regime", "weight": 10, "contrib": vol_regime_contrib},
    ]


def _empty_signals(symbol) -> dict:
    """The 'insufficient data' result shape.

    Shared with the main path so the response contract can't drift between
    the early return and the computed result.
    """
    return {
        "symbol": symbol,
        "rsi": None,
        "rsi_signal": "INSUFFICIENT_DATA",
        "sma_crossover": "INSUFFICIENT_DATA",
        "sma_50": None,
        "sma_200": None,
        "volume_ratio": None,
        "volume_signal": "INSUFFICIENT_DATA",
        "macd_signal": "INSUFFICIENT_DATA",
        "macd_histogram": None,
        "bb_position": None,
        "bb_signal": "INSUFFICIENT_DATA",
        "sma_50_dist_pct": None,
        "hv_30": None,
        "current_price": None,
    }


def _composite_result(components: list[dict]) -> dict:
    """Normalize component contributions to a 0-100 score + label.

    The raw weighted sum spans [-100, +100]; the composite score is
    raw/2 + 50, clamped. Shared by compute_composite_score and score_breakdown
    so the normalization and thresholds can never diverge.
    """
    raw = sum(c["contrib"] * c["weight"] for c in components)
    score = int(round((raw + 100) / 200 * 100))
    score = max(0, min(100, score))

    if score >= 65:
        label = "BUY"
    elif score < 35:
        label = "SELL"
    else:
        label = "NEUTRAL"
    return {"score": score, "label": label}


def _round_or_none(v, ndigits=2):
    """Round a float to *ndigits*, or None for NaN/None (NaN-safe output)."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if np.isnan(f) or np.isinf(f):
        return None
    return round(f, ndigits)


def compute_composite_score(row):
    """0-100 composite signal score with BUY/NEUTRAL/SELL label.

    Uses continuous scaling instead of binary buckets so that signal
    strength is proportional to how far the indicator has moved into
    its extreme zone.  Each component maps to a [-1, +1] contribution
    (bullish positive, bearish negative), then is weighted and
    normalized to a 0-100 scale. See ``_score_components`` for the weights.
    """
    return _composite_result(_score_components(row))


def score_breakdown(row) -> dict:
    """Composite score plus per-component contribution in score points.

    Each component's contribution is reported in score points (contrib * weight
    / 2), so the sum of contributions plus the 50-point neutral baseline
    equals the composite score. Direction is derived from the sign of the
    contribution. This is the "why is this score X?" surface.
    """
    components = _score_components(row)
    composite = _composite_result(components)

    breakdown = []
    for c in components:
        points = round(c["contrib"] * c["weight"] / 2, 2)
        direction = "bullish" if points > 0.05 else "bearish" if points < -0.05 else "neutral"
        breakdown.append({
            "name": c["name"],
            "weight": c["weight"],
            "contribution": points,
            "direction": direction,
        })

    return {"score": composite["score"], "label": composite["label"], "components": breakdown}


def generate_signals(symbol, price_df):
    """Generate all signals for a symbol given its daily OHLCV DataFrame.

    Returns dict with signal data including extended indicators.
    """
    if price_df.empty or len(price_df) < 20:
        return _empty_signals(symbol)

    close = price_df["close"]
    vol = price_df["volume"]
    current_price = float(close.iloc[-1])

    # RSI
    rsi_val = rsi(close).iloc[-1]
    if np.isnan(rsi_val):
        rsi_signal = "INSUFFICIENT_DATA"
    elif rsi_val >= 70:
        rsi_signal = "OVERBOUGHT"
    elif rsi_val <= 30:
        rsi_signal = "OVERSOLD"
    else:
        rsi_signal = "NEUTRAL"

    # SMA crossover
    crossover = detect_sma_crossover(close)

    sma_50_val = sma(close, 50).iloc[-1] if len(close) >= 50 else None
    sma_200_val = sma(close, 200).iloc[-1] if len(close) >= 200 else None

    # Volume
    vol_ratio = unusual_volume(vol.iloc[-1], vol)
    if vol_ratio >= 2.0:
        vol_signal = "UNUSUAL_HIGH"
    elif vol_ratio <= 0.5:
        vol_signal = "UNUSUAL_LOW"
    else:
        vol_signal = "NORMAL"

    # MACD
    if len(close) >= 35:
        macd_line, signal_line, hist = macd(close)
        macd_hist_val = float(hist.iloc[-1])
        macd_hist_prev = float(hist.iloc[-2])
        if macd_hist_prev <= 0 and macd_hist_val > 0:
            macd_sig = "BULLISH"
        elif macd_hist_prev >= 0 and macd_hist_val < 0:
            macd_sig = "BEARISH"
        else:
            macd_sig = "NEUTRAL"
    else:
        macd_sig = "INSUFFICIENT_DATA"
        macd_hist_val = None

    # Bollinger Bands
    if len(close) >= 20:
        bb_pct_b = bollinger_position(close).iloc[-1]
        bb_pct_b_val = float(bb_pct_b) if not np.isnan(bb_pct_b) else None
        if bb_pct_b_val is not None:
            if bb_pct_b_val > 1.0:
                bb_sig = "ABOVE_UPPER"
            elif bb_pct_b_val < 0.0:
                bb_sig = "BELOW_LOWER"
            else:
                bb_sig = "NEUTRAL"
        else:
            bb_sig = "INSUFFICIENT_DATA"
    else:
        bb_pct_b_val = None
        bb_sig = "INSUFFICIENT_DATA"

    # SMA 50 distance
    if sma_50_val is not None and not np.isnan(sma_50_val) and sma_50_val != 0:
        sma_50_dist = (current_price - sma_50_val) / sma_50_val * 100
    else:
        sma_50_dist = None

    # Historical volatility (30-day)
    if len(close) >= 31:
        hv30_series = historical_volatility(close, 30)
        hv30_val = float(hv30_series.iloc[-1])
        if np.isnan(hv30_val):
            hv30_val = None
    else:
        hv30_val = None

    result = {
        "symbol": symbol,
        "rsi": _round_or_none(rsi_val, 2),
        "rsi_signal": rsi_signal,
        "sma_crossover": crossover,
        "sma_50": _round_or_none(sma_50_val, 2),
        "sma_200": _round_or_none(sma_200_val, 2),
        "volume_ratio": round(vol_ratio, 2),
        "volume_signal": vol_signal,
        "macd_signal": macd_sig,
        "macd_histogram": _round_or_none(macd_hist_val, 4),
        "bb_position": _round_or_none(bb_pct_b_val, 2),
        "bb_signal": bb_sig,
        "sma_50_dist_pct": _round_or_none(sma_50_dist, 2),
        "hv_30": _round_or_none(hv30_val * 100, 1) if hv30_val is not None else None,
        "current_price": round(current_price, 2),
    }

    # Composite score
    composite = compute_composite_score(result)
    result["composite_score"] = composite["score"]
    result["composite_label"] = composite["label"]

    return result
