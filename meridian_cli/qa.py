"""End-to-end checks an agent can run after install, setup, or update.

`doctor` answers "is this install healthy?"; `qa` answers "does the product
actually work end to end?" It reads the book, runs the risk math on real price
history. Every check is read-only.

An agent should run this before answering questions, after a setup, and after
an update. A failed check is a report, not a patch to the installed app.
"""

from __future__ import annotations

import math
import time

from . import home, net, ui


def _finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _pct(value) -> float:
    """VaR is a fraction; Monte Carlo stress is already a percent."""
    number = float(value)
    return number * 100 if abs(number) <= 1 else number


def _count(value) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        for key in ("items", "results", "data", "scenarios"):
            if isinstance(value.get(key), list):
                return len(value[key])
    return 0


def _check(name: str, status: str, detail: str, ms: int = 0, hint: str = "") -> dict:
    return {"name": name, "status": status, "detail": detail, "ms": ms, "hint": hint}


def _timed(fn):
    started = time.perf_counter()
    try:
        return fn(), int((time.perf_counter() - started) * 1000), None
    except Exception as exc:  # noqa: BLE001 - report the failure, do not crash QA
        return None, int((time.perf_counter() - started) * 1000), exc


def _emit(checks: list[dict], version: str, channel: str, json_mode: bool) -> int:
    failures = [c for c in checks if c["status"] == "fail"]
    warnings = [c for c in checks if c["status"] == "warn"]
    if json_mode:
        ui.json_out({
            "ok": not failures,
            "version": version,
            "channel": channel,
            "checks": checks,
            "failures": len(failures),
            "warnings": len(warnings),
        })
        return 1 if failures else 0

    print()
    suffix = f" · {channel}" if channel != "stable" else ""
    ui.banner("Meridian Beta · QA", f"{version}{suffix}")
    icons = {"ok": ui.green("✓"), "warn": ui.yellow("!"), "fail": ui.red("✗")}
    for check in checks:
        ms = f" · {check['ms']}ms" if check.get("ms") else ""
        print(f"  {icons[check['status']]} {check['name']:<12} {check['detail']}{ui.dim(ms)}")
        if check.get("hint") and check["status"] != "ok":
            print(f"    {ui.dim('→ ' + check['hint'])}")
    print()
    if failures:
        print(ui.red(f"  {len(failures)} check(s) failed. File a report: meridian report --kind bug"))
    elif warnings:
        print(ui.yellow(f"  {len(warnings)} warning(s). The desk runs; note them in a report if they matter."))
    else:
        print(ui.green("  Everything an operator needs is working."))
    print()
    return 1 if failures else 0


def run(cfg: dict, json_mode: bool = False) -> int:
    version = home.read_version()
    channel = home.read_channel()
    checks: list[dict] = []

    health = net.health(cfg)
    if not health:
        checks.append(_check("api", "fail", "no answer on /api/health",
                             hint="meridian start; then meridian logs api"))
        return _emit(checks, version, channel, json_mode)
    checks.append(_check("api", "ok", f"healthy on {home.api_url(cfg)}"))

    # Auth: a bearer token is what the agent uses; a password is what the
    # operator uses. Both should be present on a configured desk.
    data, ms, error = _timed(lambda: net.get(cfg, "/api/auth/status", timeout=5))
    if error:
        checks.append(_check("auth", "fail", f"could not read auth status: {error}", ms,
                             "meridian doctor"))
    elif not isinstance(data, dict):
        checks.append(_check("auth", "fail", "auth status is not an object", ms,
                             "meridian report --context api --kind bug"))
    elif data.get("locked"):
        checks.append(_check("auth", "fail", "no password set", ms, "meridian passwd"))
    elif data.get("authenticated"):
        checks.append(_check("auth", "ok", "password set · session authenticated", ms))
    else:
        # /api/auth/status reports cookie sessions, not bearer use; the fact
        # that this call succeeded proves the CLI token was accepted.
        checks.append(_check("auth", "ok", "password set · API token accepted", ms))

    positions, ms, error = _timed(lambda: net.get(cfg, "/api/accounts/positions", timeout=30))
    if error:
        checks.append(_check("positions", "fail", f"could not read positions: {error}", ms,
                             "meridian logs api"))
        positions = []
    elif not isinstance(positions, list):
        checks.append(_check("positions", "fail", "positions response is not a list", ms,
                             "meridian report --context api --kind bug"))
        positions = []
    else:
        priced = sum(1 for row in positions
                     if _finite(row.get("current_price")) and float(row.get("current_price")) > 0)
        stale = sum(1 for row in positions if row.get("stale"))
        if not positions:
            checks.append(_check("positions", "warn", "no positions imported", ms,
                                 "meridian import positions.csv"))
        elif not priced:
            checks.append(_check("positions", "warn",
                                 f"{len(positions)} positions · none priced", ms,
                                 "check market data: meridian doctor"))
        else:
            checks.append(_check("positions", "ok",
                                 f"{len(positions)} positions · {priced} priced · {stale} stale", ms))

    summary, ms, error = _timed(lambda: net.get(cfg, "/api/accounts/summary", timeout=15))
    if error:
        checks.append(_check("summary", "fail", f"could not read summary: {error}", ms,
                             "meridian logs api"))
    elif not isinstance(summary, dict) or not _finite(summary.get("net_liquidation")):
        checks.append(_check("summary", "fail", "summary has no net_liquidation", ms,
                             "meridian report --context api --kind bug"))
    else:
        checks.append(_check("summary", "ok",
                             f"NAV {float(summary['net_liquidation']):,.0f} · "
                             f"cash {float(summary.get('cash_balance') or 0):,.0f}", ms))

    # VaR is the cheapest end-to-end proof that quotes *and* history work.
    if positions:
        rows = sorted(
            [row for row in positions if _finite(row.get("market_value")) and float(row["market_value"]) > 0],
            key=lambda row: float(row["market_value"]), reverse=True,
        )[:20]
        total = sum(float(row["market_value"]) for row in rows)
        if total > 0:
            body = {
                "symbols": [str(row.get("symbol")) for row in rows],
                "weights": [float(row["market_value"]) / total for row in rows],
                "confidence": 0.95,
            }
            data, ms, error = _timed(lambda: net.post(cfg, "/api/analysis/var", body, timeout=90))
            if error:
                checks.append(_check("var", "fail", f"VaR failed: {error}", ms,
                                     "meridian report --context api --kind bug"))
            elif not isinstance(data, dict) or not _finite(data.get("var_95")):
                checks.append(_check("var", "fail", "VaR returned no var_95", ms,
                                     "meridian report --context api --kind bug"))
            else:
                coverage = data.get("coverage")
                extra = ""
                if isinstance(coverage, dict) and coverage.get("requested"):
                    extra = f" · {coverage.get('resolved')}/{coverage.get('requested')} resolved"
                checks.append(_check("var", "ok",
                                     f"{len(rows)} symbols · VaR95 {_pct(data['var_95']):.2f}%{extra}", ms))
        else:
            checks.append(_check("var", "warn", "skipped: positions have no market value"))
    else:
        checks.append(_check("var", "warn", "skipped: no positions"))

    data, ms, error = _timed(lambda: net.post(cfg, "/api/analysis/stress", {}, timeout=120))
    if error:
        checks.append(_check("stress", "fail", f"stress failed: {error}", ms,
                             "meridian report --context api --kind bug"))
    elif not isinstance(data, dict):
        checks.append(_check("stress", "fail", "stress response is not an object", ms,
                             "meridian report --context api --kind bug"))
    else:
        scenarios = _count(data.get("scenarios"))
        mc = data.get("monte_carlo") or {}
        if scenarios and _finite(mc.get("var_95")):
            checks.append(_check("stress", "ok",
                                 f"{scenarios} scenarios · MC VaR95 {_pct(mc['var_95']):.2f}%", ms))
        else:
            checks.append(_check("stress", "warn", "no scenarios returned", ms,
                                 "needs at least one position and price history"))

    for name, path in (("liabilities", "/api/liabilities"), ("theses", "/api/theses")):
        data, ms, error = _timed(lambda p=path: net.get(cfg, p, timeout=15))
        if error:
            checks.append(_check(name, "warn", f"could not read: {error}", ms))
        else:
            checks.append(_check(name, "ok", f"{_count(data)} entries", ms))

    data, ms, error = _timed(lambda: net.get(cfg, "/api/decisions?limit=50", timeout=15))
    if error:
        checks.append(_check("decisions", "warn", f"could not read: {error}", ms))
    else:
        checks.append(_check("decisions", "ok", f"{_count(data)} entries", ms))

    data, ms, error = _timed(lambda: net.get(cfg, "/api/meta/data-health", timeout=15))
    if error:
        checks.append(_check("data health", "warn", f"could not read: {error}", ms))
    else:
        checks.append(_check("data health", "ok", "reported", ms))

    return _emit(checks, version, channel, json_mode)
