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
    """Extract event-level metadata from the page HTML."""
    # Unescape for searching
    unescaped = html.replace('\\"', '"')

    meta = {}
    for field_name in ["name", "venue", "area_name", "doors_open_time"]:
        match = re.search(f'"{field_name}":"([^"]+)"', unescaped)
        if match:
            meta[field_name] = match.group(1)

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

    # Compute summary prices
    if event.asks:
        event.min_ask = min(a.price for a in event.asks)
    if event.bids:
        event.max_bid = max(b.price for b in event.bids)

    return event


def fetch_all_events() -> list[CrowdVoltEvent]:
    """Fetch all active CrowdVolt events with marketplace data."""
    print("[CrowdVolt] Fetching sitemap...")
    slugs = fetch_sitemap()
    print(f"[CrowdVolt] Found {len(slugs)} event URLs in sitemap")

    events = []
    for i, slug in enumerate(slugs):
        event = fetch_event(slug)
        if event:
            has_market = len(event.asks) > 0 or len(event.bids) > 0
            status = "active" if has_market else "no listings"
            print(f"  [{i+1}/{len(slugs)}] {event.name} — {status}")
            if has_market:
                events.append(event)
        else:
            print(f"  [{i+1}/{len(slugs)}] {slug} — skipped (not found)")

        # Be polite with request spacing
        if i < len(slugs) - 1:
            time.sleep(config.REQUEST_DELAY_SECONDS)

    print(f"[CrowdVolt] {len(events)} events with active listings")
    return events
