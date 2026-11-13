"""Analysis router package.

Split from a single 919-line router into domain modules:
- risk.py       correlation, VaR, frontier, stress, blend, drawdown, greeks
- sizing.py     sizing, sizing-preview, backtest
- composites.py composites, position-intel
- factors.py    factor exposures + factor-aware covariance

Shared helpers (NaN-safe serialization, cached computations, request bodies)
live in _common.py. The router is assembled here so main.py imports it the
same way it always did.
"""

from backend.routers.analysis import composites as _composites  # noqa: F401
from backend.routers.analysis import factors as _factors  # noqa: F401
from backend.routers.analysis import outcomes as _outcomes  # noqa: F401
from backend.routers.analysis import risk as _risk  # noqa: F401
from backend.routers.analysis import sizing as _sizing  # noqa: F401
from backend.routers.analysis._common import router

__all__ = ["router"]
