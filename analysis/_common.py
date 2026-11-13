"""Shared helpers for the analysis layer.

Holds cross-module utilities that are too small to justify per-module copies
and don't belong to a single domain. Currently: NaN-safe numeric coercion,
which is required by the layer invariant "no NaN in output dicts".
"""

from __future__ import annotations

import numpy as np


def safe_float(v, default=0.0):
    """Coerce *v* to float, returning *default* for NaN/Inf/non-numeric.

    This is the layer's serialization guard: a NaN that leaks into JSON
    breaks the frontend silently, so every numeric output passes through here
    (or a caller-level equivalent).
    """
    try:
        f = float(v)
        return default if np.isnan(f) or np.isinf(f) else f
    except (TypeError, ValueError):
        return default
