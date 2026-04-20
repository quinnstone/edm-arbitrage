"""SeatGeek API client for fetching event pricing."""

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import requests
from dateutil import parser as dateparser

import config

BASE_URL = "https://api.seatgeek.com/2"


@dataclass
class SeatGeekEvent:
    id: int
    title: str
    venue: str
    city: str
    event_date: Optional[datetime]
    lowest_price: Optional[float]
    average_price: Optional[float]
    highest_price: Optional[float]
    url: str


def search_events(query: str, date_str: Optional[str] = None) -> list[SeatGeekEvent]:
    """Search SeatGeek for events matching a query.

    Args:
        query: Artist or event name to search for.
        date_str: Optional date string (YYYY-MM-DD) to filter results.
    """
    if not config.SEATGEEK_CLIENT_ID:
        print("[SeatGeek] No API key configured — skipping")
        return []

    params = {
        "client_id": config.SEATGEEK_CLIENT_ID,
        "q": query,
        "per_page": 25,
        "type": "concert",
    }

    # Narrow by date range if provided
    if date_str:
        params["datetime_local.gte"] = f"{date_str}T00:00:00"
        params["datetime_local.lte"] = f"{date_str}T23:59:59"

    data = None
    for attempt in range(3):
        try:
            resp = requests.get(
                f"{BASE_URL}/events",
                params=params,
                timeout=config.REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                data = resp.json()
                break
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                print(f"[SeatGeek] HTTP {resp.status_code}, retrying...")
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
        except requests.RequestException as e:
            if attempt < 2:
                print(f"[SeatGeek] API error: {e}, retrying...")
                time.sleep(2 * (attempt + 1))
                continue
            print(f"[SeatGeek] API error after retries: {e}")
            return []

    if data is None:
        return []

    events = []
    for item in data.get("events", []):
        stats = item.get("stats", {})
        venue_obj = item.get("venue", {})

        dt = None
        if item.get("datetime_local"):
            try:
                dt = dateparser.parse(item["datetime_local"])
            except (ValueError, TypeError):
                pass

        events.append(SeatGeekEvent(
            id=item.get("id", 0),
            title=item.get("title", ""),
            venue=venue_obj.get("name", ""),
            city=venue_obj.get("city", ""),
            event_date=dt,
            lowest_price=stats.get("lowest_price"),
            average_price=stats.get("average_price"),
            highest_price=stats.get("highest_price"),
            url=item.get("url", ""),
        ))

    return events
