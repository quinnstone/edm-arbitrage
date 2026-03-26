"""Ticket arbitrage scanner — CrowdVolt vs SeatGeek + Ticketmaster.

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
import ticketmaster


def scan_once() -> int:
    """Run a full scan. Returns number of opportunities found."""
    print(f"\n{'='*60}")
    print(f"[Scan] Starting at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    # Step 1: Fetch all CrowdVolt events with active listings
    cv_events = crowdvolt.fetch_all_events()
    if not cv_events:
        print("[Scan] No CrowdVolt events with active listings — nothing to do")
        notifier.send_summary(0, 0, 0)
        return 0

    # Step 2: For each CrowdVolt event, search SeatGeek + Ticketmaster
    all_opportunities = []
    errors = 0

    for cv_event in cv_events:
        print(f"\n[Match] {cv_event.name} (ask: ${cv_event.min_ask}, bid: ${cv_event.max_bid or 'none'})")

        # Build search query from event name
        query = cv_event.name

        # Date string for filtering
        date_str = None
        if cv_event.event_date:
            date_str = cv_event.event_date.strftime("%Y-%m-%d")

        # Search SeatGeek
        try:
            sg_results = seatgeek.search_events(query, date_str)
            if sg_results:
                sg_opps = matcher.match_seatgeek(cv_event, sg_results)
                for opp in sg_opps:
                    _log_opportunity(opp)
                all_opportunities.extend(sg_opps)
        except Exception as e:
            print(f"  [SeatGeek] Error: {e}")
            errors += 1

        # Search Ticketmaster
        try:
            tm_results = ticketmaster.search_events(query, date_str)
            if tm_results:
                tm_opps = matcher.match_ticketmaster(cv_event, tm_results)
                for opp in tm_opps:
                    _log_opportunity(opp)
                all_opportunities.extend(tm_opps)
        except Exception as e:
            print(f"  [Ticketmaster] Error: {e}")
            errors += 1

        # Small delay between event lookups to respect rate limits
        time.sleep(0.5)

    # Step 3: Filter to real opportunities and notify
    real_opps = _filter_opportunities(all_opportunities)
    print(f"\n[Scan] {len(real_opps)} opportunities passed filters")

    for opp in real_opps:
        notifier.send_alert(opp)
        time.sleep(1)  # respect Discord rate limits

    notifier.send_summary(len(cv_events), len(real_opps), errors)

    print(f"[Scan] Done — {len(real_opps)} alerts sent")
    return len(real_opps)


def _log_opportunity(opp):
    """Print an opportunity to the console."""
    label = opp.source_platform
    src = opp.source_price

    parts = [f"  [{label}] ${src:.0f}"]
    if opp.profit_vs_bid is not None:
        parts.append(f"vs bid ${opp.crowdvolt_bid:.0f} → profit ${opp.profit_vs_bid:.0f}")
    if opp.profit_vs_ask is not None:
        parts.append(f"vs ask ${opp.crowdvolt_ask:.0f} → spread ${opp.profit_vs_ask:.0f}")

    print(" | ".join(parts))


def _filter_opportunities(opps: list) -> list:
    """Keep only opportunities that meet profit thresholds."""
    filtered = []

    for opp in opps:
        # Priority 1: Can fill an existing bid (guaranteed buyer)
        if opp.profit_vs_bid is not None and opp.profit_vs_bid > 0:
            margin = (opp.profit_vs_bid / opp.source_price) * 100
            if (opp.profit_vs_bid >= config.MIN_PROFIT_THRESHOLD
                    and margin >= config.MIN_PROFIT_MARGIN_PCT):
                filtered.append(opp)
                continue

        # Priority 2: Can undercut lowest ask (need to find a buyer)
        if opp.profit_vs_ask is not None and opp.profit_vs_ask > 0:
            margin = (opp.profit_vs_ask / opp.source_price) * 100
            if (opp.profit_vs_ask >= config.MIN_PROFIT_THRESHOLD
                    and margin >= config.MIN_PROFIT_MARGIN_PCT):
                filtered.append(opp)
                continue

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
    print(f"[Test] Asks: {len(event.asks)} (lowest: ${event.min_ask})")
    print(f"[Test] Bids: {len(event.bids)} (highest: ${event.max_bid or 'none'})")

    for ask in event.asks:
        print(f"  Sell: {ask.user} — ${ask.price} (${ask.all_in_price} all-in) x{ask.qty} [{ask.ticket_type}]")
    for bid in event.bids:
        print(f"  Buy: {bid.user} — ${bid.price} x{bid.qty} [{bid.ticket_type}]")

    # Try matching
    print(f"\n[Test] Searching SeatGeek for '{event.name}'...")
    sg_results = seatgeek.search_events(event.name)
    print(f"[Test] SeatGeek returned {len(sg_results)} results")
    for sg in sg_results[:5]:
        print(f"  {sg.title} — ${sg.lowest_price} at {sg.venue}")

    print(f"\n[Test] Searching Ticketmaster for '{event.name}'...")
    tm_results = ticketmaster.search_events(event.name)
    print(f"[Test] Ticketmaster returned {len(tm_results)} results")
    for tm in tm_results[:5]:
        print(f"  {tm.name} — ${tm.min_price}-${tm.max_price} at {tm.venue}")

    # Check matches
    sg_opps = matcher.match_seatgeek(event, sg_results)
    tm_opps = matcher.match_ticketmaster(event, tm_results)
    all_opps = sg_opps + tm_opps

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
        notifier.send_summary(1, 0, 0)


def main():
    parser = argparse.ArgumentParser(description="Ticket arbitrage scanner")
    parser.add_argument("--loop", action="store_true", help="Run continuously on a schedule")
    parser.add_argument("--test", action="store_true", help="Test with a single event")
    args = parser.parse_args()

    # Validate config
    has_api = bool(config.SEATGEEK_CLIENT_ID or config.TICKETMASTER_API_KEY)
    if not has_api:
        print("ERROR: Set at least one API key:")
        print("  export SEATGEEK_CLIENT_ID='your_key'")
        print("  export TICKETMASTER_API_KEY='your_key'")
        sys.exit(1)

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
