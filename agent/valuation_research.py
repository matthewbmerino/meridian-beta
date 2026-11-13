"""Alternative-asset valuation research via the local Hermes agent.

Same substrate as ``hermes_bridge`` (subprocess, never HTTP, no gateway
session) but with a STRICT output contract: the reply must be a single
JSON object {value, value_low, value_high, as_of, source, confidence,
caveat} — the shape the alternatives page records as a mark. Kind-specific
prompts: watches resolve to a reference number and price against WatchCharts
(sales/index) with Chrono24 asks as ceiling; cards follow the PSA-cert +
aggregator sold-row procedure where the exact-grade latest recorded sale is
the mark (confidence ``none`` when no recorded sale — an unknown exit price is
illiquidity, never a number); property takes a signed contract price as the
mark (gross, not net proceeds) or same-building SOLD comps, never a list price;
vehicles take private-party/trade-in values for the exact trim, mileage and
options, with wholesale bids as context.

The mark is recorded by the CALLER (the router) with the research's own
source + as_of — every recorded figure stays defensible to its evidence.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

VALUATION_MODULE_VERSION = 7  # bump when the contract/debug machinery changes

_HERMES_CANDIDATES = (
    os.path.expanduser("~/.local/bin/hermes"),
    "/usr/local/bin/hermes",
    shutil.which("hermes") or "",
)

_MAX_QUESTION = 4000
_DEFAULT_TIMEOUT = 240


def _hermes_bin() -> str | None:
    for cand in _HERMES_CANDIDATES:
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


_WATCH_PROMPT = (
    "Value the watch for a portfolio mark. Item description: {description}.\\n"
    "{detail}Resolve the exact reference number from the description (never "
    "guess one — if the description cannot resolve to a single reference, "
    "return confidence none and say why in caveat). Then search WatchCharts "
    "and Chrono24 for the reference's market index / last recorded sales and "
    "current asks (asks are ceiling context, not sales). Note whether comps "
    "are watch-only or full set.\\n"
    "Reply with ONLY a single JSON object, no prose, no code fences:\\n"
    '{{"value": <number USD>, "value_low": <number>, "value_high": <number>, '
    '"as_of": "<YYYY-MM-DD>", "source": "<named sources + the specific figures read>", '
    '"confidence": "tight|wide|none", "caveat": "<one thing that would change this>"}}'
)

_CARD_PROMPT = (
    "Value a PSA-graded trading card for a portfolio mark. Card description: "
    "{description} Cert: {cert}.\\n"
    "{detail}Procedure: (1) resolve the exact card: year, brand/set, card "
    "number, parallel/variety; verify the PSA cert via psacard.com/cert/ when "
    "a cert number is given (Cloudflare-walled: read text after the check, "
    "don't conclude failure); (2) search card aggregators (130point, Sports "
    "Market Index) for RECORDED SALES of this card at this exact grade, "
    "querying with the words an eBay title would use (player + set + card "
    "number + grade); (3) the mark is the most recent recorded sale at the "
    "exact grade — a live ASK is not a sale. If no recorded sale at that "
    "grade exists, set \"value\" to 0, \"confidence\" to "
    "\"none\", and explain the illiquidity in \"caveat\" (pop evidence, "
    "nearest ladders). Never invent a number.\n"
    "Reply with ONLY a single JSON object, no prose, no code fences:\n"
    '{{"value": <number USD — 0 when unpriced>, "value_low": <number>, '
    '"value_high": <number>, "as_of": "<YYYY-MM-DD>", '
    '"source": "<named sources + what was read>", '
    '"confidence": "tight|wide|none", "caveat": "<why; what would change this>"}}'
)

_OTHER_PROMPT = (
    "Value the alternative asset for a portfolio mark. Description: "
    "{description}.\\n{detail}Search the open market for comparable recorded "
    "SALES (not asks). Reply with ONLY a single JSON object, no prose, no "
    "code fences:\\n"
    '{{"value": <number USD — 0 when unpriced>, "value_low": <number>, '
    '"value_high": <number>, "as_of": "<YYYY-MM-DD>", '
    '"source": "<named sources + what was read>", '
    '"confidence": "tight|wide|none", "caveat": "<why; what would change this>"}}'
)


_PROPERTY_PROMPT = (
    "Value a residential property for a portfolio mark. Property: {description}.\n"
    "{detail}Procedure: (1) if the property is UNDER CONTRACT, the signed "
    "contract price IS the mark — report it and name the contract; that is a "
    "gross price, NOT net proceeds after fees and concessions, so do not "
    "subtract costs; (2) otherwise find RECORDED SALES of comparable units in "
    "the same building, street or submarket (county deed/recorder records, "
    "Zillow/Redfin SOLD listings), adjusting only for factors you can source "
    "(square footage, floor, condition, sale date). A list price is an ASK, "
    "not a sale — never use one as the mark; (3) no recorded comparable = "
    "value 0, confidence none, and say in caveat that the mark cannot be "
    "anchored to a transaction. Never apply a renovation premium you cannot "
    "source to a completed sale.\n"
    "Reply with ONLY a single JSON object, no prose, no code fences:\n"
    '{{"value": <number USD — 0 when unpriced>, "value_low": <number>, '
    '"value_high": <number>, "as_of": "<YYYY-MM-DD>", '
    '"source": "<named sources + the specific figures read>", '
    '"confidence": "tight|wide|none", "caveat": "<why; what would change this>"}}'
)

_VEHICLE_PROMPT = (
    "Value a vehicle for a portfolio mark. Vehicle: {description}.\n"
    "{detail}Procedure: (1) pin the exact model year, trim, drivetrain and "
    "factory options from the description, and note options that carry resale "
    "value (e.g. Full Self-Driving purchased outright is an option; a "
    "subscription is not); (2) read recorded transaction / trade-in values — "
    "Kelley Blue Book, Black Book, Manheim sold data, or a CarMax/Carvana "
    "instant offer (an offer is a wholesale BID: context, not the private-party "
    "mark); comparable private-party LISTINGS are asks, not sales; (3) the mark "
    "is the private-party value for that mileage and option set. If no "
    "transactional source exists, return the strongest bid you actually read, "
    "confidence wide, and say in caveat that it is a bid, not a sale. Never "
    "invent a depreciation curve.\n"
    "Reply with ONLY a single JSON object, no prose, no code fences:\n"
    '{{"value": <number USD — 0 when unpriced>, "value_low": <number>, '
    '"value_high": <number>, "as_of": "<YYYY-MM-DD>", '
    '"source": "<named sources + the specific figures read>", '
    '"confidence": "tight|wide|none", "caveat": "<why; what would change this>"}}'
)


_PC_PART_PROMPT = (
    "Value a used PC component for a portfolio mark. Part: {description}.\n"
    "{detail}Procedure: (1) pin the exact model / part number and the variant "
    "that drives price (GPU board partner and cooler, CPU socket and SKU, "
    "motherboard chipset); (2) read RECORDED SALES on eBay's sold filter or a "
    "sold-price tracker for that exact part — an asking price is not a sale, and "
    "a 'for parts' listing is not a working comp; (3) for a quantity above one, "
    "value ONE unit and say so, because a lot clears slightly under the sum of "
    "singles; (4) if the market is thin or the variant is ambiguous, give the "
    "range you actually read and name what would firm it up. Never extrapolate "
    "a depreciation curve.\n"
    "Reply with ONLY a single JSON object, no prose, no code fences:\n"
    '{{"value": <number USD per unit — 0 when unpriced>, "value_low": <number>, '
    '"value_high": <number>, "as_of": "<YYYY-MM-DD>", '
    '"source": "<named sources + the specific figures read>", '
    '"confidence": "tight|wide|none", "caveat": "<why; what would change this>"}}'
)


def build_question(kind: str, description: str, detail: str | None, cert: str | None) -> str:
    """Kind-aware research question. Raises ValueError on unknown kind."""
    detail_block = f"Detail: {detail.strip()}\\n" if detail and detail.strip() else ""
    if kind == "watch":
        q = _WATCH_PROMPT.format(description=description, detail=detail_block)
    elif kind == "card":
        q = _CARD_PROMPT.format(
            description=description, detail=detail_block, cert=cert or "not recorded"
        )
    elif kind == "property":
        q = _PROPERTY_PROMPT.format(description=description, detail=detail_block)
    elif kind == "vehicle":
        q = _VEHICLE_PROMPT.format(description=description, detail=detail_block)
    elif kind == "pc_part":
        q = _PC_PART_PROMPT.format(description=description, detail=detail_block)
    elif kind == "other":
        q = _OTHER_PROMPT.format(description=description, detail=detail_block)
    else:
        raise ValueError(f"unknown kind: {kind}")
    return q[:_MAX_QUESTION]


_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)
_REQUIRED_KEYS = ("value", "as_of", "source", "confidence")


def parse_answer(output: str) -> dict:
    """Pull the JSON contract out of the CLI's chat-rendered reply.

    The _extract_answer-style box extraction (take the last ╭─ box), then
    a balanced-brace scan for the object. Raises ValueError when no
    contract-shaped object is present — a wrong shape is an error, never a
    fabricated mark.
    """
    starts = [m.start() for m in re.finditer("\u256d", output)]
    segment = output[starts[-1]:] if starts else output
    m = _JSON_OBJ.search(segment)
    if not m:
        # fall back to whole output — some paths print the answer raw
        m = _JSON_OBJ.search(output)
    if not m:
        raise ValueError("no JSON contract in hermes reply")
    text = m.group(0)
    # Find the balanced object starting at the first '{'
    obj = _balanced_json(text)
    if obj is None:
        raise ValueError("unparseable JSON contract in hermes reply")
    result = json.loads(obj)
    if not isinstance(result, dict):
        raise ValueError("contract is not an object")
    missing = [k for k in _REQUIRED_KEYS if k not in result]
    if missing:
        raise ValueError(f"contract missing keys: {missing}")
    if not isinstance(result.get("value"), (int, float)):
        raise ValueError("contract value is not a number")
    if result.get("confidence") not in ("tight", "wide", "none"):
        raise ValueError(f"bad confidence: {result.get('confidence')}")
    return result


def _balanced_json(text: str) -> str | None:
    try:
        start = text.index("{")
    except ValueError:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if esc:
            esc = False
            continue
        if in_str:
            if c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i+1]
    return None


def research_value(
    kind: str,
    description: str,
    detail: str | None = None,
    cert: str | None = None,
    timeout_s: int = _DEFAULT_TIMEOUT,
) -> dict:
    """Run the Hermes agent for this item and return the parsed contract.

    Returns the contract dict (+ duration_s + session). Raises ValueError
    on an unknown kind, an unreachable CLI, a timeout (recoverable), or a
    contract-violating reply — the caller renders the error; a wrong shape
    never becomes a mark.
    """
    question = build_question(kind, description, detail, cert)
    binary = _hermes_bin()
    if binary is None:
        raise ValueError("hermes CLI not found on this host")
    # Long-running systemd services (uvicorn) get a spartan env; hermes's
    # bootstrap dies instantly inside them with a misleading EROFS — while
    # the identical CLI under a systemd transient works every time. So we
    # ALWAYS spawn through a systemd-run --pipe transient: the output still
    # streams back to this process; the agent runs in a clean unit.
    home = os.environ.get("HOME", "/home/matthew") or "/home/matthew"
    # NoNewPrivileges on the meridian units blocks sudo; the user manager
    # (lingering enabled) runs transients without any privilege dance — and
    # its env is the operator's, so hermes's bootstrap works.
    systemd_cmd = [
        "systemd-run", "--user", "--pipe", "--wait",
        "-p", f"WorkingDirectory={home}",
        "-p", f"Environment=HOME={home}",
        binary, "chat", "-q", question,
    ]
    child_env = {
        **os.environ,
        "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
    }
    try:
        r = subprocess.run(
            systemd_cmd,
            capture_output=True, text=True, timeout=timeout_s,
            env=child_env,
        )
    except subprocess.TimeoutExpired:
        raise ValueError(f"research timed out after {timeout_s}s — retry; deep comps may need longer")
    except Exception as e:
        raise ValueError(f"hermes research failed: {str(e)[:200]}")
    output = (r.stdout or "") + (r.stderr or "")
    if not output.strip():
        raise ValueError("hermes returned no output")
    try:
        result = parse_answer(output)
    except ValueError:
        import datetime as _dt
        try:
            Path("/tmp/hermes_reply").write_text(
                _dt.datetime.now(_dt.timezone.utc).isoformat() + "\n" + output[-6000:], encoding="utf-8")
        except OSError:
            pass
        log.warning("hermes valuation reply not contract-shaped: %s", output[-800:])
        raise
    session_m = re.search(r"Session:\s+(\S+)", output)
    result["session"] = session_m.group(1) if session_m else None
    dur_m = re.search(r"Duration:\s+([\dms\.]+)", output)
    result["duration_s"] = dur_m.group(1) if dur_m else None
    return result
