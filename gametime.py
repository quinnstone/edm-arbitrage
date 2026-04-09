"""Gametime scraper using plain HTTP requests for resale ticket pricing.

Gametime server-renders JSON-LD (schema.org) event data in search result
pages, so we can extract names, dates, venues, and all-in prices without
a headless browser.
"""

import json
import re
from dataclasses import dataclass
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
class GametimeEvent:
    name: str
    venue: str
    city: str
    event_date: Optional[datetime]
    min_price: Optional[float]
    url: str
    price_is_all_in: bool = False  # Gametime shows all-in prices by default


def search_events(query: str, date_str: Optional[str] = None) -> list[GametimeEvent]:
    """Search Gametime for events matching a query.

    Fetches the search results page and extracts event data from
    server-rendered JSON-LD blocks.

    Args:
        query: Artist or event name to search for.
        date_str: Optional date string (YYYY-MM-DD) to filter results.
    """
    url = f"https://gametime.co/search?q={query.replace(' ', '+')}"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=config.REQUEST_TIMEOUT)
        if resp.status_code != 200:
            print(f"  [Gametime] HTTP {resp.status_code}")
            return []
    except requests.RequestException as e:
        print(f"  [Gametime] Request failed: {e}")
        return []

    html = resp.text
    events = _extract_json_ld(html)

    # Filter by date if provided
    if date_str and events:
        try:
            target_date = dateparser.parse(date_str).date()
            events = [
                e for e in events
                if e.event_date is None
                or abs((e.event_date.date() - target_date).days) <= 1
            ]
        except (ValueError, TypeError):
            pass

    return events


def _extract_json_ld(html: str) -> list[GametimeEvent]:
    """Extract event data from JSON-LD schema.org blocks in the HTML."""
    events = []
    ld_matches = re.findall(
        r'<script type="application/ld\+json">(.*?)</script>',
        html,
        re.DOTALL,
    )

    for block in ld_matches:
        try:
            data = json.loads(block)
        except (json.JSONDecodeError, TypeError):
            continue

        items = data if isinstance(data, list) else [data]
        for item in items:
            event = _parse_ld_event(item)
            if event:
                events.append(event)

    return events


def _parse_ld_event(data: dict) -> Optional[GametimeEvent]:
    """Parse a JSON-LD event object into a GametimeEvent."""
    if data.get("@type") not in ("MusicEvent", "Event", "Festival", "SportsEvent"):
        return None

    name = data.get("name", "")
    if not name:
        return None

    offers = data.get("offers", {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    low_price = offers.get("lowPrice", offers.get("price"))
    min_price = float(low_price) if low_price is not None else None

    event_date = None
    if data.get("startDate"):
        try:
            event_date = dateparser.parse(data["startDate"])
        except (ValueError, TypeError):
            pass

    venue = ""
    city = ""
    location = data.get("location", {})
    if isinstance(location, dict):
        venue = location.get("name", "")
        address = location.get("address", {})
        if isinstance(address, dict):
            city = address.get("addressLocality", "")

    event_url = data.get("url", "")

    return GametimeEvent(
        name=name,
        venue=venue,
        city=city,
        event_date=event_date,
        min_price=min_price,
        url=event_url,
        price_is_all_in=True,  # Gametime prices are all-in
    )
