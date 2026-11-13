"""Persistent user preferences.

A small JSON-backed key/value store for settings that should survive across
browser refreshes and app restarts (mode choice, remote host, model picks,
onboarding flags). Mirrors the data/watchlist.py storage pattern.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from data.crypto import load_json_store, write_json
from data.session import data_root


def _file() -> Path:
    return data_root() / "prefs.json"

_DEFAULTS: dict[str, Any] = {
    # LLM provider — "ollama" (default, open cloud models) | "perplexity"
    "llm_provider": "ollama",
    "ollama_model": "deepseek-v4.1-flash",       # Compass chat (fast, tool-calling)
    "ollama_research_model": "deepseek-v4-pro",  # Agentic viz agent (deep reasoning)
    "ollama_writer_model": "deepseek-v4-pro",    # Journalist report writer (research prose)
    # Compass — Perplexity sonar-pro (fallback provider)
    "perplexity_model": "sonar-pro",
    # Position sizing — growth-tilted defaults (stay invested, respect
    # conviction). See analysis/sizing.py for provenance.
    "sizing_w_cap": 0.09,              # max single-name weight
    "sizing_sigma_target": 0.16,       # annualized portfolio vol target
    "sizing_k": 0.17,                  # fractional-Kelly coefficient
    "sizing_target_gross": 0.92,       # aspirational invested ratio (8% cash)
    "sizing_cash_floor": 0.03,         # hard minimum cash
    "sizing_sector_cap": 0.35,         # max weight in any sector
    "sizing_corr_assumed": 0.40,       # avg pairwise correlation for vol check
    "sizing_trade_band": 0.01,         # no-trade band (|Δw|)
    "sizing_friction_bps": 25.0,       # minimum trade in bps of equity
    "sizing_min_position_weight": 0.01,  # below this, close rather than hold
    # Decision Book — operator-outcome calibration for sizing (the honest
    # marriage): blends the operator's own resolved-decision win rates into
    # the hybrid Kelly edge. Default OFF until the sample is meaningful;
    # operator_bucket_stats refuses to build below min_obs regardless.
    "calibration_operator_outcomes": False,
    "calibration_operator_min_obs": 30,
    # Desk Loop — Discipline Score component weights (review freshness /
    # forecast precision / research discipline). Transparent defaults from
    # analysis/engagement.DEFAULT_WEIGHTS; a component with no input is absent
    # and the score renormalizes. Tune once live.
    "discipline_weight_review": 0.40,
    "discipline_weight_forecast": 0.35,
    "discipline_weight_research": 0.25,
    # Manual override for total free cash across all accounts (brokerage +
    # external savings). When > 0, replaces broker/CSV cash entirely so
    # cash_balance, net_liquidation, allocation, and risk all reflect the
    # user's authoritative number. When 0, falls back to broker/CSV cash.
    "cash_override": 0.0,
}


def _read() -> dict[str, Any]:
    data = load_json_store(_file(), "prefs", {})
    return data if isinstance(data, dict) else {}


def _write(prefs: dict[str, Any]) -> None:
    try:
        write_json(_file(), "prefs", prefs)
    except OSError:
        pass


def get_prefs() -> dict[str, Any]:
    """Return current prefs merged with defaults (defaults fill missing keys)."""
    merged = dict(_DEFAULTS)
    merged.update(_read())
    return merged


def get(key: str) -> Any:
    return get_prefs().get(key)


def update_prefs(**kwargs: Any) -> dict[str, Any]:
    # Allowlist: only known pref keys may be written. Without this, a client
    # could set arbitrary keys (e.g. cash_override to inflate NAV, or a
    # sizing_* knob to 1.0) — the prefs PATCH endpoint accepts raw JSON.
    allowed = set(_DEFAULTS.keys())
    unknown = set(kwargs.keys()) - allowed
    if unknown:
        raise ValueError(f"Unknown preference key(s): {', '.join(sorted(unknown))}")
    current = _read()
    current.update(kwargs)
    _write(current)
    return get_prefs()
