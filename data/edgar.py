"""SEC EDGAR Form 4 insider activity data — free public API, no key required."""

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import requests

from data.cache import ttl_cache

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_AGENT = "Meridian Portfolio Dashboard contact@meridian.app"
HEADERS = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}
TIMEOUT = 5

TRANSACTION_CODES = {
    "P": "Purchase",
    "S": "Sale",
    "A": "Award",
    "M": "Exercise",
    "G": "Gift",
    "F": "Tax",
    "C": "Conversion",
    "D": "Disposition",
    "J": "Other",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

@ttl_cache(ttl_seconds=86400)
def _get_cik_map():
    """Fetch SEC ticker→CIK mapping. Cached 24h."""
    resp = requests.get(
        "https://www.sec.gov/files/company_tickers.json",
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        entry["ticker"].upper(): int(entry["cik_str"])
        for entry in data.values()
    }


@ttl_cache(ttl_seconds=3600)
def _get_submissions(cik):
    """Fetch filing submissions for a CIK. Cached 1h. Returns Form 4 filings."""
    padded = str(cik).zfill(10)
    resp = requests.get(
        f"https://data.sec.gov/submissions/CIK{padded}.json",
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    dates = recent.get("filingDate", [])
    docs = recent.get("primaryDocument", [])

    filings = []
    for i, form in enumerate(forms):
        if form == "4":
            filings.append({
                "accession": accessions[i],
                "filing_date": dates[i],
                "primary_doc": docs[i],
            })
    return filings


@ttl_cache(ttl_seconds=86400)
def _parse_form4(cik, accession):
    """Parse a Form 4 XML filing. Cached 24h (filings are immutable)."""
    xml_url = _find_xml_url(cik, accession)
    if xml_url is None:
        return None

    resp = requests.get(xml_url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        return None

    reporter_name, role = _parse_reporter(root)
    return {
        "reporter": reporter_name,
        "role": role,
        "transactions": _parse_transactions(root),
    }


def _find_xml_url(cik, accession) -> str | None:
    """Fetch the filing index page and return the URL of the Form 4 XML doc.

    The index page lists several .xml files (schema, primary_doc, etc.); the
    actual filing is usually the first non-schema, non-primary document.
    """
    acc_no_dashes = accession.replace("-", "")
    index_url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_no_dashes}/"
    )

    resp = requests.get(index_url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    index_html = resp.text

    # Look for the XML filing link (usually ends in .xml, contains "form4" or similar)
    xml_files = re.findall(r'href="([^"]+\.xml)"', index_html, re.IGNORECASE)
    if not xml_files:
        return None

    # Pick the first XML that isn't the primary_doc.xml schema file
    xml_filename = None
    for f in xml_files:
        fname = f.split("/")[-1].lower()
        if fname.startswith("r") or "primary" in fname:
            continue
        xml_filename = f
        break
    if xml_filename is None:
        xml_filename = xml_files[0]

    # Build full URL — handle both relative and absolute paths
    if xml_filename.startswith("http"):
        return xml_filename
    if xml_filename.startswith("/"):
        return f"https://www.sec.gov{xml_filename}"
    return f"{index_url}{xml_filename}"


def _parse_reporter(root):
    """Extract the reporting owner's name and role from a Form 4 root."""
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    reporter_el = root.find(f".//{ns}reportingOwner")
    reporter_name = ""
    is_director = False
    is_officer = False
    officer_title = ""

    if reporter_el is not None:
        id_el = reporter_el.find(f"{ns}reportingOwnerId")
        if id_el is not None:
            name_el = id_el.find(f"{ns}rptOwnerName")
            if name_el is not None and name_el.text:
                reporter_name = name_el.text.strip()

        rel_el = reporter_el.find(f"{ns}reportingOwnerRelationship")
        if rel_el is not None:
            dir_el = rel_el.find(f"{ns}isDirector")
            if dir_el is not None and dir_el.text:
                is_director = dir_el.text.strip() in ("1", "true")
            off_el = rel_el.find(f"{ns}isOfficer")
            if off_el is not None and off_el.text:
                is_officer = off_el.text.strip() in ("1", "true")
            title_el = rel_el.find(f"{ns}officerTitle")
            if title_el is not None and title_el.text:
                officer_title = title_el.text.strip()

    return reporter_name, _role_string(is_director, is_officer, officer_title)


def _parse_transactions(root) -> list[dict]:
    """Parse the non-derivative transactions of a Form 4 root element."""
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    transactions = []
    for txn in root.findall(f".//{ns}nonDerivativeTransaction"):
        date_el = txn.find(f".//{ns}transactionDate/{ns}value")
        code_el = txn.find(f".//{ns}transactionCoding/{ns}transactionCode")
        shares_el = txn.find(f".//{ns}transactionAmounts/{ns}transactionShares/{ns}value")
        price_el = txn.find(f".//{ns}transactionAmounts/{ns}transactionPricePerShare/{ns}value")
        ad_el = txn.find(f".//{ns}transactionAmounts/{ns}transactionAcquiredDisposedCode/{ns}value")

        txn_date = date_el.text.strip() if date_el is not None and date_el.text else ""
        txn_code = code_el.text.strip() if code_el is not None and code_el.text else ""

        try:
            shares = float(shares_el.text.strip()) if shares_el is not None and shares_el.text else 0
        except ValueError:
            shares = 0
        try:
            price = float(price_el.text.strip()) if price_el is not None and price_el.text else 0
        except ValueError:
            price = 0

        direction = ad_el.text.strip() if ad_el is not None and ad_el.text else ""

        transactions.append({
            "date": txn_date,
            "code": txn_code,
            "shares": shares,
            "price": price,
            "direction": direction,
        })

    return transactions


def _role_string(is_director, is_officer, officer_title):
    """Convert relationship flags to a display string."""
    if is_officer and officer_title:
        return officer_title
    if is_officer:
        return "Officer"
    if is_director:
        return "Director"
    return "Insider"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_insider_activity(symbol, months=6):
    """Fetch and aggregate insider activity from SEC Form 4 filings.

    Returns a dict with buy/sell totals, net sentiment, transactions list,
    and insider roles — or None if data unavailable.
    """
    symbol = symbol.upper()

    try:
        cik_map = _get_cik_map()
    except Exception as e:
        log.warning("Failed to fetch SEC CIK map: %s", e)
        return None

    cik = cik_map.get(symbol)
    if cik is None:
        return None

    try:
        filings = _get_submissions(cik)
    except Exception as e:
        log.warning("Failed to fetch SEC submissions for %s (CIK %s): %s", symbol, cik, e)
        return None

    if not filings:
        return None

    # Filter to lookback window
    cutoff = (datetime.now() - timedelta(days=months * 30)).strftime("%Y-%m-%d")
    recent_filings = [f for f in filings if f["filing_date"] >= cutoff][:20]

    if not recent_filings:
        return None

    # Parse each filing
    all_transactions = []
    insiders = {}

    for filing in recent_filings:
        try:
            parsed = _parse_form4(cik, filing["accession"])
        except Exception as e:
            log.debug("Failed to parse Form 4 filing %s for %s: %s", filing["accession"], symbol, e)
            continue
        if parsed is None:
            continue

        reporter = parsed["reporter"]
        role = parsed["role"]
        if reporter:
            insiders[reporter] = role

        for txn in parsed["transactions"]:
            code = txn["code"]
            shares = txn["shares"]
            price = txn["price"]
            direction = txn["direction"]

            # Determine human-readable direction
            if code == "P" and direction == "A":
                dir_label = "Buy"
            elif code == "S" and direction == "D":
                dir_label = "Sell"
            else:
                dir_label = TRANSACTION_CODES.get(code, code)

            all_transactions.append({
                "date": txn["date"],
                "reporter": reporter,
                "role": role,
                "code": code,
                "shares": shares,
                "price": price,
                "value": round(shares * price, 2),
                "direction": dir_label,
            })

    if not all_transactions:
        return None

    # Sort by date descending
    all_transactions.sort(key=lambda t: t["date"], reverse=True)

    # Aggregate buy/sell totals (only actual purchases and sales)
    total_buys = 0
    total_sells = 0
    buy_value = 0.0
    sell_value = 0.0

    for txn in all_transactions:
        if txn["code"] == "P" and txn["direction"] == "Buy":
            total_buys += txn["shares"]
            buy_value += txn["value"]
        elif txn["code"] == "S" and txn["direction"] == "Sell":
            total_sells += txn["shares"]
            sell_value += txn["value"]

    net_shares = total_buys - total_sells
    if net_shares > 0:
        net_sentiment = "positive"
    elif net_shares < 0:
        net_sentiment = "negative"
    else:
        net_sentiment = "neutral"

    return {
        "symbol": symbol,
        "total_buys": int(total_buys),
        "total_sells": int(total_sells),
        "buy_value": round(buy_value, 2),
        "sell_value": round(sell_value, 2),
        "net_shares": int(net_shares),
        "net_sentiment": net_sentiment,
        "transactions": all_transactions,
        "insiders": insiders,
    }
