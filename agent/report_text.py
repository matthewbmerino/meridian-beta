"""Pure text utilities for persisted research reports.

The report writer (a streaming LLM) sometimes narrates its own planning before
producing the report proper — "I have the full picture ... Writing the report
now." — and that narration was being persisted as the first lines of the
report and shown as its summary. It must never reach the store.

The writer is also instructed to end every report with a fenced json action
block; this module parses it into a structured item. Everything here is pure
text handling: no data imports, no network, trivially testable.
"""

from __future__ import annotations

import json
import re

# A line that is a markdown heading ("# ...", "## ...") at the start of a line.
_HEADING_RE = re.compile(r"^#{1,3}\s+\S", re.M)

# Fenced json blocks, language tag optional.
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S | re.I)

# Phrases that only ever appear in the writer's planning narration.
_NARRATION_MARKERS = (
    "i have the full picture",
    "writing the report now",
    "i have everything needed",
    "let me gather",
    "let me now",
    "now i have everything",
    "i now have everything",
    "with all the data",
)


def strip_narration(content: str) -> str:
    """Drop the writer's pre-report planning narration.

    Everything before the first markdown heading is narration — but only when
    that pre-heading chunk actually looks like narration: short (<= 800 chars)
    AND either ends with a horizontal rule or carries a narration phrase. A
    report without headings, or one that opens with a real lede, passes
    through untouched (never eat a report to satisfy a heuristic).
    """
    if not content:
        return content
    m = _HEADING_RE.search(content)
    if not m or m.start() == 0:
        return content
    pre = content[: m.start()].strip()
    if not pre or len(pre) > 800:
        return content
    lowered = pre.lower()
    looks_like_narration = any(p in lowered for p in _NARRATION_MARKERS) or pre.rstrip().endswith("---")
    if not looks_like_narration:
        return content
    return content[m.start():].lstrip()


def extract_actions(content: str) -> list[dict]:
    """Parse the report's trailing action block into structured items.

    Returns [] when there is no block, the block is not valid json, or it has
    no 'recommendation' key — absence is a reason, never a fabricated action.
    """
    if not content:
        return []
    for raw in reversed(_FENCE_RE.findall(content)):
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and obj.get("recommendation"):
            return [obj]
    return []


# Hosts whose pages are user-generated or promotional. A citation from one of
# these is a search RESULT the writer never opened — never evidence.
_UGC_HOSTS = (
    "facebook.com", "fb.com", "linkedin.com", "x.com", "twitter.com",
    "tiktok.com", "instagram.com", "pinterest.", "quora.com", "reddit.com",
    "youtube.com", "medium.com",
)


def clean_citations(urls: list[str] | None, limit: int = 40) -> list[str]:
    """Dedupe and de-junk a report's citation list, preserving order.

    `citations` is collected from every search RESULT the tools returned, not
    from what the report actually cites, so one search that fell through to an
    off-topic page donates its whole result set to the source list. Live case: a
    Fed-timing report shipped 39 sources including a Wikipedia "Market"
    definition, a city farmers-market page and a Facebook video. Keep order,
    drop the classes that can never support a claim (bare domain roots, social
    and UGC hosts, repeats).

    The cap is a RUNAWAY guard, not a curation device: at 30 it dropped three
    real calendar pages from a 38-source report purely because they sat past
    position 30, and deleting a relevant source to hit a number is worse than a
    long list. Relevance is the writer's judgement, not this function's.
    """
    out: list[str] = []
    for raw in urls or []:
        if not isinstance(raw, str):
            continue
        url = raw.strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        host, _, rest = url.split("://", 1)[1].partition("/")
        host = host.lower()
        if any(h in host for h in _UGC_HOSTS):
            continue
        if not rest.strip("/"):
            continue  # a bare domain is not a source
        if url in out:
            continue
        out.append(url)
        if len(out) >= limit:
            break
    return out


def first_paragraph(content: str, limit: int = 280) -> str:
    """The report's opening paragraph after its title — used as a list summary.

    Skips headings and the markdown title, returns the first substantive
    paragraph, truncated. '' when there is nothing substantive yet.
    """
    if not content:
        return ""
    out: list[str] = []
    for block in re.split(r"\n\s*\n", content):
        b = block.strip()
        if not b or b.startswith("#") or b.startswith("---") or b.startswith("```"):
            continue
        out.append(re.sub(r"[*_`]", "", b).replace("\n", " "))
        if sum(len(x) for x in out) >= limit:
            break
    return " ".join(out)[:limit].strip()
