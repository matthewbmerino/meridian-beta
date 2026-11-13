"""APScheduler background jobs: sonar scan, alerts, GPU snapshots."""

import datetime
import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def _scan_all_universes(tag: str) -> None:
    """Scan every configured universe and publish results to the cache.

    Shared by the 15-minute sonar job and the startup warm-up so the two paths
    can't drift (they previously duplicated this loop almost line-for-line).
    """
    from backend.routers.sonar import _run_scan, update_scan_cache
    from data.universe import UNIVERSES

    for universe_name, loader in UNIVERSES.items():
        symbols = loader()
        results = _run_scan(symbols)
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        update_scan_cache(universe_name, results, ts)
        logger.info("Sonar %s complete: %s — %d symbols", tag, universe_name, len(results))


def _run_sonar_scan():
    now = datetime.datetime.now()
    if now.weekday() >= 5:
        return
    if not (9 <= now.hour <= 16):
        return

    try:
        _scan_all_universes("scan")
    except Exception as e:
        logger.warning("Sonar scan failed: %s", e)


def _run_alert_scan():
    """Detect regime/weather shifts, STBR warnings, earnings proximity.

    Single-operator: reads global data directly — no session binding.
    Alerts are global (data/alerts.py).
    """
    now = datetime.datetime.now()
    if now.weekday() >= 5:
        return
    if not (9 <= now.hour <= 16):
        return

    try:
        _alert_scan_body()
    except Exception as e:
        logger.warning("Alert scan failed: %s", e)


def _alert_scan_body():
    """The actual alert scan (global data, no session binding)."""
    from data.account import get_combined_positions

    # Each concern is a separate function so one failing concern can't abort
    # the others; per-concern error handling lives at that function's level.
    positions = get_combined_positions()
    _scan_composite_alerts(positions)
    _scan_stbr_alerts(positions)
    _scan_entry_alerts(positions)
    _scan_earnings_alerts(positions)
    _scan_thesis_alerts()


def _scan_composite_alerts(positions) -> None:
    """Fire weather/regime shift alerts from the shared composite stack."""
    try:
        from backend.services.composites import get_composites
        from data.alerts import add_alert

        # get_composites() both computes the stack AND populates the
        # session-keyed cache the /api/analysis/composites endpoint serves —
        # the alert scan and the dashboard can never diverge.
        comps = get_composites()
        weather = comps.get("weather", {})
        regime = comps.get("regime", {})
        if weather.get("value") in ("Elevated", "Storm"):
            add_alert(
                "weather", f"Portfolio Weather: {weather['value']}",
                weather.get("change_reason", "Risk conditions elevated"),
                severity="warning" if weather["value"] == "Elevated" else "critical",
                link="/overview",
            )
        if regime.get("value") in ("Distribution", "Panic-Rebound"):
            add_alert(
                "regime", f"Risk Regime: {regime['value']}",
                regime.get("change_reason", "Market regime deteriorating"),
                severity="warning",
                link="/risk",
            )
    except Exception as e:
        logger.warning("Alert scan (composites) failed: %s", e)


def _scan_stbr_alerts(positions) -> None:
    """Fire STBR warnings on watchlist + holdings."""
    try:
        from analysis.stbr import calculate_stbr, current_risk_label
        from data.alerts import add_alert
        from data.market import get_price_history
        from data.watchlist import get_watchlist

        watch = get_watchlist()
        if not positions.empty:
            watch = list(dict.fromkeys(watch + [s for s in positions["symbol"].unique() if s]))
        for sym in watch[:30]:
            try:
                df = get_price_history(sym)
                if df.empty:
                    continue
                stbr_df = calculate_stbr(df["close"])
                if stbr_df.empty:
                    continue
                val = float(stbr_df["stbr"].iloc[-1])
                label, _ = current_risk_label(val)
                if label in ("Bubble", "Bubble Pop", "Overextended"):
                    add_alert(
                        "stbr", f"{sym} STBR: {label}",
                        f"{sym} short-term bubble risk at {val:.2f} — overextended",
                        severity="warning",
                        symbol=sym,
                        link=f"/search?symbol={sym}",
                    )
            except Exception as e:
                logger.debug("STBR scan skipped %s: %s", sym, e)
    except Exception as e:
        logger.warning("Alert scan (STBR) failed: %s", e)


def _scan_entry_alerts(positions) -> None:
    """Fire entry-timing alerts on watchlist names that are NOT held.

    Buy-side counterpart to ``_scan_stbr_alerts`` (which only warns when a name
    is overextended). Signals on the name's OWN STBR distribution rather than a
    fixed threshold, and refuses to score a name with too little real history.

    Deliberately conservative: silence is the normal outcome. This fires only
    when a name is in the genuinely rare part of its own range.
    """
    try:
        from analysis.stbr import calculate_stbr
        from data.alerts import add_alert
        from data.market import get_price_history
        from data.watchlist import get_watchlist

        MIN_SESSIONS = 140  # below this the 140-day SMA is stub-dominated

        held = set()
        try:
            if not positions.empty:
                held = {str(s).upper() for s in positions["symbol"].unique() if s}
        except Exception:
            held = set()

        candidates = [s for s in get_watchlist() if str(s).upper() not in held]
        for sym in candidates[:30]:
            try:
                # 1825 days matches the app's own STBR endpoint; the 365
                # default yields too few observations to rank a percentile.
                df = get_price_history(sym, days=1825)
                if df.empty or "close" not in df:
                    continue

                series = df["close"].dropna()

                # Count sessions with MEANINGFUL volume, not rows. A recently
                # listed name carries pre-listing stub rows (SPCX: 203 rows but
                # ~137 real), and those stubs poison the 140-day average - the
                # ratio then reads whatever the stub price implies, which in the
                # cheap direction manufactures a false buy signal.
                real_sessions = len(series)
                if "volume" in df.columns:
                    vol = df["volume"].dropna()
                    peak = float(vol.max()) if len(vol) else 0.0
                    if peak > 0:
                        # Max-relative, not median-relative: a recent listing has
                        # mostly stub rows whose volume sits thousands of times
                        # below its peak, so a median-based floor counts them as
                        # real and the stub prices then contaminate the average.
                        floor = peak * 0.01
                        real_sessions = int((vol >= floor).sum())

                if real_sessions < MIN_SESSIONS:
                    add_alert(
                        "entry_data",
                        f"{sym}: too little history to score",
                        f"{sym} has {real_sessions} sessions with meaningful volume "
                        f"(out of {len(series)} rows); STBR needs about {MIN_SESSIONS} "
                        f"before the 140-day average is not dominated by stub prices. "
                        f"No entry signal will fire until then - do not read the "
                        f"published ratio as a buy or a warning.",
                        severity="info",
                        symbol=sym,
                        link=f"/search?symbol={sym}",
                    )
                    continue
                series = series.iloc[-real_sessions:] if real_sessions < len(series) else series

                stbr_df = calculate_stbr(series)
                if stbr_df.empty or "stbr" not in stbr_df:
                    continue

                hist = stbr_df["stbr"].dropna()
                if len(hist) < 30:
                    continue

                current = float(hist.iloc[-1])
                lo, hi = float(hist.min()), float(hist.max())
                median = float(hist.median())
                pct = float((hist <= current).mean() * 100)
                below = float((hist < current).mean() * 100)

                # Rare in its OWN range: bottom decile and under its median.
                # Both conditions, because a name can sit in the low decile of a
                # narrow range and still not be cheap relative to its trend.
                if pct <= 10.0 and current < median:
                    add_alert(
                        "entry_signal",
                        f"{sym} in its own buy zone (STBR {current:.2f})",
                        f"{sym} STBR {current:.2f} is the {below:.0f}th percentile "
                        f"of its own {len(hist)}-day history (range {lo:.2f}-{hi:.2f}, "
                        f"median {median:.2f}). It has been this low or lower on "
                        f"about {below:.0f}% of days. Entry zone - verify the thesis "
                        f"before sizing.",
                        severity="warning",
                        symbol=sym,
                        link=f"/search?symbol={sym}",
                    )
            except Exception as e:
                logger.debug("Entry scan skipped %s: %s", sym, e)
    except Exception as e:
        logger.warning("Alert scan (entry) failed: %s", e)


def _scan_earnings_alerts(positions) -> None:
    """Fire earnings-proximity alerts on urgent holdings."""
    try:
        if positions.empty:
            return
        from data.alerts import add_alert
        from data.earnings import get_portfolio_earnings

        equities = positions[positions["asset_type"] != "OPTION"]
        symbols = [s for s in equities["symbol"].unique() if s]
        for e in get_portfolio_earnings(symbols, positions):
            if e.get("urgent"):
                add_alert(
                    "earnings", f"{e['symbol']} earnings in {e['days_until']}d",
                    f"Reports on {e['earnings_date']} — event risk",
                    severity="info",
                    symbol=e["symbol"],
                    link=f"/search?symbol={e['symbol']}",
                )
    except Exception as e:
        logger.warning("Alert scan (earnings) failed: %s", e)


def _scan_thesis_alerts() -> None:
    """Fire thesis-invalidation alerts from the shared review orchestration.

    Theses are GLOBAL (not session-scoped) — the operator's reasoning is their
    own. Alerts stay session-scoped (data/alerts.py), so this scan runs inside
    the active session like the other alert scans. A thesis that is
    invalidated or at-risk fires a warning; a stale thesis fires an info
    reminder to review it.

    Stale-review nags are aggregated into a single alert per scan listing all
    symbols that need review. This prevents alert fatigue: a portfolio with
    forty stale theses used to emit forty identical info alerts, burying the
    one critical invalidation.
    """
    try:
        from backend.services.theses import review_all_theses
        from data.alerts import add_alert

        stale_symbols: list[str] = []
        for review in review_all_theses():
            sym = review["symbol"]
            status = review["status"]
            if status == "invalidated":
                add_alert(
                    "thesis", f"{sym} thesis invalidated",
                    "; ".join(review["triggers_hit"]) or "Invalidation trigger hit",
                    severity="critical",
                    symbol=sym,
                    link=f"/search?symbol={sym}",
                )
            elif status == "target_hit":
                add_alert(
                    "thesis", f"{sym} target reached",
                    "; ".join(review["triggers_hit"]) or "Upside target hit",
                    severity="info",
                    symbol=sym,
                    link=f"/search?symbol={sym}",
                )
            elif status == "at_risk":
                add_alert(
                    "thesis", f"{sym} thesis at risk",
                    "; ".join(review["triggers_hit"]) or "Warning trigger hit",
                    severity="warning",
                    symbol=sym,
                    link=f"/search?symbol={sym}",
                )
            elif status == "stale":
                stale_symbols.append(sym)

        if stale_symbols:
            count = len(stale_symbols)
            symbol_list = ", ".join(sorted(stale_symbols))
            if count == 1:
                title = "1 thesis needs review"
                msg = f"1 thesis has not been reviewed within the review interval: {symbol_list}"
            else:
                title = f"{count} theses need review"
                msg = f"{count} theses have not been reviewed within the review interval: {symbol_list}"
            add_alert(
                "thesis_stale_review",
                title,
                msg,
                severity="info",
                link="/positions",
            )
    except Exception as e:
        logger.warning("Alert scan (theses) failed: %s", e)


def _run_correlation_scan():
    """Detect correlation regime changes among current holdings.

    Compares each pair's recent rolling correlation (last 63d) against its
    trailing baseline (prior ~189d). A shift beyond the threshold fires an
    alert — the pair's diversification benefit (or concentration risk) has
    structurally changed even though allocation weights didn't.

    Single-operator: reads global data directly — no session binding.
    """
    now = datetime.datetime.now()
    if now.weekday() >= 5:
        return
    if not (9 <= now.hour <= 16):
        return

    try:
        _correlation_scan_body()
    except Exception as e:
        logger.warning("Correlation scan failed: %s", e)


def _correlation_scan_body():
    """The actual correlation scan, run with the active session bound."""
    try:
        from analysis.relationships import correlation_regime_shifts
        from analysis.risk import daily_returns
        from data.account import get_combined_positions
        from data.alerts import add_alert
        from data.market import get_price_history

        positions = get_combined_positions()
        if positions.empty:
            return
        equities = positions[positions["asset_type"] != "OPTION"]
        symbols = [s for s in equities["symbol"].unique() if s]
        if len(symbols) < 2:
            return

        returns_dict: dict = {}
        for sym in symbols:
            try:
                df = get_price_history(sym, days=400)
                if not df.empty and len(df) > 260:
                    returns_dict[sym] = daily_returns(df)
            except Exception as e:
                logger.debug("Correlation scan skipped %s: %s", sym, e)
        if len(returns_dict) < 2:
            return

        shifts = correlation_regime_shifts(returns_dict, window=63, lookback=252, threshold=0.15)
        for s in shifts[:5]:
            if s["direction"] == "DIVERGING":
                title = f"{s['pair']} correlation dropped"
                message = (
                    f"{s['pair']} correlation fell from {s['baseline']:.2f} to "
                    f"{s['recent']:.2f} — diversification benefit increased"
                )
            else:
                title = f"{s['pair']} correlation rose"
                message = (
                    f"{s['pair']} correlation rose from {s['baseline']:.2f} to "
                    f"{s['recent']:.2f} — portfolio more concentrated in risk space"
                )
            add_alert(
                "correlation", title, message,
                severity="info",
                # `symbol` carries the PAIR, not a ticker. The store's dedup key
                # is (type, symbol, date); with symbol unset every pair collapsed
                # onto one key, so only the first pair per day was deduped and the
                # rest accumulated duplicates on every scan.
                symbol=str(s["pair"]),
                link="/flows",
            )
    except Exception as e:
        logger.warning("Correlation scan body failed: %s", e)


def _run_gpu_snapshot():
    """Append the latest GPU-market snapshot to the local history store.

    Runs every 15 minutes so Meridian's compute history only grows, independent
    of Vast.ai's retention window. GPU data is global — no session binding.
    Raw offers are persisted alongside the derived metrics so any statistic can
    be re-derived from the source of truth.
    """
    try:
        from data.gpu import get_gpu_metrics, get_last_offers, record_offers, record_snapshot
        metrics = get_gpu_metrics()
        if not metrics:
            logger.warning("GPU_SNAPSHOT_FAILED: no metrics returned (feed unavailable or rate-limited)")
            return
        ts = record_snapshot(metrics)
        if ts:
            logger.info("GPU snapshot recorded: %s (%d GPUs)", ts, len(metrics))
        # Raw offers are captured during the get_gpu_metrics fetch (no extra
        # requests) and persisted as the source of truth.
        offers = get_last_offers()
        if offers:
            record_offers(offers)
    except Exception as e:
        logger.warning("GPU_SNAPSHOT_FAILED: %s", e)


def _run_outcome_resolve():
    """Resolve the Decision Book's closed forecast windows.

    Runs every 15 minutes (cheap when nothing is pending — resolution is a
    no-op on already-closed windows). Not market-hours-gated: decisions arm
    and resolve around the clock; the price history lookup is TTL-cached.
    """
    try:
        from data.outcomes import resolve_outcomes
        r = resolve_outcomes()
        if r.get("resolved"):
            logger.info("Outcome resolve: %d resolved, %d pending", r["resolved"], r["pending"])
    except Exception as e:
        logger.warning("Outcome resolve failed: %s", e)



def _run_market_panel_update():
    """Extend the market-wide panel through the latest completed session.

    Cheap when current: one grouped-daily call per new session, and NOTHING
    when the panel has already caught up (update() returns before the reference
    refresh, so a no-op run costs no calls at all). Runs hourly rather than once
    a day so an early close or a late settle is picked up without a deploy, and
    so the board is never a session behind the tape. A cold start (panel absent)
    backfills five years in about two minutes inside this thread, which is what
    makes the feature self-bootstrapping on a fresh machine.
    """
    try:
        from data import market_panel

        meta = market_panel.update(compact_when_done=True)
        if meta.get("updated"):
            logger.info(
                "Market panel updated: %s sessions through %s (%d discontinuity flags)",
                meta.get("sessions"), meta.get("as_of"),
                len(meta.get("discontinuities") or {}),
            )
    except Exception as e:
        logger.warning("Market panel update failed: %s", e)



def _warm_page_caches():
    """Keep the page-data caches hot — every visit should be a cache hit.

    The boot warm-ups were one-shot `date` jobs: once their TTL expired, the
    next visit paid the cold compute again (attribution ~729ms, meta ~354ms,
    board ~860ms). These are the same computes, re-run periodically so the TTL
    never lapses in normal use. Each entry is isolated: one cold feed must not
    skip the rest. Body-taking endpoints (sizing/factors) are excluded — they
    warm themselves on first use.
    """
    jobs = [
        ("backend.routers.analysis.risk.attribution",
         lambda: __import__("backend.routers.analysis.risk", fromlist=["x"]).attribution()),
        ("backend.routers.theses.list_thesis_reviews",
         lambda: __import__("backend.routers.theses", fromlist=["x"]).list_thesis_reviews()),
        ("backend.routers.theses.decisions_stats",
         lambda: __import__("backend.routers.theses", fromlist=["x"]).decisions_stats()),
        ("backend.routers.theses.decisions_integrity",
         lambda: __import__("backend.routers.theses", fromlist=["x"]).decisions_integrity()),
        ("backend.routers.leaderboard.leaderboard_meta",
         lambda: __import__("backend.routers.leaderboard", fromlist=["x"]).leaderboard_meta()),
        ("backend.routers.intelligence.portfolio_relationships",
         lambda: __import__("backend.routers.intelligence", fromlist=["x"]).portfolio_relationships()),
        ("backend.routers.history.get_breadth",
         lambda: __import__("backend.routers.history", fromlist=["x"]).get_breadth()),
        ("backend.routers.history.get_regime",
         lambda: __import__("backend.routers.history", fromlist=["x"]).get_regime()),
        # Every period the page offers: switching periods is the board's most
        # common interaction, and a cold period compose costs ~3.5-4s. Each
        # period caches independently; warming them keeps the switch instant.
        *[
            (f"leaderboard.board_response[{period}]",
             (lambda per: (lambda: __import__("backend.routers.leaderboard", fromlist=["x"])
                           ._cached_board_response("S&P 500", per, 100, "", "", "", "score", False, False)))(period))
            for period in ("1D", "1W", "1M", "3M", "6M", "1Y", "2Y", "3Y", "YTD")
        ],
        # sizing is a POST the page calls with an empty body; warming the
        # same SizingBody() means the page's request is a cache hit too.
        ("analysis.sizing (empty body)",
         lambda: __import__("backend.routers.analysis.sizing", fromlist=["x"])
         ._cached_position_sizing(__import__("backend.routers.analysis._common", fromlist=["x"]).SizingBody())),
        ("backend.routers.liabilities.liability_analysis",
         lambda: __import__("backend.routers.liabilities", fromlist=["x"])._cached_liability_analysis(True)),
        ("backend.routers.snapshots.portfolio_history",
         lambda: __import__("backend.routers.snapshots", fromlist=["x"])._cached_portfolio_history(None, None)),
        ("backend.routers.alternatives.list_alternatives",
         lambda: __import__("backend.routers.alternatives", fromlist=["x"]).list_alternatives()),
    ]
    for label, fn in jobs:
        try:
            fn()
        except Exception as e:
            logger.warning("Page-cache warm failed (%s): %s", label, e)
    # Price histories behind every price surface: the index strips, the book, and
    # the watchlist. get_price_history is TTL-cached for an HOUR, so after the
    # first cycle this refresh is cache-cheap; skipping it meant a 400-550ms cold
    # fetch landing on whatever page asked first (measured: NVDA 410ms, JPM 553ms).
    try:
        from data.market import get_price_history

        warm_symbols = {"SPY", "QQQ", "DIA", "IWM"}  # index strips (/search, /overview)
        warm_symbols.update(("AAPL", "MSFT", "NVDA", "GOOG", "AMZN", "META", "TSLA",
                             "AVGO", "JPM", "LLY", "V", "UNH", "XOM", "COST"))  # search strip
        try:
            from data.watchlist import get_watchlist
            warm_symbols.update(s for s in get_watchlist() if s)
        except Exception as e:
            logger.warning("Watchlist warm read failed: %s", e)
        try:
            from data.account import get_combined_positions
            pos = get_combined_positions()
            if pos is not None and not pos.empty:
                warm_symbols.update(str(s).upper() for s in pos["symbol"].dropna().unique() if s)
        except Exception as e:
            logger.warning("Position warm read failed: %s", e)

        # Background lane: this warm runs every five minutes over ~70 symbols.
        # On the shared lane it could consume the whole minute's budget and make
        # a click wait a full window; on its own lane it can only slow itself.
        from data.schwab import background_priority

        with background_priority():
            for sym in sorted(warm_symbols):
                if sym.endswith(("-USD", "=F")) or len(sym) > 6:
                    continue  # crypto/futures resolve through their own feeds
                for days in (5, 30):
                    try:
                        get_price_history(sym, days)
                    except Exception as e:
                        logger.warning("Price warm failed (%s/%sd): %s", sym, days, e)
    except Exception as e:
        logger.warning("Price warm unavailable: %s", e)


def _warm_leaderboard():
    """Compose the DEFAULT board of every universe the selector offers.

    The board is TTL-cached for 15 minutes at the composition layer; warming it
    here means the operator never pays the first-call cost, the same reasoning
    as the sonar warm-up below. The warm used to compose ONLY the S&P 500, so
    the first switch to Nasdaq 100 / Dow 30 / All US paid a cold compose
    (~4-8s, during which the previous board is held behind a skeleton) — the
    universe selector's options are as much a standing interaction as the
    period buttons warmed below.
    """
    from backend.routers import leaderboard as lb

    for universe in ("S&P 500", "Nasdaq 100", "Dow 30", "All US"):
        try:
            payload = lb.assemble_cached(universe, "YTD", None)
            # _assemble returns the composed FRAME, not the paged response —
            # reading "rows" here logged 0 for a warm-up that had actually
            # ranked 491 names, which reads as though the warm-up did nothing.
            frame = payload.get("_frame")
            logger.info(
                "Leaderboard warm-up [%s]: available=%s ranked=%s as_of=%s",
                universe, payload.get("available"),
                None if frame is None else len(frame), payload.get("as_of"),
            )
        except Exception as e:
            logger.warning("Leaderboard warm-up failed for %s: %s", universe, e)


def _run_schwab_grant_check():
    """Warn before the seven-day Schwab grant lapses.

    Schwab's refresh token cannot be extended, so the only defence is warning
    the operator early enough to reconnect on their own schedule instead of
    discovering a greyed-out book. One alert per day (alerts dedupe on
    type+symbol+date), and none at all when Schwab is not configured.
    """
    try:
        from data import schwab
        from data.alerts import add_alert

        status = schwab.token_status()
    except Exception as e:
        logger.warning("Schwab grant check failed: %s", e)
        return

    if not status.get("configured"):
        return

    link = "/settings"
    if not status.get("connected"):
        add_alert(
            "schwab_grant",
            "Schwab connection lapsed",
            "Equity quotes are running on the fallback feed. Reconnect Schwab from Settings.",
            severity="warning",
            link=link,
        )
        return

    days_left = float(status.get("days_left") or 0)
    if days_left <= 2:
        add_alert(
            "schwab_grant",
            f"Schwab re-authorization due in {days_left:.0f}d",
            "The market-data grant expires seven days after authorization and cannot be extended. "
            "Reconnect from Settings; it takes one click.",
            severity="warning" if days_left <= 1 else "info",
            link=link,
        )


def _run_schwab_positions_pull():
    """Keep the Schwab holdings current — the CSV upload, automated.

    Every 30 minutes: one read-only account call. When the holdings are
    unchanged this writes nothing, so the import list does not fill with
    identical snapshots. A grant missing the Accounts and Trading product is
    reported once (on the first failure), then held at debug — a permission the
    operator has to enable on the app's page is not an incident every half hour.
    """
    try:
        from data.schwab_sync import sync
        result = sync(reason="scheduler")
        if result.get("changed"):
            logger.info(
                "Schwab positions updated: import %s (%s positions, cash %s)",
                result.get("import_id"), result.get("positions"), result.get("cash"),
            )
        elif not result.get("ok"):
            if int(result.get("consecutive_failures") or 0) <= 1:
                logger.warning("SCHWAB_SYNC: %s", result.get("error"))
            else:
                logger.debug("Schwab sync still failing: %s", result.get("error"))
    except Exception as e:
        logger.warning("SCHWAB_SYNC_FAILED: %s", e)


def start():
    global _scheduler
    if _scheduler is not None:
        return

    # Load the persisted sonar cache so a restart doesn't throw away a warm
    # scan, then warm it in the background so the first Sonar visit is instant.
    try:
        from backend.routers.sonar import _load_cache
        _load_cache()
    except Exception as e:
        logger.warning("Sonar cache load failed: %s", e)

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(_run_sonar_scan, "interval", minutes=15, id="sonar")
    _scheduler.add_job(_run_alert_scan, "interval", minutes=15, id="alerts")
    _scheduler.add_job(_run_correlation_scan, "interval", minutes=15, id="correlation")
    _scheduler.add_job(_run_outcome_resolve, "interval", minutes=15, id="outcomes")
    # The market-wide daily panel behind /leaderboard. Hourly, because a no-op
    # run costs zero requests and a missed session makes the board stale.
    _scheduler.add_job(_run_market_panel_update, "interval", minutes=60, id="market_panel")
    # The Schwab grant is a seven-day hard wall; check often enough that a
    # Friday expiry is not discovered on Monday.
    _scheduler.add_job(_run_schwab_grant_check, "interval", hours=6, id="schwab_grant")
    # Holdings from the Trader API: the positions CSV, automated. Cheap (one
    # account call) and it writes nothing unless something actually changed.
    _scheduler.add_job(_run_schwab_positions_pull, "interval", minutes=30, id="schwab_positions")
    # GPU snapshots are opt-out (GPU_SNAPSHOT_ENABLED=0): Vast.ai enforces an
    # account-level quota that the 15-min × 9-GPU × 3-retry request pattern
    # exhausts (358 consecutive failed runs observed before disable). The
    # disable keeps the stored history and /api/gpu endpoints; set the env var
    # and restart to re-enable once the fetch pattern respects the quota.
    if os.getenv("GPU_SNAPSHOT_ENABLED", "1") == "0":
        logger.info("GPU snapshot job disabled (GPU_SNAPSHOT_ENABLED=0)")
    else:
        _scheduler.add_job(_run_gpu_snapshot, "interval", minutes=15, id="gpu")
    _scheduler.start()
    logger.info("Scheduler started")

    # Warm the scan cache in the background (only if it's stale/empty) so the
    # operator never waits on a cold scan. Bypasses the market-hours guard
    # deliberately — a warm cache is better than a 31s first-visit stall.
    try:
        from backend.routers.sonar import _scan_cache
        _stale = True
        for _entry in _scan_cache.values():
            ts = _entry.get("timestamp")
            if ts:
                try:
                    ts_dt = datetime.datetime.fromisoformat(ts)
                    if (datetime.datetime.now(datetime.timezone.utc) - ts_dt).total_seconds() < 900:
                        _stale = False
                        break
                except ValueError:
                    continue
        if _stale:
            def _warm_sonar():
                try:
                    _scan_all_universes("warm-up")
                except Exception as e:
                    logger.warning("Sonar warm-up failed: %s", e)
            _scheduler.add_job(
                _warm_sonar, "date",
                run_date=datetime.datetime.now() + datetime.timedelta(seconds=120),
                id="sonar_warm",  # staggered: a CPU-bound warm at +2s made the
                # first page load after a deploy the slowest one (measured 30s)
            )

        # Compose the default leaderboard once, so the first visit to a page
        # that reads the whole market is not a cold compute.
        _scheduler.add_job(
            _warm_leaderboard, "date",
            run_date=datetime.datetime.now() + datetime.timedelta(seconds=90),
            id="leaderboard_warm",
        )

        # Page-cache warmer: PERIODIC, not one-shot. The boot `date` jobs above
        # only cover the first 15 minutes of uptime; this keeps the analysis,
        # journal, history, and index-strip caches hot for the whole session.
        _scheduler.add_job(
            _warm_leaderboard, "interval", minutes=10, id="leaderboard_periodic",
        )
        _scheduler.add_job(
            _warm_page_caches, "interval", minutes=5, id="page_caches",
            next_run_time=datetime.datetime.now() + datetime.timedelta(seconds=180),
        )

        # Warm the sector-close cache (the Flows endpoints sample the same
        # universe at days=90, a different cache key than sonar's days=365).
        # The sector-close cache is in-memory only, so it's always empty after
        # a restart — gate on that, not on sonar staleness.
        try:
            from data.cache import _cache
            _sector_cold = not any("sector" in k[0] for k in _cache)
        except Exception as e:
            logger.warning("Sector-cache cold-check failed, assuming cold: %s", e)
            _sector_cold = True
        if _sector_cold:
            def _warm_sector_closes():
                try:
                    from backend.routers.intelligence import _cached_sector_closes, _sample_sector_symbols
                    from data.schwab import background_priority
                    from data.universe import UNIVERSE_SECTORS

                    with background_priority():
                        for universe_name, loader in UNIVERSE_SECTORS.items():
                            sector_map = loader()
                            sampled = _sample_sector_symbols(sector_map)
                            closes = _cached_sector_closes(sampled)
                            logger.info("Sector-close warm-up complete: %s — %d symbols", universe_name, len(closes))
                except Exception as e:
                    logger.warning("Sector-close warm-up failed: %s", e)
            _scheduler.add_job(
                _warm_sector_closes, "date",
                run_date=datetime.datetime.now() + datetime.timedelta(seconds=150),
                id="sector_warm",
            )
    except Exception as e:
        logger.warning("Sonar warm-up setup failed: %s", e)


def stop():
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler stopped")
