"""Thorough test of name, venue, and city matching across all sources.

Fetches live CrowdVolt catalog, queries third-party providers, and
checks every match/rejection with detailed logging.
"""

import sys
import time
from datetime import datetime

import config
import crowdvolt
import matcher
import seatgeek
import tickpick
import groupme


def test_name_extraction():
    """Test extract_artist_name on known tricky cases."""
    print("=" * 60)
    print("NAME EXTRACTION TESTS")
    print("=" * 60)

    cases = [
        ("Two Friends", "two friends"),
        ("Two Friends Austin", "two friends"),
        ("Baby Jane", "baby jane"),
        ("Baby J & Belters Only", "baby j & belters only"),
        ("Zedd Live Brooklyn", "zedd"),
        ("Bonobo DJ Set", "bonobo"),
        ("Ultra Music Festival Miami", "ultra music"),
        ("Wire Festival", "wire festival"),  # idx < 5, keeps "festival"
        ("Adam Beyer", "adam beyer"),
        ("Adam Ten", "adam ten"),
        ("Martin Garrix", "martin garrix"),
        ("Ranger Trucco", "ranger trucco"),
        ("Rüfüs Du Sol", "rufus du sol"),
        ("Oskar Med K", "oskar med k"),
        ("Chris Lake at Brooklyn Mirage", "chris lake"),
        ("Fred again.. NYC", "fred again.."),
        ("Solid Grooves New York", "solid grooves"),
    ]

    failures = 0
    for raw, expected in cases:
        result = matcher.extract_artist_name(raw)
        status = "✓" if result == expected else "✗"
        if status == "✗":
            failures += 1
        print(f"  {status} \"{raw}\" → \"{result}\"" +
              (f"  (expected \"{expected}\")" if status == "✗" else ""))

    print(f"\n  {len(cases) - failures}/{len(cases)} passed\n")
    return failures


def test_name_similarity_pairs():
    """Test known match/non-match pairs."""
    print("=" * 60)
    print("NAME SIMILARITY TESTS")
    print("=" * 60)

    # (name1, name2, should_match, description)
    pairs = [
        ("Two Friends", "Two Friends", True, "exact match"),
        ("Adam Beyer", "Adam Ten", False, "different artists"),
        ("Martin Garrix", "Ranger Trucco", False, "completely different"),
        ("Baby Jane", "Baby J & Belters Only", False, "partial name overlap"),
        ("Zedd Live", "Zedd", True, "suffix stripping"),
        ("Bonobo DJ Set", "Bonobo", True, "suffix stripping"),
        ("Chris Lake", "Chris Lake at Brooklyn Mirage", True, "venue in name"),
        ("Rüfüs Du Sol", "Rufus Du Sol", True, "accent normalization"),
        ("Fred again..", "Fred Again", True, "punctuation difference"),
        ("Adam Beyer", "Adam Beyer", True, "exact match"),
        ("Two Friends", "Three Friends", False, "similar but different"),
    ]

    failures = 0
    for name1, name2, should_match, desc in pairs:
        score = matcher._name_similarity(name1, name2)
        matched = score >= matcher.MATCH_THRESHOLD
        correct = matched == should_match
        status = "✓" if correct else "✗"
        if not correct:
            failures += 1
        label = "MATCH" if matched else "NO MATCH"
        expected_label = "should match" if should_match else "should NOT match"
        print(f"  {status} [{score:3d}] \"{name1}\" vs \"{name2}\" → {label} ({desc}, {expected_label})")

    # Also test GroupMe thresholds
    print(f"\n  GroupMe thresholds (GM_MATCH=80, GM_CONFIRMED=70):")
    gm_pairs = [
        ("Adam Beyer", "Adam Ten", "should reject"),
        ("Two Friends", "Two Friends", "should accept"),
        ("Chris Lake", "Chris Lake", "should accept"),
        ("Martin Garrix", "Ranger Trucco", "should reject"),
    ]
    for name1, name2, desc in gm_pairs:
        score = matcher._name_similarity(name1, name2)
        gm_status = "MATCH" if score >= 80 else ("CONFIRMED ONLY" if score >= 70 else "REJECT")
        print(f"    [{score:3d}] \"{name1}\" vs \"{name2}\" → {gm_status} ({desc})")

    print(f"\n  {len(pairs) - failures}/{len(pairs)} passed\n")
    return failures


def test_city_matching():
    """Test city matching and aliases."""
    print("=" * 60)
    print("CITY MATCHING TESTS")
    print("=" * 60)

    pairs = [
        ("New York", "New York", True, "exact"),
        ("Brooklyn, NY, US", "New York, NY, US", True, "NYC alias"),
        ("Austin", "New York", False, "different cities"),
        ("Miami", "Miami Beach", True, "Miami alias"),
        ("Los Angeles", "Hollywood", True, "LA alias"),
        ("", "New York", True, "empty allows through"),
        ("", "", True, "both empty allows through"),
        ("Chicago", "Rosemont", True, "Chicago alias"),
        ("Austin", "Brooklyn", False, "Austin vs Brooklyn"),
        ("East Rutherford", "New York", True, "NJ metro alias"),
    ]

    failures = 0
    for c1, c2, expected, desc in pairs:
        result = matcher._cities_match(c1, c2)
        correct = result == expected
        status = "✓" if correct else "✗"
        if not correct:
            failures += 1
        print(f"  {status} \"{c1}\" vs \"{c2}\" → {result} ({desc})")

    print(f"\n  {len(pairs) - failures}/{len(pairs)} passed\n")
    return failures


def test_live_crowdvolt_vs_providers():
    """Fetch real CrowdVolt events and test matching against providers."""
    print("=" * 60)
    print("LIVE MATCHING: CrowdVolt vs Third-Party Providers")
    print("=" * 60)

    cv_events = crowdvolt.fetch_all_events()
    if not cv_events:
        print("  Could not fetch CrowdVolt events — skipping live test")
        return 0

    print(f"  Fetched {len(cv_events)} CrowdVolt events\n")

    issues = 0
    tested = 0

    for cv in cv_events[:30]:  # cap at 30 to avoid rate limits
        query = matcher.extract_artist_name(cv.name)
        date_str = cv.event_date.strftime("%Y-%m-%d") if cv.event_date else None
        cv_date_str = cv.event_date.strftime("%b %d") if cv.event_date else "no date"
        cv_city = cv.city or "(no city)"
        cv_venue = cv.venue or "(no venue)"

        print(f"  [{cv.name}] city=\"{cv_city}\" venue=\"{cv_venue}\" date={cv_date_str} platform={cv.ticket_platform}")
        print(f"    query → \"{query}\"")

        # SeatGeek
        if config.SEATGEEK_CLIENT_ID:
            try:
                sg_results = seatgeek.search_events(query, date_str)
                for sg in sg_results[:3]:
                    score = matcher._name_similarity(cv.name, sg.title)
                    date_ok = matcher._dates_match(cv.event_date, sg.event_date)
                    loc_ok = matcher._cities_match(cv.city, sg.city)
                    would_match = score >= matcher.MATCH_THRESHOLD and date_ok and loc_ok

                    sg_city = sg.city or "(no city)"
                    sg_venue = sg.venue or "(no venue)"

                    flag = ""
                    if score >= matcher.MATCH_THRESHOLD and not date_ok:
                        flag = " ⚠️ DATE MISMATCH"
                        issues += 1
                    elif score >= matcher.MATCH_THRESHOLD and not loc_ok:
                        flag = " ⚠️ LOCATION MISMATCH — correctly rejected"
                    elif would_match:
                        flag = " ✓ MATCHED"

                    print(f"    [SG] [{score:3d}] \"{sg.title}\" city=\"{sg_city}\" venue=\"{sg_venue}\"{flag}")
                tested += 1
            except Exception as e:
                print(f"    [SG] Error: {e}")
            time.sleep(0.3)

        # TickPick
        try:
            tp_results = tickpick.search_events(query, date_str)
            for tp in tp_results[:3]:
                score = matcher._name_similarity(cv.name, tp.name)
                date_ok = matcher._dates_match(cv.event_date, tp.event_date)
                loc_ok = matcher._cities_match(cv.city, tp.city)
                would_match = score >= matcher.MATCH_THRESHOLD and date_ok and loc_ok

                tp_city = tp.city or "(no city)"
                tp_venue = tp.venue or "(no venue)"

                flag = ""
                if score >= matcher.MATCH_THRESHOLD and not date_ok:
                    flag = " ⚠️ DATE MISMATCH"
                    issues += 1
                elif score >= matcher.MATCH_THRESHOLD and not loc_ok:
                    flag = " ⚠️ LOCATION MISMATCH — correctly rejected"
                elif would_match:
                    flag = " ✓ MATCHED"

                print(f"    [TP] [{score:3d}] \"{tp.name}\" city=\"{tp_city}\" venue=\"{tp_venue}\"{flag}")
            tested += 1
        except Exception as e:
            print(f"    [TP] Error: {e}")
        time.sleep(0.3)

        print()

    print(f"  Tested {tested} event/provider combinations, {issues} potential issues\n")
    return issues


def test_live_groupme():
    """Fetch real GroupMe messages and test matching against CrowdVolt."""
    print("=" * 60)
    print("LIVE MATCHING: GroupMe vs CrowdVolt")
    print("=" * 60)

    if not config.GROUPME_TOKEN or not config.GROUPME_GROUP_ID:
        print("  GroupMe not configured — skipping")
        return 0

    cv_events = crowdvolt.fetch_all_events()
    if not cv_events:
        print("  Could not fetch CrowdVolt events — skipping")
        return 0

    # Filter past events like the real flow does
    today = datetime.now().date()
    active_events = [
        e for e in cv_events
        if e.event_date is None or e.event_date.date() >= today
    ]

    messages = groupme.fetch_recent_messages(
        minutes=config.GROUPME_LOOKBACK_DAYS * 24 * 60,
    )
    print(f"  {len(messages)} messages in {config.GROUPME_LOOKBACK_DAYS}-day window")
    print(f"  {len(active_events)} active CrowdVolt events (past filtered)\n")

    # Parse buy requests
    buy_requests = groupme.parse_buy_requests(messages)
    print(f"  BUY REQUESTS ({len(buy_requests)}):")
    for req in buy_requests:
        date_str = req.mentioned_date.strftime("%b %d") if req.mentioned_date else "no date"
        print(f"    \"{req.event_query}\" (date={date_str}) — {req.user}: \"{req.text[:80]}\"")

        # Show what it would match against
        best_score = 0
        best_event = None
        for event in active_events:
            score = matcher._name_similarity(req.event_query, event.name)
            if score > best_score:
                best_score = score
                best_event = event

        if best_event and best_score >= 70:
            cv_city = best_event.city or "(no city)"
            local = cv_city.lower().startswith("new york") or "brooklyn" in cv_city.lower() or "nyc" in cv_city.lower()
            date_confirmed = False
            if req.mentioned_date and best_event.event_date:
                date_confirmed = matcher._dates_match(req.mentioned_date, best_event.event_date)

            status = "ACCEPT"
            reason = ""
            if best_score >= 80:
                reason = "score >= 80"
            elif best_score >= 70 and (date_confirmed or local):
                reason = f"score >= 70 + {'date' if date_confirmed else 'NYC'} confirmed"
            else:
                status = "REJECT"
                reason = f"score {best_score} < 80 and no confirmation"

            # Date contradiction check
            if status == "ACCEPT" and req.mentioned_date and best_event.event_date:
                if not matcher._dates_match(req.mentioned_date, best_event.event_date):
                    status = "REJECT (date contradiction)"

            print(f"      → [{best_score}] \"{best_event.name}\" ({cv_city}) — {status} ({reason})")
        elif best_event:
            print(f"      → [{best_score}] \"{best_event.name}\" — REJECT (below threshold)")
        print()

    # Parse sell listings
    sell_listings = groupme.parse_sell_listings(messages)
    print(f"  SELL LISTINGS ({len(sell_listings)}):")
    events_with_bids = [e for e in active_events if e.max_bid is not None]

    for sl in sell_listings:
        price_str = f"${sl.price:.0f}" if sl.price else "no price"
        date_str = sl.mentioned_date.strftime("%b %d") if sl.mentioned_date else "no date"
        print(f"    \"{sl.event_query}\" ({price_str}, date={date_str}) — {sl.user}: \"{sl.text[:80]}\"")

        best_score = 0
        best_event = None
        for event in events_with_bids:
            score = matcher._name_similarity(sl.event_query, event.name)
            if score > best_score:
                best_score = score
                best_event = event

        if best_event and best_score >= 70:
            cv_city = best_event.city or "(no city)"
            local = matcher._cities_match("New York", best_event.city) if best_event.city else False
            date_confirmed = False
            if sl.mentioned_date and best_event.event_date:
                date_confirmed = matcher._dates_match(sl.mentioned_date, best_event.event_date)

            status = "ACCEPT"
            if best_score >= 80:
                reason = "score >= 80"
            elif date_confirmed or local:
                reason = f"score >= 70 + {'date' if date_confirmed else 'NYC'} confirmed"
            else:
                status = "REJECT"
                reason = f"score {best_score} < 80 and no confirmation"

            if status == "ACCEPT" and sl.mentioned_date and best_event.event_date:
                if not matcher._dates_match(sl.mentioned_date, best_event.event_date):
                    status = "REJECT (date contradiction)"

            bid_str = f"${best_event.max_bid:.0f}" if best_event.max_bid else "no bid"
            print(f"      → [{best_score}] \"{best_event.name}\" ({cv_city}, bid={bid_str}) — {status} ({reason})")
        elif best_event:
            print(f"      → [{best_score}] \"{best_event.name}\" — REJECT (below threshold)")
        print()

    # Run actual matchers
    print("  ACTUAL MATCHER RESULTS:")
    demand_matches = groupme.match_demand(buy_requests, active_events)
    print(f"    Demand matches: {len(demand_matches)}")
    for m in demand_matches:
        users = ", ".join(r.user for r in m.buy_requests)
        print(f"      {m.crowdvolt_event.name} ({m.crowdvolt_event.city}) ← {users}")

    supply_matches = groupme.match_supply(sell_listings, active_events)
    print(f"    Supply matches: {len(supply_matches)}")
    for m in supply_matches:
        sellers = ", ".join(s.user for s in m.sell_listings)
        print(f"      {m.crowdvolt_event.name} ({m.crowdvolt_event.city}) ← {sellers}")

    print()
    return 0


if __name__ == "__main__":
    total_failures = 0

    # Unit tests (no API calls)
    total_failures += test_name_extraction()
    total_failures += test_name_similarity_pairs()
    total_failures += test_city_matching()

    # Live tests (API calls)
    total_failures += test_live_crowdvolt_vs_providers()
    total_failures += test_live_groupme()

    print("=" * 60)
    if total_failures:
        print(f"DONE — {total_failures} issues found")
    else:
        print("DONE — all tests passed")
    print("=" * 60)
