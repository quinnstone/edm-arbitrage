"""Standalone promo/discount code scanner for CrowdVolt events.

Searches Reddit, Twitter/X, promoter websites, and the web for promo
codes on ticketing platforms (DICE, Eventbrite, AXS, etc.) for events
with active CrowdVolt bids. Runs daily and sends a Discord summary.

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
import groupme


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
        (?:code|promo\s*code|discount\s*code|coupon|use)\s*  # keyword before
        [:=\s"']+                                            # separator
        ([A-Z][A-Z0-9_-]{2,19})                              # the code (starts with letter)
    |
        ["']([A-Z][A-Z0-9_-]{3,14})["']                      # quoted code
        \s*(?:for|to\s+get|saves?|off|discount)               # keyword after
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Common words that look like codes but aren't
_CODE_BLACKLIST = {
    "THE", "FOR", "AND", "GET", "USE", "OFF", "CODE", "WITH", "FREE",
    "SALE", "HTTP", "HTTPS", "HTML", "JSON", "NULL", "THIS", "THAT",
    "THEY", "THEM", "WHEN", "WHAT", "WILL", "YOUR", "FROM", "HAVE",
    "BEEN", "SOME", "DOES", "DONT", "WERE", "HIYA", "HELLO", "PLEASE",
    "ALSO", "JUST", "LIKE", "MORE", "MOST", "VERY", "MUCH", "THAN",
    "THEN", "ONLY", "EACH", "BOTH", "INTO", "OVER", "SUCH", "MAKE",
    "BACK", "EVEN", "GOOD", "WELL", "MUST", "HERE", "COME", "COULD",
    "WOULD", "ABOUT", "EMAIL", "LATER", "COULD", "PROMO", "WHICH",
    "THERE", "WHERE", "THESE", "THOSE", "STILL", "AFTER", "BEFORE",
    "CAN", "BUT", "NOT", "WAS", "ARE", "OUR", "HIS", "HER", "ITS",
    "MAY", "NOW", "OLD", "NEW", "WAY", "DAY", "DID", "HAD", "HAS",
    "HOW", "ITS", "LET", "MAY", "OWN", "SAY", "SHE", "TOO", "WHO",
    "FUL", "DON",
}

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

def _search_reddit(query: str, platform: str, city: str = "") -> list[dict]:
    """Search Reddit for promo code posts related to the event."""
    results = []
    city_term = f' "{city}"' if city else ""
    search_queries = [
        f'"{query}"{city_term} promo code',
        f'"{query}"{city_term} presale code',
        f'"{query}"{city_term} discount ticket',
    ]

    for sq in search_queries:
        url = "https://www.reddit.com/search.json"
        params = {
            "q": sq,
            "sort": "new",
            "t": "month",
            "limit": 5,
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
                    title = d.get("title", "")
                    body = d.get("selftext", "")[:300]
                    combined = f"{title} {body}".lower()
                    # Only keep results that mention the event name
                    query_lower = query.lower()
                    if query_lower in combined or _fuzzy_contains(query_lower, combined):
                        results.append({
                            "source": "Reddit",
                            "title": title,
                            "snippet": body,
                            "url": f"https://reddit.com{d.get('permalink', '')}",
                        })
        except requests.RequestException:
            pass
        time.sleep(1)

    return results


def _fuzzy_contains(query: str, text: str) -> bool:
    """Check if query words appear close together in text."""
    words = query.split()
    if len(words) < 2:
        return query in text
    # All words must appear somewhere in the text
    return all(w in text for w in words)


def _search_web(query: str, platform: str, city: str = "") -> list[dict]:
    """Search the web via DuckDuckGo HTML for promo codes."""
    results = []
    city_term = f' "{city}"' if city else ""
    search_queries = [
        f'"{query}"{city_term} {platform} promo code 2026',
        f'"{query}"{city_term} presale code discount',
    ]

    for sq in search_queries:
        url = "https://html.duckduckgo.com/html/"
        try:
            resp = requests.post(
                url,
                data={"q": sq},
                headers=HEADERS,
                timeout=config.REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                for result in soup.select(".result")[:5]:
                    title_el = result.select_one(".result__title a")
                    snippet_el = result.select_one(".result__snippet")
                    if title_el:
                        title = title_el.get_text(strip=True)
                        snippet = snippet_el.get_text(strip=True) if snippet_el else ""
                        combined = f"{title} {snippet}".lower()
                        query_lower = query.lower()
                        if query_lower in combined or _fuzzy_contains(query_lower, combined):
                            href = title_el.get("href", "")
                            results.append({
                                "source": "Web",
                                "title": title,
                                "snippet": snippet,
                                "url": href,
                            })
        except requests.RequestException:
            pass
        time.sleep(1.5)

    return results


# ---------------------------------------------------------------------------
# NYC promoter & venue sites — curated sources for promo codes
# ---------------------------------------------------------------------------

PROMOTER_SITES = [
    # Venues
    {"name": "Avant Gardner", "url": "https://www.avantgardner.com", "twitter": "avaboreal"},
    {"name": "Elsewhere", "url": "https://www.elsewherebrooklyn.com", "twitter": "elsewherezbk"},
    {"name": "Brooklyn Mirage", "url": "https://www.brooklynmirage.com", "twitter": "thebkmirage"},
    {"name": "Knockdown Center", "url": "https://knockdown.center", "twitter": "knockdowncenter"},
    {"name": "Superior Ingredients", "url": "https://superioringredients.com", "twitter": "sup_ingredients"},
    {"name": "Basement", "url": "https://basementny.com", "twitter": "basaboreal"},
    # Promoters
    {"name": "Teksupport", "url": "https://teksupport.com", "twitter": "taboreal"},
    {"name": "Cityfox", "url": "https://cityfox.com", "twitter": "thecityfox"},
    {"name": "Good Room", "url": "https://goodroombk.com", "twitter": "goodroombk"},
    {"name": "Nowadays", "url": "https://nowadays.nyc", "twitter": "nowadaysnyc"},
    {"name": "Bona Fide", "url": "https://www.bonafide.nyc", "twitter": "bonafidenyc"},
    {"name": "Under Construction", "url": "https://underconstruction.nyc"},
    {"name": "Resolute", "url": "https://ra.co/promoters/62737"},
    {"name": "Schimanski", "url": "https://www.schimanskinyc.com", "twitter": "schimanskinyc"},
]


def _search_twitter(query: str) -> list[dict]:
    """Search Twitter/X posts via DuckDuckGo site-scoped search."""
    results = []
    search_queries = [
        f'site:x.com "{query}" code OR promo OR discount OR guestlist',
        f'site:twitter.com "{query}" code OR promo OR discount',
    ]

    for sq in search_queries:
        url = "https://html.duckduckgo.com/html/"
        try:
            resp = requests.post(
                url, data={"q": sq}, headers=HEADERS, timeout=10,
            )
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            for result in soup.select(".result")[:5]:
                title_el = result.select_one(".result__title a")
                snippet_el = result.select_one(".result__snippet")
                if title_el:
                    title = title_el.get_text(strip=True)
                    snippet = snippet_el.get_text(strip=True) if snippet_el else ""
                    href = title_el.get("href", "")
                    combined = f"{title} {snippet}".lower()
                    query_lower = query.lower()
                    if query_lower in combined or _fuzzy_contains(query_lower, combined):
                        results.append({
                            "source": "Twitter",
                            "title": title[:100],
                            "snippet": snippet[:200],
                            "url": href,
                        })
        except requests.RequestException:
            continue
        time.sleep(1)

    return results


def _search_ra(query: str, event_date: str = None, city: str = "") -> list[dict]:
    """Search Resident Advisor for event pages with promo code mentions."""
    results = []

    # Step 1: Search RA for matching events
    gql_search = {
        "query": (
            '{ search(searchTerm: "%s", indices: [EVENT], limit: 5) '
            '{ id value date contentUrl clubName areaName } }'
        ) % query.replace('"', '\\"')
    }
    ra_headers = {
        **HEADERS,
        "Content-Type": "application/json",
        "Referer": "https://ra.co/",
    }

    try:
        resp = requests.post(
            "https://ra.co/graphql", json=gql_search,
            headers=ra_headers, timeout=config.REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return results
        hits = resp.json().get("data", {}).get("search", [])
    except requests.RequestException:
        return results

    if not hits:
        return results

    # Step 2: For each hit, fetch full event details
    for hit in hits[:3]:
        # Optional date filter — skip if dates don't match
        if event_date and hit.get("date"):
            try:
                ra_date = hit["date"][:10]  # "2026-04-10T..."
                if ra_date != event_date:
                    continue
            except (IndexError, TypeError):
                pass

        # City filter — skip RA results in different cities
        if city and hit.get("areaName"):
            ra_area = hit["areaName"].lower()
            city_lower = city.lower()
            if city_lower not in ra_area and ra_area not in city_lower:
                continue

        event_id = hit["id"]
        gql_event = {
            "query": (
                '{ event(id: %s) { title content cost contentUrl '
                'promotionalLinks { url title } '
                'tickets { title } } }'
            ) % event_id
        }

        try:
            resp = requests.post(
                "https://ra.co/graphql", json=gql_event,
                headers=ra_headers, timeout=config.REQUEST_TIMEOUT,
            )
            if resp.status_code != 200:
                continue
            ev = resp.json().get("data", {}).get("event")
            if not ev:
                continue
        except requests.RequestException:
            continue

        # Combine all text fields for code extraction
        content = ev.get("content") or ""
        cost = ev.get("cost") or ""
        promo_links = ev.get("promotionalLinks") or []
        link_text = " ".join(
            f"{pl.get('title', '')} {pl.get('url', '')}" for pl in promo_links
        )
        combined = f"{content} {cost} {link_text}"

        # Check for promo-related content
        codes = _extract_codes(combined)
        has_promo = any(
            kw in combined.lower()
            for kw in ["promo", "discount", "code", "coupon", "early bird",
                        "guest list", "guestlist", "reduced", "% off",
                        "free before", "no cover", "rsvp"]
        )

        # Also check for embedded codes in ticket URLs (e.g., ?code=XYZ)
        for pl in promo_links:
            url = pl.get("url", "")
            if "code=" in url.lower():
                # Extract the code param
                m = re.search(r'[?&]code=([A-Za-z0-9_-]+)', url)
                if m and m.group(1).upper() not in _CODE_BLACKLIST:
                    codes.append(m.group(1).upper())

        if codes or has_promo:
            event_url = f"https://ra.co{ev.get('contentUrl', '')}"
            snippet = content[:200] if content else ""
            results.append({
                "source": "RA",
                "title": ev.get("title", hit.get("value", "")),
                "snippet": snippet,
                "url": event_url,
                "codes": codes,
            })

        time.sleep(0.5)

    return results


def _search_promoter_sites(query: str, venue: str) -> list[dict]:
    """Check curated NYC promoter/venue websites for promo codes."""
    results = []
    query_lower = query.lower()
    venue_lower = venue.lower() if venue else ""

    for site in PROMOTER_SITES:
        # Only check sites relevant to this event's venue
        site_name_lower = site["name"].lower()
        if venue_lower and site_name_lower not in venue_lower and venue_lower not in site_name_lower:
            # Also check via web search for this promoter + event
            continue

        try:
            resp = requests.get(
                site["url"], headers=HEADERS, timeout=10, allow_redirects=True,
            )
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            page_text = soup.get_text(" ", strip=True).lower()

            # Check if this event is mentioned on the promoter's site
            if query_lower not in page_text and not _fuzzy_contains(query_lower, page_text):
                continue

            # Look for promo-related content near the event mention
            codes = _extract_codes(page_text)
            has_promo = any(
                kw in page_text
                for kw in ["promo", "discount", "code", "early bird",
                           "guest list", "guestlist", "reduced", "% off",
                           "free before", "no cover", "rsvp"]
            )

            if codes or has_promo:
                results.append({
                    "source": site["name"],
                    "title": f"{site['name']} — event page mentions promo",
                    "snippet": "",
                    "url": site["url"],
                    "codes": codes,
                })
        except requests.RequestException:
            continue
        time.sleep(0.5)

    # Also search for venue/promoter Twitter posts about this event
    for site in PROMOTER_SITES:
        twitter_handle = site.get("twitter")
        if not twitter_handle:
            continue

        sq = f'site:x.com from:{twitter_handle} "{query_lower}" code OR promo OR discount OR guestlist'
        url = "https://html.duckduckgo.com/html/"
        try:
            resp = requests.post(url, data={"q": sq}, headers=HEADERS, timeout=10)
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            for result in soup.select(".result")[:3]:
                title_el = result.select_one(".result__title a")
                snippet_el = result.select_one(".result__snippet")
                if title_el:
                    title = title_el.get_text(strip=True)
                    snippet = snippet_el.get_text(strip=True) if snippet_el else ""
                    href = title_el.get("href", "")
                    results.append({
                        "source": f"@{twitter_handle}",
                        "title": title[:100],
                        "snippet": snippet[:200],
                        "url": href,
                    })
        except requests.RequestException:
            continue
        time.sleep(0.5)

    return results



def _extract_codes(text: str) -> list[str]:
    """Pull promo-code-looking strings from text."""
    codes = set()
    for match in _CODE_RE.finditer(text):
        code = match.group(1) or match.group(2)
        if code and code.upper() not in _CODE_BLACKLIST:
            codes.add(code.upper())
    return sorted(codes)


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

def scan_promos(dry_run: bool = False, cv_events: list = None) -> list[PromoResult]:
    """Scan for promo codes on events with active CrowdVolt bids."""
    print(f"\n{'='*60}")
    print(f"[Promo] Starting scan at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    # Fetch CrowdVolt events if not provided
    if cv_events is None:
        cv_events = crowdvolt.fetch_all_events()
    if not cv_events:
        print("[Promo] No CrowdVolt events found")
        return []

    # Only scan events with active bids on platforms that support promo codes,
    # happening within the next 14 days (promo codes are shared close to event)
    from datetime import timedelta
    horizon = datetime.now() + timedelta(days=14)
    eligible = [
        e for e in cv_events
        if e.max_bid is not None
        and e.ticket_platform.upper() in PROMO_PLATFORMS
        and (e.event_date is None or e.event_date <= horizon)
    ]

    print(f"[Promo] {len(eligible)} events with bids on promo-eligible platforms "
          f"within 14 days (out of {len(cv_events)} total)")

    all_results = []

    for event in eligible:
        platform = event.ticket_platform
        print(f"\n[Promo] {event.name} [{platform}] — bid ${event.max_bid:.0f}")

        # Search all sources
        date_str = event.event_date.strftime("%Y-%m-%d") if event.event_date else None
        city = event.city or ""
        raw_results = []
        raw_results.extend(_search_reddit(event.name, platform, city))
        raw_results.extend(_search_twitter(event.name))
        raw_results.extend(_search_ra(event.name, date_str, city))
        raw_results.extend(_search_promoter_sites(event.name, event.venue))
        raw_results.extend(_search_web(event.name, platform, city))

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
            codes = r.get("codes", []) or _extract_codes(combined_text)

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


def scan_groupme(cv_events: list, dry_run: bool = False) -> dict:
    """Scan GroupMe for buy/sell activity and send a daily digest.

    Uses the full CrowdVolt catalog (including DICE) since GroupMe
    sellers transfer tickets directly — platform doesn't matter.

    Rules carried over from the per-scan alerts:
    - Demand (buy requests): only matches events with active sellers
      (min_ask is not None) so there's something to buy on CrowdVolt.
    - Supply (sell listings): only matches events with active buyers
      (max_bid is not None) so there's a guaranteed buyer on CrowdVolt.
    - 7-day rolling lookback window.
    - Tiered matching: score >= 80 auto-match, 70-79 needs date/city.
    """
    print(f"\n{'='*60}")
    print(f"[GroupMe] Daily digest at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    if not config.GROUPME_TOKEN or not config.GROUPME_GROUP_ID:
        print("[GroupMe] No token configured — skipping")
        return {"requests": 0, "demand_matches": 0, "listings": 0, "supply_matches": 0}

    # Fetch messages from the rolling lookback window
    gm_messages = groupme.fetch_recent_messages(
        minutes=config.GROUPME_LOOKBACK_DAYS * 24 * 60,
    )
    print(f"[GroupMe] {len(gm_messages)} messages in last {config.GROUPME_LOOKBACK_DAYS} days")

    # Parse buy requests
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

    # Parse sell listings
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
    """Send a single daily GroupMe digest to Discord."""
    if not config.DISCORD_WEBHOOK_URL:
        print("[GroupMe] No Discord webhook configured")
        return False

    today = datetime.now().strftime("%b %d, %Y")
    fields = []

    # Demand section — people looking to buy
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

    # Supply section — people selling
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
            # Highlight spread
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
        color = 0x95A5A6  # grey
    else:
        description = (
            f"**{total_requests}** buy requests → **{len(demand_matches)}** matched\n"
            f"**{total_listings}** sell listings → **{len(supply_matches)}** matched\n"
            f"Rolling {config.GROUPME_LOOKBACK_DAYS}-day window"
        )
        color = 0xFF9800  # orange

    # Cap fields at 25 (Discord limit)
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
    parser = argparse.ArgumentParser(description="Promo code scanner + GroupMe digest")
    parser.add_argument("--dry", action="store_true", help="Preview without sending to Discord")
    args = parser.parse_args()

    # Both scans share the same CrowdVolt fetch
    cv_events = crowdvolt.fetch_all_events()

    scan_promos(dry_run=args.dry, cv_events=cv_events)
    scan_groupme(cv_events, dry_run=args.dry)


if __name__ == "__main__":
    main()
