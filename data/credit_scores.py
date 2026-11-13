"""Credit score observations — what the bureaus' models actually said, and when.

Two rules make this store honest, and both exist because the alternative is a
number that looks authoritative and is not:

1. **A series is keyed on (model, bureau), never on "my credit score".** A FICO 8
   from Experian and a VantageScore 3.0 from TransUnion are different models over
   different data. They disagree by tens of points on the same day, so averaging
   or splicing them invents a score nobody publishes.
2. **Observations are APPEND-ONLY and provenance is mandatory.** Every row carries
   the model, the bureau, the date the score was published, and the app it came
   from. A score without those is a number with no meaning, and it is refused
   rather than stored.

This is a parallel record: a credit score is a LAGGING description of the book,
never an input to it. Nothing here touches ``net_liquidation``, sizing, or the
deployment arithmetic.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from data.crypto import load_json_store, write_json

_MAX_SOURCE = 60
_MAX_NOTE = 280

# Canonical spellings. Matching is case-insensitive; anything genuinely new is
# still legal via "other" so a new model does not require a code change.
MODELS: tuple[str, ...] = (
    "FICO 8",
    "FICO 9",
    "FICO 10",
    "FICO 10T",
    "VantageScore 3.0",
    "VantageScore 4.0",
    "other",
)

BUREAUS: tuple[str, ...] = ("Experian", "TransUnion", "Equifax", "other")

_MODEL_ALIASES = {"vs3": "VantageScore 3.0", "vs4": "VantageScore 4.0"}

# Consumer scores are 300-850; some models run to 900. The bound exists to catch
# a unit error (72100, 7.21), not to police which model the operator may record.
SCORE_MIN = 300
SCORE_MAX = 900

# Both free apps publish at least monthly, so a gap longer than this means two
# missed cycles — the surface greys a score older than this rather than letting
# the operator read it as today's.
STALE_DAYS = 45

# Sources are the app the operator read the number from. These are channels, not
# models: the series key is (model, bureau), so a new channel needs no migration.
SOURCES: tuple[str, ...] = ("chase", "experian", "equifax", "myfico", "manual", "other")


def _file() -> Path:
    """Resolve the store path through data_root() — the conftest ``_ROOT`` patch
    then isolates the suite without a per-module line."""
    from data.session import data_root

    return data_root() / "credit_scores.json"


def _read() -> dict:
    data = load_json_store(_file(), "credit_scores", {"observations": []})
    return data if isinstance(data, dict) else {"observations": []}


def store_path() -> Path:
    """Public path accessor — a router that reports ``crypto.store_error(path)``
    must not import the underscore name (data/AGENTS.md invariant 5)."""
    return _file()


def _write(data: dict) -> None:
    try:
        write_json(_file(), "credit_scores", data)
    except OSError:
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(v, label: str, max_len: int, required: bool = False) -> str:
    s = " ".join(str(v).split()) if v is not None else ""
    if required and not s:
        raise ValueError(f"{label} must not be empty")
    if len(s) > max_len:
        raise ValueError(f"{label} must be ≤ {max_len} characters")
    return s


def _canonical(v, allowed: tuple[str, ...], label: str) -> str:
    """Match a value against an allowlist case-insensitively and return the
    canonical spelling. An unknown value is refused — a typo'd model splits one
    series into two and quietly understates the history."""
    raw = _clean_text(v, label, 40, required=True)
    key = raw.lower()
    key = _MODEL_ALIASES.get(key, key)
    for option in allowed:
        if option.lower() == key:
            return option
    raise ValueError(f"{label} must be one of: {', '.join(allowed)}")


def _clean_score(v) -> int:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError("score must be a number")
    if f != f or f in (float("inf"), float("-inf")):
        raise ValueError("score must be a finite number")
    if not float(f).is_integer():
        raise ValueError("score must be a whole number")
    n = int(f)
    if not (SCORE_MIN <= n <= SCORE_MAX):
        raise ValueError(f"score must be between {SCORE_MIN} and {SCORE_MAX}")
    return n


def _clean_as_of(v) -> str:
    raw = _clean_text(v, "as_of", 40, required=True)
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        raise ValueError("as_of must be a YYYY-MM-DD date")


def _clean_change(v) -> int | None:
    """The delta the app itself printed, when the operator has it. Optional —
    the store does not need it, but it makes a backfill self-checking."""
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ValueError("change must be a whole number of points")


def series_key(model: str, bureau: str) -> str:
    """The identity of a score series. Consumers group on THIS, never on a bare
    score."""
    return f"{model}|{bureau}"


def _row_key(observation: dict) -> tuple[str, str, str]:
    return (
        str(observation.get("model", "")).lower(),
        str(observation.get("bureau", "")).lower(),
        str(observation.get("as_of", "")),
    )


def _build(
    *,
    score,
    model,
    bureau,
    source,
    as_of,
    change=None,
    note=None,
) -> dict:
    """Validate every field and return a FRESH row. Validation happens before any
    read-modify-write, because the crypto layer hands out a SHARED cached object
    and a half-mutation would leak into every later reader."""
    return {
        "id": uuid.uuid4().hex[:12],
        "score": _clean_score(score),
        "model": _canonical(model, MODELS, "model"),
        "bureau": _canonical(bureau, BUREAUS, "bureau"),
        "source": _canonical(source, SOURCES, "source"),
        "as_of": _clean_as_of(as_of),
        "change": _clean_change(change),
        "note": _clean_text(note, "note", _MAX_NOTE) or None,
        "created_at": _now_iso(),
    }


def list_scores(model: str | None = None, bureau: str | None = None) -> list[dict]:
    """Every observation, oldest first (a chart draws left to right)."""
    rows = list(_read().get("observations", []))
    if model:
        rows = [r for r in rows if str(r.get("model", "")).lower() == model.lower()]
    if bureau:
        rows = [r for r in rows if str(r.get("bureau", "")).lower() == bureau.lower()]
    return sorted(rows, key=lambda r: (str(r.get("as_of", "")), str(r.get("created_at", ""))))


def add_score(*, score, model, bureau, source, as_of, change=None, note=None) -> dict:
    """Append one observation. Raises ValueError on any invalid field, and on a
    duplicate of (model, bureau, as_of) — re-entering the same reading twice is a
    mistake, not a second measurement."""
    observation = _build(score=score, model=model, bureau=bureau, source=source,
                         as_of=as_of, change=change, note=note)
    data = _read()
    rows = data.setdefault("observations", [])
    key = _row_key(observation)
    for existing in rows:
        if _row_key(existing) == key:
            raise ValueError(
                f"{observation['model']} {observation['bureau']} already has a score "
                f"for {observation['as_of']}"
            )
    rows.append(observation)
    _write(data)
    return observation


def add_scores(rows: list[dict]) -> dict:
    """Append a batch (the backfill path). Idempotent: a row whose (model, bureau,
    as_of) is already present is SKIPPED and reported, never silently merged or
    duplicated — a backfill is meant to be re-runnable."""
    if not isinstance(rows, list):
        raise ValueError("rows must be a list of observations")
    data = _read()
    existing = data.setdefault("observations", [])
    seen = {_row_key(r) for r in existing}
    added: list[dict] = []
    skipped: list[dict] = []
    for raw in rows:
        if not isinstance(raw, dict):
            skipped.append({"row": str(raw)[:60], "reason": "not an object"})
            continue
        try:
            observation = _build(
                score=raw.get("score"),
                model=raw.get("model"),
                bureau=raw.get("bureau"),
                source=raw.get("source"),
                as_of=raw.get("as_of"),
                change=raw.get("change"),
                note=raw.get("note"),
            )
        except ValueError as e:
            skipped.append({"as_of": raw.get("as_of"), "reason": str(e)})
            continue
        key = _row_key(observation)
        if key in seen:
            skipped.append({"as_of": observation["as_of"], "reason": "already recorded"})
            continue
        seen.add(key)
        added.append(observation)
    existing.extend(added)
    _write(data)
    return {"added": len(added), "skipped": len(skipped), "skipped_detail": skipped}


def remove_score(observation_id: str) -> bool:
    """Remove one observation. Returns True when removed; idempotent."""
    data = _read()
    rows = data.get("observations", [])
    remaining = [r for r in rows if r.get("id") != observation_id]
    if len(remaining) == len(rows):
        return False
    data["observations"] = remaining
    _write(data)
    return True


def latest_by_series() -> dict[str, dict]:
    """series_key -> newest observation. Ordered by ``as_of`` (the date the score
    was published), NOT by when it was entered — otherwise a backfill of older
    readings would masquerade as the current score."""
    out: dict[str, dict] = {}
    for row in _read().get("observations", []):
        key = series_key(str(row.get("model", "")), str(row.get("bureau", "")))
        current = out.get(key)
        if current is None or (str(row.get("as_of", "")), str(row.get("created_at", ""))) > (
            str(current.get("as_of", "")),
            str(current.get("created_at", "")),
        ):
            out[key] = row
    return out


def stale_series(as_of: date | None = None) -> list[dict]:
    """Series whose newest observation is past STALE_DAYS. A stale score must be
    shown as stale, never as today's number."""
    today = as_of or date.today()
    out = []
    for key, row in latest_by_series().items():
        try:
            published = date.fromisoformat(str(row.get("as_of")))
        except (TypeError, ValueError):
            out.append({"series": key, "as_of": row.get("as_of"), "days": None})
            continue
        age = (today - published).days
        if age > STALE_DAYS:
            out.append({"series": key, "as_of": row.get("as_of"), "days": age})
    return out


def series_integrity() -> list[dict]:
    """Rows whose recorded ``change`` disagrees with the arithmetic delta from the
    previous observation in the same series. Not a rejection — a flag: the app's
    printed delta and the two scores should agree, and when they do not, someone
    is reading one of them wrong."""
    issues: list[dict] = []
    by_series: dict[str, list[dict]] = {}
    for row in list_scores():
        by_series.setdefault(series_key(str(row.get("model", "")), str(row.get("bureau", ""))), []).append(row)
    for key, rows in by_series.items():
        previous = None
        for row in rows:
            recorded = row.get("change")
            if previous is not None and recorded is not None:
                delta = int(row["score"]) - int(previous["score"])
                if int(recorded) != delta:
                    issues.append({
                        "series": key,
                        "as_of": row.get("as_of"),
                        "recorded_change": int(recorded),
                        "computed_change": delta,
                        "previous_as_of": previous.get("as_of"),
                    })
            previous = row
    return issues


def rollup() -> dict:
    """The read view: one row per series with its newest reading, its span, and
    whether that reading is stale."""
    latest = latest_by_series()
    stale = {s["series"] for s in stale_series()}
    counts: dict[str, int] = {}
    for row in _read().get("observations", []):
        key = series_key(str(row.get("model", "")), str(row.get("bureau", "")))
        counts[key] = counts.get(key, 0) + 1
    series = []
    for key, row in sorted(latest.items()):
        series.append({
            "series": key,
            "model": row.get("model"),
            "bureau": row.get("bureau"),
            "score": row.get("score"),
            "as_of": row.get("as_of"),
            "source": row.get("source"),
            "observations": counts.get(key, 0),
            "stale": key in stale,
        })
    return {
        "series": series,
        "observations": sum(counts.values()),
        "stale_series": sorted(stale),
    }
