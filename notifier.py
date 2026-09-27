"""Send arbitrage alerts to Discord via webhook."""

import requests

import config
from matcher import ArbitrageOpportunity, _localize_cv_date


def _bounded_lines(lines, limit=1024):
    chosen = []
    for line in lines:
        if len("\n".join(chosen + [line])) > limit - 32:
            chosen.append("More comparisons in the scan log.")
            break
        chosen.append(line)
    return "\n".join(chosen) or "See scan log."


def _format_opportunity(opps: list[ArbitrageOpportunity]) -> dict:
    """Format a consolidated arbitrage alert for one CrowdVolt event.

    Takes all platform opportunities for a single event and renders
    one embed showing prices across platforms, the best arb, and links.
    """
    # Only include opportunities that have a bid to sell to
    opps = [o for o in opps if o.tier_verified and o.profit_vs_bid is not None and o.profit_vs_bid > 0]
    if not opps:
        return None

    # Rank by achievable lot profit; products may have different bid prices.
    opps = sorted(opps, key=lambda o: o.profit_vs_bid * o.quantity, reverse=True)
    best = opps[0]
    cv = best.crowdvolt_event

    margin = (best.profit_vs_bid / best.source_price) * 100

    # Price list across platforms
    price_lines = []
    for opp in opps:
        label = f"{opp.source_platform} [{opp.ticket_type}, {opp.quantity} ticket(s)]"
        price_str = f"${opp.source_price:.0f}"
        if opp.fees_estimated:
            price_str += " (est. w/ fees)"
        price_lines.append(f"**{label}** — {price_str}")

    fields = [
        {
            "name": "Prices",
            "value": _bounded_lines(price_lines),
            "inline": True,
        },
        {
            "name": "Matched CrowdVolt Offer",
            "value": f"**${best.crowdvolt_bid:.0f}**",
            "inline": True,
        },
        {
            "name": "Best Arbitrage",
            "value": (
                f"{best.ticket_type} · {best.quantity} ticket(s), prices/profit per ticket\n"
                f"Buy on **{best.source_platform}** (${best.source_price:.0f})"
                f" → Sell on **CrowdVolt** (${best.crowdvolt_bid:.0f})\n"
                f"**+${best.profit_vs_bid:.0f}** ({margin:.1f}%)"
            ),
            "inline": False,
        },
    ]

    # Links
    link_parts = [f"[CrowdVolt]({cv.url})"]
    for platform, url in dict.fromkeys((o.source_platform, o.source_url) for o in opps):
        link_parts.append(f"[{platform}]({url})")
    fields.append({
        "name": "Links",
        "value": _bounded_lines(link_parts),
        "inline": False,
    })

    # Display the venue-local date, not raw UTC. CV stores doors_open_time
    # as UTC, which rolls into the next calendar day for evening events,
    # producing a confusing alert where the displayed date doesn't match
    # the source-platform URL the link goes to (e.g., "May 31" in alert
    # but ticketmaster link is for the May 30 show).
    cv_local = _localize_cv_date(cv)
    date_str = (cv_local or cv.event_date).strftime("%b %d, %Y") if cv.event_date else "TBD"
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



def send_summary(
    total_events: int,
    opportunities: int,
    errors: int,
    events_with_bids: int = 0,
    match_failures: int = 0,
    dice_filtered: int = 0,
    undercut_sent: int = 0,
    compared_events: int = None,
    tier_unavailable: int = 0,
) -> bool:
    """Send a scan summary to Discord."""
    asks_only = total_events - events_with_bids

    # Show which sources ran
    sources = ["TickPick"]
    from config import SEATGEEK_CLIENT_ID
    if SEATGEEK_CLIENT_ID:
        sources.append("SeatGeek")
    if events_with_bids > 0:
        sources.extend(["StubHub", "VividSeats", "Gametime"])
    sources_str = " · ".join(sources)

    if events_with_bids == 0:
        browser_note = "Reverse searches use TickPick and StubHub; other sources require waiting buyers"
    else:
        browser_note = "Reverse: TickPick/StubHub. Blocked or missing tier data produces no verified alert."

    payload = {
        "username": "Ticket Arb",
        "embeds": [{
            "title": "Scan Complete",
            "description": (
                f"**{total_events}** CrowdVolt events in scope\n"
                f"**{compared_events if compared_events is not None else events_with_bids}** events sent to comparison\n"
                f"**{tier_unavailable}** event/provider matches lacked tier metadata\n"
                f"**{dice_filtered}** DICE-only events (spec digest only)\n"
                f"**{events_with_bids}** with waiting buyers · "
                f"**{asks_only}** sellers only\n"
                f"**{opportunities}** arbitrage opportunities found\n"
                f"**{undercut_sent}** speculative listing opportunities\n"
                f"**{match_failures}** events with no cross-platform match\n"
                f"**{errors}** API/scrape errors\n\n"
                f"Sources: {sources_str}\n"
                f"{browser_note}"
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
