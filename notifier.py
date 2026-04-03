"""Send arbitrage alerts to Discord via webhook."""

import requests

import config
from groupme import GroupMeMatch, GroupMeSellMatch
from matcher import ArbitrageOpportunity


def _format_opportunity(opps: list[ArbitrageOpportunity]) -> dict:
    """Format a consolidated arbitrage alert for one CrowdVolt event.

    Takes all platform opportunities for a single event and renders
    one embed showing prices across platforms, the best arb, and links.
    """
    # Only include opportunities that have a bid to sell to
    opps = [o for o in opps if o.profit_vs_bid is not None]
    if not opps:
        return None

    # Sort cheapest first
    opps = sorted(opps, key=lambda o: o.source_price)
    best = opps[0]
    cv = best.crowdvolt_event

    margin = (best.profit_vs_bid / best.source_price) * 100

    # Price list across platforms
    price_lines = []
    for opp in opps:
        label = opp.source_platform
        price_str = f"${opp.source_price:.0f}"
        if opp.fees_estimated:
            price_str += " (est. w/ fees)"
        price_lines.append(f"**{label}** — {price_str}")

    fields = [
        {
            "name": "Prices",
            "value": "\n".join(price_lines),
            "inline": True,
        },
        {
            "name": "Highest CrowdVolt Offer",
            "value": f"**${best.crowdvolt_bid:.0f}**",
            "inline": True,
        },
        {
            "name": "Best Arbitrage",
            "value": (
                f"Buy on **{best.source_platform}** (${best.source_price:.0f})"
                f" → Sell on **CrowdVolt** (${best.crowdvolt_bid:.0f})\n"
                f"**+${best.profit_vs_bid:.0f}** ({margin:.1f}%)"
            ),
            "inline": False,
        },
    ]

    # Links
    link_parts = [f"[CrowdVolt]({cv.url})"]
    for opp in opps:
        link_parts.append(f"[{opp.source_platform}]({opp.source_url})")
    fields.append({
        "name": "Links",
        "value": " | ".join(link_parts),
        "inline": False,
    })

    date_str = cv.event_date.strftime("%b %d, %Y") if cv.event_date else "TBD"
    platform_str = f" · via {cv.ticket_platform}" if cv.ticket_platform else ""

    return {
        "title": f"🎫 {cv.name}",
        "description": f"{cv.venue} — {cv.city} — {date_str}{platform_str}",
        "color": 0x00FF00,
        "fields": fields,
    }


def send_alert(opps: list[ArbitrageOpportunity]) -> bool:
    """Send a consolidated arbitrage alert for one event. Returns True on success."""
    embed = _format_opportunity(opps)
    if embed is None:
        return False

    payload = {
        "username": "Ticket Arb",
        "embeds": [embed],
    }

    try:
        resp = requests.post(
            config.DISCORD_WEBHOOK_URL,
            json=payload,
            timeout=config.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"[Discord] Failed to send alert: {e}")
        return False


def send_groupme_alert(match: GroupMeMatch) -> bool:
    """Send a GroupMe demand alert — visually distinct from arbitrage alerts."""
    cv = match.crowdvolt_event

    # Format the buy requests (cap at 5 to keep the embed compact)
    request_lines = []
    for req in match.buy_requests[:5]:
        request_lines.append(f'**{req.user}**: "{req.text}"')
    if len(match.buy_requests) > 5:
        request_lines.append(f"*…and {len(match.buy_requests) - 5} more*")

    # CrowdVolt price info
    price_parts = []
    if cv.min_ask is not None:
        price_parts.append(f"Lowest seller: **${cv.min_ask:.0f}**")
    if cv.max_bid is not None:
        price_parts.append(f"Highest buyer: **${cv.max_bid:.0f}**")
    if not price_parts:
        price_parts.append("No active listings")

    date_str = cv.event_date.strftime("%b %d, %Y") if cv.event_date else "TBD"
    platform_str = f" · via {cv.ticket_platform}" if cv.ticket_platform else ""

    fields = [
        {
            "name": f"Buy Requests ({len(match.buy_requests)})",
            "value": "\n".join(request_lines),
            "inline": False,
        },
        {
            "name": "CrowdVolt Prices",
            "value": "\n".join(price_parts),
            "inline": True,
        },
        {
            "name": "Links",
            "value": f"[CrowdVolt]({cv.url})",
            "inline": True,
        },
    ]

    embed = {
        "title": f"💬 {cv.name}",
        "description": f"{cv.venue} — {cv.city} — {date_str}{platform_str}",
        "color": 0xFF9800,  # orange — distinct from green arbitrage alerts
        "fields": fields,
    }

    payload = {"username": "Ticket Arb", "embeds": [embed]}

    try:
        resp = requests.post(
            config.DISCORD_WEBHOOK_URL,
            json=payload,
            timeout=config.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"[Discord] Failed to send GroupMe alert: {e}")
        return False


def send_groupme_sell_alert(match: GroupMeSellMatch) -> bool:
    """Send a GroupMe supply alert — purple embed for sell-side opportunities."""
    cv = match.crowdvolt_event

    # Format the sell listings (cap at 5)
    listing_lines = []
    for sl in match.sell_listings[:5]:
        price_str = f" — **${sl.price:.0f}**" if sl.price else ""
        qty_str = f" x{sl.qty}" if sl.qty > 1 else ""
        listing_lines.append(f'**{sl.user}**: "{sl.text}"{price_str}{qty_str}')
    if len(match.sell_listings) > 5:
        listing_lines.append(f"*…and {len(match.sell_listings) - 5} more*")

    # CrowdVolt bid info (the buyer side)
    price_parts = []
    if cv.max_bid is not None:
        price_parts.append(f"Highest buyer: **${cv.max_bid:.0f}**")
    if cv.min_ask is not None:
        price_parts.append(f"Lowest seller: **${cv.min_ask:.0f}**")

    # Highlight spread if a seller listed a price
    priced = [sl for sl in match.sell_listings if sl.price is not None]
    if priced and cv.max_bid is not None:
        cheapest = min(sl.price for sl in priced)
        spread = cv.max_bid - cheapest
        if spread > 0:
            price_parts.append(f"Potential spread: **+${spread:.0f}**")

    date_str = cv.event_date.strftime("%b %d, %Y") if cv.event_date else "TBD"
    platform_str = f" · via {cv.ticket_platform}" if cv.ticket_platform else ""

    fields = [
        {
            "name": f"GroupMe Sellers ({len(match.sell_listings)})",
            "value": "\n".join(listing_lines),
            "inline": False,
        },
        {
            "name": "CrowdVolt Buyers",
            "value": "\n".join(price_parts) if price_parts else "No active listings",
            "inline": True,
        },
        {
            "name": "Links",
            "value": f"[CrowdVolt]({cv.url})",
            "inline": True,
        },
    ]

    embed = {
        "title": f"🏷️ {cv.name}",
        "description": f"{cv.venue} — {cv.city} — {date_str}{platform_str}",
        "color": 0x9B59B6,  # purple — distinct from green arb and orange demand
        "fields": fields,
    }

    payload = {"username": "Ticket Arb", "embeds": [embed]}

    try:
        resp = requests.post(
            config.DISCORD_WEBHOOK_URL,
            json=payload,
            timeout=config.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"[Discord] Failed to send GroupMe sell alert: {e}")
        return False


def send_summary(
    total_events: int,
    opportunities: int,
    errors: int,
    events_with_bids: int = 0,
    match_failures: int = 0,
    dice_filtered: int = 0,
    groupme_requests: int = 0,
    groupme_matches: int = 0,
    groupme_sell_listings: int = 0,
    groupme_sell_matches: int = 0,
) -> bool:
    """Send a scan summary to Discord."""
    asks_only = total_events - events_with_bids

    # Show which sources ran
    sources = ["TickPick"]
    from config import SEATGEEK_CLIENT_ID
    if SEATGEEK_CLIENT_ID:
        sources.append("SeatGeek")
    if events_with_bids > 0:
        sources.extend(["StubHub", "VividSeats"])
    sources_str = " · ".join(sources)

    if events_with_bids == 0:
        browser_note = "StubHub/VividSeats skipped (no waiting buyers)"
    else:
        browser_note = f"StubHub/VividSeats ran for **{events_with_bids}** events with waiting buyers"

    payload = {
        "username": "Ticket Arb",
        "embeds": [{
            "title": "Scan Complete",
            "description": (
                f"**{total_events}** CrowdVolt events scanned\n"
                f"**{dice_filtered}** DICE-only events filtered out\n"
                f"**{events_with_bids}** with waiting buyers · "
                f"**{asks_only}** sellers only\n"
                f"**{opportunities}** arbitrage opportunities found\n"
                f"**{match_failures}** events with no cross-platform match\n"
                f"**{errors}** API/scrape errors\n\n"
                f"Sources: {sources_str}\n"
                f"{browser_note}\n"
                f"GroupMe: **{groupme_requests}** buy requests · "
                f"**{groupme_matches}** matched to CrowdVolt\n"
                f"GroupMe: **{groupme_sell_listings}** sell listings · "
                f"**{groupme_sell_matches}** matched to CrowdVolt"
            ),
            "color": 0x5865F2,
        }],
    }

    try:
        resp = requests.post(
            config.DISCORD_WEBHOOK_URL,
            json=payload,
            timeout=config.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"[Discord] Failed to send summary: {e}")
        return False
