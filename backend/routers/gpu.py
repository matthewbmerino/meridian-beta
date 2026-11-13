"""GPU / compute-market endpoints — Vast.ai prices + the Compute Index."""

from fastapi import APIRouter

from backend.routers.analysis._common import _safe_float

router = APIRouter(prefix="/api/gpu", tags=["gpu"])


def _clean_metric(m: dict) -> dict:
    """NaN-safe per-GPU metric dict.

    `total`/`rented`/`usage` are None (search offers exposes only available
    offers) — _safe_float's default of 0 would fabricate "0% utilization".
    """
    return {
        "gpu_name": str(m.get("gpu_name") or ""),
        "price_median": _safe_float(m.get("price_median")),
        "price_p10": _safe_float(m.get("price_p10")),
        "price_p25": _safe_float(m.get("price_p25")),
        "price_p75": _safe_float(m.get("price_p75")),
        "price_p90": _safe_float(m.get("price_p90")),
        "price_mean": _safe_float(m.get("price_mean"), default=None),
        "price_std": _safe_float(m.get("price_std"), default=None),
        "price_min": _safe_float(m.get("price_min"), default=None),
        "price_max": _safe_float(m.get("price_max"), default=None),
        "count": _safe_float(m.get("count"), default=None),
        "total": _safe_float(m.get("total"), default=None),
        "available": _safe_float(m.get("available")),
        "rented": _safe_float(m.get("rented"), default=None),
        "usage": _safe_float(m.get("usage"), default=None),
        "tflops": _safe_float(m.get("tflops")),
        "tflops_per_dollar": _safe_float(m.get("tflops_per_dollar")),
    }


@router.get("/metrics")
def gpu_metrics():
    """Current per-GPU snapshot + the Compute Index composite.

    Returns {available, metrics: [...], index: {...}, tightness: {...}}.
    Degrades to available:false when the feed is down or the key is missing.
    """
    from analysis.compute_index import compute_index, tightness_composite
    from data.gpu import GPU_BASKET, get_gpu_metrics

    metrics = get_gpu_metrics()
    if not metrics:
        return {"available": False, "metrics": [], "index": None, "tightness": None}

    clean = [_clean_metric(m) for m in metrics]

    # Build the index from the basket GPUs present in the snapshot.
    prices = {m["gpu_name"]: m["price_median"] for m in clean if m["price_median"] > 0}
    basket_prices = {g: prices[g] for g in GPU_BASKET if g in prices}
    utilization = {m["gpu_name"]: m["usage"] for m in clean if m["usage"] is not None}

    # Base = earliest persisted snapshot (or current if no history yet).
    from data.gpu import earliest_ts, load_history
    base_prices = {}
    hist = load_history()
    if not hist.empty:
        first = hist[hist["ts"] == hist["ts"].min()]
        base_prices = {
            r["gpu_name"]: float(r["price_median"])
            for _, r in first.iterrows() if r["price_median"] and r["price_median"] > 0
        }
    if not base_prices:
        base_prices = basket_prices  # first-ever snapshot: index = 100

    index = compute_index(
        basket_prices, base_prices, GPU_BASKET,
        utilization=utilization,
        price_spread=_price_spread(clean),
    )

    tightness = tightness_composite(_price_spread(clean))

    return {
        "available": True,
        "metrics": clean,
        "index": index,
        "tightness": tightness,
        "base_ts": earliest_ts(),
    }


def _price_spread(metrics: list[dict]) -> float | None:
    """p90/p10 spread across the basket (market tightness)."""
    spreads = [m["price_p90"] / m["price_p10"] for m in metrics
               if m["price_p10"] and m["price_p10"] > 0 and m["price_p90"]]
    if not spreads:
        return None
    return sum(spreads) / len(spreads)


@router.get("/trends")
def gpu_trends(days: int = 90):
    """Full local history for the index + per-GPU price/supply/demand.

    Returns {available, series: [{ts, value}], gpus: {gpu: [{ts, price,
    available, rented}]}}. Serves from the persisted SQLite history so the
    operator can analyze the events that drove prices.
    """
    from analysis.compute_index import compute_index_series
    from data.gpu import GPU_BASKET, load_history

    hist = load_history(days=days)
    if hist.empty:
        return {"available": False, "series": [], "gpus": {}}

    # Per-timestamp price dicts for the index series.
    snapshots = []
    for ts, group in hist.groupby("ts"):
        snapshots.append({
            "ts": ts.isoformat(),
            "prices": {
                r["gpu_name"]: float(r["price_median"])
                for _, r in group.iterrows() if r["price_median"] and r["price_median"] > 0
            },
        })
    series = compute_index_series(snapshots, GPU_BASKET)

    gpus = {}
    for name, group in hist.groupby("gpu_name"):
        gpus[name] = [
            {
                "ts": r["ts"].isoformat(),
                "price": _safe_float(r["price_median"]),
                "available": _safe_float(r["available"]),
                "rented": _safe_float(r["rented"], default=None),
            }
            for _, r in group.iterrows()
        ]

    return {"available": True, "series": series, "gpus": gpus}


@router.get("/offers")
def gpu_offers(days: int = 7, gpu: str | None = None):
    """Raw search-offer rows from the persisted source of truth.

    Returns {available, offers: [{ts, gpu_name, dph_total, num_gpus,
    dph_per_gpu, total_flops, verification, geolocation, reliability, rented,
    is_bid, start_date, end_date}]}. Serves the raw offers captured during
    each snapshot so any statistic can be re-derived from the source.
    """
    from data.gpu import load_offers

    df = load_offers(days=days, gpu=gpu)
    if df.empty:
        return {"available": False, "offers": []}

    offers = []
    for _, r in df.iterrows():
        offers.append({
            "ts": r["ts"].isoformat(),
            "gpu_name": str(r["gpu_name"] or ""),
            "dph_total": _safe_float(r["dph_total"], default=None),
            "num_gpus": _safe_float(r["num_gpus"], default=None),
            "dph_per_gpu": _safe_float(r["dph_per_gpu"], default=None),
            "total_flops": _safe_float(r["total_flops"], default=None),
            "verification": r["verification"] if r["verification"] is not None else None,
            "geolocation": r["geolocation"] if r["geolocation"] is not None else None,
            "reliability": _safe_float(r["reliability"], default=None),
            "rented": _safe_float(r["rented"], default=None),
            "is_bid": _safe_float(r["is_bid"], default=None),
            "start_date": r["start_date"] if r["start_date"] is not None else None,
            "end_date": r["end_date"] if r["end_date"] is not None else None,
        })
    return {"available": True, "offers": offers}


@router.get("/index")
def gpu_index():
    """The Compute Index composite + methodology pointer."""
    from analysis.compute_index import compute_index, tightness_composite
    from data.gpu import GPU_BASKET, get_gpu_metrics, load_history

    metrics = get_gpu_metrics()
    if not metrics:
        return {"available": False, "index": None, "tightness": None}

    clean = [_clean_metric(m) for m in metrics]
    prices = {m["gpu_name"]: m["price_median"] for m in clean if m["price_median"] > 0}
    basket_prices = {g: prices[g] for g in GPU_BASKET if g in prices}
    utilization = {m["gpu_name"]: m["usage"] for m in clean if m["usage"] is not None}

    base_prices = {}
    hist = load_history()
    if not hist.empty:
        first = hist[hist["ts"] == hist["ts"].min()]
        base_prices = {
            r["gpu_name"]: float(r["price_median"])
            for _, r in first.iterrows() if r["price_median"] and r["price_median"] > 0
        }
    if not base_prices:
        base_prices = basket_prices

    index = compute_index(
        basket_prices, base_prices, GPU_BASKET,
        utilization=utilization,
        price_spread=_price_spread(clean),
    )
    tightness = tightness_composite(_price_spread(clean))
    return {"available": True, "index": index, "tightness": tightness}
