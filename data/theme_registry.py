"""Theme alias/canonical resolution — the registry keeps names from fracturing.

Grouping by exact string is how "AI Power & Compute", "Power" and "Power &
AI Infrastructure" end up as three 8%-buckets. ``canonical_theme`` resolves
a stored (or freshly typed) name through the alias table to the operator's
canonical set; unknown names pass through untouched so a genuinely new
theme still works (free-text entry is a feature, not a bug).

The alias table is DATA, not law — the operator re-cuts it whenever their
thesis structure changes. Migration (scripts/migrate_theme_aliases.py)
rewrites stored strings once; resolution at group time keeps any survivor
legacy string grouping correctly anyway.
"""

from __future__ import annotations

# alias (any casing/spacing) -> canonical theme name.
# Canonical names are the keys of the VALUES; keep them stable — they are
# what the aggregation rows, ThesisPanel datalist, and journal prose use.
THEME_ALIASES: dict[str, str] = {
    # Power & AI Infrastructure
    "power": "Power & AI Infrastructure",
    "ai power & compute": "Power & AI Infrastructure",
    "ai power and compute": "Power & AI Infrastructure",
    "energy infrastructure": "Power & AI Infrastructure",
    # Semis
    "semiconductors": "Semis",
    "ai memory": "Semis",
    # Compute & Cloud
    "compute & cloud": "Compute & Cloud",
    "cloud & compute": "Compute & Cloud",
    "compute and cloud": "Compute & Cloud",
    "cloud and compute": "Compute & Cloud",
    "compute & launch": "Compute & Cloud",
    "compute and launch": "Compute & Cloud",
    # Bitcoin — already canonical; aliasing the lowercase form is harmless
    "bitcoin": "Bitcoin",
}


def _key(name: str | None) -> str:
    """Collapse a theme name to its lookup key: trimmed, lowercase, single
    spaces, 'and' normalized to '&' — so 'AI Power and Compute' and
    'ai power & compute' resolve identically."""
    if name is None:
        return ""
    s = " ".join(str(name).split()).lower()
    s = s.replace(" and ", " & ")
    return s.strip()


def canonical_theme(name: str | None) -> str:
    """Resolve a theme name to its canonical form.

    Unknown names pass through whitespace-normalized (never fabricated into
    a different theme). Empty/None comes back as an empty string — callers
    treat that as 'no theme'.
    """
    if name is None:
        return ""
    raw = " ".join(str(name).split())
    if not raw:
        return ""
    return THEME_ALIASES.get(_key(raw), raw)


def canonical_theme_set() -> list[str]:
    """The distinct canonical names (sorted) — ThesisPanel's datalist source."""
    return sorted(set(THEME_ALIASES.values()))
