"""Scrape CrowdVolt for event listings and bid/ask order book data."""

import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import requests
from dateutil import parser as dateparser

import config

PREMIUM_KEYWORDS = {"vip", "platinum", "backstage", "meet & greet", "meet and greet"}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


@dataclass
class Listing:
    user: str
    price: float
    all_in_price: float
    qty: int
    ticket_type: str


@dataclass
class CrowdVoltEvent:
    slug: str
    name: str
    venue: str
    city: str
    event_date: Optional[datetime] = None
    ticket_platform: str = ""  # e.g. "DICE", "AXS", "Ticketmaster"
    ticket_types: list[dict] = field(default_factory=list)
    bids: list[Listing] = field(default_factory=list)  # buy side
    asks: list[Listing] = field(default_factory=list)  # sell side
    min_ask: Optional[float] = None
    max_bid: Optional[float] = None
    url: str = ""


def fetch_sitemap() -> list[str]:
    """Fetch all event slugs from CrowdVolt's sitemap."""
    resp = requests.get(
        f"{config.CROWDVOLT_BASE_URL}/sitemap.xml",
        timeout=config.REQUEST_TIMEOUT,
        headers=HEADERS,
    )
    resp.raise_for_status()

    root = ET.fromstring(resp.content)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

    slugs = []
    for loc in root.findall(".//sm:loc", ns):
        url = loc.text or ""
        if "/event/" in url:
            slug = url.rstrip("/").split("/event/")[-1]
            slugs.append(slug)

    return slugs


def _extract_book_json(html: str) -> Optional[dict]:
    """Extract the initialBook buy/sell data from the page HTML.

    CrowdVolt uses Next.js with server components. The marketplace data is
    embedded in a JS string with escaped quotes. We find the initialBook
    marker, unescape the surrounding region, and parse the JSON.
    """
    if "initialBook" not in html:
        return None

    idx = html.index("initialBook")

    # Grab a generous window around initialBook for the full order book
    start = max(0, idx - 100)
    end = min(len(html), idx + 30000)
    region = html[start:end]

    # Unescape JS string escaping (\" → ")
    unescaped = region.replace('\\"', '"')

    # Extract the book object: {"buy": [...], "sell": [...]}
    book_match = re.search(r'"initialBook"\s*:\s*(\{"buy":\[)', unescaped)
    if not book_match:
        return None

    book_start = book_match.start(1)
    # Track brace depth to find the end of the object
    depth = 0
    pos = book_start
    while pos < len(unescaped):
        ch = unescaped[pos]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        pos += 1

    book_str = unescaped[book_start : pos + 1]
    try:
        return json.loads(book_str)
    except json.JSONDecodeError:
        return None


def _extract_event_metadata(html: str) -> dict:
    """Extract event-level metadata from the page HTML.

    Prefers the JSON-LD MusicEvent block for name/date, then falls back
    to the Next.js embedded data for venue/area_name/doors_open_time.
    """
    unescaped = html.replace('\\"', '"')
    meta = {}

    # Try JSON-LD first — reliable for name and date
    ld_match = re.search(
        r'<script type="application/ld\+json">(.*?)</script>',
        html, re.DOTALL,
    )
    if ld_match:
        try:
            ld = json.loads(ld_match.group(1))
            if ld.get("@type") in ("MusicEvent", "Event"):
                meta["name"] = ld.get("name", "")
        except (json.JSONDecodeError, TypeError):
            pass

    # Extract venue, area_name, doors_open_time, and app_name from the
    # embedded Next.js data.  These fields are unique to the event payload
    # (unlike "name" which also appears in HTML meta tags), so first-match
    # is safe.  app_name tells us which ticketing platform issued the
    # tickets (e.g. "DICE", "AXS", "Ticketmaster").
    for field_name in ["venue", "area_name", "doors_open_time", "app_name"]:
        match = re.search(f'"{field_name}":"([^"]+)"', unescaped)
        if match:
            meta[field_name] = match.group(1)

    # Fallback: if JSON-LD didn't give us a name, search near doors_open_time
    # where the event payload lives (avoids meta tag false positives).
    if not meta.get("name") and "doors_open_time" in meta:
        dt_idx = unescaped.find('"doors_open_time"')
        if dt_idx > 0:
            region = unescaped[max(0, dt_idx - 2000):dt_idx]
            name_match = re.search(r'"name":"([^"]+)"', region)
            if name_match:
                meta["name"] = name_match.group(1)

    return meta


def _parse_listings(items: list[dict]) -> list[Listing]:
    """Parse a list of raw listing dicts into Listing objects."""
    listings = []
    for item in items:
        listings.append(
            Listing(
                user=item.get("user_first", "Unknown"),
                price=float(item.get("price", 0)),
                all_in_price=float(item.get("all_in_price", 0)),
                qty=int(item.get("qty", 1)),
                ticket_type=item.get("ticket_type", "GA"),
            )
        )
    return listings


def fetch_event(slug: str) -> Optional[CrowdVoltEvent]:
    """Fetch and parse a single CrowdVolt event page."""
    url = f"{config.CROWDVOLT_BASE_URL}/event/{slug}"
    try:
        resp = requests.get(url, timeout=config.REQUEST_TIMEOUT, headers=HEADERS)
        if resp.status_code != 200:
            return None
    except requests.RequestException:
        return None

    html = resp.text

    # Check for dead pages
    if "Event Not found" in html or "Event not found" in html:
        return None

    # Extract metadata
    meta = _extract_event_metadata(html)
    if not meta.get("name"):
        return None

    event = CrowdVoltEvent(
        slug=slug,
        name=meta["name"],
        venue=meta.get("venue", ""),
        city=meta.get("area_name", ""),
        ticket_platform=meta.get("app_name", ""),
        url=url,
    )

    # Parse date
    if meta.get("doors_open_time"):
        try:
            event.event_date = dateparser.parse(meta["doors_open_time"])
        except (ValueError, TypeError):
            pass

    # Extract order book
    book = _extract_book_json(html)
    if book:
        event.bids = _parse_listings(book.get("buy", []))
        event.asks = _parse_listings(book.get("sell", []))

    # Compute summary prices excluding premium tiers so we don't
    # compare VIP bids against GA asks from external sources.
    ga_asks = [a for a in event.asks
               if not any(k in a.ticket_type.lower() for k in PREMIUM_KEYWORDS)]
    ga_bids = [b for b in event.bids
               if not any(k in b.ticket_type.lower() for k in PREMIUM_KEYWORDS)]
    if ga_asks:
        event.min_ask = min(a.all_in_price for a in ga_asks)
    if ga_bids:
        event.max_bid = max(b.all_in_price for b in ga_bids)

    return event


def fetch_all_events() -> list[CrowdVoltEvent]:
    """Fetch all active CrowdVolt events with marketplace data.

    Uses a thread pool to fetch pages concurrently (respecting a semaphore
    so we don't hammer CrowdVolt too hard).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    print("[CrowdVolt] Fetching sitemap...")
    slugs = fetch_sitemap()
    print(f"[CrowdVolt] Found {len(slugs)} event URLs in sitemap")

    events = []
    lock = threading.Lock()
    # Limit concurrency to 5 parallel requests
    semaphore = threading.Semaphore(5)

    def _fetch_one(slug: str, index: int):
        with semaphore:
            event = fetch_event(slug)
            time.sleep(0.3)  # small delay per request
        return index, slug, event

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(_fetch_one, slug, i): slug
            for i, slug in enumerate(slugs)
        }

        done_count = 0
        for future in as_completed(futures):
            done_count += 1
            index, slug, event = future.result()
            if event:
                has_market = len(event.asks) > 0 or len(event.bids) > 0
                if has_market:
                    with lock:
                        events.append(event)
                    platform_tag = f" [{event.ticket_platform}]" if event.ticket_platform else ""
                    print(f"  [{done_count}/{len(slugs)}] {event.name}{platform_tag} — active")
            # Only log every 50th skip to reduce noise
            elif done_count % 50 == 0:
                print(f"  [{done_count}/{len(slugs)}] scanning...")

    print(f"[CrowdVolt] {len(events)} events with active listings")
    return events
