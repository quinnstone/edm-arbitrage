"""GroupMe demand/supply daily digest.

Extracted from the old promo_scanner.py — runs once a day, parses GroupMe
buy requests and sell listings from a 7-day rolling window, matches them
to CrowdVolt events for context, and sends a single Discord digest.

This is a demand/supply *signal* — it shows what people are looking for
and selling, mapped to CrowdVolt events. Does NOT filter by arbitrage
conditions (the Reddit ticket scanner does that work).

Usage:
    python groupme_scanner.py            # run + send digest
    python groupme_scanner.py --dry      # preview, no Discord
"""

import argparse
from datetime import datetime

import requests

import config
import crowdvolt
import groupme


def scan_groupme(cv_events: list, dry_run: bool = False) -> dict:
    """Parse GroupMe activity and emit a daily digest."""
    print(f"\n{'=' * 60}")
    print(f"[GroupMe] Daily digest at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 60}")

    if not config.GROUPME_TOKEN or not config.GROUPME_GROUP_ID:
        print("[GroupMe] No token configured — skipping")
        return {"requests": 0, "demand_matches": 0, "listings": 0, "supply_matches": 0}

    gm_messages = groupme.fetch_recent_messages(
        minutes=config.GROUPME_LOOKBACK_DAYS * 24 * 60,
    )
    print(f"[GroupMe] {len(gm_messages)} messages in last {config.GROUPME_LOOKBACK_DAYS} days")

    gm_requests = groupme.parse_buy_requests(gm_messages)
    print(f"[GroupMe] {len(gm_requests)} buy requests")

    demand_matches = []
    if gm_requests:
        demand_matches = groupme.match_demand(gm_requests, cv_events)
        print(f"[GroupMe] {len(demand_matches)} demand matched to CrowdVolt")
        for m in demand_matches:
            cv = m.crowdvolt_event
            users = ", ".join(r.user for r in m.buy_requests)
            print(f"  {cv.name} [{cv.ticket_platform}] ← {users}")

    gm_sell_listings = groupme.parse_sell_listings(gm_messages)
    print(f"[GroupMe] {len(gm_sell_listings)} sell listings")

    supply_matches = []
    if gm_sell_listings:
        supply_matches = groupme.match_supply(gm_sell_listings, cv_events)
        print(f"[GroupMe] {len(supply_matches)} supply matched to CrowdVolt")
        for m in supply_matches:
            cv = m.crowdvolt_event
            sellers = ", ".join(s.user for s in m.sell_listings)
            priced = [s for s in m.sell_listings if s.price is not None]
            price_info = f" (from ${min(s.price for s in priced):.0f})" if priced else ""
            print(f"  {cv.name} [{cv.ticket_platform}] ← {sellers}{price_info}")

    if not dry_run:
        _send_groupme_digest(demand_matches, supply_matches,
                             len(gm_requests), len(gm_sell_listings))

    return {
        "requests": len(gm_requests),
        "demand_matches": len(demand_matches),
        "listings": len(gm_sell_listings),
        "supply_matches": len(supply_matches),
    }


def _send_groupme_digest(
    demand_matches: list,
    supply_matches: list,
    total_requests: int,
    total_listings: int,
) -> bool:
    if not config.DISCORD_WEBHOOK_URL:
        print("[GroupMe] No Discord webhook configured")
        return False

    today = datetime.now().strftime("%b %d, %Y")
    fields = []

    if demand_matches:
        for m in demand_matches[:10]:
            cv = m.crowdvolt_event
            users = ", ".join(r.user for r in m.buy_requests[:5])
            if len(m.buy_requests) > 5:
                users += f" +{len(m.buy_requests) - 5} more"

            price_parts = []
            if cv.min_ask is not None:
                price_parts.append(f"Lowest seller: ${cv.min_ask:.0f}")
            if cv.max_bid is not None:
                price_parts.append(f"Highest buyer: ${cv.max_bid:.0f}")
            price_str = " · ".join(price_parts) if price_parts else "No listings"

            platform_str = f" [{cv.ticket_platform}]" if cv.ticket_platform else ""
            fields.append({
                "name": f"🔎 {cv.name}{platform_str}",
                "value": f"Buyers: {users}\n{price_str}\n[CrowdVolt]({cv.url})",
                "inline": False,
            })

    if supply_matches:
        for m in supply_matches[:10]:
            cv = m.crowdvolt_event
            lines = []
            for sl in m.sell_listings[:5]:
                price_str = f" — ${sl.price:.0f}" if sl.price else ""
                lines.append(f"{sl.user}{price_str}")
            if len(m.sell_listings) > 5:
                lines.append(f"+{len(m.sell_listings) - 5} more")

            bid_str = f"Highest CrowdVolt buyer: ${cv.max_bid:.0f}" if cv.max_bid else ""
            spread_str = ""
            priced = [sl for sl in m.sell_listings if sl.price is not None]
            if priced and cv.max_bid is not None:
                cheapest = min(sl.price for sl in priced)
                spread = cv.max_bid - cheapest
                if spread > 0:
                    spread_str = f" · Spread: **+${spread:.0f}**"

            platform_str = f" [{cv.ticket_platform}]" if cv.ticket_platform else ""
            fields.append({
                "name": f"🏷️ {cv.name}{platform_str}",
                "value": (
                    f"Sellers: {', '.join(lines)}\n"
                    f"{bid_str}{spread_str}\n"
                    f"[CrowdVolt]({cv.url})"
                ),
                "inline": False,
            })

    if not demand_matches and not supply_matches:
        description = (
            f"**{total_requests}** buy requests · **{total_listings}** sell listings "
            f"in the last {config.GROUPME_LOOKBACK_DAYS} days\n\n"
            f"No matches to CrowdVolt events."
        )
        color = 0x95A5A6
    else:
        description = (
            f"**{total_requests}** buy requests → **{len(demand_matches)}** matched\n"
            f"**{total_listings}** sell listings → **{len(supply_matches)}** matched\n"
            f"Rolling {config.GROUPME_LOOKBACK_DAYS}-day window"
        )
        color = 0xFF9800

    fields = fields[:25]

    payload = {
        "username": "Ticket Arb",
        "embeds": [{
            "title": f"💬 GroupMe Daily Digest — {today}",
            "description": description,
            "color": color,
            "fields": fields,
        }],
    }

    try:
        resp = requests.post(
            config.DISCORD_WEBHOOK_URL,
            json=payload,
            timeout=config.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        print("[GroupMe] Daily digest sent to Discord")
        return True
    except requests.RequestException as e:
        print(f"[GroupMe] Failed to send digest: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="GroupMe daily demand/supply digest")
    parser.add_argument("--dry", action="store_true", help="Preview without Discord")
    args = parser.parse_args()

    cv_events = crowdvolt.fetch_all_events()
    scan_groupme(cv_events, dry_run=args.dry)

    # Reddit ticket arbitrage scan — runs alongside, failures isolated
    try:
        import reddit_tix_scanner
        reddit_tix_scanner.scan(dry_run=args.dry, cv_events=cv_events)
    except Exception as e:
        print(f"[Reddit Tix] Scan failed: {e}")


if __name__ == "__main__":
    main()
