"""VividSeats scraper using Playwright for resale ticket pricing.

VividSeats is behind aggressive bot protection that blocks simple HTTP
requests. Playwright with a real browser bypasses this to extract pricing.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from dateutil import parser as dateparser
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

import config


@dataclass
class VividSeatsEvent:
    name: str
    venue: str
    city: str
    event_date: Optional[datetime]
    min_price: Optional[float]
    url: str
    price_is_all_in: bool = False  # True when min_price includes fees


def search_events(query: str, date_str: Optional[str] = None) -> list[VividSeatsEvent]:
    """Search VividSeats for events matching a query using Playwright.

    Args:
        query: Artist or event name to search for.
        date_str: Optional date string (YYYY-MM-DD) to filter results.
    """
    events = []
    url = f"https://www.vividseats.com/search?searchTerm={query.replace(' ', '+')}"

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

                # VividSeats serves a JS challenge page first (~5s) that
                # auto-solves and reloads. Wait for the real page to appear.
                try:
                    page.wait_for_selector(
                        "#__NEXT_DATA__, [data-testid='productions-list'] a, a[href*='/tickets/']",
                        timeout=15000,
                    )
                except PwTimeout:
                    print("  [VividSeats] Page did not load past challenge")
                    return []

                # Try __NEXT_DATA__ first (most reliable), fall back to DOM
                events = _extract_next_data(page)
                if not events:
                    events = _extract_from_search(page)
            finally:
                browser.close()
    except Exception as e:
        print(f"  [VividSeats] Playwright error: {e}")
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


def _extract_next_data(page) -> list[VividSeatsEvent]:
    """Extract event data from Next.js __NEXT_DATA__ JSON blob."""
    events = []
    try:
        import json
        json_text = page.evaluate("""() => {
            const el = document.querySelector('#__NEXT_DATA__');
            return el ? el.textContent : null;
        }""")
        if not json_text:
            return []

        data = json.loads(json_text)

        # Recursively search for event/production objects with pricing
        def _search(obj, depth=0):
            if depth > 12 or not obj:
                return
            if isinstance(obj, dict):
                # VividSeats uses "productions" or "events" arrays
                has_name = "name" in obj or "title" in obj
                has_price = any(k in obj for k in [
                    "minAipPrice", "minPrice", "minListPrice",
                    "price", "lowPrice",
                ])
                if has_name and has_price:
                    name = obj.get("name", obj.get("title", ""))
                    # Prefer minAipPrice (all-in with fees) over base price
                    aip = obj.get("minAipPrice")
                    base = obj.get("minPrice", obj.get("minListPrice",
                           obj.get("price", obj.get("lowPrice"))))
                    price = aip or base
                    is_all_in = aip is not None
                    if name and price:
                        venue_name = ""
                        city_name = ""
                        venue_obj = obj.get("venue", {})
                        if isinstance(venue_obj, dict):
                            venue_name = venue_obj.get("name", "")
                            city_name = venue_obj.get("city", "")

                        event_date = None
                        date_val = obj.get("date", obj.get("eventDate",
                                   obj.get("startDate", "")))
                        if date_val:
                            try:
                                event_date = dateparser.parse(str(date_val))
                            except (ValueError, TypeError):
                                pass

                        event_url = obj.get("url", obj.get("webPath", ""))
                        if event_url and not event_url.startswith("http"):
                            event_url = f"https://www.vividseats.com{event_url}"

                        events.append(VividSeatsEvent(
                            name=str(name),
                            venue=venue_name,
                            city=city_name,
                            event_date=event_date,
                            min_price=float(price),
                            url=event_url,
                            price_is_all_in=is_all_in,
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


def _extract_from_search(page) -> list[VividSeatsEvent]:
    """Extract event data from VividSeats search results."""
    events = []

    # VividSeats search results contain event cards with links to ticket pages
    cards = page.query_selector_all("a[href*='/tickets/'], a[href*='/performer/']")

    seen_urls = set()
    for card in cards:
        try:
            href = card.get_attribute("href") or ""
            if not href:
                continue

            full_url = href if href.startswith("http") else f"https://www.vividseats.com{href}"

            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)

            text = card.inner_text().strip()
            if not text:
                continue

            lines = [l.strip() for l in text.split("\n") if l.strip()]
            if len(lines) < 2:
                continue

            name = lines[0]
            venue = ""
            city = ""
            event_date = None
            min_price = None

            for line in lines[1:]:
                # Price — VividSeats shows "From $XX"
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

                # Venue/city
                if not venue and not any(c in line.lower() for c in ["from", "ticket", "$", "buy"]):
                    if "," in line:
                        city = line
                    else:
                        venue = line

            if name and min_price is not None:
                events.append(VividSeatsEvent(
                    name=name,
                    venue=venue,
                    city=city,
                    event_date=event_date,
                    min_price=min_price,
                    url=full_url,
                ))
        except Exception:
            continue

    # Fallback: try JSON-LD if DOM extraction failed
    if not events:
        events = _extract_json_ld(page)

    return events


def _extract_json_ld(page) -> list[VividSeatsEvent]:
    """Extract event data from JSON-LD schema.org blocks."""
    events = []

    try:
        ld_blocks = page.evaluate("""() => {
            const scripts = document.querySelectorAll('script[type="application/ld+json"]');
            return Array.from(scripts).map(s => s.textContent);
        }""")

        import json
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

                # Handle nested events (e.g., performer with event list)
                for nested in item.get("event", item.get("subEvent", [])):
                    if isinstance(nested, dict):
                        event = _parse_ld_event(nested)
                        if event:
                            events.append(event)
    except Exception:
        pass

    return events


def _parse_ld_event(data: dict) -> Optional[VividSeatsEvent]:
    """Parse a JSON-LD event object."""
    if data.get("@type") not in ("MusicEvent", "Event", "Festival"):
        return None

    offers = data.get("offers", {})
    low_price = offers.get("lowPrice", offers.get("price"))
    if low_price is None:
        return None

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

    return VividSeatsEvent(
        name=data.get("name", ""),
        venue=venue,
        city=city,
        event_date=event_date,
        min_price=float(low_price),
        url=data.get("url", ""),
    )
