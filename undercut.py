"""Speculative listing arbitrage — list on 3P platforms, source from CrowdVolt.

The strategy: CrowdVolt sellers price below mainstream market. List a ticket
on StubHub/VividSeats at market price (speculative — you don't have it yet).
When a buyer purchases on the 3P platform, buy from CrowdVolt at the lower
ask price and transfer to fulfill the order.

Profit = 3P sale price - 3P seller fees - CrowdVolt ask price.

This module:
1. Logs every scan result to a JSONL opportunity log for backtesting
2. Saves bid snapshots for trend analysis (48h rolling window)
3. Evaluates speculative listing opportunities
4. Sends deduplicated Discord alerts when confidence is high
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import requests

import config
from crowdvolt import CrowdVoltEvent
from matcher import ArbitrageOpportunity

# Estimated seller fees per platform (what the platform takes from the seller).
# These are distinct from buyer fees in config.py.
SELLER_FEES = {
    "StubHub": 0.125,
    "VividSeats": 0.125,
    "SeatGeek": 0.125,
    "TickPick": 0.10,
    "Gametime": 0.10,
}

# Don't re-alert the same event within this window
ALERT_COOLDOWN_HOURS = 24

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SENT_ALERTS_FILE = os.path.join(_DATA_DIR, "undercut_sent.json")
BID_HISTORY_FILE = os.path.join(_DATA_DIR, "bid_snapshots.json")
OPPORTUNITY_LOG = os.path.join(_DATA_DIR, "opportunity_log.jsonl")
LISTING_PERSISTENCE_FILE = os.path.join(_DATA_DIR, "listing_persistence.json")

# Trend snapshots and per-listing persistence both age out at this horizon.
# 7 days lets us see real direction on 30-day-out events without bloating
# disk; the prior 48h window was too short for slow-moving demand.
HISTORY_WINDOW_DAYS = 7


@dataclass
class SpeculativeOpportunity:
    crowdvolt_event: CrowdVoltEvent
    sell_platform: str         # where you'd list (e.g. "StubHub")
    sell_price: float          # market price on that platform (what you'd list at)
    sell_url: str              # link to event on 3P platform
    cv_ask: float              # what you'd pay on CrowdVolt
    seller_fee_pct: float      # 3P platform seller fee
    est_payout: float          # sell_price after seller fee
    est_profit: float          # payout - cv_ask
    margin_pct: float          # profit as % of cv_ask
    bid_count: int
    ask_count: int
    days_until: Optional[int]
    bid_trend: Optional[str]   # "up", "down", "stable", or None
    ask_trend: Optional[str]   # "up" = source cost rising (risk), "down" = cheaper
    fees_estimated: bool       # True when sell_price includes estimated buyer fees


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def _load_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# JSONL opportunity log — append-only, one line per event per scan
# ---------------------------------------------------------------------------

def _ask_depth(asks: list) -> list[dict]:
    """Summarize CrowdVolt ask-side depth: price, qty, and ticket type
    for each seller, sorted cheapest-first.  Captures up to 10 asks."""
    from crowdvolt import PREMIUM_KEYWORDS
    depth = []
    for a in sorted(asks, key=lambda x: x.all_in_price)[:10]:
        is_premium = any(k in a.ticket_type.lower() for k in PREMIUM_KEYWORDS)
        depth.append({
            "price": a.all_in_price,
            "qty": a.qty,
            "type": a.ticket_type,
            "premium": is_premium,
        })
    return depth


def _bid_depth(bids: list) -> list[dict]:
    """Summarize CrowdVolt bid-side depth: price, qty, and ticket type
    for each buyer, sorted highest-first.  Captures up to 10 bids."""
    from crowdvolt import PREMIUM_KEYWORDS
    depth = []
    for b in sorted(bids, key=lambda x: x.all_in_price, reverse=True)[:10]:
        is_premium = any(k in b.ticket_type.lower() for k in PREMIUM_KEYWORDS)
        depth.append({
            "price": b.all_in_price,
            "qty": b.qty,
            "type": b.ticket_type,
            "premium": is_premium,
        })
    return depth


def log_scan_results(
    cv_events: list[CrowdVoltEvent],
    all_opportunities: list[ArbitrageOpportunity],
    arb_count: int,
    spec_opps: list = None,
) -> None:
    """Append scan results to the opportunity log for backtesting.

    Each line captures one CrowdVolt event with full supply depth
    (individual asks with prices, quantities, and ticket types),
    cross-platform prices, demand signals, and alert outcomes.

    Key fields for speculative listing analysis:
    - crowdvolt.asks: supply depth — are there multiple sellers near min_ask?
    - crowdvolt.ask_range: price gap between cheapest and 3rd cheapest ask
    - platforms: what buyers are paying on 3P markets
    - alert: whether this event was flagged, at what price, and est. profit
    """
    os.makedirs(_DATA_DIR, exist_ok=True)
    now = datetime.now()
    scan_id = now.strftime("%Y%m%d_%H%M%S")
    today = now.date()

    # Index platform results by CV slug
    platform_prices: dict[str, dict] = {}
    for opp in all_opportunities:
        slug = opp.crowdvolt_event.slug
        platform_prices.setdefault(slug, {})[opp.source_platform] = {
            "price": opp.source_price,
            "all_in": not opp.fees_estimated,
            "url": opp.source_url,
        }

    # Index spec alerts by slug
    spec_by_slug: dict[str, "SpeculativeOpportunity"] = {}
    for s in (spec_opps or []):
        spec_by_slug[s.crowdvolt_event.slug] = s

    with open(OPPORTUNITY_LOG, "a") as f:
        for ev in cv_events:
            if ev.min_ask is None and ev.max_bid is None:
                continue

            days_until = None
            if ev.event_date:
                days_until = (ev.event_date.date() - today).days

            # Ask depth — the supply picture
            asks = _ask_depth(ev.asks)
            ga_asks = [a for a in asks if not a["premium"]]
            ask_range = None
            if len(ga_asks) >= 3:
                ask_range = round(ga_asks[2]["price"] - ga_asks[0]["price"], 2)

            # Bid depth — the demand picture (mirrors ask side)
            bids = _bid_depth(ev.bids)
            ga_bids = [b for b in bids if not b["premium"]]
            bid_range = None
            if len(ga_bids) >= 3:
                bid_range = round(ga_bids[0]["price"] - ga_bids[2]["price"], 2)

            # Alert outcome
            spec = spec_by_slug.get(ev.slug)
            alert_data = None
            if spec:
                alert_data = {
                    "platform": spec.sell_platform,
                    "sell_price": spec.sell_price,
                    "cv_source": spec.cv_ask,
                    "est_profit": spec.est_profit,
                    "margin_pct": spec.margin_pct,
                }

            entry = {
                "timestamp": now.isoformat(),
                "scan_id": scan_id,
                "event": {
                    "slug": ev.slug,
                    "name": ev.name,
                    "venue": ev.venue,
                    "city": ev.city,
                    "date": ev.event_date.strftime("%Y-%m-%d") if ev.event_date else None,
                    "platform": ev.ticket_platform,
                    "days_until": days_until,
                },
                "crowdvolt": {
                    "min_ask": ev.min_ask,
                    "max_bid": ev.max_bid,
                    "bid_count": len(ev.bids),
                    "ask_count": len(ev.asks),
                    "spread": round(ev.min_ask - ev.max_bid, 2) if ev.min_ask and ev.max_bid else None,
                    "asks": asks,
                    "ask_range": ask_range,
                    "bids": bids,
                    "bid_range": bid_range,
                    "url": ev.url,
                },
                "platforms": platform_prices.get(ev.slug, {}),
                "alert": alert_data,
            }
            f.write(json.dumps(entry) + "\n")

    count = sum(1 for ev in cv_events if ev.min_ask is not None or ev.max_bid is not None)
    print(f"[Log] {count} events written to opportunity log")


# ---------------------------------------------------------------------------
# Bid history tracking
# ---------------------------------------------------------------------------

def save_bid_snapshot(cv_events: list[CrowdVoltEvent]) -> None:
    """Append current bid/ask state for trend analysis.

    Snapshots age out at HISTORY_WINDOW_DAYS so we can read direction on
    slow-moving demand for events 2–4 weeks out.
    """
    history = _load_json(BID_HISTORY_FILE, {"snapshots": []})

    snapshot = {"timestamp": datetime.now().isoformat(), "events": {}}
    for e in cv_events:
        if e.max_bid is not None or e.min_ask is not None:
            snapshot["events"][e.slug] = {
                "bid_count": len(e.bids),
                "ask_count": len(e.asks),
                "max_bid": e.max_bid,
                "min_ask": e.min_ask,
            }

    history["snapshots"].append(snapshot)

    cutoff = (datetime.now() - timedelta(days=HISTORY_WINDOW_DAYS)).isoformat()
    history["snapshots"] = [
        s for s in history["snapshots"] if s["timestamp"] > cutoff
    ]

    _save_json(BID_HISTORY_FILE, history)


def _get_trend(slug: str, field: str) -> Optional[str]:
    """Compare a field's current value against the earliest snapshot.

    Returns "up", "down", "stable", or None if insufficient history.
    """
    history = _load_json(BID_HISTORY_FILE, {"snapshots": []})
    if len(history["snapshots"]) < 2:
        return None

    oldest = history["snapshots"][0].get("events", {}).get(slug)
    newest = history["snapshots"][-1].get("events", {}).get(slug)

    if not oldest or not newest:
        return None

    old_val = oldest.get(field)
    new_val = newest.get(field)

    if old_val is None or new_val is None:
        return None

    if new_val > old_val * 1.05:
        return "up"
    elif new_val < old_val * 0.95:
        return "down"
    return "stable"


def get_bid_trend(slug: str) -> Optional[str]:
    """Is the highest bid rising, falling, or stable?"""
    return _get_trend(slug, "max_bid")


# ---------------------------------------------------------------------------
# Per-listing persistence — track first-seen / last-seen for each individual
# bid and ask. Lets us tell sticky orders (real demand/supply) from
# flickering ones (noise) without claiming we observe fills, which we can't.
# ---------------------------------------------------------------------------

def _listing_key(listing) -> str:
    """Stable identity key for a CrowdVolt listing across scans.

    Two buyers named "John" at the same price/type would collide — accepted
    as a small ambiguity since the buyer list is rarely dense enough to hit.
    Excludes qty so a buyer reducing their order isn't seen as a new listing.
    """
    return f"{listing.user}|{listing.ticket_type}|{listing.all_in_price}"


def update_listing_persistence(cv_events: list[CrowdVoltEvent]) -> None:
    """Record first-seen and last-seen timestamps for each individual
    CrowdVolt bid and ask. Stale entries (last_seen older than the
    history window) are pruned each scan."""
    data = _load_json(LISTING_PERSISTENCE_FILE, {"events": {}})
    now_iso = datetime.now().isoformat()
    cutoff = (datetime.now() - timedelta(days=HISTORY_WINDOW_DAYS)).isoformat()

    for ev in cv_events:
        slug_data = data["events"].setdefault(ev.slug, {"bids": {}, "asks": {}})
        for side, listings in (("bids", ev.bids), ("asks", ev.asks)):
            current = slug_data[side]
            for listing in listings:
                key = _listing_key(listing)
                if key in current:
                    current[key]["last_seen"] = now_iso
                    current[key]["qty"] = listing.qty
                else:
                    current[key] = {
                        "first_seen": now_iso,
                        "last_seen": now_iso,
                        "user": listing.user,
                        "type": listing.ticket_type,
                        "price": listing.all_in_price,
                        "qty": listing.qty,
                    }

    # Prune entries we haven't seen recently, and drop empty event blocks
    for slug in list(data["events"].keys()):
        slug_data = data["events"][slug]
        for side in ("bids", "asks"):
            slug_data[side] = {
                k: v for k, v in slug_data[side].items()
                if v["last_seen"] > cutoff
            }
        if not slug_data["bids"] and not slug_data["asks"]:
            del data["events"][slug]

    _save_json(LISTING_PERSISTENCE_FILE, data)


def get_oldest_bid_age_hours(slug: str) -> Optional[float]:
    """Hours since the oldest still-active bid first appeared on this event."""
    data = _load_json(LISTING_PERSISTENCE_FILE, {"events": {}})
    bids = data.get("events", {}).get(slug, {}).get("bids", {})
    if not bids:
        return None
    oldest = min(b["first_seen"] for b in bids.values())
    delta = datetime.now() - datetime.fromisoformat(oldest)
    return delta.total_seconds() / 3600


def get_oldest_ask_age_hours(slug: str) -> Optional[float]:
    """Hours since the oldest still-active ask first appeared on this event."""
    data = _load_json(LISTING_PERSISTENCE_FILE, {"events": {}})
    asks = data.get("events", {}).get(slug, {}).get("asks", {})
    if not asks:
        return None
    oldest = min(a["first_seen"] for a in asks.values())
    delta = datetime.now() - datetime.fromisoformat(oldest)
    return delta.total_seconds() / 3600


def get_ask_trend(slug: str) -> Optional[str]:
    """Is the lowest ask rising, falling, or stable?

    For speculative listing: a rising ask means CrowdVolt source cost
    is increasing (risk). A falling ask means more supply / cheaper sourcing.
    """
    return _get_trend(slug, "min_ask")


# ---------------------------------------------------------------------------
# Alert deduplication
# ---------------------------------------------------------------------------

def _load_sent() -> dict:
    return _load_json(SENT_ALERTS_FILE, {})


def _save_sent(data: dict) -> None:
    cutoff = (datetime.now() - timedelta(hours=48)).isoformat()
    data = {k: v for k, v in data.items() if v > cutoff}
    _save_json(SENT_ALERTS_FILE, data)


def _was_recently_alerted(slug: str) -> bool:
    sent = _load_sent()
    last = sent.get(slug)
    if not last:
        return False
    try:
        last_dt = datetime.fromisoformat(last)
        return datetime.now() - last_dt < timedelta(hours=ALERT_COOLDOWN_HOURS)
    except (ValueError, TypeError):
        return False


def _mark_alerted(slug: str) -> None:
    sent = _load_sent()
    sent[slug] = datetime.now().isoformat()
    _save_sent(sent)


# ---------------------------------------------------------------------------
# Speculative listing evaluation
# ---------------------------------------------------------------------------

def find_opportunities(
    all_opportunities: list[ArbitrageOpportunity],
    cv_events: list[CrowdVoltEvent],
) -> list[SpeculativeOpportunity]:
    """Identify events where listing on a 3P platform and sourcing from
    CrowdVolt would be profitable.

    For each event with CrowdVolt sellers, checks if any 3P platform has
    higher market prices.  After deducting the 3P seller fee, the spread
    must be positive.

    Criteria:
    - CrowdVolt has active sellers (min_ask exists) — required to source
    - 3P market price > CrowdVolt ask + 3P seller fee (profitable)
    - Event is within 30 days

    Bid count is informational, not a gate — the alert surfaces it
    alongside ask depth and persistence ages so the operator can judge
    demand confidence per-opportunity.
    """
    results = []
    today = datetime.now().date()

    # Build event lookup for bid/ask counts
    event_map = {e.slug: e for e in cv_events}

    # Group platform results by event
    by_event: dict[str, list[ArbitrageOpportunity]] = {}
    for opp in all_opportunities:
        slug = opp.crowdvolt_event.slug
        by_event.setdefault(slug, []).append(opp)

    for slug, opps in by_event.items():
        cv = opps[0].crowdvolt_event

        # Must have CrowdVolt sellers to source from
        if cv.min_ask is None:
            continue

        bid_count = len(cv.bids)

        # Must be upcoming
        days_until = None
        if cv.event_date:
            days_until = (cv.event_date.date() - today).days
            if days_until > 30 or days_until < 0:
                continue

        bid_trend = get_bid_trend(slug)
        ask_trend = get_ask_trend(slug)

        # Check each platform — find the best spread
        best = None
        for opp in opps:
            platform = opp.source_platform
            seller_fee = SELLER_FEES.get(platform, 0.125)

            # The 3P price is what buyers pay on that platform.
            # If we list at that price, we'd receive: price * (1 - seller_fee)
            sell_price = opp.source_price
            payout = sell_price * (1 - seller_fee)
            profit = payout - cv.min_ask

            if profit <= 0:
                continue

            margin = (profit / cv.min_ask) * 100

            candidate = SpeculativeOpportunity(
                crowdvolt_event=cv,
                sell_platform=platform,
                sell_price=sell_price,
                sell_url=opp.source_url,
                cv_ask=cv.min_ask,
                seller_fee_pct=seller_fee,
                est_payout=round(payout, 2),
                est_profit=round(profit, 2),
                margin_pct=round(margin, 1),
                bid_count=bid_count,
                ask_count=len(cv.asks),
                days_until=days_until,
                bid_trend=bid_trend,
                ask_trend=ask_trend,
                fees_estimated=opp.fees_estimated,
            )

            if best is None or candidate.est_profit > best.est_profit:
                best = candidate

        if best:
            results.append(best)

    results.sort(key=lambda o: o.est_profit, reverse=True)
    return results


# ---------------------------------------------------------------------------
# Discord alerts
# ---------------------------------------------------------------------------

def send_alerts(opportunities: list[SpeculativeOpportunity]) -> int:
    """Send Discord alerts for speculative listing opportunities.

    Deduplicates so the same event isn't alerted more than once per 24h.
    Returns the number of alerts actually sent.
    """
    if not config.DISCORD_WEBHOOK_URL:
        return 0

    sent = 0
    for opp in opportunities:
        slug = opp.crowdvolt_event.slug
        if _was_recently_alerted(slug):
            print(f"  [Spec] {opp.crowdvolt_event.name} — already alerted, skipping")
            continue

        embed = _format_alert(opp)
        payload = {"username": "Ticket Arb", "embeds": [embed]}

        try:
            resp = requests.post(
                config.DISCORD_WEBHOOK_URL, json=payload,
                timeout=config.REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            _mark_alerted(slug)
            sent += 1
            print(f"  [Spec] Alert sent for {opp.crowdvolt_event.name}")
        except requests.RequestException as e:
            print(f"  [Spec] Failed to send alert: {e}")

    return sent


def _format_alert(opp: SpeculativeOpportunity) -> dict:
    """Format a speculative listing opportunity as a Discord embed."""
    cv = opp.crowdvolt_event
    date_str = cv.event_date.strftime("%b %d, %Y") if cv.event_date else "TBD"

    # Supply + demand signals
    signals = []
    signals.append(f"{opp.ask_count} CV sellers")
    signals.append(f"{opp.bid_count} CV buyers")
    if opp.days_until is not None:
        signals.append(f"{opp.days_until} days out")
    if opp.ask_trend == "up":
        signals.append("CV asks rising (source cost up)")
    elif opp.ask_trend == "down":
        signals.append("CV asks falling (cheaper sourcing)")
    if opp.bid_trend == "up":
        signals.append("bids trending up")

    fee_pct = int(opp.seller_fee_pct * 100)
    price_note = " (est.)" if opp.fees_estimated else ""

    return {
        "title": f"Speculative Listing — {cv.name}",
        "description": f"{cv.venue} — {cv.city} — {date_str}",
        "color": 0xFFA500,  # orange — distinct from green arb alerts
        "fields": [
            {
                "name": f"List on {opp.sell_platform}",
                "value": f"**${opp.sell_price:.0f}**{price_note} (market price)",
                "inline": True,
            },
            {
                "name": "Source from CrowdVolt",
                "value": f"**${opp.cv_ask:.0f}** (lowest seller)",
                "inline": True,
            },
            {
                "name": "Estimated Profit",
                "value": (
                    f"Payout after ~{fee_pct}% seller fee: ${opp.est_payout:.0f}\n"
                    f"Cost to source: ${opp.cv_ask:.0f}\n"
                    f"**+${opp.est_profit:.0f}** ({opp.margin_pct:.0f}%)"
                ),
                "inline": False,
            },
            {
                "name": "Demand Signals",
                "value": " | ".join(signals),
                "inline": False,
            },
            {
                "name": "Links",
                "value": (
                    f"[{opp.sell_platform}]({opp.sell_url}) | "
                    f"[CrowdVolt]({cv.url})"
                ),
                "inline": False,
            },
        ],
    }
