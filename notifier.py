"""Send arbitrage alerts to Discord via webhook."""

import requests

import config
from matcher import ArbitrageOpportunity


def _format_opportunity(opp: ArbitrageOpportunity) -> dict:
    """Format an arbitrage opportunity as a Discord embed."""
    cv = opp.crowdvolt_event

    # Header color: green if profit vs bid, yellow if only vs ask
    color = 0x00FF00 if opp.profit_vs_bid and opp.profit_vs_bid > 0 else 0xFFAA00

    fields = [
        {
            "name": "Buy On",
            "value": f"**{opp.source_platform}** — ${opp.source_price:.0f}",
            "inline": True,
        },
    ]

    if opp.crowdvolt_ask is not None:
        fields.append({
            "name": "CrowdVolt Lowest Ask",
            "value": f"${opp.crowdvolt_ask:.0f}",
            "inline": True,
        })

    if opp.crowdvolt_bid is not None:
        fields.append({
            "name": "CrowdVolt Highest Bid",
            "value": f"${opp.crowdvolt_bid:.0f}",
            "inline": True,
        })

    # Profit lines
    if opp.profit_vs_bid is not None and opp.profit_vs_bid > 0:
        margin = (opp.profit_vs_bid / opp.source_price) * 100
        fields.append({
            "name": "Profit (Fill Bid)",
            "value": f"**+${opp.profit_vs_bid:.0f}** ({margin:.1f}%)",
            "inline": True,
        })

    if opp.profit_vs_ask is not None and opp.profit_vs_ask > 0:
        margin = (opp.profit_vs_ask / opp.source_price) * 100
        fields.append({
            "name": "Spread vs Ask",
            "value": f"**+${opp.profit_vs_ask:.0f}** ({margin:.1f}%)",
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


def send_summary(total_events: int, opportunities: int, errors: int) -> bool:
    """Send a scan summary to Discord."""
    payload = {
        "username": "Ticket Arb",
        "embeds": [{
            "title": "Scan Complete",
            "description": (
                f"**{total_events}** CrowdVolt events scanned\n"
                f"**{opportunities}** arbitrage opportunities found\n"
                f"**{errors}** errors"
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
