"""TTL cache with LRU eviction and thread safety for API rate-limit compliance."""

import copy
import threading
import time
from functools import wraps

import pandas as pd

# key = (fn qualname, args tuple, kwargs tuple); value = (result, ts, last_access)
_cache: dict[tuple[object, ...], tuple[object, float, float]] = {}
_MAX_CACHE_ENTRIES = 512
_lock = threading.Lock()

# Failure sentinels (None, empty containers) are cached for a short time only,
# so a transient feed blip degrades to a retry within a minute instead of
# poisoning the feed for the full TTL (up to 24h for reference/universe data).
_NEGATIVE_TTL = 60


def _is_negative(value) -> bool:
    """True for failure sentinels: None, empty dict/list/str, empty DataFrame."""
    if value is None:
        return True
    if isinstance(value, pd.DataFrame):
        return value.empty
    if isinstance(value, (dict, list, str)):
        return len(value) == 0
    return False


def _safe_copy(value):
    """Return a deep copy of mutable containers so callers can't corrupt the cache."""
    if isinstance(value, pd.DataFrame):
        return value.copy()
    if isinstance(value, dict):
        return copy.deepcopy(value)
    if isinstance(value, list):
        return copy.deepcopy(value)
    return value


def _evict_lru():
    """Remove the least-recently-used entry when cache exceeds max size.
    Caller must hold _lock."""
    while len(_cache) > _MAX_CACHE_ENTRIES:
        lru_key = min(_cache, key=lambda k: _cache[k][2])
        del _cache[lru_key]


def ttl_cache(ttl_seconds=60, copy=True):
    """Decorator that caches function results for *ttl_seconds*.

    copy=False skips the defensive deep copy on cache hits — only for values
    callers treat as strictly read-only (shallow-copy the outer dict if you
    need to add keys). Deep-copying a board-sized DataFrame on every hit was
    itself ~700ms; correctness is preserved by convention, not duplication.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            key_args = []
            for a in args:
                try:
                    hash(a)
                    key_args.append(a)
                except TypeError:
                    # Unhashable args (e.g. a list of symbols) must still key
                    # deterministically. id() is wrong: object ids are reused
                    # after GC, so two unrelated objects could collide, and a
                    # freshly-built list never hits the cache anyway. repr() of
                    # plain data is stable; fall back to a type+length marker
                    # so we never cache a wrong result for an exotic object.
                    try:
                        key_args.append(repr(a))
                    except Exception:
                        key_args.append(f"<{type(a).__name__}:{len(a) if hasattr(a, '__len__') else '?'}>")
            # Use __qualname__ (not __name__) so nested functions with the
            # same local name (e.g. the _compute helpers in the analysis
            # routers) don't collide in the shared cache. For module-level
            # functions __qualname__ == __name__, so invalidate() by name
            # still works.
            key = (fn.__qualname__, tuple(key_args), tuple(sorted(kwargs.items())))

            now = time.time()
            with _lock:
                if key in _cache:
                    value, ts, _last_access = _cache[key]
                    ttl = _NEGATIVE_TTL if _is_negative(value) else ttl_seconds
                    if now - ts < ttl:
                        _cache[key] = (value, ts, now)
                        return _safe_copy(value) if copy else value

            result = fn(*args, **kwargs)
            with _lock:
                _cache[key] = (result, time.time(), time.time())
                _evict_lru()
            # copy=False means NO copies anywhere — the caller receives the same
            # object that now sits in the cache. Read-only by convention (decorator doc).
            return _safe_copy(result) if copy else result
        return wrapper
    return decorator


def invalidate(fn_name: str):
    """Drop all cached entries for a specific function."""
    with _lock:
        keys = [k for k in _cache if k[0] == fn_name]
        for k in keys:
            del _cache[k]
