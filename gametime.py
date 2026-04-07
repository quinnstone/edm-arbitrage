"""Gametime scraper using Playwright for resale ticket pricing.

Gametime is a JS-heavy React app that shows "all-in" prices by default
(no hidden fees). We use a headless browser to load search results and
extract event data from embedded JSON or DOM parsing.
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
class GametimeEvent:
    name: str
    venue: str
    city: str
    event_date: Optional[datetime]
    min_price: Optional[float]
    url: str
    price_is_all_in: bool = False  # Gametime shows all-in prices by default


def search_events(query: str, date_str: Optional[str] = None) -> list[GametimeEvent]:
    """Search Gametime for events matching a query using Playwright.

    Args:
        query: Artist or event name to search for.
        date_str: Optional date string (YYYY-MM-DD) to filter results.
    """
    events = []
    url = f"https://gametime.co/search?q={query.replace(' ', '+')}"

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

                # Wait for search results to render
                try:
                    page.wait_for_selector(
                        "a[href*='/tickets/'], a[href*='/performers/'], a[href*='/events/']",
                        timeout=15000,
                    )
                except PwTimeout:
                    print("  [Gametime] No results rendered in time")
                    return []

                # Strategy 1: __NEXT_DATA__ or similar embedded JSON
                events = _extract_next_data(page)

                # Strategy 2: JSON-LD blocks
                if not events:
                    events = _extract_json_ld(page)

                # Strategy 3: DOM text parsing
                if not events:
                    events = _extract_from_dom(page)

                # If search results link to performer pages, follow the first
                # one to get individual event listings
                if not events:
                    events = _follow_performer_page(browser, page)

                # If we got candidates without prices, navigate to event pages
                # to extract pricing. Limit to 3 to keep runtime reasonable.
                needs_price = [e for e in events if e.min_price is None]
                has_price = [e for e in events if e.min_price is not None]

                for candidate in needs_price[:3]:
                    if not candidate.url:
                        continue
                    event_page = browser.new_page(
                        user_agent=(
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36"
                        ),
                        viewport={"width": 1280, "height": 800},
                    )
                    try:
                        price = _fetch_event_price(event_page, candidate.url)
                        if price is not None:
                            candidate.min_price = price
                            candidate.price_is_all_in = True
                            has_price.append(candidate)
                    finally:
                        event_page.close()

                events = has_price

            finally:
                browser.close()
    except Exception as e:
        print(f"  [Gametime] Playwright error: {e}")
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


def _extract_next_data(page) -> list[GametimeEvent]:
    """Extract event data from __NEXT_DATA__ or similar embedded JSON."""
    events = []
    try:
        json_text = page.evaluate("""() => {
            const el = document.querySelector('#__NEXT_DATA__');
            return el ? el.textContent : null;
        }""")
        if not json_text:
            return []

        data = json.loads(json_text)

        def _search(obj, depth=0):
            if depth > 12 or not obj:
                return
            if isinstance(obj, dict):
                has_name = any(k in obj for k in ["name", "title", "performer"])
                has_price = any(k in obj for k in [
                    "minPrice", "price", "lowPrice", "cheapestPrice",
                    "min_price", "lowest_price",
                ])
                if has_name and has_price:
                    name = obj.get("name", obj.get("title", obj.get("performer", "")))
                    price = (obj.get("minPrice") or obj.get("cheapestPrice")
                             or obj.get("min_price") or obj.get("lowest_price")
                             or obj.get("price") or obj.get("lowPrice"))
                    if name and price:
                        venue_name = ""
                        city_name = ""
                        venue_obj = obj.get("venue", {})
                        if isinstance(venue_obj, dict):
                            venue_name = venue_obj.get("name", "")
                            city_name = venue_obj.get("city", "")
                        elif isinstance(venue_obj, str):
                            venue_name = venue_obj

                        event_date = None
                        date_val = obj.get("date", obj.get("eventDate",
                                   obj.get("startDate", obj.get("datetime_local", ""))))
                        if date_val:
                            try:
                                event_date = dateparser.parse(str(date_val))
                            except (ValueError, TypeError):
                                pass

                        event_url = obj.get("url", obj.get("webPath", obj.get("path", "")))
                        if event_url and not event_url.startswith("http"):
                            event_url = f"https://gametime.co{event_url}"

                        events.append(GametimeEvent(
                            name=str(name),
                            venue=venue_name,
                            city=city_name,
                            event_date=event_date,
                            min_price=float(price),
                            url=event_url,
                            price_is_all_in=True,  # Gametime prices are all-in
                        ))
                        return
                for v in obj.values():
                    _search(v, depth + 1)
            elif isinstance(obj, list):
                for item in obj:
                    _search(item, depth + 1)

        _search(data)
    except Exception:
        pass
    return events


def _extract_json_ld(page) -> list[GametimeEvent]:
    """Extract event data from JSON-LD schema.org blocks."""
    events = []
    try:
        ld_blocks = page.evaluate("""() => {
            const scripts = document.querySelectorAll('script[type="application/ld+json"]');
            return Array.from(scripts).map(s => s.textContent);
        }""")

        for block in (ld_blocks or []):
            try:
                data = json.loads(block)
            except (json.JSONDecodeError, TypeError):
                continue

            items = data if isinstance(data, list) else [data]
            for item in items:
                event = _parse_ld_event(item)
                if event:
                    events.append(event)

                # Handle nested events
                for nested in item.get("event", item.get("subEvent", [])):
                    if isinstance(nested, dict):
                        event = _parse_ld_event(nested)
                        if event:
                            events.append(event)
    except Exception:
        pass
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
    if event_url and not event_url.startswith("http"):
        event_url = f"https://gametime.co{event_url}"

    return GametimeEvent(
        name=name,
        venue=venue,
        city=city,
        event_date=event_date,
        min_price=min_price,
        url=event_url,
        price_is_all_in=True,  # Gametime prices are all-in
    )


def _extract_from_dom(page) -> list[GametimeEvent]:
    """Extract event data from DOM text by parsing event cards/links."""
    events = []

    cards = page.query_selector_all(
        "a[href*='/tickets/'], a[href*='/events/']"
    )

    seen_urls = set()
    for card in cards:
        try:
            href = card.get_attribute("href") or ""
            if not href:
                continue

            full_url = href if href.startswith("http") else f"https://gametime.co{href}"

            clean_url = full_url.split("?")[0]
            if clean_url in seen_urls:
                continue
            seen_urls.add(clean_url)

            text = card.inner_text().strip()
            if not text:
                continue

            lines = [l.strip() for l in text.split("\n") if l.strip()]
            if len(lines) < 1:
                continue

            name = lines[0]
            venue = ""
            city = ""
            event_date = None
            min_price = None

            for line in lines[1:]:
                # Price — look for "$XX" patterns
                price_match = re.search(r'\$(\d+(?:,\d{3})*(?:\.\d{2})?)', line)
                if price_match and min_price is None:
                    min_price = float(price_match.group(1).replace(",", ""))
                    continue

                # Date
                if event_date is None:
                    try:
                        parsed = dateparser.parse(line, fuzzy=True)
                        if parsed and parsed.year >= 2025:
                            event_date = parsed
                            continue
                    except (ValueError, TypeError):
                        pass

                # Venue/city heuristic
                if not venue and not any(c in line.lower() for c in [
                    "from", "ticket", "$", "buy", "see", "view",
                ]):
                    if "," in line:
                        city = line
                    else:
                        venue = line

            events.append(GametimeEvent(
                name=name,
                venue=venue,
                city=city,
                event_date=event_date,
                min_price=min_price,
                url=clean_url,
                price_is_all_in=True,
            ))
        except Exception:
            continue

    return events


def _follow_performer_page(browser, page) -> list[GametimeEvent]:
    """Follow performer links from search results to get event listings.

    Gametime search results often link to performer pages like
    /chris-lake-tickets/performers/chrlk — these pages list individual events.
    """
    events = []

    performer_links = page.query_selector_all("a[href*='/performers/']")
    if not performer_links:
        return []

    # Follow the first performer link
    href = performer_links[0].get_attribute("href") or ""
    if not href:
        return []

    performer_url = href if href.startswith("http") else f"https://gametime.co{href}"

    try:
        perf_page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        try:
            perf_page.goto(performer_url, wait_until="domcontentloaded", timeout=20000)

            try:
                perf_page.wait_for_selector(
                    "a[href*='/tickets/'], a[href*='/events/']",
                    timeout=15000,
                )
            except PwTimeout:
                print("  [Gametime] Performer page did not load events")
                return []

            # Try embedded JSON first
            events = _extract_next_data(perf_page)
            if not events:
                events = _extract_json_ld(perf_page)
            if not events:
                events = _extract_from_dom(perf_page)

        finally:
            perf_page.close()
    except Exception as e:
        print(f"  [Gametime] Failed to follow performer page: {e}")

    return events


def _fetch_event_price(page, event_url: str) -> Optional[float]:
    """Navigate to a Gametime event page and extract the lowest price.

    Returns the price as a float, or None if extraction fails.
    Gametime prices are all-in by default.
    """
    try:
        page.goto(event_url, wait_until="domcontentloaded", timeout=15000)

        # Wait for pricing to render
        try:
            page.wait_for_selector(
                "[class*='price'], [data-testid*='price']",
                timeout=10000,
            )
        except PwTimeout:
            pass

        # Try JSON-LD on event page
        ld_blocks = page.evaluate("""() => {
            const scripts = document.querySelectorAll('script[type="application/ld+json"]');
            return Array.from(scripts).map(s => s.textContent);
        }""")

        for block in (ld_blocks or []):
            try:
                data = json.loads(block)
                if data.get("@type") in ("MusicEvent", "Event", "Festival", "SportsEvent"):
                    offers = data.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    low = offers.get("lowPrice", offers.get("price"))
                    if low is not None:
                        return float(low)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue

        # Fallback: scan page text for price patterns
        body = page.inner_text("body")

        # Look for "from $XX" or standalone "$XX" patterns
        from_match = re.search(r'[Ff]rom\s*\$(\d+(?:,\d{3})*)', body)
        if from_match:
            return float(from_match.group(1).replace(",", ""))

        # Find all dollar amounts and return the lowest
        all_prices = re.findall(r'\$(\d+(?:,\d{3})*(?:\.\d{2})?)', body)
        if all_prices:
            prices = [float(p.replace(",", "")) for p in all_prices]
            # Filter out obviously wrong prices (< $1 or > $50000)
            prices = [p for p in prices if 1 <= p <= 50000]
            if prices:
                return min(prices)

    except Exception as e:
        print(f"  [Gametime] Failed to fetch event page price: {e}")

    return None
