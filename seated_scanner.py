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
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

import config
import crowdvolt
import tickpick
from matcher import _name_similarity, _localize_cv_date, _dates_match, search_queries

# Persistent state directory (shared with other scanners)
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_SEATED_SPEC_SENT_FILE = os.path.join(_DATA_DIR, "seated_spec_sent.json")
_SEATED_SPEC_COOLDOWN_HOURS = 18  # matches undercut's dual-digest cadence
_SEATED_SPEC_DIGEST_TZ = ZoneInfo("America/New_York")
_TICKPICK_SELLER_FEE = 0.10  # TickPick takes ~10% from sellers

# Venues where tickets are sold by section — the main scanner skips these
SEATED_VENUES = {
    "barclays center",
    "madison square garden",
    "msg",
    "forest hills stadium",
}

# Slug keywords to find seated events in the CrowdVolt sitemap
_SEATED_SLUG_KEYWORDS = [
    "barclays",          # Barclays Center, Brooklyn
    "madison-square",    # Madison Square Garden, NYC
    "msg",               # MSG alias
    "forest-hills",      # Forest Hills Stadium, Queens
    "huntington-bank",   # Huntington Bank Pavilion, Chicago (Northerly Island)
    "wrigley-field",     # Wrigley Field, Chicago (stadium concerts)
]

# How often to scan in loop mode (minutes)
SEATED_SCAN_INTERVAL = 30

# Section name aliases — maps platform-specific names to a common key.
# normalize_section() canonicalizes "General Admission" → "GA" before
# this lookup runs, so both spellings hit the same entries.
#
# Design principle: bucket spelling variants of the SAME physical area
# (e.g. "Lawn", "GA Lawn", "Reserved Lawn" all = the lawn area), but
# keep distinct areas distinct (Lawn ≠ Pit ≠ Box) because they have
# different prices and a buyer expecting one will reject the other.
_SECTION_ALIASES = {
    # Bowl venue GA (Barclays / MSG / Forest Hills) — floor standing
    "front ga": "front_ga",
    "back ga": "back_ga",
    "rear ga": "back_ga",  # rear == back
    "ga (floor)": "floor_ga",
    "ga (bowl)": "bowl_ga",
    "ga floor": "floor_ga",
    "ga bowl": "bowl_ga",
    "floor": "floor",
    "front floor ga": "front_ga",
    "rear floor ga": "back_ga",
    "floor ga": "floor_ga",
    "ga": "ga",
    # Amphitheater PIT — premium standing area in front of stage
    "pit": "pit",
    "ga pit": "pit",
    "pit ga": "pit",
    # Amphitheater LAWN — grass area, all considered same physical area
    "lawn": "lawn",
    "ga lawn": "lawn",
    "lawn ga": "lawn",
    "reserved lawn": "lawn",
    "general lawn": "lawn",
    # Lawn sub-areas (front vs back of lawn) — distinct prices
    "front lawn": "front_lawn",
    "lawn front": "front_lawn",
    "back lawn": "back_lawn",
    "lawn back": "back_lawn",
    "rear lawn": "back_lawn",
    "lawn rear": "back_lawn",
    # Amphitheater BOX — premium covered seating
    "box": "box",
    "ga box": "box",
    "pavilion box": "box",
    "box seats": "box",
}

# Position qualifiers — words that mean "this GA is a specific physical
# area, not generic GA." When present in a section name, the wide GA
# bucketing rule (^ga\b → ga) should NOT apply. The alias table above
# handles the actual mapping; this list just keeps the wide rule from
# overcollapsing things like "GA Lawn" or "GA Pit" into bare "ga".
_POSITION_QUALIFIERS = {
    "lawn", "pit", "box", "bowl", "floor",
    "front", "back", "rear", "side",
    "balcony", "mezzanine", "mezz", "loge",
    "pavilion", "field", "deck",
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
    # not real tier upgrades. SKIP this collapse when a position qualifier
    # is present (e.g. "GA Lawn", "GA Pit") — those are physically distinct
    # areas with different prices, not just spelling variants of generic GA.
    # The alias table above maps known venue-specific GA areas; this rule
    # only fires for things like "GA Standing" / "GA Plus" that have no
    # position qualifier.
    if re.match(r"^ga\b", lower):
        rest = lower[2:].strip()
        if not any(q in rest.split() for q in _POSITION_QUALIFIERS):
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

            # --- Date check — centralized on nightlife semantics ---
            # Uses _dates_match so a TP listing stored as next-day-1am
            # (when the actual show is the prior evening's afters) doesn't
            # falsely reject. Same fix pattern as the main matchers.
            if not tp.event_date or not cv_local:
                continue
            if not _dates_match(cv_local, tp.event_date):
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
# Section-level speculative listing (CV → TP direction)
# ---------------------------------------------------------------------------
# Mirror of find_section_arbitrage but inverted: list on TickPick at the
# section's market price, source from CrowdVolt at a cheaper ask. Same
# section-matching machinery as forward arb, opposite price direction.

@dataclass
class SeatedSpecOpportunity:
    crowdvolt_event: crowdvolt.CrowdVoltEvent
    section: str            # normalized section key
    cv_ask_price: float     # what we'd pay on CV
    tp_price: float         # what buyers pay on TP (our listing price)
    est_payout: float       # tp_price * (1 - seller_fee)
    est_profit: float       # payout - cv_ask_price
    margin_pct: float
    days_until: Optional[int]
    tp_section_raw: str     # original TP section string
    cv_section_raw: str     # original CV ticket_type string
    tp_url: str
    ask_count_at_section: int  # supply cushion at this section


def find_section_spec_arbitrage(
    cv_event: crowdvolt.CrowdVoltEvent,
    tp_listings: list[SectionListing],
    tp_url: str,
) -> list[SeatedSpecOpportunity]:
    """Spec direction: sell on TP at market section price, source from CV
    ask at the same section.

    Requirements:
    - Event in the future (past shows can't be spec-listed)
    - Section has 3+ CV asks (ample sourcing cushion)
    - profit >= config.MIN_SPEC_PROFIT after 10% TickPick seller fee
    """
    today = datetime.now().date()

    # Past-event filter only (no upper horizon — far-out arena shows
    # still produce real opportunities, esp. when CV ask cushion is deep)
    cv_local = _localize_cv_date(cv_event)
    days_until = None
    if cv_local:
        days_until = (cv_local.date() - today).days
        if days_until < 0:
            return []

    # Group CV asks by normalized section
    asks_by_section: dict[str, list] = {}
    for ask in cv_event.asks:
        key = normalize_section(ask.ticket_type)
        asks_by_section.setdefault(key, []).append(ask)

    # Cheapest TP listing per section
    tp_cheapest: dict[str, SectionListing] = {}
    for listing in tp_listings:
        key = listing.section_norm
        if key not in tp_cheapest or listing.price < tp_cheapest[key].price:
            tp_cheapest[key] = listing

    opportunities = []
    for section_key, asks in asks_by_section.items():
        if section_key not in tp_cheapest:
            continue
        if len(asks) < 3:
            continue  # ample-cushion gate

        cheapest_ask = min(asks, key=lambda a: a.all_in_price)
        tp = tp_cheapest[section_key]
        payout = tp.price * (1 - _TICKPICK_SELLER_FEE)
        profit = payout - cheapest_ask.all_in_price

        if profit < config.MIN_SPEC_PROFIT:
            continue

        margin = (profit / cheapest_ask.all_in_price) * 100

        opportunities.append(SeatedSpecOpportunity(
            crowdvolt_event=cv_event,
            section=section_key,
            cv_ask_price=cheapest_ask.all_in_price,
            tp_price=tp.price,
            est_payout=round(payout, 2),
            est_profit=round(profit, 2),
            margin_pct=round(margin, 1),
            days_until=days_until,
            tp_section_raw=tp.section,
            cv_section_raw=cheapest_ask.ticket_type,
            tp_url=tp_url,
            ask_count_at_section=len(asks),
        ))

    opportunities.sort(key=lambda o: o.est_profit, reverse=True)
    return opportunities


def _spec_was_recently_alerted(slug: str, section: str) -> bool:
    try:
        with open(_SEATED_SPEC_SENT_FILE) as f:
            sent = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    last = sent.get(f"{slug}:{section}")
    if not last:
        return False
    try:
        return datetime.now() - datetime.fromisoformat(last) < timedelta(hours=_SEATED_SPEC_COOLDOWN_HOURS)
    except (ValueError, TypeError):
        return False


def _spec_mark_alerted(slug: str, section: str) -> None:
    try:
        with open(_SEATED_SPEC_SENT_FILE) as f:
            sent = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        sent = {}
    sent[f"{slug}:{section}"] = datetime.now().isoformat()
    cutoff = (datetime.now() - timedelta(hours=_SEATED_SPEC_COOLDOWN_HOURS * 2)).isoformat()
    sent = {k: v for k, v in sent.items() if v > cutoff}
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(_SEATED_SPEC_SENT_FILE, "w") as f:
        json.dump(sent, f)


def _format_spec_embed(opp: SeatedSpecOpportunity) -> dict:
    cv = opp.crowdvolt_event
    cv_local = _localize_cv_date(cv) if cv.event_date else None
    date_str = (cv_local or cv.event_date).strftime("%b %d, %Y") if cv.event_date else "TBD"
    days_label = f"{opp.days_until} days out" if opp.days_until is not None else ""

    return {
        "title": f"🏟️ {cv.name} — {opp.cv_section_raw}",
        "description": f"{cv.venue} — {cv.city} — {date_str}",
        "color": 0x8B5CF6,  # purple — distinct from forward-arb green / GA-spec orange
        "fields": [
            {"name": "Section", "value": f"`{opp.section}`", "inline": True},
            {"name": "Sell on TP / Source CV",
             "value": f"${opp.tp_price:.0f} / ${opp.cv_ask_price:.0f}",
             "inline": True},
            {"name": "Est. Profit",
             "value": f"**+${opp.est_profit:.0f}** ({opp.margin_pct:.0f}%)",
             "inline": True},
            {"name": "Supply cushion",
             "value": f"{opp.ask_count_at_section} CV asks at this section · {days_label}",
             "inline": False},
            {"name": "Links",
             "value": f"[TickPick]({opp.tp_url}) | [CrowdVolt]({cv.url})",
             "inline": False},
        ],
    }


def send_spec_digest(opportunities: list[SeatedSpecOpportunity]) -> int:
    """Section-level spec digest. Gated on config.SPEC_DIGEST_HOURS in NYC time
    so it lands alongside the GA-level spec digest from undercut.py."""
    if not config.DISCORD_WEBHOOK_URL or not opportunities:
        return 0

    now_nyc = datetime.now(_SEATED_SPEC_DIGEST_TZ)
    if now_nyc.hour not in config.SPEC_DIGEST_HOURS:
        return 0

    fresh = [o for o in opportunities
             if not _spec_was_recently_alerted(o.crowdvolt_event.slug, o.section)]
    if not fresh:
        return 0

    fresh.sort(key=lambda o: o.est_profit, reverse=True)
    top = fresh[:10]

    date_str = now_nyc.strftime("%b %d %I%p").lstrip("0").replace(" 0", " ")
    plural = "ies" if len(fresh) != 1 else "y"
    summary = (f"🏟️ **Stadium/Arena Spec Digest** — {date_str} — "
               f"{len(fresh)} section opportunit{plural}")
    if len(fresh) > 10:
        summary += "  _(showing top 10 by profit)_"

    payload = {
        "username": "Ticket Arb",
        "content": summary,
        "embeds": [_format_spec_embed(o) for o in top],
    }
    try:
        resp = requests.post(config.DISCORD_WEBHOOK_URL, json=payload,
                             timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
        for o in top:
            _spec_mark_alerted(o.crowdvolt_event.slug, o.section)
        print(f"  [Seated Spec] Digest sent — {len(top)} section opps")
        return len(top)
    except requests.RequestException as e:
        print(f"  [Seated Spec] Digest send failed: {e}")
        return 0


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

    # Process events with bids (forward arb) OR asks (spec listing). Same
    # TP fetch supports both directions, so doing them together is efficient.
    active_events = [e for e in seated_events if e.bids or e.asks]
    bid_events = [e for e in active_events if e.bids]
    ask_events = [e for e in active_events if e.asks]
    print(f"[Seated] {len(bid_events)} have bids (forward arb), "
          f"{len(ask_events)} have asks (spec listing)")

    if not active_events:
        print("[Seated] No seated events with market activity — nothing to scan")
        if not dry_run:
            send_summary(0, 0, 0, 0)
        return 0

    all_opportunities = []
    all_spec_opportunities = []
    matched_count = 0
    errors = 0

    for cv_event in active_events:
        print(f"\n[Seated] {cv_event.name} @ {cv_event.venue}")
        for bid in cv_event.bids:
            print(f"  Bid: [{bid.ticket_type}] ${bid.all_in_price:.0f} x{bid.qty}")
        if cv_event.asks:
            print(f"  Asks: {len(cv_event.asks)} sellers (min ${cv_event.min_ask:.0f})")

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

        # Forward arb: TP cheap → fill CV bid. Only when event has bids.
        if cv_event.bids:
            opps = find_section_arbitrage(cv_event, tp_listings, tp_event.url)
            if opps:
                print(f"  [Forward arb: {len(opps)} opportunities]")
                for opp in opps:
                    print(f"    [{opp.cv_section_raw}] TP ${opp.tp_price:.0f} "
                          f"→ CV bid ${opp.cv_bid_price:.0f} = "
                          f"+${opp.profit:.0f}")
                all_opportunities.extend(opps)
            else:
                print("  No forward-arb section opportunities")

        # Spec listing: sell on TP at section market → source from CV ask.
        # Only when event has asks (need CV sellers to source from).
        if cv_event.asks:
            spec_opps = find_section_spec_arbitrage(
                cv_event, tp_listings, tp_event.url)
            if spec_opps:
                print(f"  [Spec listing: {len(spec_opps)} opportunities]")
                for opp in spec_opps:
                    print(f"    [{opp.cv_section_raw}] CV ${opp.cv_ask_price:.0f} "
                          f"→ TP ${opp.tp_price:.0f} = +${opp.est_profit:.0f} "
                          f"({opp.ask_count_at_section} CV asks cushion)")
                all_spec_opportunities.extend(spec_opps)
            else:
                print("  No section-level spec opportunities")

        time.sleep(1)  # pace requests

    # Send forward-arb alerts (per event, per scan — short-lived opps)
    if not dry_run:
        by_event: dict[str, list[SeatedOpportunity]] = {}
        for opp in all_opportunities:
            slug = opp.crowdvolt_event.slug
            by_event.setdefault(slug, []).append(opp)

        for slug, opps in by_event.items():
            send_alert(opps)
            time.sleep(1)

        # Send spec digest (only fires at 1pm/5pm NYC hours, deduped)
        if all_spec_opportunities:
            send_spec_digest(all_spec_opportunities)

        send_summary(len(bid_events), matched_count,
                     len(all_opportunities), errors)

    print(f"\n[Seated] Done — {len(all_opportunities)} forward-arb opps, "
          f"{len(all_spec_opportunities)} spec opps")
    return len(all_opportunities) + len(all_spec_opportunities)


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
