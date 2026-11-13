"""Institutional research registry — who counts as an analyst worth listening to.

An unfiltered consensus averages everyone who publishes: a bulge-bracket desk that
carries the name for years and a one-person shop that upgrades everything. The mean of
those two is not a view of anything. This registry names the houses, tiers them, and
resolves the messy spellings the data actually contains.

Tiers (the operator-facing criteria, published with every payload so the filter is
auditable rather than a black box):

  tier 1 — GLOBAL / BULGE BRACKET
      The large sell-side research desks. Coverage is continuous, the analyst is named,
      and the target is a published estimate with a dated revision.

  tier 2 — MAJOR INDEPENDENT / REGIONAL INSTITUTIONAL
      Established research houses and regional investment banks: independent research
      (Bernstein, Evercore ISI, New Street, MoffettNathanson, Wolfe), the larger
      regionals (Raymond James, Baird, Stifel, Truist, KeyBanc, Piper Sandler), and the
      long-standing boutiques that publish real estimates.

  tier 3 — OTHER / UNVETTED
      Everything else, including shops with a single publishing name and firms whose
      coverage is marketing rather than research. These are KEPT AND LISTED, never
      silently dropped — they are excluded from the vetted consensus and shown by name
      so the reader can see exactly what was set aside and disagree with the call.

`VETTED_TIERS` is the default filter: tiers 1 and 2. `resolve()` is the only way a firm
string should be turned into a tier — a bare dict lookup misses the spellings below,
which are real strings observed in the provider feed ("JP Morgan", "B of A Securities",
"Keybanc", "Cowen & Co." alongside "TD Cowen").
"""

from __future__ import annotations

from typing import NamedTuple

VETTED_TIERS = (1, 2)

TIER_LABELS = {
    1: "Global / bulge bracket",
    2: "Major independent / regional institutional",
    3: "Other / unvetted",
}

# canonical name -> (tier, aliases...). Aliases are lowercase-compared; the canonical
# string is what the operator sees in the panel and the table.
_REGISTRY: dict[str, tuple[int, tuple[str, ...]]] = {
    # ---------------------------------------------------------------- tier 1
    "Goldman Sachs": (1, ("goldman", "goldman sachs group")),
    "Morgan Stanley": (1, ()),
    "JPMorgan": (1, ("jp morgan", "j.p. morgan", "jpmorgan chase", "jpm")),
    "BofA Securities": (1, ("b of a securities", "bank of america", "merrill lynch", "bofa", "bac")),
    "Citigroup": (1, ("citi", "citibank")),
    "Barclays": (1, ()),
    "UBS": (1, ("ubs securities", "union bank of switzerland")),
    "Deutsche Bank": (1, ()),
    "Wells Fargo": (1, ("wells fargo securities",)),
    "HSBC": (1, ("hsbc securities",)),
    "BNP Paribas": (1, ("bnpp", "exane bnp paribas", "exane")),
    "RBC Capital": (1, ("rbc", "rbc capital markets", "royal bank of canada")),
    "BMO Capital": (1, ("bmo", "bmo capital markets", "bank of montreal")),
    "TD Cowen": (1, ("td securities", "cowen", "cowen & co.", "cowen and co")),
    "Scotiabank": (1, ("bank of nova scotia", "scotia capital")),
    "Mizuho": (1, ("mizuho securities",)),
    "Nomura": (1, ("nomura securities", "instinet")),
    "Macquarie": (1, ("macquarie research",)),
    "Jefferies": (1, ("jefferies llc",)),
    "Evercore ISI": (1, ("evercore", "evercore isi group", "isi group")),
    "Bernstein": (1, ("ab bernstein", "sanford c. bernstein", "bernstein research", "sg bernstein")),
    "Credit Suisse": (1, ("credit suisse securities",)),
    "Daiwa Capital": (1, ("daiwa", "daiwa securities")),
    "CLSA": (1, ("clsa americas", "credit lyonnais securities")),
    # ---------------------------------------------------------------- tier 2
    "Argus Research": (2, ("argus",)),
    "CFRA": (2, ("cfra research", "standard & poor's cfra")),
    "Morningstar": (2, ("morningstar research",)),
    "Raymond James": (2, ("raymond james financial", "rjf")),
    "Piper Sandler": (2, ("piper jaffray", "piper")),
    "Stifel": (2, ("stifel nicolaus", "stifel financial")),
    "Robert W. Baird": (2, ("baird", "rw baird", "robert baird")),
    "Truist Securities": (2, ("truist", "suntrust robinson humphrey", "suntrust")),
    "KeyBanc": (2, ("keybanc capital markets", "key banc", "keycorp")),
    "Oppenheimer": (2, ("oppenheimer & co", "oppenheimer & co.")),
    "Needham": (2, ("needham & company", "needham & co")),
    "William Blair": (2, ("william blair & company",)),
    "Canaccord Genuity": (2, ("canaccord",)),
    "Wedbush": (2, ("wedbush securities", "wedbush morgan")),
    "Susquehanna": (2, ("susquehanna financial", "sig")),
    "Wolfe Research": (2, ("wolfe",)),
    "Melius Research": (2, ("melius",)),
    "MoffettNathanson": (2, ("moffett nathanson",)),
    "Guggenheim": (2, ("guggenheim securities", "guggenheim partners")),
    "Loop Capital": (2, ("loop capital markets",)),
    "Seaport Global": (2, ("seaport research", "seaport global securities")),
    "Cantor Fitzgerald": (2, ("cantor",)),
    "New Street Research": (2, ("new street",)),
    "Redburn Atlantic": (2, ("redburn", "redburn partners", "redburn atlantic llc")),
    "Vertical Research": (2, ("vertical research partners",)),
    "Longbow Research": (2, ("longbow",)),
    "Benchmark": (2, ("benchmark company", "the benchmark company")),
    "D.A. Davidson": (2, ("da davidson", "d.a. davidson & co")),
    "Rosenblatt": (2, ("rosenblatt securities",)),
    "Roth MKM": (2, ("roth capital", "roth capital partners", "mkm partners", "roth mkm partners")),
    "B. Riley Securities": (2, ("b. riley", "b riley", "b. riley financial")),
    "Pivotal Research": (2, ("pivotal research group",)),
    "Citizens JMP": (2, ("citizens", "jmp securities", "citizens jmp securities")),
    "Edward Jones": (2, ()),
    "Stephens": (2, ("stephens inc",)),
    "Janney Montgomery Scott": (2, ("janney",)),
    "Northland Securities": (2, ("northland capital markets",)),
    "H.C. Wainwright": (2, ("hc wainwright", "h.c. wainwright & co", "hc wainwright & co.")),
    "BTIG": (2, ("btig llc", "btig research")),
    "Keefe, Bruyette & Woods": (2, ("keefe bruyette & woods", "keefe, bruyette & woods, inc.", "kbw")),
    "Ladenburg Thalmann": (2, ("ladenburg", "ladenburg thalmann & co")),
    "Summit Insights Group": (2, ("summit insights",)),
    "Clear Street": (2, ()),
}

_ALIASES: dict[str, str] = {}
for canonical, (_tier, aliases) in _REGISTRY.items():
    _ALIASES[canonical.lower()] = canonical
    for alias in aliases:
        _ALIASES[alias.lower().strip()] = canonical


class Institution(NamedTuple):
    """A resolved firm: the name we display, its tier, and whether it is vetted."""

    canonical: str
    tier: int
    vetted: bool
    known: bool


def _normalize(name: str) -> str:
    return " ".join((name or "").lower().replace("&amp;", "&").split())


def resolve(firm: str | None) -> Institution:
    """Resolve a provider firm string to the registry's canonical entry.

    An unknown firm is NOT dropped and NOT assumed reputable: it comes back named as
    itself, tier 3, unvetted, so every excluded house stays visible in the payload.
    """
    raw = (firm or "").strip()
    canonical = _ALIASES.get(_normalize(raw))
    if canonical:
        tier, _ = _REGISTRY[canonical]
        return Institution(canonical=canonical, tier=tier, vetted=tier in VETTED_TIERS, known=True)
    return Institution(canonical=raw or "Unknown", tier=3, vetted=False, known=False)


def tier_label(tier: int) -> str:
    return TIER_LABELS.get(tier, TIER_LABELS[3])


def registry() -> list[dict]:
    """The whole registry, ordered by tier — what the panel prints so the filter is
    inspectable rather than magic."""
    rows = [
        {"name": name, "tier": tier, "tier_label": tier_label(tier), "vetted": tier in VETTED_TIERS}
        for name, (tier, _) in _REGISTRY.items()
    ]
    rows.sort(key=lambda r: (r["tier"], r["name"]))
    return rows


def criteria() -> str:
    return (
        "Vetted = tier 1 (global/bulge-bracket desks) and tier 2 (major independent and "
        "regional institutional research). Tier 3 firms are named and excluded from the "
        "vetted consensus, never hidden."
    )
