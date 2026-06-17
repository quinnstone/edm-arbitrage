"""Gametime scraper for resale ticket pricing.

Two data sources:

- The public search page server-renders JSON-LD event listings — used to
  resolve query → list of candidate events (name, venue, date, URL with
  event ID).
- mobile.gametime.co/v1 (the same unauthenticated API the website's own
  React frontend hits) exposes per-listing depth, exact all-in pricing,
  face values, and section/row data. We hit it lazily — only after a CV
  event matches all the standard filters — to keep the per-scan request
  count bounded.
"""

import json
import re
import time
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

MOBILE_API = "https://mobile.gametime.co/v1"
MOBILE_HEADERS = {
    **HEADERS,
    "Accept": "application/json",
    "Origin": "https://gametime.co",
    "Referer": "https://gametime.co/",
}


@dataclass
class GametimeListing:
    """A single ask from Gametime's listings book. Prices in dollars."""
    listing_id: str
    section: str
    row: str
    qty: int
    face_value: float
    prefee: float
    total: float
    delivery_type: str


@dataclass
class GametimeEvent:
    name: str
    venue: str
    city: str
    event_date: Optional[datetime]
    min_price: Optional[float]
    url: str
    price_is_all_in: bool = False  # Gametime shows all-in prices by default
    # Populated when enrich_with_listings() succeeds:
    event_id: str = ""
    face_value: Optional[float] = None
    listings: list = field(default_factory=list)


def search_events(query: str, date_str: Optional[str] = None) -> list[GametimeEvent]:
    """Search Gametime for events matching a query.

    Fetches the search results page and extracts event data from
    server-rendered JSON-LD blocks.

    Args:
        query: Artist or event name to search for.
        date_str: Optional date string (YYYY-MM-DD) to filter results.
    """
    url = f"https://gametime.co/search?q={query.replace(' ', '+')}"

    html = None
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=config.REQUEST_TIMEOUT)
            if resp.status_code == 200:
                html = resp.text
                break
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                print(f"  [Gametime] HTTP {resp.status_code}, retrying...")
                time.sleep(2 * (attempt + 1))
                continue
            print(f"  [Gametime] HTTP {resp.status_code}")
            return []
        except requests.RequestException as e:
            if attempt < 2:
                print(f"  [Gametime] Request error: {e}, retrying...")
                time.sleep(2 * (attempt + 1))
                continue
            print(f"  [Gametime] Request failed after retries: {e}")
            return []

    if html is None:
        return []
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

    # Event ID is the final path segment after /events/ — used to hit
    # the mobile API for per-listing depth.
    event_id = ""
    id_match = re.search(r"/events/([a-f0-9]{20,})", event_url)
    if id_match:
        event_id = id_match.group(1)

    return GametimeEvent(
        name=name,
        venue=venue,
        city=city,
        event_date=event_date,
        min_price=min_price,
        url=event_url,
        price_is_all_in=True,  # JSON-LD lowPrice is shown all-in
        event_id=event_id,
    )


# ---------------------------------------------------------------------------
# Mobile API — per-listing book with exact all-in pricing and face values
# ---------------------------------------------------------------------------

def fetch_listings(event_id: str, retries: int = 2) -> list[GametimeListing]:
    """Fetch full per-listing book from Gametime's mobile API.

    Returns [] on any failure so callers can fall back to JSON-LD pricing.
    Prices in the API are integer cents; we convert to dollars.
    """
    if not event_id:
        return []
    for attempt in range(1 + retries):
        try:
            resp = requests.get(
                f"{MOBILE_API}/listings",
                params={"event_id": event_id},
                headers=MOBILE_HEADERS,
                timeout=config.REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                break
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            return []
        except requests.RequestException:
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            return []

    try:
        payload = resp.json()
    except ValueError:
        return []

    listings = []
    for item in payload.get("listings", []):
        price = item.get("price") or {}
        total_cents = price.get("total")
        if total_cents is None:
            continue
        lots = item.get("lots") or [1]
        listings.append(GametimeListing(
            listing_id=item.get("id", ""),
            section=item.get("section", ""),
            row=item.get("row", ""),
            qty=len(lots) if isinstance(lots, list) else 1,
            face_value=(price.get("face_value") or 0) / 100,
            prefee=(price.get("prefee") or 0) / 100,
            total=total_cents / 100,
            delivery_type=item.get("delivery_type", ""),
        ))
    return listings


def enrich_with_listings(event: GametimeEvent) -> bool:
    """Populate per-listing depth + replace estimated min_price with the
    API's exact all-in price. Returns True if enrichment succeeded.

    Designed to be called lazily — typically once per CV→Gametime match —
    so a full scan adds at most one extra request per real match.
    """
    if event.listings or not event.event_id:
        return bool(event.listings)
    listings = fetch_listings(event.event_id)
    if not listings:
        return False
    event.listings = listings
    event.min_price = min(l.total for l in listings)
    event.price_is_all_in = True
    face_values = [l.face_value for l in listings if l.face_value > 0]
    if face_values:
        event.face_value = min(face_values)
    return True
