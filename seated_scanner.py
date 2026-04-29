"""Seated venue arbitrage scanner — section-level matching via TickPick.

Handles arena/stadium events (MSG, Barclays, Forest Hills) that the main
scanner skips because section-based pricing makes overall lowest price
meaningless. Compares CrowdVolt bids per section against TickPick listings
in the same section.

TickPick is the only platform that exposes section-level listing data via
an internal API we can intercept with Playwright. Other platforms either
block scraping (SeatGeek, VividSeats) or don't expose section data.

Usage:
    python seated_scanner.py          # run once
    python seated_scanner.py --dry    # preview without sending to Discord
    python seated_scanner.py --loop   # run on a schedule (every 30 min)
"""

import argparse
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import requests
from playwright.sync_api import sync_playwright

import config
import crowdvolt
import tickpick
from matcher import _name_similarity, _localize_cv_date, search_queries

# Venues where tickets are sold by section — the main scanner skips these
SEATED_VENUES = {
    "barclays center",
    "madison square garden",
    "msg",
    "forest hills stadium",
}

# Slug keywords to find seated events in the CrowdVolt sitemap
_SEATED_SLUG_KEYWORDS = ["barclays", "madison-square", "forest-hills", "msg"]

# How often to scan in loop mode (minutes)
SEATED_SCAN_INTERVAL = 30

# Section name aliases — maps platform-specific names to a common key.
# normalize_section() canonicalizes "General Admission" → "GA" before
# this lookup runs, so both spellings hit the same entries.
_SECTION_ALIASES = {
    # CrowdVolt-style
    "front ga": "front_ga",
    "back ga": "back_ga",
    "rear ga": "back_ga",  # rear == back
    "ga (floor)": "floor_ga",
    "ga (bowl)": "bowl_ga",
    "ga floor": "floor_ga",
    "ga bowl": "bowl_ga",
    "floor": "floor",
    "pit": "pit",
    # TickPick-style
    "front floor ga": "front_ga",
    "rear floor ga": "back_ga",
    "floor ga": "floor_ga",
    "ga": "ga",
}


@dataclass
class SectionListing:
    section: str        # raw section name from TickPick
    section_norm: str   # normalized section key
    row: str
    price: float        # all-in price (TickPick has no buyer fees)
    qty: int


@dataclass
class SeatedOpportunity:
    crowdvolt_event: crowdvolt.CrowdVoltEvent
    section: str        # normalized section key
    cv_bid_price: float
    tp_price: float
    profit: float
    tp_section_raw: str
    cv_section_raw: str
    tp_url: str


def normalize_section(raw: str) -> str:
    """Normalize a section name for cross-platform comparison.

    CrowdVolt: "Section 221", "Sec 16", "Front General Admission", "GA (Floor)"
    TickPick:  "221", "Front Floor GA", "Rear Floor GA"

    Returns a common key like "221", "front_ga", "floor_ga".
    """
    lower = raw.lower().strip()

    # Canonicalize "General Admission" → "GA" so both spellings collapse
    # onto the same alias entries (e.g. "Front General Admission" and
    # "Front Floor GA" both end up as "front_ga").
    lower = re.sub(r"\bgeneral admission\b", "ga", lower)

    # Check alias table first
    if lower in _SECTION_ALIASES:
        return _SECTION_ALIASES[lower]

    # "Section 221" / "Sec 16" / "Sec. 7" → "221" / "16" / "7"
    num_match = re.match(r"^(?:section|sec)\.?\s*(\d+)", lower)
    if num_match:
        return num_match.group(1)

    # Already a bare number
    if re.match(r"^\d+$", lower):
        return lower

    # Bucket unrecognized GA-prefix variants (GA+, GA Plus, GA Premium, etc.)
    # to plain "ga" — these are vendor-specific spellings of the same tier,
    # not real tier upgrades. Structured GA variants (Front/Back/Floor/Bowl)
    # are caught by the alias table above before this runs.
    if re.match(r"^ga\b", lower):
        return "ga"

    # Fallback — return cleaned lowercase
    return re.sub(r"[^a-z0-9]+", "_", lower).strip("_")


# ---------------------------------------------------------------------------
# TickPick event discovery
# ---------------------------------------------------------------------------

def _find_tickpick_event(
    cv_event: crowdvolt.CrowdVoltEvent,
) -> Optional[tickpick.TickPickEvent]:
    """Find the TickPick event matching a CrowdVolt seated-venue event.

    All three must match:
    - Artist name (fuzzy, score >= 65)
    - Venue (must be the same arena)
    - Date (exact calendar day — critical for multi-night runs)
    """
    queries = search_queries(cv_event.name)
    cv_local = _localize_cv_date(cv_event)

    for q in queries:
        date_str = cv_local.strftime("%Y-%m-%d") if cv_local else None
        try:
            results = tickpick.search_events(q, date_str)
        except Exception as e:
            print(f"  [TickPick] Search error for '{q}': {e}")
            continue

        for tp in results:
            # --- Name check ---
            score = _name_similarity(q, tp.name) if tp.name else 0
            if score < 65:
                continue

            # --- Venue check ---
            if not tp.venue:
                continue
            tp_v = tp.venue.lower().strip()
            cv_v = cv_event.venue.lower().strip()
            venue_ok = (
                tp_v == cv_v
                or tp_v in cv_v
                or cv_v in tp_v
                or (cv_v == "msg" and "madison square garden" in tp_v)
                or ("madison square garden" in cv_v and tp_v == "msg")
            )
            if not venue_ok:
                continue

            # --- Date check (exact day) ---
            if not tp.event_date or not cv_local:
                continue  # can't confirm without both dates
            if tp.event_date.date() != cv_local.date():
                continue

            print(f"  [TickPick] Matched: {tp.name} @ {tp.venue} "
                  f"({tp.event_date.date()}) [score={score}]")
            return tp

    return None


# ---------------------------------------------------------------------------
# TickPick section-level listings
# ---------------------------------------------------------------------------

def fetch_section_listings(event_url: str) -> list[SectionListing]:
    """Load a TickPick event page and intercept the listings API response.

    TickPick's browser calls api.tickpick.com/1.0/listings/internal/event-v2/{id}
    which returns all listings with section, row, price, and quantity.
    This API returns 403 on direct HTTP — Playwright is required.
    """
    api_data = []

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 800},
                )

                def on_response(response):
                    if "listings/internal/event-v2" in response.url:
                        try:
                            api_data.append(response.json())
                        except Exception:
                            pass

                page.on("response", on_response)
                page.goto(event_url, wait_until="networkidle", timeout=30000)
            finally:
                browser.close()
    except Exception as e:
        print(f"  [Seated] Playwright error: {e}")
        return []

    if not api_data:
        print("  [Seated] No listings API data intercepted")
        return []

    raw_listings = api_data[0].get("listings", [])
    listings = []

    for item in raw_listings:
        sid = str(item.get("sid", ""))
        row = str(item.get("r", ""))
        price = item.get("p")
        qty = item.get("q", 1)

        if not sid or price is None:
            continue

        # Skip parking listings
        if "parking" in sid.lower() or "parking" in row.lower():
            continue

        # Skip entries without a section marker (e.g. misc add-ons)
        mk = item.get("mk", "")
        if not mk and not re.match(
            r"^(?:Section |Sec |Front |Rear |GA|Floor|Pit|\d)", sid
        ):
            continue

        listings.append(SectionListing(
            section=sid,
            section_norm=normalize_section(sid),
            row=row,
            price=float(price),
            qty=int(qty),
        ))

    return listings


# ---------------------------------------------------------------------------
# Arbitrage comparison
# ---------------------------------------------------------------------------

def find_section_arbitrage(
    cv_event: crowdvolt.CrowdVoltEvent,
    tp_listings: list[SectionListing],
    tp_url: str,
) -> list[SeatedOpportunity]:
    """Compare CrowdVolt bids against TickPick listings, section by section."""

    # Cheapest TickPick listing per normalized section
    tp_cheapest: dict[str, SectionListing] = {}
    for listing in tp_listings:
        key = listing.section_norm
        if key not in tp_cheapest or listing.price < tp_cheapest[key].price:
            tp_cheapest[key] = listing

    opportunities = []

    for bid in cv_event.bids:
        bid_section = normalize_section(bid.ticket_type)

        if bid_section not in tp_cheapest:
            continue

        tp = tp_cheapest[bid_section]
        profit = bid.all_in_price - tp.price

        if profit <= 0:
            continue

        margin = (profit / tp.price) * 100
        if margin < config.MIN_PROFIT_MARGIN_PCT:
            continue

        opportunities.append(SeatedOpportunity(
            crowdvolt_event=cv_event,
            section=bid_section,
            cv_bid_price=bid.all_in_price,
            tp_price=tp.price,
            profit=profit,
            tp_section_raw=tp.section,
            cv_section_raw=bid.ticket_type,
            tp_url=tp_url,
        ))

    opportunities.sort(key=lambda o: o.profit, reverse=True)
    return opportunities


# ---------------------------------------------------------------------------
# Discord notifications
# ---------------------------------------------------------------------------

def send_alert(opportunities: list[SeatedOpportunity]) -> bool:
    """Send a Discord alert for section-level arbitrage at one event."""
    if not config.DISCORD_WEBHOOK_URL or not opportunities:
        return False

    cv = opportunities[0].crowdvolt_event
    tp_url = opportunities[0].tp_url

    fields = []
    for opp in opportunities[:15]:
        margin = (opp.profit / opp.tp_price) * 100
        fields.append({
            "name": f"{opp.cv_section_raw} — +${opp.profit:.0f} ({margin:.0f}%)",
            "value": (
                f"TickPick: **${opp.tp_price:.0f}**\n"
                f"CrowdVolt buyer: **${opp.cv_bid_price:.0f}**"
            ),
            "inline": True,
        })

    payload = {
        "username": "Ticket Arb",
        "embeds": [{
            "title": f"\U0001f3df\ufe0f {cv.name} @ {cv.venue}",
            "description": (
                f"**{len(opportunities)}** section-level opportunities\n"
                f"[TickPick]({tp_url}) | [CrowdVolt]({cv.url})"
            ),
            "color": 0x4CAF50,
            "fields": fields[:25],
        }],
    }

    try:
        resp = requests.post(
            config.DISCORD_WEBHOOK_URL, json=payload,
            timeout=config.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        print(f"[Seated] Alert sent for {cv.name}")
        return True
    except requests.RequestException as e:
        print(f"[Seated] Failed to send alert: {e}")
        return False


def send_summary(
    total_events: int, matched: int, opportunities: int, errors: int,
) -> bool:
    """Send a scan summary to Discord."""
    if not config.DISCORD_WEBHOOK_URL:
        return False

    now = datetime.now().strftime("%b %d, %Y %I:%M %p")
    payload = {
        "username": "Ticket Arb",
        "embeds": [{
            "title": f"\U0001f3df\ufe0f Seated Venue Scan — {now}",
            "description": (
                f"**{total_events}** seated events with buyers\n"
                f"**{matched}** matched on TickPick\n"
                f"**{opportunities}** section-level opportunities\n"
                f"**{errors}** errors"
            ),
            "color": 0x4CAF50 if opportunities > 0 else 0x95A5A6,
        }],
    }

    try:
        resp = requests.post(
            config.DISCORD_WEBHOOK_URL, json=payload,
            timeout=config.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        print("[Seated] Summary sent to Discord")
        return True
    except requests.RequestException as e:
        print(f"[Seated] Failed to send summary: {e}")
        return False


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

def scan_once(dry_run: bool = False) -> int:
    """Run a single seated venue scan. Returns number of opportunities."""
    print(f"\n{'='*60}")
    print(f"[Seated] Starting at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    # Fetch only seated-venue events from the sitemap — no need to crawl
    # all 600+ CrowdVolt pages when we only care about ~10 slugs.
    print("[Seated] Fetching CrowdVolt sitemap...")
    slugs = crowdvolt.fetch_sitemap()
    seated_slugs = [
        s for s in slugs
        if any(k in s.lower() for k in _SEATED_SLUG_KEYWORDS)
    ]
    print(f"[Seated] {len(seated_slugs)} seated venue slugs found")

    # Fetch each event individually
    seated_events = []
    for slug in seated_slugs:
        event = crowdvolt.fetch_event(slug)
        if event:
            seated_events.append(event)
        time.sleep(0.3)

    # Filter to future events
    today = datetime.now().date()
    seated_events = [
        e for e in seated_events
        if e.event_date is None or e.event_date.date() >= today
    ]
    print(f"[Seated] {len(seated_events)} future seated events")

    for e in seated_events:
        bid_info = f"max bid ${e.max_bid:.0f}" if e.max_bid else "no bids"
        ask_info = f"min ask ${e.min_ask:.0f}" if e.min_ask else "no asks"
        print(f"  {e.name} @ {e.venue} — {bid_info}, {ask_info}")

    # Only process events with active bids
    bid_events = [e for e in seated_events if e.bids]
    print(f"[Seated] {len(bid_events)} have active buyers")

    if not bid_events:
        print("[Seated] No seated events with bids — nothing to scan")
        if not dry_run:
            send_summary(0, 0, 0, 0)
        return 0

    all_opportunities = []
    matched_count = 0
    errors = 0

    for cv_event in bid_events:
        print(f"\n[Seated] {cv_event.name} @ {cv_event.venue}")
        for bid in cv_event.bids:
            print(f"  Bid: [{bid.ticket_type}] ${bid.all_in_price:.0f} x{bid.qty}")

        # Find matching TickPick event
        try:
            tp_event = _find_tickpick_event(cv_event)
        except Exception as e:
            print(f"  [TickPick] Error: {e}")
            errors += 1
            continue

        if not tp_event:
            print("  [TickPick] No matching event found")
            continue

        matched_count += 1

        # Fetch section-level listings
        try:
            tp_listings = fetch_section_listings(tp_event.url)
        except Exception as e:
            print(f"  [TickPick] Listings error: {e}")
            errors += 1
            continue

        if not tp_listings:
            continue

        # Log section summary
        sections: dict[str, float] = {}
        for l in tp_listings:
            if l.section_norm not in sections:
                sections[l.section_norm] = l.price
            else:
                sections[l.section_norm] = min(sections[l.section_norm], l.price)

        print(f"  [TickPick] {len(tp_listings)} listings across "
              f"{len(sections)} sections")

        # Compare per section
        opps = find_section_arbitrage(cv_event, tp_listings, tp_event.url)

        if opps:
            print(f"  [{len(opps)} opportunities]")
            for opp in opps:
                print(f"    [{opp.cv_section_raw}] TP ${opp.tp_price:.0f} "
                      f"→ CV bid ${opp.cv_bid_price:.0f} = "
                      f"+${opp.profit:.0f}")
            all_opportunities.extend(opps)
        else:
            print("  No section-level arbitrage")

        time.sleep(1)  # pace requests

    # Send alerts grouped by event
    if not dry_run:
        by_event: dict[str, list[SeatedOpportunity]] = {}
        for opp in all_opportunities:
            slug = opp.crowdvolt_event.slug
            by_event.setdefault(slug, []).append(opp)

        for slug, opps in by_event.items():
            send_alert(opps)
            time.sleep(1)

        send_summary(len(bid_events), matched_count,
                     len(all_opportunities), errors)

    print(f"\n[Seated] Done — {len(all_opportunities)} opportunities")
    return len(all_opportunities)


def main():
    parser = argparse.ArgumentParser(
        description="Seated venue arbitrage scanner (section-level)")
    parser.add_argument("--dry", action="store_true",
                        help="Preview without sending to Discord")
    parser.add_argument("--loop", action="store_true",
                        help="Run on a schedule")
    args = parser.parse_args()

    if args.loop:
        print(f"[Seated] Running every {SEATED_SCAN_INTERVAL} minutes")
        print("[Seated] Press Ctrl+C to stop\n")
        while True:
            try:
                scan_once(dry_run=args.dry)
                print(f"\n[Seated] Next scan in {SEATED_SCAN_INTERVAL} min...")
                time.sleep(SEATED_SCAN_INTERVAL * 60)
            except KeyboardInterrupt:
                print("\n[Seated] Stopped")
                break
    else:
        scan_once(dry_run=args.dry)


if __name__ == "__main__":
    main()
