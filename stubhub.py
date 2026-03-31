"""StubHub scraper using Playwright for resale ticket pricing.

StubHub renders pricing via JavaScript, so we use a headless browser
to load the search results page and extract event data from the DOM.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from dateutil import parser as dateparser
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

import config


@dataclass
class StubHubEvent:
    name: str
    venue: str
    city: str
    event_date: Optional[datetime]
    min_price: Optional[float]
    url: str


def _launch_browser(pw):
    """Launch a headless Chromium with stealth-ish settings."""
    return pw.chromium.launch(headless=True)


def _build_search_url(query: str, date_str: Optional[str] = None) -> str:
    """Build a StubHub search URL."""
    base = "https://www.stubhub.com/find/s/"
    params = f"?q={query.replace(' ', '+')}"
    if date_str:
        params += f"&date={date_str}"
    return base + params


def search_events(query: str, date_str: Optional[str] = None) -> list[StubHubEvent]:
    """Search StubHub for events matching a query using Playwright.

    Args:
        query: Artist or event name to search for.
        date_str: Optional date string (YYYY-MM-DD) to filter results.
    """
    events = []
    url = _build_search_url(query, date_str)

    try:
        with sync_playwright() as pw:
            browser = _launch_browser(pw)
            try:
                page = browser.new_page(
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 800},
                )

                page.goto(url, wait_until="domcontentloaded", timeout=20000)

                # StubHub may serve a JS challenge. Wait for real content.
                try:
                    page.wait_for_selector(
                        "#__NEXT_DATA__, [data-testid='primaryGridListing'], a[href*='/event/']",
                        timeout=15000,
                    )
                except PwTimeout:
                    print("  [StubHub] No results rendered in time")
                    return []

                # Try JSON extraction first, fall back to DOM parsing
                events = _extract_from_json(page)
                if not events:
                    events = _extract_from_search(page)
            finally:
                browser.close()
    except Exception as e:
        print(f"  [StubHub] Playwright error: {e}")
        return []

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


def _extract_from_search(page) -> list[StubHubEvent]:
    """Extract event data from StubHub search results page."""
    events = []

    # StubHub search results are typically in card/list format
    # Try multiple selector strategies for resilience

    # Strategy 1: Look for structured event links with pricing
    cards = page.query_selector_all("a[href*='/event/']")

    seen_urls = set()
    for card in cards:
        try:
            href = card.get_attribute("href") or ""
            if not href or "/event/" not in href:
                continue

            full_url = href if href.startswith("http") else f"https://www.stubhub.com{href}"

            # Deduplicate
            event_id = re.search(r"/event/(\d+)", href)
            if event_id:
                eid = event_id.group(1)
                if eid in seen_urls:
                    continue
                seen_urls.add(eid)

            # Get text content from the card
            text = card.inner_text().strip()
            if not text:
                continue

            # Parse the card text — StubHub cards typically show:
            # Event Name \n Date \n Venue \n City, State \n From $XX
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            if len(lines) < 2:
                continue

            name = lines[0]
            venue = ""
            city = ""
            event_date = None
            min_price = None

            for line in lines[1:]:
                # Price detection
                price_match = re.search(r'\$(\d+(?:,\d{3})*(?:\.\d{2})?)', line)
                if price_match and min_price is None:
                    min_price = float(price_match.group(1).replace(",", ""))
                    continue

                # Date detection
                if event_date is None:
                    try:
                        parsed = dateparser.parse(line, fuzzy=True)
                        if parsed and parsed.year >= 2025:
                            event_date = parsed
                            continue
                    except (ValueError, TypeError):
                        pass

                # Venue/city — usually contains a comma for "City, State"
                if not venue and not any(c in line.lower() for c in ["from", "ticket", "$"]):
                    if "," in line:
                        city = line
                    else:
                        venue = line

            if name and min_price is not None:
                events.append(StubHubEvent(
                    name=name,
                    venue=venue,
                    city=city,
                    event_date=event_date,
                    min_price=min_price,
                    url=full_url,
                ))
        except Exception:
            continue

    # Strategy 2: If no cards found, try extracting from any JSON in the page
    if not events:
        events = _extract_from_json(page)

    return events


def _extract_from_json(page) -> list[StubHubEvent]:
    """Fallback: extract event data from embedded JSON in the page."""
    events = []

    try:
        # Check for __NEXT_DATA__ or similar embedded JSON
        json_content = page.evaluate("""() => {
            const el = document.querySelector('#__NEXT_DATA__');
            if (el) return el.textContent;

            // Try window state
            if (window.__data) return JSON.stringify(window.__data);
            if (window.__NEXT_DATA__) return JSON.stringify(window.__NEXT_DATA__);

            return null;
        }""")

        if json_content:
            import json
            data = json.loads(json_content)
            events = _parse_next_data(data)
    except Exception:
        pass

    return events


def _parse_next_data(data: dict) -> list[StubHubEvent]:
    """Recursively search Next.js data for event listings with pricing."""
    events = []

    def _search(obj, depth=0):
        if depth > 10:
            return
        if isinstance(obj, dict):
            # Look for event-like objects with pricing
            if "minPrice" in obj or "minListPrice" in obj or "priceRange" in obj:
                name = obj.get("name", obj.get("title", obj.get("eventName", "")))
                price = obj.get("minPrice", obj.get("minListPrice"))
                if isinstance(price, dict):
                    price = price.get("amount")
                if name and price:
                    events.append(StubHubEvent(
                        name=str(name),
                        venue=obj.get("venue", obj.get("venueName", "")),
                        city=obj.get("city", ""),
                        event_date=None,
                        min_price=float(price),
                        url=obj.get("url", ""),
                    ))
                    return
            for v in obj.values():
                _search(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                _search(item, depth + 1)

    _search(data)
    return events
