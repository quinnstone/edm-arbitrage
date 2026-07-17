"""Resident Advisor (ra.co) event scraper via public GraphQL endpoint.

RA is a primary listing platform for many electronic music events. When
CV lists an event with ticket_platform='Resident Advisor' AND has an
active bid, RA is a potential acquisition channel — buy on RA, fulfill
CV bid.

Key constraints discovered during prototyping (2026-07-15):
- The public GraphQL endpoint accepts unauthenticated requests with
  browser-like Origin/Referer headers.
- Availability fields (`totalTicketAllocation`, `totalTicketsSold`) on
  Event require auth and return AUTH_NOT_AUTHORIZED. We can't check
  sold-out status without logging in. The alert flow must therefore
  surface listings unconditionally and let the operator verify by
  clicking through to the RA event page.
- Server-side filters (`title.contains`, `name.match`) don't reliably
  narrow the result set. We do coarse filtering by area + date range,
  then local fuzzy matching.
- Pagination is mandatory — a 3-day NYC window can have 200+ events;
  page 1 alone won't surface the target. Skipping this was the bug in
  the first prototype.
- All sampled events show `ticketingSystem="LEGACY"` meaning RA is
  listing the event but pointing to an external primary (DICE,
  Eventbrite, etc). The `cost` string field is the only accessible
  price signal — usually formatted as "$70", "$20+", "£41+", "".

The `cost` string parses to a base-price floor. Buyer fees are added
at checkout on the external ticket vendor, typically ~10-15%, which
the matcher's `fees_estimated=True` path handles.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import requests
from dateutil import parser as dateparser

import config

GRAPHQL_URL = "https://ra.co/graphql"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Origin": "https://ra.co",
    "Referer": "https://ra.co/",
}

# RA area IDs. Discovered via the `areas(searchTerm: ...)` query. Each
# CV event's city may map to more than one candidate area, so we search
# each and union the results. Expand this list to cover more markets as
# needed.
CITY_TO_RA_AREAS = {
    "new york": [8, 605],           # NYC City + NY State (some venues sit under state area)
    "brooklyn": [8, 605],           # under NYC City
    "los angeles": [23],
    "san francisco": [26],
    "chicago": [27],
    "miami": [61],
    "boston": [24],
    "detroit": [29],
    "london": [13],
    "berlin": [34],
    "amsterdam": [17],
    "paris": [44],
    "barcelona": [63],
}

PAGE_SIZE = 50
MAX_PAGES = 10  # covers ~500 events per area — enough for a 3-day window


@dataclass
class ResidentAdvisorEvent:
    name: str
    venue: str
    city: str
    event_date: Optional[datetime]
    min_price: Optional[float]
    url: str
    # `cost` from RA is the base price (external primary charges buyer
    # fees at checkout). matcher sees this and applies the estimated-fee
    # multiplier.
    price_is_all_in: bool = False


_LISTINGS_QUERY = """query GET_DEFAULT_EVENTS_LISTING(
    $filters: FilterInputDtoInput!,
    $page: Int,
    $pageSize: Int,
    $sort: SortInputDtoInput
) {
    eventListings(filters: $filters, page: $page, pageSize: $pageSize, sort: $sort) {
        data { event {
            id title date cost isTicketed
            venue { name area { name } }
            contentUrl
            artists { name }
        } }
        totalResults
    }
}"""


def search_events(query: str, date_str: Optional[str] = None,
                  city_hint: Optional[str] = None) -> list[ResidentAdvisorEvent]:
    """Fetch RA events in a +/-1 day window and locally match by query.

    RA's server-side title filter is unreliable — it returns broad popular
    results instead of narrowing. We fetch upcoming events in the target
    area(s) and date window, then filter locally on title + artists.

    Requires date_str. If city_hint is provided and known, restricts the
    area filter to those RA area IDs; otherwise searches globally.
    """
    if not date_str:
        return []
    try:
        target = dateparser.parse(date_str).date()
    except (ValueError, TypeError):
        return []

    start = (target - timedelta(days=1)).isoformat()
    end = (target + timedelta(days=1)).isoformat()

    area_ids = _resolve_area_ids(city_hint)
    q_lower = query.lower().strip()
    q_words = {w for w in q_lower.split() if len(w) > 2}

    seen_ids = set()
    matches = []

    # Search each area, paginating until either we hit an artist match
    # in each area or exhaust MAX_PAGES.
    for area_id in area_ids:
        for page in range(1, MAX_PAGES + 1):
            filters = {"listingDate": {"gte": start, "lte": end}}
            if area_id is not None:
                filters["areas"] = {"eq": area_id}

            payload = {
                "operationName": "GET_DEFAULT_EVENTS_LISTING",
                "variables": {
                    "filters": filters,
                    "page": page,
                    "pageSize": PAGE_SIZE,
                    "sort": {"listingDate": {"order": "ASCENDING"}},
                },
                "query": _LISTINGS_QUERY,
            }
            try:
                resp = requests.post(GRAPHQL_URL, json=payload, headers=HEADERS,
                                     timeout=config.REQUEST_TIMEOUT)
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as e:
                print(f"  [RA] Search error (area={area_id}, page={page}): {e}",
                      flush=True)
                break

            listings = (data.get("data", {}) or {}).get("eventListings", {}).get("data") or []
            if not listings:
                break

            for item in listings:
                ev = item.get("event") or {}
                eid = ev.get("id")
                if not eid or eid in seen_ids:
                    continue

                title = ev.get("title") or ""
                artists = " ".join(a.get("name", "") for a in (ev.get("artists") or []))
                haystack = f"{title} {artists}".lower()

                # Match if query appears as substring OR most of the query's
                # significant words appear in the haystack. This handles
                # RA's "Teksupport: Adriatique present X" naming pattern
                # where a promoter prefixes the artist name.
                if q_lower not in haystack:
                    hay_words = set(haystack.split())
                    if not q_words or len(q_words & hay_words) < max(1, len(q_words) - 1):
                        continue

                seen_ids.add(eid)

                min_price = _parse_cost(ev.get("cost") or "")

                event_date = None
                if ev.get("date"):
                    try:
                        event_date = dateparser.parse(ev["date"])
                    except (ValueError, TypeError):
                        pass

                venue_obj = ev.get("venue") or {}
                area_obj = venue_obj.get("area") or {}

                matches.append(ResidentAdvisorEvent(
                    name=title,
                    venue=venue_obj.get("name") or "",
                    city=area_obj.get("name") or "",
                    event_date=event_date,
                    min_price=min_price,
                    url=f"https://ra.co{ev.get('contentUrl', '')}",
                    price_is_all_in=False,
                ))

            # Stop paginating this area if we've hit at least one match —
            # further pages are unlikely to add for the same query in a
            # 3-day window.
            if any(m for m in matches if m.city and area_obj.get("name") == m.city):
                if matches:
                    break

    return matches


def _resolve_area_ids(city_hint: Optional[str]) -> list[Optional[int]]:
    """Map a city hint to RA area IDs. Falls back to [None] (unfiltered
    global search) when the city isn't in our map — with pagination this
    is expensive but still finds events."""
    if not city_hint:
        return [None]
    h = city_hint.lower().strip()
    for key, ids in CITY_TO_RA_AREAS.items():
        if key in h:
            return list(ids)
    return [None]


def _parse_cost(cost_str: str) -> Optional[float]:
    """Extract the numeric floor from RA's cost string.

    RA uses forms like "$20", "£41+", "20", "$20 - $30", "". We take
    the first numeric group after stripping currency symbols and ranges.
    """
    if not cost_str:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", cost_str)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None
