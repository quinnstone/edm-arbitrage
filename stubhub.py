"""StubHub scraper using Playwright for resale ticket pricing.

StubHub renders pricing via JavaScript, so we use a headless browser
to load the search results page and extract event data from the DOM.
Prices are only available on individual event pages, not in search results,
so we navigate to each candidate event page to extract pricing from JSON-LD.
"""

import json
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
    price_is_all_in: bool = False  # True when price includes fees


def search_events(query: str, date_str: Optional[str] = None) -> list[StubHubEvent]:
    """Search StubHub for events matching a query using Playwright.

    Finds matching events on the search page, then navigates to event
    pages to extract actual pricing from JSON-LD.
    """
    events = []
    url = f"https://www.stubhub.com/search?q={query.replace(' ', '+')}"

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
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

                try:
                    page.wait_for_selector(
                        "a[href*='/event/']",
                        timeout=15000,
                    )
                except PwTimeout:
                    print("  [StubHub] No results rendered in time")
                    return []

                # Parse event cards from search results (no prices here)
                candidates = _parse_search_cards(page)
                if not candidates:
                    return []

                # Filter by date before navigating to event pages
                if date_str:
                    try:
                        target_date = dateparser.parse(date_str).date()
                        candidates = [
                            c for c in candidates
                            if c.event_date is None
                            or abs((c.event_date.date() - target_date).days) <= 1
                        ]
                    except (ValueError, TypeError):
                        pass

                # Navigate to each candidate's event page to get pricing.
                # Use a fresh page per event — StubHub throttles JS rendering
                # on subsequent loads within the same page context.
                # Limit to 3 to keep runtime reasonable.
                for candidate in candidates[:3]:
                    event_page = browser.new_page(
                        user_agent=(
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36"
                        ),
                        viewport={"width": 1280, "height": 800},
                    )
                    try:
                        result = _fetch_event_price(event_page, candidate.url)
                        if result is not None:
                            candidate.min_price, candidate.price_is_all_in = result
                            events.append(candidate)
                    finally:
                        event_page.close()

            finally:
                browser.close()
    except Exception as e:
        print(f"  [StubHub] Playwright error: {e}")
        return []

    return events


def _parse_search_cards(page) -> list[StubHubEvent]:
    """Parse event cards from StubHub search results.

    Cards contain name, date, venue, and city but no prices.
    Format: Month \\n Day \\n Weekday \\n Name \\n Time+Venue+City \\n "See tickets"
    """
    candidates = []
    cards = page.query_selector_all("a[href*='/event/']")

    seen_urls = set()
    for card in cards:
        try:
            href = card.get_attribute("href") or ""
            if not href or "/event/" not in href:
                continue

            full_url = href if href.startswith("http") else f"https://www.stubhub.com{href}"

            # Strip query params for dedup
            clean_url = full_url.split("?")[0]
            if clean_url in seen_urls:
                continue
            seen_urls.add(clean_url)

            text = card.inner_text().strip()
            if not text:
                continue

            lines = [l.strip() for l in text.split("\n") if l.strip()]
            # Expect: [month, day, weekday, name, venue_info, "See tickets"]
            if len(lines) < 4:
                continue

            # Name is typically the 4th line (after month, day, weekday)
            name = lines[3] if len(lines) > 3 else lines[0]

            # Parse date from first lines (e.g., "Aug", "29", "Sat")
            event_date = None
            if len(lines) >= 3:
                date_text = f"{lines[0]} {lines[1]}"
                try:
                    parsed = dateparser.parse(date_text, fuzzy=True)
                    if parsed:
                        # dateutil defaults to current year if not specified;
                        # if the date is in the past, bump to next year
                        if parsed.date() < datetime.now().date():
                            parsed = parsed.replace(year=parsed.year + 1)
                        event_date = parsed
                except (ValueError, TypeError):
                    pass

            # Venue+city is usually the 5th line: "7:30 PMVenue NameCity, State"
            venue = ""
            city = ""
            if len(lines) >= 5:
                venue_line = lines[4]
                # Strip leading time like "7:30 PM"
                venue_line = re.sub(r'^\d{1,2}:\d{2}\s*[AP]M\s*', '', venue_line)
                # Try to split on city pattern "City, State" at end
                city_match = re.search(r'([A-Z][a-zA-Z\s]+,\s*[A-Z]{2}(?:,\s*\w+)?)\s*$', venue_line)
                if city_match:
                    city = city_match.group(1)
                    venue = venue_line[:city_match.start()].strip()
                else:
                    venue = venue_line

            candidates.append(StubHubEvent(
                name=name,
                venue=venue,
                city=city,
                event_date=event_date,
                min_price=None,
                url=clean_url,
            ))
        except Exception as e:
            print(f"  [StubHub] Card parse error: {e}")
            continue

    return candidates


def _fetch_event_price(page, event_url: str) -> Optional[tuple[float, bool]]:
    """Navigate to a StubHub event page and extract the lowest all-in price."""
    try:
        page.goto(event_url, wait_until="domcontentloaded", timeout=15000)

        # Wait for ticket listing prices to render (JS-rendered, takes a moment)
        try:
            page.wait_for_function(
                "() => document.body.innerText.includes('incl. fees') || document.querySelector('script[type=\"application/ld+json\"]')",
                timeout=12000,
            )
        except PwTimeout:
            pass

        # Prefer the "incl. fees" price from the page — it's the real all-in
        body = page.inner_text("body")
        match = re.search(r'\$(\d+(?:,\d{3})*)\s*incl\.\s*fees', body)
        if match:
            return float(match.group(1).replace(",", "")), True

        # Fallback: JSON-LD lowPrice (base price before fees)
        ld_blocks = page.evaluate("""() => {
            const scripts = document.querySelectorAll('script[type="application/ld+json"]');
            return Array.from(scripts).map(s => s.textContent);
        }""")

        for block in (ld_blocks or []):
            try:
                data = json.loads(block)
                if data.get("@type") in ("MusicEvent", "Event", "Festival"):
                    offers = data.get("offers", {})
                    low = offers.get("lowPrice")
                    if low is not None:
                        return float(low), False
            except (json.JSONDecodeError, TypeError, ValueError):
                continue

    except Exception as e:
        print(f"  [StubHub] Failed to fetch event page price: {e}")

    return None
