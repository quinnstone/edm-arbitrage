"""TickPick scraper for resale ticket pricing.

TickPick embeds schema.org JSON-LD in their performer pages with real
AggregateOffer pricing (lowPrice, highPrice). No API key needed, no
buyer fees on their platform.
"""

import re
import json
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
class TickPickEvent:
    name: str
    venue: str
    city: str
    event_date: Optional[datetime]
    low_price: Optional[float]
    high_price: Optional[float]
    url: str


def _slugify_query(query: str) -> str:
    """Convert an event/artist name to a TickPick URL slug."""
    slug = query.lower().strip()
    # Remove common noise
    for noise in [" at ", " @ ", " b2b ", " & ", " and ", " x "]:
        slug = slug.replace(noise, "-")
    # Replace non-alphanumeric with hyphens
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug


def search_events(query: str, date_str: Optional[str] = None) -> list[TickPickEvent]:
    """Search TickPick for events matching a query.

    Scrapes the performer page and extracts JSON-LD pricing data.
    Falls back to search page if the direct performer URL doesn't work.

    Args:
        query: Artist or event name to search for.
        date_str: Optional date string (YYYY-MM-DD) to filter results.
    """
    events = []

    # Try direct performer page first
    slug = _slugify_query(query)
    performer_url = f"https://www.tickpick.com/concerts/{slug}-tickets/"

    try:
        resp = requests.get(performer_url, timeout=config.REQUEST_TIMEOUT, headers=HEADERS)
        if resp.status_code == 200 and len(resp.text) > 5000:
            events = _extract_events_from_html(resp.text)
    except requests.RequestException:
        pass

    # If direct URL didn't work, try search
    if not events:
        try:
            resp = requests.get(
                "https://www.tickpick.com/search",
                params={"q": query},
                timeout=config.REQUEST_TIMEOUT,
                headers=HEADERS,
            )
            if resp.status_code == 200:
                # Find performer links in search results
                performer_links = re.findall(
                    r'href="(/concerts/[^"]+tickets/)"', resp.text
                )
                # Try first matching performer page
                for link in performer_links[:3]:
                    try:
                        r2 = requests.get(
                            f"https://www.tickpick.com{link}",
                            timeout=config.REQUEST_TIMEOUT,
                            headers=HEADERS,
                        )
                        if r2.status_code == 200:
                            found = _extract_events_from_html(r2.text)
                            if found:
                                events = found
                                break
                    except requests.RequestException:
                        continue
        except requests.RequestException:
            pass

    # Filter by date if provided
    if date_str and events:
        target_date = dateparser.parse(date_str).date()
        events = [
            e for e in events
            if e.event_date is None
            or abs((e.event_date.date() - target_date).days) <= 1
        ]

    return events


def _extract_events_from_html(html: str) -> list[TickPickEvent]:
    """Extract event pricing from JSON-LD schema.org data in the page."""
    events = []

    ld_blocks = re.findall(
        r'<script type="application/ld\+json">(.*?)</script>',
        html,
        re.DOTALL,
    )

    for block in ld_blocks:
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue

        # Handle MusicEvent directly
        if data.get("@type") in ("MusicEvent", "Event"):
            event = _parse_event(data)
            if event:
                events.append(event)

        # Handle MusicGroup with nested events
        elif data.get("@type") == "MusicGroup" and data.get("event"):
            for e in data["event"]:
                event = _parse_event(e)
                if event:
                    events.append(event)

    return events


def _parse_event(data: dict) -> Optional[TickPickEvent]:
    """Parse a single JSON-LD event into a TickPickEvent."""
    offers = data.get("offers", {})
    low_price = offers.get("lowPrice")
    high_price = offers.get("highPrice")

    # Skip events with no pricing
    if low_price is None:
        return None

    # Parse date
    event_date = None
    if data.get("startDate"):
        try:
            event_date = dateparser.parse(data["startDate"])
        except (ValueError, TypeError):
            pass

    # Parse venue
    venue = ""
    city = ""
    location = data.get("location", {})
    if isinstance(location, dict):
        venue = location.get("name", "")
        address = location.get("address", {})
        if isinstance(address, dict):
            city = address.get("addressLocality", "")

    return TickPickEvent(
        name=data.get("name", ""),
        venue=venue,
        city=city,
        event_date=event_date,
        low_price=float(low_price) if low_price else None,
        high_price=float(high_price) if high_price else None,
        url=data.get("url", ""),
    )
