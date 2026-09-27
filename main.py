"""Ticket arbitrage scanner — CrowdVolt vs SeatGeek + TickPick + StubHub + VividSeats.

Usage:
    python main.py              # run once
    python main.py --loop       # run on a schedule (every SCAN_INTERVAL_MINUTES)
    python main.py --test       # test with a single known CrowdVolt event
"""

import argparse
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeout
from datetime import datetime

import config
import crowdvolt
import gametime
import matcher
import notifier
import resident_advisor
import seatgeek
import stubhub
import tickpick
import undercut
import vividseats


def _run_with_timeout(fn, timeout_sec: int, label: str):
    """Execute fn() in a worker thread with a hard timeout. Returns fn's
    result or None on timeout. The worker thread continues running in the
    background if timed out (may leak Playwright browser processes) but
    the main thread is guaranteed to return control — that's what matters
    when a downstream step silently hangs. GH Actions cleans up the whole
    process tree at job end regardless."""
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fn)
    try:
        return future.result(timeout=timeout_sec)
    except _FuturesTimeout:
        print(f"  [Timeout] {label} exceeded {timeout_sec}s — moving on", flush=True)
        return None
    finally:
        executor.shutdown(wait=False)


def scan_once() -> int:
    """Run a full scan. Returns number of opportunities found."""
    print(f"\n{'='*60}")
    print(f"[Scan] Starting at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    # Step 1: Fetch all CrowdVolt events with active listings
    cv_events = crowdvolt.fetch_all_events()
    if not cv_events:
        print("[Scan] No CrowdVolt events with active listings — nothing to do")
        notifier.send_summary(0, 0, 0, 0, 0, dice_filtered=0)
        return 0

    # Filter out past events — no point scanning events that already happened
    today = datetime.now().date()
    past_events = [e for e in cv_events if e.event_date and e.event_date.date() < today]
    cv_events = [e for e in cv_events if e.event_date is None or e.event_date.date() >= today]
    if past_events:
        print(f"[Scan] Filtered out {len(past_events)} past events")

    # Snapshot the upcoming catalog before the DICE/seated filters drop it.
    # The Reddit ticket scanner (called below) can source DICE and seated
    # tickets directly via fan-to-fan transfer, so it needs the broader set.
    upcoming_events = list(cv_events)

    # DICE events: spec-only path, no forward arb. Operator can't reliably
    # acquire a DICE-issued ticket on a 3P platform to fill a CV bid (tickets
    # are wallet-bound). But the reverse — listing on 3P and sourcing from
    # CV when sold — is workable for events where 3P listings exist. Lean
    # scan: include DICE in the matcher loop but skip the slow StubHub
    # scraper for those events (low hit rate, expensive). Forward arb
    # alerts are filtered downstream.
    dice_events = [e for e in cv_events if e.ticket_platform.upper() == "DICE"]
    print(f"[Scan] {len(dice_events)} DICE events kept for spec digest "
          f"(forward arb + StubHub search skipped on those); "
          f"{len(cv_events) - len(dice_events)} non-DICE events scanned both directions")

    # Filter out seated venues — section-based pricing makes lowest
    # third-party price meaningless vs CrowdVolt bids for specific sections.
    # The seated_scanner module handles these via section-level matching.
    # Word-boundary matching (not exact) so venue-name variants like
    # "Huntington Bank Pavilion at Northerly Island" still get filtered.
    SEATED_VENUES = {
        "barclays center",
        "madison square garden",
        "msg",
        "forest hills stadium",
        "huntington bank pavilion",
        "wrigley field",
    }

    def _is_seated_venue(venue) -> bool:
        v = (venue or "").lower().strip()
        if not v:
            return False
        return any(re.search(r"\b" + re.escape(k) + r"\b", v) for k in SEATED_VENUES)

    seated = [e for e in cv_events if _is_seated_venue(e.venue)]
    cv_events = [e for e in cv_events if not _is_seated_venue(e.venue)]
    if seated:
        print(f"[Scan] Filtered out {len(seated)} seated venue events (Barclays/MSG)")

    # Track bid availability
    events_with_bids = sum(1 for e in cv_events if e.max_bid is not None)
    events_with_asks_only = len(cv_events) - events_with_bids
    print(f"[Scan] {events_with_bids}/{len(cv_events)} events have waiting buyers, "
          f"{events_with_asks_only} have sellers only")

    # Step 2: Only scan events with active bids against third-party platforms.
    # No bid = no guaranteed buyer = no arbitrage opportunity.
    bid_events = [e for e in cv_events if e.max_bid is not None]
    print(f"[Scan] Scanning {len(bid_events)} events with active buyers against third-party platforms")

    all_opportunities = []
    errors = 0
    match_failures = 0

    for cv_event in bid_events:
        ask_str = f"${cv_event.min_ask:.0f}" if cv_event.min_ask else "none"
        print(f"\n[Match] {cv_event.name} (lowest seller: {ask_str}, highest buyer: ${cv_event.max_bid:.0f})")

        # Generate candidate search queries — multi-artist names produce
        # multiple queries (featured artist first, promoter second).
        queries = matcher.search_queries(cv_event.name)

        # Use localized date for search filters so late-night ET shows
        # don't search the wrong calendar day (CrowdVolt stores UTC).
        local_dt = matcher._localize_cv_date(cv_event)
        date_str = local_dt.strftime("%Y-%m-%d") if local_dt else None

        print(f"  [Query] {queries} (from \"{cv_event.name}\") date={date_str}")

        event_matched = False

        # --- HTTP-based sources (fast) ---

        # Search SeatGeek — try each query, break when we get a matched opportunity
        sg_opps = []
        for q in queries:
            try:
                sg_results = seatgeek.search_events(q, date_str)
                if sg_results:
                    sg_opps = matcher.match_seatgeek(cv_event, sg_results)
                    if sg_opps:
                        break  # got a real match, stop trying queries
            except Exception as e:
                print(f"  [SeatGeek] Error on query '{q}': {e}")
                errors += 1
        if sg_opps:
            event_matched = True
            for opp in sg_opps:
                _log_opportunity(opp)
            all_opportunities.extend(sg_opps)

        # Search TickPick — try each query, break when we get a matched opportunity
        tp_opps = []
        for q in queries:
            try:
                tp_results = tickpick.search_events(q, date_str)
                if tp_results:
                    tp_opps = matcher.match_tickpick(cv_event, tp_results)
                    if tp_opps:
                        break
            except Exception as e:
                print(f"  [TickPick] Error on query '{q}': {e}")
                errors += 1
        if tp_opps:
            event_matched = True
            for opp in tp_opps:
                _log_opportunity(opp)
            all_opportunities.extend(tp_opps)

        # --- Playwright-based sources (slower, headless browser) ---

        # Search StubHub — skip for DICE events: tickets are wallet-bound so
        # StubHub almost never lists them, and the scraper is 7-12s/query.
        # Wrapped in a hard 90s per-event timeout because StubHub uses
        # Playwright and can silently hang in the sync API (browser process
        # unresponsive). Losing 1 event's StubHub is better than the whole
        # scan hanging past the workflow timeout.
        sh_opps = []
        if cv_event.ticket_platform.upper() != "DICE":
            def _search_stubhub():
                opps = []
                for q in queries:
                    try:
                        results = stubhub.search_events(q, date_str)
                        if results:
                            matched = matcher.match_stubhub(cv_event, results)
                            if matched:
                                return matched
                    except Exception as ex:
                        print(f"  [StubHub] Error on query '{q}': {ex}", flush=True)
                return opps
            sh_opps = _run_with_timeout(
                _search_stubhub,
                timeout_sec=90,
                label=f"StubHub for {cv_event.name}",
            ) or []
            if not sh_opps and not isinstance(sh_opps, list):
                errors += 1
        if sh_opps:
            event_matched = True
            for opp in sh_opps:
                _log_opportunity(opp)
            all_opportunities.extend(sh_opps)

        # Search VividSeats — also Playwright, also anti-botted (Cloudflare
        # challenge with 3 retry attempts internally). Same hard-timeout
        # wrap as StubHub.
        def _search_vividseats():
            opps = []
            for q in queries:
                try:
                    results = vividseats.search_events(q, date_str)
                    if results:
                        matched = matcher.match_vividseats(cv_event, results)
                        if matched:
                            return matched
                except Exception as ex:
                    print(f"  [VividSeats] Error on query '{q}': {ex}", flush=True)
            return opps
        vs_opps = _run_with_timeout(
            _search_vividseats,
            timeout_sec=60,
            label=f"VividSeats for {cv_event.name}",
        ) or []
        if vs_opps:
            event_matched = True
            for opp in vs_opps:
                _log_opportunity(opp)
            all_opportunities.extend(vs_opps)

        # Search Gametime — try each query, break when we get a matched opportunity
        gt_opps = []
        for q in queries:
            try:
                gt_results = gametime.search_events(q, date_str)
                if gt_results:
                    gt_opps = matcher.match_gametime(cv_event, gt_results)
                    if gt_opps:
                        break
            except Exception as e:
                print(f"  [Gametime] Error on query '{q}': {e}")
                errors += 1
        if gt_opps:
            event_matched = True
            for opp in gt_opps:
                _log_opportunity(opp)
            all_opportunities.extend(gt_opps)

        # Search Resident Advisor — scoped to events where CV has already
        # tagged RA as the primary listing platform. RA doesn't sell
        # tickets directly for most shows (LEGACY ticketing → external
        # vendor at checkout), but the `cost` field is a reliable base
        # price signal that the matcher inflates with the estimated ~15%
        # buyer fee. Availability isn't checkable without auth — the
        # operator verifies at click-through.
        ra_opps = []
        if cv_event.ticket_platform.lower() == "resident advisor":
            def _search_ra():
                opps = []
                for q in queries:
                    try:
                        results = resident_advisor.search_events(
                            q, date_str, city_hint=cv_event.city)
                        if results:
                            matched = matcher.match_resident_advisor(cv_event, results)
                            if matched:
                                return matched
                    except Exception as ex:
                        print(f"  [ResidentAdvisor] Error on query '{q}': {ex}",
                              flush=True)
                return opps
            ra_opps = _run_with_timeout(
                _search_ra,
                timeout_sec=60,
                label=f"ResidentAdvisor for {cv_event.name}",
            ) or []
        if ra_opps:
            event_matched = True
            for opp in ra_opps:
                _log_opportunity(opp)
            all_opportunities.extend(ra_opps)

        if not event_matched:
            print(f"  [No Match] Could not match on any platform")
            match_failures += 1

        # Small delay between event lookups to respect rate limits
        time.sleep(0.5)

    # Step 3: Filter to real opportunities and notify
    real_opps = _filter_opportunities(all_opportunities)
    # DICE: spec-only — exclude from forward-arb alerts.
    forward_opps = [o for o in real_opps
                    if o.crowdvolt_event.ticket_platform.upper() != "DICE"]
    dice_in_forward = len(real_opps) - len(forward_opps)
    print(f"\n[Scan] {len(forward_opps)} forward-arb opportunities passed filters "
          f"({dice_in_forward} DICE excluded — spec only)")
    print(f"[Scan] {match_failures} events had no cross-platform match")

    # Group opportunities by CrowdVolt event so we send one alert per event
    by_event: dict[str, list] = {}
    for opp in forward_opps:
        slug = opp.crowdvolt_event.slug
        by_event.setdefault(slug, []).append(opp)

    for slug, opps in by_event.items():
        notifier.send_alert(opps)
        time.sleep(1)  # respect Discord rate limits

    # Step 3b (removed): the Reddit Tix scanner against r/avesNYC_tix has
    # been broken in production for the past week — both Reddit's API and
    # the PullPush fallback return 403/503 on every call. The scanner file
    # (reddit_tix_scanner.py) is left in place but no longer called from
    # the main scan. To revive, restore the try/except block here.

    # Step 3c: Marquee Skydeck face-value arb — flag CV buyer offers that
    # exceed Tao primary face value when the primary isn't sold out (buy
    # at face, fill the bid). Makes zero Tao requests unless a bidded
    # Skydeck event exists. Wrapped with hard timeout — a hung Tao HTTP
    # call was suspected as the post-matcher hang cause.
    try:
        import skydeck_scanner
        # 90s — discovery is now parallel (5-worker ThreadPoolExecutor,
        # ~15s for 50 slots down from ~140s serial). Non-discovery scans
        # complete in <10s. 90s gives 4-5x headroom for Tao slowness or
        # transient 502s without wasting scan budget.
        _run_with_timeout(
            lambda: skydeck_scanner.scan(cv_events=upcoming_events),
            timeout_sec=90,
            label="Skydeck scan",
        )
    except Exception as e:
        print(f"[Skydeck] Inline scan failed (main scan continues): {e}", flush=True)

    # Step 3d: Open-position risk monitor — watches naked spec listings
    # recorded in positions.json and alerts when CV's cheapest ask erodes
    # the after-fee payout (thin < $5 margin / underwater / no supply).
    # Problem-alerts only; wrapped in hard timeout for the same reason as
    # Skydeck.
    try:
        import position_monitor
        _run_with_timeout(
            lambda: position_monitor.scan(cv_events=upcoming_events),
            timeout_sec=60,
            label="Position monitor",
        )
    except Exception as e:
        print(f"[Positions] Inline monitor failed (main scan continues): {e}", flush=True)

    # Step 4: Track bid/ask snapshots and evaluate speculative opportunities.
    undercut.save_bid_snapshot(cv_events)
    undercut.update_listing_persistence(cv_events)

    # Step 5: Evaluate speculative listing opportunities.
    # List on 3P platform at market price → source from CrowdVolt when sold.
    spec_opps = undercut.find_opportunities(all_opportunities, bid_events)
    spec_sent = 0
    if spec_opps:
        print(f"[Scan] {len(spec_opps)} speculative listing opportunities detected")
        spec_sent = undercut.send_alerts(spec_opps)

    # Step 6: Log full scan results for backtesting (includes alert outcomes).
    undercut.log_scan_results(
        bid_events, all_opportunities,
        arb_count=len(by_event), spec_opps=spec_opps,
    )

    notifier.send_summary(
        len(cv_events), len(by_event), errors,
        events_with_bids, match_failures,
        dice_filtered=len(dice_events),
        undercut_sent=spec_sent,
    )

    print(f"[Scan] Done — {len(by_event)} arb alerts, {spec_sent} speculative alerts")
    return len(by_event)


def _log_opportunity(opp):
    """Print an opportunity to the console."""
    label = opp.source_platform
    src = opp.source_price

    parts = [f"  [{label}] ${src:.0f}"]
    if opp.profit_vs_bid is not None:
        parts.append(f"vs buyer ${opp.crowdvolt_bid:.0f} → profit ${opp.profit_vs_bid:.0f}")
    if opp.profit_vs_ask is not None:
        parts.append(f"vs seller ${opp.crowdvolt_ask:.0f} → spread ${opp.profit_vs_ask:.0f}")

    print(" | ".join(parts))


def _filter_opportunities(opps: list) -> list:
    """Keep only opportunities where an active CrowdVolt bid exists.

    Only alerts when someone on CrowdVolt is actively offering to buy
    at a price higher than what you'd pay on the source platform.
    No bid = no guaranteed buyer = no alert.
    """
    filtered = []

    for opp in opps:
        # ONLY alert when there is an active bid we can profit from
        if opp.profit_vs_bid is not None and opp.profit_vs_bid > 0:
            margin = (opp.profit_vs_bid / opp.source_price) * 100
            if margin >= config.MIN_PROFIT_MARGIN_PCT:
                filtered.append(opp)

    return filtered


def test_single():
    """Test with a known CrowdVolt event to verify the pipeline works."""
    print("[Test] Fetching Ultra Miami 2026 from CrowdVolt...")
    event = crowdvolt.fetch_event("ultra-miami-2026")

    if not event:
        print("[Test] Failed to fetch event")
        return

    print(f"[Test] Event: {event.name}")
    print(f"[Test] Platform: {event.ticket_platform or 'Unknown'}")
    print(f"[Test] Venue: {event.venue} — {event.city}")
    print(f"[Test] Date: {event.event_date}")
    print(f"[Test] Sellers: {len(event.asks)} (lowest: ${event.min_ask})")
    print(f"[Test] Buyers: {len(event.bids)} (highest: ${event.max_bid or 'none'})")

    for ask in event.asks:
        print(f"  Seller: {ask.user} — ${ask.price} (${ask.all_in_price} all-in) x{ask.qty} [{ask.ticket_type}]")
    for bid in event.bids:
        print(f"  Buyer: {bid.user} — ${bid.price} (${bid.all_in_price} all-in) x{bid.qty} [{bid.ticket_type}]")

    # Extract search queries
    queries = matcher.search_queries(event.name)
    print(f"\n[Test] Search queries: {queries} (from \"{event.name}\")")
    query = queries[0]

    # SeatGeek
    print(f"\n[Test] Searching SeatGeek for '{query}'...")
    sg_results = seatgeek.search_events(query)
    print(f"[Test] SeatGeek returned {len(sg_results)} results")
    for sg in sg_results[:5]:
        print(f"  {sg.title} — ${sg.lowest_price} at {sg.venue}")

    # TickPick
    print(f"\n[Test] Searching TickPick for '{query}'...")
    tp_results = tickpick.search_events(query)
    print(f"[Test] TickPick returned {len(tp_results)} results")
    for tp in tp_results[:5]:
        print(f"  {tp.name} — ${tp.low_price}-${tp.high_price} at {tp.venue}")

    # StubHub
    print(f"\n[Test] Searching StubHub for '{query}'...")
    sh_results = stubhub.search_events(query)
    print(f"[Test] StubHub returned {len(sh_results)} results")
    for sh in sh_results[:5]:
        print(f"  {sh.name} — ${sh.min_price} at {sh.venue}")

    # VividSeats
    print(f"\n[Test] Searching VividSeats for '{query}'...")
    vs_results = vividseats.search_events(query)
    print(f"[Test] VividSeats returned {len(vs_results)} results")
    for vs in vs_results[:5]:
        print(f"  {vs.name} — ${vs.min_price} at {vs.venue}")

    # Check matches
    sg_opps = matcher.match_seatgeek(event, sg_results)
    tp_opps = matcher.match_tickpick(event, tp_results)
    sh_opps = matcher.match_stubhub(event, sh_results)
    vs_opps = matcher.match_vividseats(event, vs_results)
    all_opps = sg_opps + tp_opps + sh_opps + vs_opps

    print(f"\n[Test] {len(all_opps)} potential matches found")
    for opp in all_opps:
        _log_opportunity(opp)

    # Send a test alert if any opportunities exist
    real = _filter_opportunities(all_opps)
    if real:
        print(f"\n[Test] Sending test alert for best opportunity...")
        notifier.send_alert(real)
        print("[Test] Alert sent to Discord!")
    else:
        print("\n[Test] No opportunities passed filters — sending test summary")
        notifier.send_summary(1, 0, 0, 1 if event.max_bid else 0, 0)


def main():
    parser = argparse.ArgumentParser(description="Ticket arbitrage scanner")
    parser.add_argument("--loop", action="store_true", help="Run continuously on a schedule")
    parser.add_argument("--test", action="store_true", help="Test with a single event")
    args = parser.parse_args()

    # TickPick requires no API key, so we can always run.
    # SeatGeek is an optional addition. StubHub/VividSeats use Playwright.
    if not config.DISCORD_WEBHOOK_URL:
        print("ERROR: Set DISCORD_WEBHOOK_URL")
        sys.exit(1)

    if args.test:
        test_single()
    elif args.loop:
        print(f"[Loop] Running every {config.SCAN_INTERVAL_MINUTES} minutes")
        print("[Loop] Press Ctrl+C to stop\n")
        while True:
            try:
                scan_once()
                print(f"\n[Loop] Next scan in {config.SCAN_INTERVAL_MINUTES} minutes...")
                time.sleep(config.SCAN_INTERVAL_MINUTES * 60)
            except KeyboardInterrupt:
                print("\n[Loop] Stopped")
                break
    else:
        scan_once()
        # Force process exit. Playwright browser processes launched via
        # ThreadPoolExecutor worker threads leave non-daemon threads alive
        # for 18+ minutes after the scan completes, keeping Python running
        # until the workflow timeout (35 min) fires SIGTERM and reports
        # "cancelled" despite the scan actually succeeding. All state is
        # already saved and Discord alerts already dispatched by this
        # point, so a hard exit is safe. Reclaims runner minutes and gives
        # accurate GH Actions status.
        os._exit(0)


if __name__ == "__main__":
    main()
