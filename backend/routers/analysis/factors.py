"""Factor model endpoints: exposures, portfolio exposure, factor-aware covariance."""

from pydantic import BaseModel

from backend.routers.analysis._common import (
    _safe_float,
    router,
)
from backend.services.sizing_book import build_factor_model
from data.cache import ttl_cache


class FactorBody(BaseModel):
    symbols: list[str]
    weights: list[float] | None = None


@router.post("/factors")
def factor_exposures_endpoint(body: FactorBody):
    """Per-symbol factor exposures + portfolio aggregate.

    Returns {symbols, factors, exposures: {symbol: {factor: beta, r2, residual_vol}},
    portfolio: {factor: exposure}, available: bool}.
    """
    return _cached_factor_exposures_endpoint(body)


def _factor_exposures_endpoint_compute(body: FactorBody):
    result = build_factor_model(tuple(s.upper() for s in body.symbols))
    if result is None:
        return {"symbols": [], "factors": [], "exposures": {}, "portfolio": {}, "available": False}
    returns_dict, factor_df, exposures = result

    factors = [f for f in factor_df.columns if f in ("market", "size", "value", "momentum", "quality")]

    weights = {}
    if body.weights:
        for sym, w in zip(body.symbols, body.weights):
            weights[sym.upper()] = float(w)

    from analysis.factors import portfolio_factor_exposures

    portfolio = portfolio_factor_exposures(exposures, weights)

    return {
        "symbols": list(exposures.keys()),
        "factors": factors,
        "exposures": {
            sym: {k: _safe_float(v) for k, v in exp.items()}
            for sym, exp in exposures.items()
        },
        "portfolio": {k: _safe_float(v) for k, v in portfolio.items()},
        "available": True,
    }


# TTL-cached: factor exposures over daily bars.
_cached_factor_exposures_endpoint = ttl_cache(ttl_seconds=300)(_factor_exposures_endpoint_compute)


@router.post("/factor-covariance")
def factor_covariance_endpoint(body: FactorBody):
    """Factor-aware covariance matrix for a symbol set.

    Returns {symbols, matrix, implied_correlations, available}. The matrix
    replaces the blanket rho=0.4 assumption in sizing.
    """
    return _cached_factor_covariance_endpoint(body)


def _factor_covariance_endpoint_compute(body: FactorBody):
    result = build_factor_model(tuple(s.upper() for s in body.symbols))
    if result is None:
        return {"symbols": [], "matrix": [], "implied_correlations": [], "available": False}
    returns_dict, factor_df, exposures = result

    from analysis.factors import factor_covariance

    cov = factor_covariance(returns_dict, factor_df, exposures)
    if cov.empty:
        return {"symbols": [], "matrix": [], "implied_correlations": [], "available": False}

    syms = list(cov.columns)
    corr = cov.corr()
    return {
        "symbols": syms,
        "matrix": [[_safe_float(v) for v in row] for row in cov.values],
        "implied_correlations": [
            {"pair": f"{syms[i]}-{syms[j]}", "correlation": _safe_float(corr.iloc[i, j])}
            for i in range(len(syms))
            for j in range(i + 1, len(syms))
        ],
        "available": True,
    }


# TTL-cached: factor model over daily bars.
_cached_factor_covariance_endpoint = ttl_cache(ttl_seconds=300)(_factor_covariance_endpoint_compute)


