"""Ticket arbitrage scanner — CrowdVolt vs SeatGeek + TickPick + StubHub + VividSeats.

Usage:
    python main.py              # run once
    python main.py --loop       # run on a schedule (every SCAN_INTERVAL_MINUTES)
    python main.py --test       # test with a single known CrowdVolt event
"""

import argparse
import sys
import time
from datetime import datetime

import config
import crowdvolt
import matcher
import notifier
import seatgeek
import stubhub
import tickpick
import vividseats


def scan_once() -> int:
    """Run a full scan. Returns number of opportunities found."""
    print(f"\n{'='*60}")
    print(f"[Scan] Starting at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    # Step 1: Fetch all CrowdVolt events with active listings
    cv_events = crowdvolt.fetch_all_events()
    if not cv_events:
        print("[Scan] No CrowdVolt events with active listings — nothing to do")
        notifier.send_summary(0, 0, 0, 0, 0)
        return 0

    # Track bid availability
    events_with_bids = sum(1 for e in cv_events if e.max_bid is not None)
    events_with_asks_only = len(cv_events) - events_with_bids
    print(f"[Scan] {events_with_bids}/{len(cv_events)} events have waiting buyers, "
          f"{events_with_asks_only} have sellers only")

    # Step 2: For each CrowdVolt event, search all sources
    all_opportunities = []
    errors = 0
    match_failures = 0

    for cv_event in cv_events:
        buyer_str = f"${cv_event.max_bid:.0f}" if cv_event.max_bid else "none"
        print(f"\n[Match] {cv_event.name} (lowest seller: ${cv_event.min_ask}, highest buyer: {buyer_str})")

        # Extract clean artist name for better search results
        query = matcher.extract_artist_name(cv_event.name)
        date_str = None
        if cv_event.event_date:
            date_str = cv_event.event_date.strftime("%Y-%m-%d")

        print(f"  [Query] \"{query}\" (from \"{cv_event.name}\")")

        event_matched = False

        # --- HTTP-based sources (fast) ---

        # Search SeatGeek
        try:
            sg_results = seatgeek.search_events(query, date_str)
            if sg_results:
                sg_opps = matcher.match_seatgeek(cv_event, sg_results)
                if sg_opps:
                    event_matched = True
                for opp in sg_opps:
                    _log_opportunity(opp)
                all_opportunities.extend(sg_opps)
        except Exception as e:
            print(f"  [SeatGeek] Error: {e}")
            errors += 1

        # Search TickPick (no API key needed)
        try:
            tp_results = tickpick.search_events(query, date_str)
            if tp_results:
                tp_opps = matcher.match_tickpick(cv_event, tp_results)
                if tp_opps:
                    event_matched = True
                for opp in tp_opps:
                    _log_opportunity(opp)
                all_opportunities.extend(tp_opps)
        except Exception as e:
            print(f"  [TickPick] Error: {e}")
            errors += 1

        # --- Playwright-based sources (slower, headless browser) ---
        # Only run for events with active bids — no bid means no
        # guaranteed buyer, so no point spending 10-15s per browser load.

        if cv_event.max_bid is not None:
            # Search StubHub
            try:
                sh_results = stubhub.search_events(query, date_str)
                if sh_results:
                    sh_opps = matcher.match_stubhub(cv_event, sh_results)
                    if sh_opps:
                        event_matched = True
                    for opp in sh_opps:
                        _log_opportunity(opp)
                    all_opportunities.extend(sh_opps)
            except Exception as e:
                print(f"  [StubHub] Error: {e}")
                errors += 1

            # Search VividSeats
            try:
                vs_results = vividseats.search_events(query, date_str)
                if vs_results:
                    vs_opps = matcher.match_vividseats(cv_event, vs_results)
                    if vs_opps:
                        event_matched = True
                    for opp in vs_opps:
                        _log_opportunity(opp)
                    all_opportunities.extend(vs_opps)
            except Exception as e:
                print(f"  [VividSeats] Error: {e}")
                errors += 1
        else:
            print(f"  [StubHub/VividSeats] Skipped — no waiting buyers")

        if not event_matched:
            print(f"  [No Match] Could not match on any platform")
            match_failures += 1

        # Small delay between event lookups to respect rate limits
        time.sleep(0.5)

    # Step 3: Filter to real opportunities and notify
    real_opps = _filter_opportunities(all_opportunities)
    print(f"\n[Scan] {len(real_opps)} opportunities passed filters")
    print(f"[Scan] {match_failures} events had no cross-platform match")

    for opp in real_opps:
        notifier.send_alert(opp)
        time.sleep(1)  # respect Discord rate limits

    notifier.send_summary(
        len(cv_events), len(real_opps), errors,
        events_with_bids, match_failures,
    )

    print(f"[Scan] Done — {len(real_opps)} alerts sent")
    return len(real_opps)


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
            if (opp.profit_vs_bid >= config.MIN_PROFIT_THRESHOLD
                    and margin >= config.MIN_PROFIT_MARGIN_PCT):
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
    print(f"[Test] Venue: {event.venue} — {event.city}")
    print(f"[Test] Date: {event.event_date}")
    print(f"[Test] Sellers: {len(event.asks)} (lowest: ${event.min_ask})")
    print(f"[Test] Buyers: {len(event.bids)} (highest: ${event.max_bid or 'none'})")

    for ask in event.asks:
        print(f"  Seller: {ask.user} — ${ask.price} (${ask.all_in_price} all-in) x{ask.qty} [{ask.ticket_type}]")
    for bid in event.bids:
        print(f"  Buyer: {bid.user} — ${bid.price} (${bid.all_in_price} all-in) x{bid.qty} [{bid.ticket_type}]")

    # Extract query
    query = matcher.extract_artist_name(event.name)
    print(f"\n[Test] Search query: \"{query}\" (from \"{event.name}\")")

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
        notifier.send_alert(real[0])
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


if __name__ == "__main__":
    main()
