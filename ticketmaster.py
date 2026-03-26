"""Ticketmaster Discovery API client for fetching event pricing."""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import requests
from dateutil import parser as dateparser

import config

BASE_URL = "https://app.ticketmaster.com/discovery/v2"


@dataclass
class TicketmasterEvent:
    id: str
    name: str
    venue: str
    city: str
    event_date: Optional[datetime]
    min_price: Optional[float]
    max_price: Optional[float]
    url: str


def search_events(query: str, date_str: Optional[str] = None) -> list[TicketmasterEvent]:
    """Search Ticketmaster for events matching a query.

    Args:
        query: Artist or event name to search for.
        date_str: Optional date string (YYYY-MM-DD) to filter results.
    """
    if not config.TICKETMASTER_API_KEY:
        print("[Ticketmaster] No API key configured — skipping")
        return []

    params = {
        "apikey": config.TICKETMASTER_API_KEY,
        "keyword": query,
        "size": 25,
        "classificationName": "Music",
        "sort": "date,asc",
    }

    if date_str:
        params["startDateTime"] = f"{date_str}T00:00:00Z"
        params["endDateTime"] = f"{date_str}T23:59:59Z"

    try:
        resp = requests.get(
            f"{BASE_URL}/events.json",
            params=params,
            timeout=config.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"[Ticketmaster] API error: {e}")
        return []

    events = []
    embedded = data.get("_embedded", {})
    for item in embedded.get("events", []):
        # Extract venue info
        venue_name = ""
        city_name = ""
        venues = item.get("_embedded", {}).get("venues", [])
        if venues:
            venue_name = venues[0].get("name", "")
            city_obj = venues[0].get("city", {})
            city_name = city_obj.get("name", "")

        # Extract price ranges
        min_price = None
        max_price = None
        price_ranges = item.get("priceRanges", [])
        if price_ranges:
            min_price = price_ranges[0].get("min")
            max_price = price_ranges[0].get("max")

        # Extract date
        dt = None
        dates = item.get("dates", {}).get("start", {})
        if dates.get("dateTime"):
            try:
                dt = dateparser.parse(dates["dateTime"])
            except (ValueError, TypeError):
                pass
        elif dates.get("localDate"):
            try:
                dt = dateparser.parse(dates["localDate"])
            except (ValueError, TypeError):
                pass

        events.append(TicketmasterEvent(
            id=item.get("id", ""),
            name=item.get("name", ""),
            venue=venue_name,
            city=city_name,
            event_date=dt,
            min_price=min_price,
            max_price=max_price,
            url=item.get("url", ""),
        ))

    return events
