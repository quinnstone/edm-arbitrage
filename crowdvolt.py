"""Scrape CrowdVolt for event listings and bid/ask order book data."""

import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse, urljoin
from pathlib import Path
from typing import Optional

import requests
from curl_cffi import requests as cf_requests
from curl_cffi.requests.exceptions import RequestException as _CfRequestException
from dateutil import parser as dateparser

import config

# CrowdVolt moved behind a Cloudflare JS challenge (~2026-08-19). Desktop
# Chrome/Firefox TLS fingerprints get a "Just a moment..." interstitial;
# mobile Safari passes clean. curl_cffi's TLS impersonation bypasses the
# challenge without needing a real browser. If safari17_2_ios eventually
# gets blocked too, rotate to the next iOS Safari profile — do NOT fall
# back silently, because we get workflow-failure emails and want to keep
# that signal loud.
CV_IMPERSONATE = "safari17_2_ios"

PREMIUM_KEYWORDS = {"vip", "platinum", "backstage", "meet & greet", "meet and greet"}

# NOTE: Do NOT set a User-Agent header on cf_requests calls to CV — curl_cffi
# generates a consistent Safari iOS UA from the impersonate profile, and
# overriding it with a desktop Chrome UA causes a TLS/UA mismatch that
# Cloudflare's bot check catches. Kept for any non-CV usage only.
HEADERS: dict = {}

# CrowdVolt's client-side book API — the same endpoint the page calls
# after hydration. Required by the gate: a magic header value sourced
# from the bundled JS. If CrowdVolt rotates this constant, calls will
# 503 and we'll fall back to the in-page tt_data summary (top-of-book
# only, no depth).
BOOK_API_URL = "https://what.crowdvolt.com/api/book/get"
BOOK_API_HEADERS = {
    "Referer": "https://www.crowdvolt.com/",
    "Content-Type": "application/json",
    "x-pokedex": "0376",
}


@dataclass
class Listing:
    user: str
    price: float
    all_in_price: float
    qty: int
    ticket_type: str
    ticket_type_id: str = ""
    price_verified: bool = True
    listing_id: str = ""
    seller_id: str = ""


@dataclass
class CrowdVoltEvent:
    slug: str
    name: str
    venue: str
    city: str
    event_date: Optional[datetime] = None
    ticket_platform: str = ""  # e.g. "DICE", "AXS", "Ticketmaster"
    ticket_types: list[dict] = field(default_factory=list)
    bids: list[Listing] = field(default_factory=list)  # buy side
    asks: list[Listing] = field(default_factory=list)  # sell side
    min_ask: Optional[float] = None
    max_bid: Optional[float] = None
    url: str = ""
    book_source: str = ""  # embedded | api | summary | "" (no book)
    comparison_status: dict = field(default_factory=dict)


def fetch_sitemap() -> list[str]:
    """Read and deduplicate event URLs, including child sitemap indexes."""
    pending = [f"{config.CROWDVOLT_BASE_URL}/sitemap.xml"]
    visited, slugs = set(), set()
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    while pending:
        url = pending.pop()
        if url in visited:
            continue
        visited.add(url)
        resp = cf_requests.get(url, timeout=config.REQUEST_TIMEOUT,
                               headers=HEADERS, impersonate=CV_IMPERSONATE)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        for loc in root.findall(".//sm:loc", ns):
            target = urljoin(url, loc.text or "")
            parsed = urlparse(target)
            if parsed.hostname != urlparse(config.CROWDVOLT_BASE_URL).hostname:
                continue
            if root.tag.endswith("sitemapindex"):
                pending.append(target)
            elif parsed.path.startswith("/event/"):
                slugs.add(parsed.path[len("/event/"):].strip("/"))
    return sorted(slugs)


def discover_events() -> tuple[list[str], dict]:
    """Union sitemaps, homepage links, and each city's paginated public catalog.

    City IDs come from the homepage's allFeaturedEvents map. Omitting the
    area parameter defaults to NYC; it is not a global catalog request.
    """
    slugs = set()
    report = {"sources": {}, "errors": []}
    try:
        slugs.update(fetch_sitemap())
        report["sources"]["sitemap"] = len(slugs)
    except Exception as exc:
        report["errors"].append(f"sitemap: {exc}")
    areas = []
    try:
        resp = cf_requests.get(config.CROWDVOLT_BASE_URL,
                               timeout=config.REQUEST_TIMEOUT, impersonate=CV_IMPERSONATE)
        resp.raise_for_status()
        home = set(re.findall(r'/event/([a-zA-Z0-9_-]+)', resp.text))
        slugs.update(home)
        report["sources"]["homepage"] = len(home)
        featured = _object_field(resp.text, "allFeaturedEvents")
        if not featured:
            raise ValueError("homepage has no city catalog map")
        areas = list(featured)
    except Exception as exc:
        report["errors"].append(f"homepage: {exc}")
    catalog = set()
    report["sources"]["catalog_complete"] = bool(areas)
    report["sources"]["catalog_areas"] = {}
    for area in areas:
        city_slugs = set()
        try:
            for offset in range(0, 20000, 20):
                resp = cf_requests.get(
                    "https://what.crowdvolt.com/api/event/list",
                    params={"limit": 20, "offset": offset, "sort_by": "dateAsc", "area_uqid": area},
                    headers=BOOK_API_HEADERS, timeout=config.REQUEST_TIMEOUT,
                    impersonate=CV_IMPERSONATE)
                resp.raise_for_status()
                rows = resp.json().get("events")
                if not isinstance(rows, list):
                    raise ValueError("catalog response has no events array")
                if not rows:
                    break
                page = {r["uqid"] for r in rows if isinstance(r, dict)
                        and isinstance(r.get("uqid"), str) and r["uqid"]}
                if not page or not page - city_slugs:
                    raise ValueError("catalog pagination stalled")
                city_slugs.update(page)
            else:
                raise ValueError("catalog pagination safety limit reached")
        except Exception as exc:
            report["errors"].append(f"catalog {area}: {exc}")
            report["sources"]["catalog_complete"] = False
        catalog.update(city_slugs)
        report["sources"]["catalog_areas"][area] = len(city_slugs)
    slugs.update(catalog)
    report["sources"]["catalog"] = len(catalog)
    if not slugs:
        raise RuntimeError(f"CrowdVolt discovery failed: {report['errors']}")
    for error in report["errors"]:
        print(f"[CrowdVolt] DEGRADED discovery: {error}")
    return sorted(slugs), report


def _data_text(html: str) -> str:
    """Decode Next Flight string literals with JSON, preserving Unicode."""
    parts = []
    for match in re.finditer(r'self\.__next_f\.push\(\[\d+,', html):
        try:
            value, _ = json.JSONDecoder().raw_decode(html[match.end():])
            if isinstance(value, str):
                parts.append(value)
        except ValueError:
            pass
    return html + "\n" + "".join(parts)


def _object_field(html: str, field_name: str):
    text = _data_text(html)
    for match in re.finditer(r'"' + re.escape(field_name) + r'"\s*:\s*', text):
        try:
            value, _ = json.JSONDecoder().raw_decode(text[match.end():])
            if isinstance(value, dict):
                return value
        except ValueError:
            continue
    return None


def _extract_book_json(html: str) -> Optional[dict]:
    """Extract the initialBook buy/sell data from the page HTML.

    CrowdVolt uses Next.js with server components. The marketplace data is
    embedded in a JS string with escaped quotes. We find the initialBook
    marker, unescape the surrounding region, and parse the JSON.
    """
    return _object_field(html, "initialBook")


def _fetch_book_api(event_uqid: str, retries: int = 2) -> Optional[dict]:
    """Fetch the full order book directly from CrowdVolt's client API.

    Same JSON shape as the legacy page-embedded initialBook
    ({"buy": [...], "sell": [...]}). Returns None on any failure so
    the caller can fall through to the summary parser.
    """
    for attempt in range(1 + retries):
        try:
            resp = cf_requests.get(
                BOOK_API_URL,
                params={"event_uqid": event_uqid},
                headers=BOOK_API_HEADERS,
                timeout=config.REQUEST_TIMEOUT,
                impersonate=CV_IMPERSONATE,
            )
            if resp.status_code == 200:
                payload = resp.json()
                if isinstance(payload, dict) and all(isinstance(payload.get(k), list) for k in ("buy", "sell")):
                    return payload
                return None
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            return None
        except (requests.RequestException, _CfRequestException, ValueError):
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            return None
    return None


def _extract_summary_book(html: str) -> Optional[dict]:
    """Fallback for pages that ship "initialBook":null (the full book
    hydrates client-side and never appears in the HTML). The server still
    embeds a per-ticket-type top-of-book summary in tt_data, plus
    event-level max_bid / max_bid_all_in. Depth is lost — only the best
    bid/ask per type is visible.
    """
    unescaped = _data_text(html)
    tt = _object_field(html, "tt_data")
    if tt is None:
        return None

    summary = {"types": tt.get("types", [])}
    for field_name in ("max_bid", "max_bid_all_in"):
        m = re.search(rf'"{field_name}":([\d.]+)', unescaped)
        if m:
            summary[field_name] = float(m.group(1))
    return summary


def _listings_from_summary(summary: dict) -> tuple[list, list]:
    """Synthesize one-deep bid/ask listings per ticket type.

    Asks carry an exact all-in price (all_in_lowest_ask_price). Bids only
    expose the raw price per type, so net-to-seller all-in is derived from
    the event-level max_bid -> max_bid_all_in ratio (exact for the type
    holding the top bid, proportional for the rest).
    """
    bids, asks = [], []
    max_bid = summary.get("max_bid") or 0
    max_bid_all_in = summary.get("max_bid_all_in") or 0
    factor = (max_bid_all_in / max_bid) if max_bid and max_bid_all_in else 1.0

    for t in summary.get("types", []):
        if not isinstance(t, dict) or t.get("visible") is False:
            continue
        name = t.get("name", "")
        ask_price = t.get("lowest_ask_price")
        if ask_price:
            asks.append(Listing(
                user="(summary)",
                price=float(ask_price),
                all_in_price=float(t.get("all_in_lowest_ask_price") or ask_price),
                qty=int(t.get("lowest_ask_qty") or 0),
                ticket_type=name,
                ticket_type_id=t.get("uqid", ""),
                price_verified=bool(t.get("all_in_lowest_ask_price")),
            ))
        bid_price = t.get("highest_bid_price")
        if bid_price:
            if float(bid_price) == max_bid and max_bid_all_in:
                bid_all_in = max_bid_all_in
            else:
                bid_all_in = round(float(bid_price) * factor, 2)
            bids.append(Listing(
                user="(summary)",
                price=float(bid_price),
                all_in_price=bid_all_in,
                qty=int(t.get("highest_bid_qty") or 0),
                ticket_type=name,
                ticket_type_id=t.get("uqid", ""),
                price_verified=bool(float(bid_price) == max_bid and max_bid_all_in),
            ))
    return bids, asks


def _extract_event_metadata(html: str) -> dict:
    """Extract event-level metadata from the page HTML.

    Prefers the JSON-LD MusicEvent block for name/date, then falls back
    to the Next.js embedded data for venue/area_name/doors_open_time.
    """
    unescaped = _data_text(html)
    meta = {}

    # Try JSON-LD first — reliable for name and date
    ld_match = re.search(
        r'<script type="application/ld\+json">(.*?)</script>',
        html, re.DOTALL,
    )
    if ld_match:
        try:
            ld = json.loads(ld_match.group(1))
            if ld.get("@type") in ("MusicEvent", "Event"):
                meta["name"] = ld.get("name", "")
        except (json.JSONDecodeError, TypeError):
            pass

    # Extract venue, area_name, doors_open_time, and app_name from the
    # embedded Next.js data.  These fields are unique to the event payload
    # (unlike "name" which also appears in HTML meta tags), so first-match
    # is safe.  app_name tells us which ticketing platform issued the
    # tickets (e.g. "DICE", "AXS", "Ticketmaster").
    for field_name in ["venue", "area_name", "doors_open_time", "app_name"]:
        match = re.search(r'"' + re.escape(field_name) + r'"\s*:\s*("(?:\\.|[^"\\])*")', unescaped)
        if match:
            try:
                meta[field_name] = json.loads(match.group(1))
            except ValueError:
                pass

    # Fallback: if JSON-LD didn't give us a name, search near doors_open_time
    # where the event payload lives (avoids meta tag false positives).
    if not meta.get("name") and "doors_open_time" in meta:
        dt_idx = unescaped.find('"doors_open_time"')
        if dt_idx > 0:
            region = unescaped[max(0, dt_idx - 2000):dt_idx]
            name_match = re.search(r'"name"\s*:\s*("(?:\\.|[^"\\])*")', region)
            if name_match:
                meta["name"] = json.loads(name_match.group(1))

    return meta


def _parse_listings(items: list[dict], side: str = "") -> list[Listing]:
    """Skip malformed, reserved or blocked rows without losing the whole book."""
    from ticket_products import positive_price
    listings = []
    if not isinstance(items, list):
        return listings
    for item in items:
        if not isinstance(item, dict) or item.get("is_blocked") or item.get("is_pending"):
            continue
        try:
            price = float(item.get("price", 0))
            all_in = float(item.get("all_in_price", 0))
            qty = item.get("qty")
            if isinstance(qty, bool) or int(qty) != float(qty):
                continue
            qty = int(qty)
            if side == "sell":
                if item.get("in_cart"):
                    continue
                available = item.get("qty_open_to_cart")
                if available is not None:
                    if isinstance(available, bool) or int(available) != float(available):
                        continue
                    qty = min(qty, int(available))
            label = item.get("ticket_type")
            if not isinstance(label, str) or not label.strip() or qty <= 0 or not positive_price(all_in):
                continue
            listings.append(Listing(
                user=item.get("user_first", "Unknown"), price=price,
                all_in_price=all_in, qty=qty, ticket_type=label,
                ticket_type_id=item.get("ticket_type_uqid", ""), price_verified=True,
                listing_id=item.get("uqid", ""),
                seller_id=item.get("profile_uqid") or item.get("user_uqid", "")))
        except (ValueError, TypeError, OverflowError):
            continue
    return listings


def fetch_event(slug: str, retries: int = 2) -> Optional[CrowdVoltEvent]:
    """Fetch and parse a single CrowdVolt event page.

    Retries on transient failures (timeouts, 429s, 5xx) so rate-limiting
    doesn't silently drop events from the scan.
    """
    url = f"{config.CROWDVOLT_BASE_URL}/event/{slug}"
    for attempt in range(1 + retries):
        try:
            resp = cf_requests.get(
                url,
                timeout=config.REQUEST_TIMEOUT,
                headers=HEADERS,
                impersonate=CV_IMPERSONATE,
            )
            if resp.status_code == 200:
                break
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 * (attempt + 1))  # backoff: 2s, 4s
                continue
            return None
        except (requests.RequestException, _CfRequestException):
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            return None

    html = resp.text

    # Check for dead pages
    if "Event Not found" in html or "Event not found" in html:
        return None

    # Extract metadata
    meta = _extract_event_metadata(html)
    if not meta.get("name"):
        return None

    event = CrowdVoltEvent(
        slug=slug,
        name=meta["name"],
        venue=meta.get("venue", ""),
        city=meta.get("area_name", ""),
        ticket_platform=meta.get("app_name", ""),
        url=url,
    )

    # Parse date
    if meta.get("doors_open_time"):
        try:
            event.event_date = dateparser.parse(meta["doors_open_time"])
        except (ValueError, TypeError):
            pass

    summary = _extract_summary_book(html)
    event.ticket_types = summary.get("types", []) if summary else []

    # Extract order book — three tiers, in order of preference:
    #   1. Page-embedded initialBook (free; ~30% of pages still ship it)
    #   2. Client-side book API (full depth; defeats the null-book issue)
    #   3. tt_data top-of-book summary (last-resort if the API is broken)
    book = _extract_book_json(html)
    if book:
        event.book_source = "embedded"
    else:
        book = _fetch_book_api(slug)
        if book:
            event.book_source = "api"
    if book:
        event.bids = _parse_listings(book.get("buy", []), side="buy")
        event.asks = _parse_listings(book.get("sell", []), side="sell")
    if book is None:
        summary = _extract_summary_book(html)
        if summary:
            event.bids, event.asks = _listings_from_summary(summary)
            event.book_source = "summary"

    # Compute summary prices excluding premium tiers so we don't
    # compare VIP bids against GA asks from external sources.
    from ticket_products import is_admission
    ga_asks = [a for a in event.asks if is_admission(a.ticket_type)
               and a.all_in_price > 0
               and not any(k in a.ticket_type.lower() for k in PREMIUM_KEYWORDS)]
    ga_bids = [b for b in event.bids if is_admission(b.ticket_type)
               and b.all_in_price > 0
               and not any(k in b.ticket_type.lower() for k in PREMIUM_KEYWORDS)]
    if ga_asks:
        event.min_ask = min(a.all_in_price for a in ga_asks)
    if ga_bids:
        event.max_bid = max(b.all_in_price for b in ga_bids)

    return event


def fetch_all_events() -> list[CrowdVoltEvent]:
    """Fetch all active CrowdVolt events with marketplace data.

    Uses a thread pool to fetch pages concurrently. ThreadPoolExecutor
    caps concurrency on its own — a separate semaphore at the same
    bound would be redundant.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    print("[CrowdVolt] Discovering sitemap and public catalog...")
    slugs, coverage = discover_events()
    coverage["events"] = {}
    print(f"[CrowdVolt] Discovered {len(slugs)} unique event URLs")

    events = []
    lock = threading.Lock()

    failed_slugs = []
    failed_lock = threading.Lock()

    def _fetch_one(slug: str, index: int):
        event = fetch_event(slug)
        time.sleep(0.15)  # small per-worker delay so we don't hammer CV
        return index, slug, event

    # 20 workers — CV scrape was 65% of scan time at 10 workers (~5.5 min
    # on a 250-event book). Doubling cuts that to ~3 min and brings the
    # total scan well under the 15-min cron interval, so GHA's "previous
    # run still active" skip-condition no longer fires. fetch_event has
    # 429/5xx retry with backoff, so any rate-limit pushback degrades
    # gracefully instead of failing.
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {
            pool.submit(_fetch_one, slug, i): slug
            for i, slug in enumerate(slugs)
        }

        done_count = 0
        for future in as_completed(futures):
            done_count += 1
            slug = futures[future]
            try:
                index, slug, event = future.result()
            except Exception as exc:
                event = None
                coverage["events"][slug] = {"status": "parse_or_fetch_error", "error": str(exc)}
            coverage["events"].setdefault(slug, {
                "status": ("active" if event.bids or event.asks else "empty") if event else "fetch_failed",
                "book_source": event.book_source if event else "",
            })
            if event:
                has_market = len(event.asks) > 0 or len(event.bids) > 0
                if has_market:
                    with lock:
                        events.append(event)
                    platform_tag = f" [{event.ticket_platform}]" if event.ticket_platform else ""
                    print(f"  [{done_count}/{len(slugs)}] {event.name}{platform_tag} — active")
            else:
                # Track failed fetches (not just empty events) for diagnostics
                with failed_lock:
                    failed_slugs.append(slug)
            # Only log every 50th skip to reduce noise
            if not event and done_count % 50 == 0:
                print(f"  [{done_count}/{len(slugs)}] scanning...")

    if failed_slugs:
        print(f"[CrowdVolt] {len(failed_slugs)} pages failed to fetch or had no data")

    src_counts = {"embedded": 0, "api": 0, "summary": 0}
    for ev in events:
        if ev.book_source in src_counts:
            src_counts[ev.book_source] += 1
    print(f"[CrowdVolt] {len(events)} events with active listings "
          f"(book source — embedded: {src_counts['embedded']}, "
          f"api: {src_counts['api']}, summary: {src_counts['summary']})")
    if src_counts["summary"] and not src_counts["api"]:
        print("[CrowdVolt] WARNING: API never succeeded — x-pokedex may have "
              "rotated. Spec digest depth is degraded.")
    path = Path(__file__).parent / "data" / "catalog_coverage.json"
    path.parent.mkdir(exist_ok=True)
    coverage["timestamp"] = datetime.now().isoformat()
    path.write_text(json.dumps(coverage, indent=2))
    return events
