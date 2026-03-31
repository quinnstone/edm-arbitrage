"""Send arbitrage alerts to Discord via webhook."""

import requests

import config
from matcher import ArbitrageOpportunity


def _format_opportunity(opp: ArbitrageOpportunity) -> dict:
    """Format an arbitrage opportunity as a Discord embed."""
    cv = opp.crowdvolt_event

    margin = (opp.profit_vs_bid / opp.source_price) * 100
    color = 0x00FF00  # green — all alerts now require an active bid

    fields = [
        {
            "name": "Buy On",
            "value": f"**{opp.source_platform}** — **${opp.source_price:.0f}**",
            "inline": True,
        },
        {
            "name": "Sell To (Active Bid)",
            "value": f"**${opp.crowdvolt_bid:.0f}** on CrowdVolt",
            "inline": True,
        },
        {
            "name": "Profit",
            "value": f"**+${opp.profit_vs_bid:.0f}** ({margin:.1f}%)",
            "inline": True,
        },
    ]

    if opp.crowdvolt_ask is not None:
        fields.append({
            "name": "CrowdVolt Lowest Ask",
            "value": f"${opp.crowdvolt_ask:.0f}",
            "inline": True,
        })

    # Bid details
    if cv.bids:
        bid_lines = [f"• {b.user}: ${b.price:.0f} x{b.qty} ({b.ticket_type})" for b in cv.bids[:5]]
        fields.append({
            "name": f"Active Bids ({len(cv.bids)})",
            "value": "\n".join(bid_lines),
            "inline": False,
        })

    # Ask details
    if cv.asks:
        ask_lines = [f"• {a.user}: ${a.price:.0f} x{a.qty} ({a.ticket_type})" for a in cv.asks[:5]]
        fields.append({
            "name": f"Active Asks ({len(cv.asks)})",
            "value": "\n".join(ask_lines),
            "inline": False,
        })

    # Links
    fields.append({
        "name": "Links",
        "value": f"[CrowdVolt]({cv.url}) | [{opp.source_platform}]({opp.source_url})",
        "inline": False,
    })

    date_str = cv.event_date.strftime("%b %d, %Y") if cv.event_date else "TBD"

    return {
        "title": f"🎫 {cv.name}",
        "description": f"{cv.venue} — {cv.city} — {date_str}",
        "color": color,
        "fields": fields,
    }


def send_alert(opp: ArbitrageOpportunity) -> bool:
    """Send a single arbitrage alert to Discord. Returns True on success."""
    embed = _format_opportunity(opp)

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
) -> bool:
    """Send a scan summary to Discord."""
    asks_only = total_events - events_with_bids

    payload = {
        "username": "Ticket Arb",
        "embeds": [{
            "title": "Scan Complete",
            "description": (
                f"**{total_events}** CrowdVolt events scanned\n"
                f"**{events_with_bids}** with active bids · **{asks_only}** asks-only\n"
                f"**{opportunities}** arbitrage opportunities found\n"
                f"**{match_failures}** events with no cross-platform match\n"
                f"**{errors}** API/scrape errors"
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
