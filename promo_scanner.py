"""Standalone promo/discount code scanner for CrowdVolt events.

Searches Reddit, blogs, and social media for promo codes on ticketing
platforms (DICE, Eventbrite, AXS, etc.) for events with active CrowdVolt
bids. Runs daily and sends a Discord summary.

Usage:
    python promo_scanner.py          # run once
    python promo_scanner.py --dry    # preview without sending to Discord

This script is intentionally standalone — it shares config and crowdvolt
imports but does NOT touch or depend on the main arbitrage pipeline.
"""

import argparse
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

import config
import crowdvolt


@dataclass
class PromoResult:
    event_name: str
    event_slug: str
    ticket_platform: str
    crowdvolt_bid: float
    source: str  # "Reddit", "Web", etc.
    title: str
    snippet: str
    url: str
    found_codes: list[str] = field(default_factory=list)


# Ticketing platforms where promo codes are commonly used
PROMO_PLATFORMS = {"DICE", "EVENTBRITE", "AXS", "TIXR", "POSH", "SEE TICKETS"}

# Patterns that look like promo/discount codes in text
_CODE_RE = re.compile(
    r"""(?:
        (?:code|promo|discount|coupon|use)   # keyword before
        \s*[:=]?\s*                          # optional separator
        ["\']?([A-Z0-9_-]{3,20})["\']?       # the code itself
    |
        ["\']([A-Z0-9_-]{4,15})["\']         # quoted code
        \s*(?:for|to get|saves?|off)          # keyword after
    )""",
    re.IGNORECASE | re.VERBOSE,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


# ---------------------------------------------------------------------------
# Search sources
# ---------------------------------------------------------------------------

def _search_reddit(query: str, platform: str) -> list[dict]:
    """Search Reddit for promo code posts."""
    results = []
    search_queries = [
        f"{query} {platform} promo code",
        f"{query} {platform} discount",
        f"{query} presale code",
    ]

    for sq in search_queries:
        url = f"https://www.reddit.com/search.json"
        params = {
            "q": sq,
            "sort": "new",
            "t": "month",  # last month
            "limit": 10,
        }
        try:
            resp = requests.get(
                url, params=params, headers={**HEADERS, "Accept": "application/json"},
                timeout=config.REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                data = resp.json()
                for post in data.get("data", {}).get("children", []):
                    d = post.get("data", {})
                    results.append({
                        "source": "Reddit",
                        "title": d.get("title", ""),
                        "snippet": d.get("selftext", "")[:300],
                        "url": f"https://reddit.com{d.get('permalink', '')}",
                        "subreddit": d.get("subreddit", ""),
                    })
        except requests.RequestException:
            pass
        time.sleep(1)  # respect rate limits

    return results


def _search_web(query: str, platform: str) -> list[dict]:
    """Search the web via DuckDuckGo HTML for promo codes."""
    results = []
    search_queries = [
        f"{query} {platform} promo code 2026",
        f"{query} {platform} discount code",
    ]

    for sq in search_queries:
        url = f"https://html.duckduckgo.com/html/"
        try:
            resp = requests.post(
                url,
                data={"q": sq},
                headers=HEADERS,
                timeout=config.REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                for result in soup.select(".result"):
                    title_el = result.select_one(".result__title a")
                    snippet_el = result.select_one(".result__snippet")
                    if title_el:
                        href = title_el.get("href", "")
                        results.append({
                            "source": "Web",
                            "title": title_el.get_text(strip=True),
                            "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
                            "url": href,
                        })
        except requests.RequestException:
            pass
        time.sleep(1.5)

    return results


def _extract_codes(text: str) -> list[str]:
    """Pull promo-code-looking strings from text."""
    codes = set()
    for match in _CODE_RE.finditer(text):
        code = match.group(1) or match.group(2)
        if code:
            # Filter out common false positives
            upper = code.upper()
            if upper not in {"THE", "FOR", "AND", "GET", "USE", "OFF",
                             "CODE", "WITH", "FREE", "SALE", "HTTP",
                             "HTTPS", "HTML", "JSON", "NULL"}:
                codes.add(code)
    return sorted(codes)


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

def scan_promos(dry_run: bool = False) -> list[PromoResult]:
    """Scan for promo codes on events with active CrowdVolt bids."""
    print(f"\n{'='*60}")
    print(f"[Promo] Starting scan at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    # Fetch CrowdVolt events
    cv_events = crowdvolt.fetch_all_events()
    if not cv_events:
        print("[Promo] No CrowdVolt events found")
        return []

    # Only scan events with active bids on platforms that support promo codes
    eligible = [
        e for e in cv_events
        if e.max_bid is not None
        and e.ticket_platform.upper() in PROMO_PLATFORMS
    ]

    print(f"[Promo] {len(eligible)} events with bids on promo-eligible platforms "
          f"(out of {len(cv_events)} total)")

    all_results = []

    for event in eligible:
        platform = event.ticket_platform
        print(f"\n[Promo] {event.name} [{platform}] — bid ${event.max_bid:.0f}")

        # Search Reddit and web
        raw_results = []
        raw_results.extend(_search_reddit(event.name, platform))
        raw_results.extend(_search_web(event.name, platform))

        # Deduplicate by URL
        seen_urls = set()
        unique = []
        for r in raw_results:
            if r["url"] not in seen_urls:
                seen_urls.add(r["url"])
                unique.append(r)

        # Score relevance — check for actual code-like strings
        for r in unique:
            combined_text = f"{r['title']} {r['snippet']}"
            codes = _extract_codes(combined_text)

            # Even without extracted codes, flag results that mention
            # promo/discount in context of this event
            has_promo_mention = any(
                kw in combined_text.lower()
                for kw in ["promo", "discount", "code", "coupon", "presale",
                           "early bird", "free entry", "guest list", "guestlist",
                           "reduced", "% off"]
            )

            if codes or has_promo_mention:
                result = PromoResult(
                    event_name=event.name,
                    event_slug=event.slug,
                    ticket_platform=platform,
                    crowdvolt_bid=event.max_bid,
                    source=r["source"],
                    title=r["title"],
                    snippet=r["snippet"][:200],
                    url=r["url"],
                    found_codes=codes,
                )
                all_results.append(result)
                code_str = f" — codes: {', '.join(codes)}" if codes else ""
                print(f"  [{r['source']}] {r['title'][:60]}{code_str}")

        if not any(r.event_slug == event.slug for r in all_results):
            print(f"  No promo results found")

        time.sleep(0.5)

    print(f"\n[Promo] {len(all_results)} total promo results across "
          f"{len(set(r.event_slug for r in all_results))} events")

    if not dry_run:
        _send_daily_digest(all_results, len(eligible))

    return all_results


def _send_daily_digest(results: list[PromoResult], events_scanned: int) -> bool:
    """Send the daily promo code digest to Discord."""
    if not config.DISCORD_WEBHOOK_URL:
        print("[Promo] No Discord webhook configured")
        return False

    today = datetime.now().strftime("%b %d, %Y")

    if not results:
        # Still send a "nothing found" update so you know it ran
        payload = {
            "username": "Ticket Arb",
            "embeds": [{
                "title": f"🔍 Promo Scan — {today}",
                "description": (
                    f"Scanned **{events_scanned}** events with active bids "
                    f"on promo-eligible platforms.\n\n"
                    f"No promo codes or discounts found today."
                ),
                "color": 0x95A5A6,  # grey
            }],
        }
    else:
        # Group results by event
        by_event: dict[str, list[PromoResult]] = {}
        for r in results:
            by_event.setdefault(r.event_slug, []).append(r)

        # Build fields — one per event
        fields = []
        for slug, event_results in by_event.items():
            first = event_results[0]
            lines = []
            for r in event_results[:3]:  # cap at 3 per event
                code_str = f" `{', '.join(r.found_codes)}`" if r.found_codes else ""
                lines.append(f"[{r.source}] [{r.title[:50]}]({r.url}){code_str}")
            if len(event_results) > 3:
                lines.append(f"*…and {len(event_results) - 3} more*")

            fields.append({
                "name": f"{first.event_name} [{first.ticket_platform}] — bid ${first.crowdvolt_bid:.0f}",
                "value": "\n".join(lines),
                "inline": False,
            })

        # Discord embeds have a 25-field limit
        fields = fields[:25]

        payload = {
            "username": "Ticket Arb",
            "embeds": [{
                "title": f"🔍 Promo Scan — {today}",
                "description": (
                    f"Scanned **{events_scanned}** events · "
                    f"**{len(results)}** promo leads across "
                    f"**{len(by_event)}** events"
                ),
                "color": 0xE91E63,  # pink — distinct from other alert types
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
        print("[Promo] Daily digest sent to Discord")
        return True
    except requests.RequestException as e:
        print(f"[Promo] Failed to send digest: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Promo code scanner")
    parser.add_argument("--dry", action="store_true", help="Preview without sending to Discord")
    args = parser.parse_args()

    scan_promos(dry_run=args.dry)


if __name__ == "__main__":
    main()
