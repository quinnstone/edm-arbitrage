"""Ticket Radar — detect ticket-acquisition opportunities for high-bid CV events.

Replaces the old promo_scanner with a state-machine + code-validation approach:

  Track 1 (availability):  poll Eventbrite/DICE event pages, persist per-event
                           state, alert on transitions (sold-out → available,
                           new tier appears, price drops > 10%).

  Track 2 (codes):         build a candidate code pool from subreddit, web,
                           Reddit, RA, Twitter, plus generic patterns
                           (artist/venue/promoter). Validate Eventbrite codes
                           via ?discount= URL. For DICE, alert with the code
                           as a candidate so the operator can manually try.

Single Discord digest per scan run. 23h per-(event, opp_type) cooldown so
the same opportunity doesn't repeat across consecutive 6h scans. Profit
floor is config.TICKET_RADAR_PROFIT_FLOOR (set to $1) — wide net.

Usage:
    python ticket_radar.py            # run once, send digest
    python ticket_radar.py --dry      # preview, no Discord
"""

import argparse
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo
from urllib.parse import quote, quote_plus

import requests
from bs4 import BeautifulSoup

import config
import crowdvolt
import matcher

# ---------------------------------------------------------------------------
# Constants & paths
# ---------------------------------------------------------------------------

TARGET_PLATFORMS = {"EVENTBRITE", "DICE"}
COOLDOWN_HOURS = 23
HORIZON_DAYS = 14                   # only scan events within this window
VALIDATION_TTL_DAYS = 7             # re-test cached code/event combos after this
STATE_TTL_DAYS = 30                 # purge events older than this from state

PROFIT_FLOOR = getattr(config, "TICKET_RADAR_PROFIT_FLOOR", 1)
SUBREDDIT = getattr(config, "TICKET_RADAR_SUBREDDIT", "avesNYC_tix")
KNOWN_CODES = getattr(config, "KNOWN_CODES", [])

DIGEST_TZ = ZoneInfo("America/New_York")

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
STATE_FILE = os.path.join(_DATA_DIR, "ticket_radar_state.json")
SENT_FILE = os.path.join(_DATA_DIR, "ticket_radar_sent.json")

# Estimated buyer-side fees per source platform (decimals).
SOURCE_FEES = {
    "EVENTBRITE": 0.05,             # ~5% buyer service fee, varies
    "DICE": 0.10,                   # DICE charges a service fee, ~10%
}

# CV seller fee — used when computing net resale on CV
CV_SELLER_FEE = 0.10                # CrowdVolt's 10% take from seller payouts

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

# ---------------------------------------------------------------------------
# Code regex — same shape as old promo_scanner but tighter
# ---------------------------------------------------------------------------

_CODE_RE = re.compile(
    r"""(?:
        \b(?:promo\s*code|discount\s*code|use\s+code|coupon|code)
        \s*[:=\s"']+
        ([A-Z][A-Z0-9_-]{2,19})
    |
        ["']([A-Z][A-Z0-9_-]{3,14})["']
        \s*(?:for|to\s+get|saves?|off|discount)
    )""",
    re.IGNORECASE | re.VERBOSE,
)

_CODE_BLACKLIST = {
    "THE", "FOR", "AND", "GET", "USE", "OFF", "CODE", "WITH", "FREE", "PROMO",
    "SALE", "HTTP", "HTTPS", "HTML", "JSON", "NULL", "THIS", "THAT", "THEY",
    "THEM", "WHEN", "WHAT", "WILL", "YOUR", "FROM", "HAVE", "BEEN", "SOME",
    "DOES", "DONT", "WERE", "HELLO", "PLEASE", "ALSO", "JUST", "LIKE", "MORE",
    "MUSIC", "SOUND", "RESPECT", "HOUSE", "BASS", "TECH", "DANCE", "PARTY",
    "NIGHT", "LIVE", "SHOW", "TOUR", "OPEN", "CLOSE", "DOOR", "DOORS",
    "FLOOR", "STAGE", "ROOM", "CLUB", "EVENT", "VENUE", "ENTRY", "COVER",
    "TICKET", "TICKETS", "PRESALE", "PRE-SALE", "EARLY", "DISCOUNT",
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Opportunity:
    event_slug: str
    event_name: str
    event_venue: str
    event_city: str
    event_date: Optional[datetime]
    platform: str                   # "Eventbrite" | "DICE"
    source_url: str

    # opp_type = "availability" | "price_drop" | "new_tier" | "code_validated" | "code_candidate"
    opp_type: str
    summary: str                    # one-line description
    detail: str                     # multi-line detail for the alert
    cost: float                     # estimated all-in cost to acquire
    cv_bid: Optional[float]         # current top CV bid
    cv_ask: Optional[float]         # current lowest CV ask
    profit: float                   # est_profit after fees
    code: Optional[str] = None
    discount_pct: Optional[float] = None


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _load_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _load_state() -> dict:
    """Schema:
    {
      "events": {
        "<slug>:<platform>": {
          "url": str, "platform": str, "last_checked": iso,
          "status": "AVAILABLE"|"SOLD_OUT"|"LIMITED"|"UNKNOWN",
          "lowest_visible_price": float|None,
          "tier_names": [str, ...],
          "history": [{ts, status, lowest_price}, ...]   # last 30 days
        }
      },
      "validations": {
        "<slug>:<code>": {
          "tested_at": iso,
          "valid": bool,
          "applied_price": float|None,
          "base_price": float|None,
          "discount_pct": float|None
        }
      }
    }
    """
    return _load_json(STATE_FILE, {"events": {}, "validations": {}})


def _save_state(state: dict) -> None:
    # Prune entries older than STATE_TTL_DAYS
    cutoff = (datetime.now() - timedelta(days=STATE_TTL_DAYS)).isoformat()
    state["events"] = {
        k: v for k, v in state.get("events", {}).items()
        if v.get("last_checked", "9999") > cutoff
    }
    val_cutoff = (datetime.now() - timedelta(days=VALIDATION_TTL_DAYS)).isoformat()
    state["validations"] = {
        k: v for k, v in state.get("validations", {}).items()
        if v.get("tested_at", "9999") > val_cutoff
    }
    _save_json(STATE_FILE, state)


def _was_recently_alerted(key: str) -> bool:
    sent = _load_json(SENT_FILE, {})
    last = sent.get(key)
    if not last:
        return False
    try:
        return datetime.now() - datetime.fromisoformat(last) < timedelta(hours=COOLDOWN_HOURS)
    except (ValueError, TypeError):
        return False


def _mark_alerted(key: str) -> None:
    sent = _load_json(SENT_FILE, {})
    sent[key] = datetime.now().isoformat()
    cutoff = (datetime.now() - timedelta(hours=COOLDOWN_HOURS * 2)).isoformat()
    sent = {k: v for k, v in sent.items() if v > cutoff}
    _save_json(SENT_FILE, sent)


# ---------------------------------------------------------------------------
# Code extraction & candidate generation
# ---------------------------------------------------------------------------

def _extract_codes(text: str) -> list[str]:
    """Extract code-like tokens from free text."""
    if not text:
        return []
    found = set()
    for match in _CODE_RE.finditer(text):
        token = (match.group(1) or match.group(2) or "").upper()
        if token and token not in _CODE_BLACKLIST:
            found.add(token)
    return sorted(found)


def _slugify_for_code(s: str) -> Optional[str]:
    """Convert a name to a code-shaped string (uppercase alphanumeric)."""
    if not s:
        return None
    cleaned = re.sub(r"[^a-zA-Z0-9]", "", s).upper()
    if 3 <= len(cleaned) <= 20:
        return cleaned
    return None


def _extract_promoter(event_name: str) -> Optional[str]:
    """Pull a promoter name from patterns like 'Teksupport: Adriatique' or
    'Factory 93 Presents: Seth Troxler'."""
    m = re.match(r"^([A-Za-z][\w\s&.-]+?)\s+presents:?\s+", event_name, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.match(r"^([A-Za-z][\w\s&.-]+?):\s+", event_name)
    if m:
        return m.group(1).strip()
    return None


def _generate_event_candidates(event) -> list[str]:
    """Generate per-event candidate codes from event metadata."""
    candidates = []

    # Artist / event name forms
    for q in matcher.search_queries(event.name):
        c = _slugify_for_code(q)
        if c:
            candidates.append(c)

    # Promoter
    promoter = _extract_promoter(event.name)
    if promoter:
        c = _slugify_for_code(promoter)
        if c:
            candidates.append(c)
            # Also try with common suffixes
            for suffix in ("10", "20", "VIP"):
                if len(c) + len(suffix) <= 20:
                    candidates.append(c + suffix)

    # Venue
    if event.venue:
        c = _slugify_for_code(event.venue)
        if c:
            candidates.append(c)

    # Generic high-frequency codes (these often work)
    candidates.extend([
        "EARLY", "EARLYBIRD", "INSIDER", "GUEST", "PRESALE",
        "FRIENDS", "PROMO10", "VIP", "DANCE",
    ])

    # Dedupe preserving order
    seen = set()
    unique = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


# ---------------------------------------------------------------------------
# External code-mention scrapes (subreddit + Reddit + web)
# ---------------------------------------------------------------------------

def _fetch_subreddit_recent(subreddit: str, limit: int = 100) -> list[dict]:
    """Pull recent posts from a subreddit and extract any code-shaped tokens.
    Returns [{title, body, url, codes: [...]}].

    Uses Reddit's public JSON endpoint (no auth required).
    """
    url = f"https://www.reddit.com/r/{subreddit}/new.json?limit={limit}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  [Subreddit] {subreddit}: fetch failed: {e}")
        return []

    out = []
    for child in data.get("data", {}).get("children", []):
        post = child.get("data", {})
        title = post.get("title", "") or ""
        body = post.get("selftext", "") or ""
        permalink = post.get("permalink", "")
        post_url = f"https://reddit.com{permalink}" if permalink else ""
        codes = _extract_codes(f"{title}\n{body}")
        if codes or any(kw in (title + body).lower()
                        for kw in ("promo", "discount", "code", "% off")):
            out.append({"title": title, "body": body, "url": post_url, "codes": codes})
    return out


def _scrape_web_for_event(event_name: str, platform: str) -> list[dict]:
    """Use DuckDuckGo HTML to find code mentions for a specific event."""
    query = f'"{event_name}" "promo code" {platform}'
    url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [Web] {event_name}: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    for result in soup.select("div.result")[:8]:
        title = (result.select_one("a.result__a") or {}).get_text("", strip=True) \
            if result.select_one("a.result__a") else ""
        snippet = (result.select_one(".result__snippet") or {}).get_text("", strip=True) \
            if result.select_one(".result__snippet") else ""
        link = result.select_one("a.result__a")
        href = link.get("href", "") if link else ""
        codes = _extract_codes(f"{title} {snippet}")
        if codes:
            out.append({"title": title, "body": snippet, "url": href, "codes": codes})
    return out


# ---------------------------------------------------------------------------
# Eventbrite — availability + code validation
# ---------------------------------------------------------------------------

def _verify_platform_url(url: str, event) -> bool:
    """Confirm a candidate platform URL actually corresponds to the CV event
    by checking name and date match."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException:
        return False

    soup = BeautifulSoup(resp.text, "html.parser")

    found_name = None
    found_date = None
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, list):
            data = next(
                (d for d in data if isinstance(d, dict)
                 and d.get("@type") in ("Event", "MusicEvent")),
                data[0] if data else {},
            )
        if isinstance(data, dict) and data.get("@type") in ("Event", "MusicEvent"):
            found_name = data.get("name", "")
            start = data.get("startDate", "")
            if start:
                try:
                    from dateutil import parser as dp
                    found_date = dp.parse(start)
                except (ValueError, TypeError):
                    pass
            break

    if not found_name:
        return False

    score = matcher._name_similarity(event.name, found_name)
    if score < 70:
        return False

    if event.event_date and found_date:
        cv_local = matcher._localize_cv_date(event)
        if not matcher._dates_match(cv_local, found_date):
            return False

    return True


def _find_platform_url(event, domain: str) -> Optional[str]:
    """Search for an event URL on a specific platform domain and verify the
    first match. Returns the verified URL or None."""
    parts = [event.name]
    if event.venue:
        parts.append(event.venue)
    if event.city:
        parts.append(event.city)
    query = quote_plus(f'{" ".join(parts)} site:{domain}')
    ddg_url = f"https://duckduckgo.com/html/?q={query}"

    try:
        resp = requests.get(ddg_url, headers=HEADERS, timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    seen = set()
    for link in soup.select('a.result__a')[:8]:
        href = link.get("href", "") or ""
        # DDG sometimes returns redirector URLs; extract the real one
        m = re.search(rf'https?://[^"\s]*{re.escape(domain)}[^"\s\\]*', href)
        if not m:
            continue
        url = m.group(0).split("&")[0].split("?")[0]
        if url in seen:
            continue
        seen.add(url)

        if _verify_platform_url(url, event):
            return url
        time.sleep(1)  # rate-limit between verification fetches

    return None


def _eventbrite_native_search(event) -> Optional[str]:
    """Search Eventbrite's own discovery endpoint, more reliable than DDG."""
    city_slug = re.sub(r"[^a-z0-9]+", "-",
                       (event.city or "online").lower()).strip("-") or "online"
    name_slug = re.sub(r"[^a-z0-9]+", "-", event.name.lower()).strip("-")
    if not name_slug:
        return None
    search_url = f"https://www.eventbrite.com/d/{city_slug}/events--{name_slug}/"

    try:
        resp = requests.get(search_url, headers=HEADERS, timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    seen = set()
    for link in soup.select('a[href*="eventbrite.com/e/"]')[:6]:
        href = (link.get("href") or "").split("?")[0]
        if href in seen or "/e/" not in href:
            continue
        seen.add(href)
        if _verify_platform_url(href, event):
            return href
        time.sleep(1)
    return None


def _fetch_eventbrite_url_for_event(event) -> Optional[str]:
    """Find a verified Eventbrite event URL — try direct search first, DDG fallback."""
    return _eventbrite_native_search(event) or _find_platform_url(event, "eventbrite.com")


def _parse_eventbrite_state(html: str) -> dict:
    """Extract availability + tiers from an Eventbrite event page HTML.

    Returns {status, lowest_visible_price, tier_names, raw_offers}.
    """
    state = {"status": "UNKNOWN", "lowest_visible_price": None,
             "tier_names": [], "raw_offers": []}

    soup = BeautifulSoup(html, "html.parser")

    # Eventbrite ships JSON-LD with offers — parse first.
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, list):
            data = next((d for d in data if d.get("@type") == "Event"), data[0] if data else {})
        if not isinstance(data, dict) or data.get("@type") not in ("Event", "MusicEvent"):
            continue

        offers = data.get("offers") or []
        if isinstance(offers, dict):
            offers = [offers]

        prices_in_stock = []
        statuses = set()
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            avail = (offer.get("availability") or "").split("/")[-1]
            statuses.add(avail)
            name = offer.get("name") or "Tier"
            price_raw = offer.get("price") or offer.get("lowPrice")
            try:
                price = float(price_raw) if price_raw is not None else None
            except (TypeError, ValueError):
                price = None
            state["tier_names"].append(name)
            state["raw_offers"].append({"name": name, "price": price, "avail": avail})
            if avail in ("InStock", "LimitedAvailability") and price is not None:
                prices_in_stock.append(price)

        # Roll up status
        if "InStock" in statuses or "LimitedAvailability" in statuses:
            state["status"] = "AVAILABLE" if "InStock" in statuses else "LIMITED"
        elif "SoldOut" in statuses or "Discontinued" in statuses:
            state["status"] = "SOLD_OUT"
        if prices_in_stock:
            state["lowest_visible_price"] = round(min(prices_in_stock), 2)
        break  # first valid Event JSON-LD wins

    # Fallback: detect "Sold Out" text in HTML when JSON-LD didn't carry it
    if state["status"] == "UNKNOWN":
        text = soup.get_text(" ", strip=True).lower()
        if "sold out" in text:
            state["status"] = "SOLD_OUT"
        elif "register" in text or "get tickets" in text:
            state["status"] = "AVAILABLE"

    return state


def _fetch_eventbrite(url: str, code: Optional[str] = None) -> Optional[dict]:
    """Fetch an Eventbrite event page and return parsed state, optionally with
    a discount code applied via ?discount= URL parameter."""
    if code:
        # Build URL with discount parameter
        sep = "&" if "?" in url else "?"
        full_url = f"{url}{sep}discount={quote(code)}"
    else:
        full_url = url

    try:
        resp = requests.get(full_url, headers=HEADERS, timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [Eventbrite] {url}: {e}")
        return None
    return _parse_eventbrite_state(resp.text)


def _validate_eventbrite_code(event_url: str, code: str, base_price: Optional[float]) -> dict:
    """Test if a code applies on Eventbrite. Returns:
    {valid: bool, applied_price: float|None, discount_pct: float|None}.

    A code is "valid" when the discount-URL request returns a lower price
    than the base.
    """
    coded_state = _fetch_eventbrite(event_url, code=code)
    if not coded_state or coded_state.get("lowest_visible_price") is None:
        return {"valid": False, "applied_price": None, "discount_pct": None}

    applied = coded_state["lowest_visible_price"]
    if base_price is None or applied >= base_price:
        return {"valid": False, "applied_price": applied, "discount_pct": None}

    discount_pct = round((1 - applied / base_price) * 100, 1)
    return {"valid": True, "applied_price": applied, "discount_pct": discount_pct}


# ---------------------------------------------------------------------------
# DICE — availability only
# ---------------------------------------------------------------------------

def _fetch_dice_url_for_event(event) -> Optional[str]:
    """Find a verified DICE event URL for a CV event."""
    return _find_platform_url(event, "dice.fm")


def _fetch_dice(url: str) -> Optional[dict]:
    """Get availability state from a DICE event page."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [DICE] {url}: {e}")
        return None

    state = {"status": "UNKNOWN", "lowest_visible_price": None,
             "tier_names": [], "has_code_input": False}

    # DICE embeds Next.js data in __NEXT_DATA__ script
    soup = BeautifulSoup(resp.text, "html.parser")
    script = soup.select_one('script#__NEXT_DATA__')
    if script and script.string:
        try:
            data = json.loads(script.string)
            event = data.get("props", {}).get("pageProps", {}).get("event", {})
            tickets = event.get("tickets") or event.get("ticket_types") or []
            if isinstance(tickets, list):
                prices_in_stock = []
                statuses = set()
                for t in tickets:
                    if not isinstance(t, dict):
                        continue
                    name = t.get("name", "Tier")
                    price_cents = t.get("price")
                    price = (price_cents / 100) if isinstance(price_cents, (int, float)) else None
                    avail = (t.get("status") or "").lower() or ("sold_out" if t.get("sold_out") else "available")
                    state["tier_names"].append(name)
                    statuses.add(avail)
                    if "sold" not in avail and price is not None:
                        prices_in_stock.append(price)
                if any("sold" not in s for s in statuses):
                    state["status"] = "AVAILABLE"
                elif statuses:
                    state["status"] = "SOLD_OUT"
                if prices_in_stock:
                    state["lowest_visible_price"] = round(min(prices_in_stock), 2)
        except (json.JSONDecodeError, TypeError):
            pass

    # Fallback text scan
    if state["status"] == "UNKNOWN":
        text = soup.get_text(" ", strip=True).lower()
        if "sold out" in text or "soldout" in text:
            state["status"] = "SOLD_OUT"
        elif "buy" in text or "tickets" in text:
            state["status"] = "AVAILABLE"

    # Detect access-code input on the page
    text_lower = resp.text.lower()
    if "access code" in text_lower or "promo code" in text_lower or "passcode" in text_lower:
        state["has_code_input"] = True

    return state


# ---------------------------------------------------------------------------
# Profit calculation
# ---------------------------------------------------------------------------

def _profit_against_cv(cost: float, cv_bid: Optional[float]) -> float:
    """Net profit if we acquire at `cost` and sell on CV at the top bid."""
    if cv_bid is None or cost is None:
        return 0.0
    payout = cv_bid * (1 - CV_SELLER_FEE)
    return round(payout - cost, 2)


def _all_in_cost(base_price: float, platform: str) -> float:
    """Estimate all-in cost on a source platform (base + buyer fees)."""
    fee_rate = SOURCE_FEES.get(platform.upper(), 0.0)
    return round(base_price * (1 + fee_rate), 2)


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

def scan(dry_run: bool = False, cv_events: Optional[list] = None) -> list[Opportunity]:
    print(f"\n{'=' * 60}")
    print(f"[Radar] Starting at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 60}")

    if cv_events is None:
        cv_events = crowdvolt.fetch_all_events()
    if not cv_events:
        print("[Radar] No CrowdVolt events; nothing to scan")
        return []

    today = datetime.now().date()
    horizon = datetime.now() + timedelta(days=HORIZON_DAYS)
    eligible = [
        e for e in cv_events
        if e.max_bid is not None
        and e.ticket_platform.upper() in TARGET_PLATFORMS
        and (e.event_date is None or e.event_date <= horizon)
        and (e.event_date is None or e.event_date.date() >= today)
    ]
    print(f"[Radar] {len(eligible)} eligible events on Eventbrite/DICE within "
          f"{HORIZON_DAYS}d (out of {len(cv_events)} total)")

    state = _load_state()
    state.setdefault("events", {})
    state.setdefault("validations", {})

    # Build a global candidate code pool from the subreddit + KNOWN_CODES.
    # These are tested against every event regardless of source mention.
    print(f"[Radar] Scraping r/{SUBREDDIT} for code candidates...")
    sub_results = _fetch_subreddit_recent(SUBREDDIT)
    global_codes = set(KNOWN_CODES)
    for r in sub_results:
        global_codes.update(r.get("codes") or [])
    print(f"[Radar] {len(global_codes)} global candidate codes "
          f"({len(sub_results)} subreddit posts had relevant text)")

    opportunities: list[Opportunity] = []

    for ev in eligible:
        platform = ev.ticket_platform.upper()
        print(f"\n[Radar] {ev.name} [{platform}] — bid ${ev.max_bid:.0f}")

        # -- Locate event URL on the source platform.
        # Reuse the cached URL from prior runs if we have one; otherwise
        # search-and-verify. URL caching dramatically reduces DDG queries
        # across consecutive 6h scans.
        state_key = f"{ev.slug}:{platform}"
        prior = state["events"].get(state_key, {})
        event_url = prior.get("url")

        if not event_url:
            if platform == "EVENTBRITE":
                event_url = _fetch_eventbrite_url_for_event(ev)
            elif platform == "DICE":
                event_url = _fetch_dice_url_for_event(ev)
            else:
                continue

        if not event_url:
            print(f"  no {platform} URL found (search returned no verified match)")
            continue

        # -- Track 1: availability state (state_key + prior already loaded above)
        prior_status = prior.get("status")
        prior_price = prior.get("lowest_visible_price")
        prior_tiers = set(prior.get("tier_names", []))

        if platform == "EVENTBRITE":
            current = _fetch_eventbrite(event_url)
        else:
            current = _fetch_dice(event_url)

        if not current:
            print(f"  could not fetch {platform} state")
            continue

        new_status = current.get("status")
        new_price = current.get("lowest_visible_price")
        new_tiers = set(current.get("tier_names") or [])
        print(f"  state: status={new_status} price={new_price} tiers={len(new_tiers)}")

        # Update state record
        state["events"][state_key] = {
            "url": event_url,
            "platform": platform,
            "last_checked": datetime.now().isoformat(),
            "status": new_status,
            "lowest_visible_price": new_price,
            "tier_names": list(new_tiers),
            "history": (prior.get("history") or [])[-30:] + [{
                "ts": datetime.now().isoformat(),
                "status": new_status,
                "lowest_price": new_price,
            }],
        }

        # -- Detect transitions
        # 1. SOLD_OUT → AVAILABLE
        if prior_status == "SOLD_OUT" and new_status in ("AVAILABLE", "LIMITED"):
            cost = _all_in_cost(new_price, platform) if new_price else 0
            profit = _profit_against_cv(cost, ev.max_bid)
            if profit >= PROFIT_FLOOR:
                opportunities.append(Opportunity(
                    event_slug=ev.slug, event_name=ev.name,
                    event_venue=ev.venue or "", event_city=ev.city or "",
                    event_date=ev.event_date, platform=platform.title(),
                    source_url=event_url,
                    opp_type="availability",
                    summary=f"Sold-out → available @ ${new_price:.0f}",
                    detail=(f"Was sold out; tickets back in stock at "
                            f"${new_price:.0f} ({platform.title()}). "
                            f"Top CV bid: ${ev.max_bid:.0f}."),
                    cost=cost, cv_bid=ev.max_bid, cv_ask=ev.min_ask, profit=profit,
                ))

        # 2. New tier appeared
        added_tiers = new_tiers - prior_tiers
        if added_tiers and prior_tiers:  # only flag if we had prior data
            cost = _all_in_cost(new_price, platform) if new_price else 0
            profit = _profit_against_cv(cost, ev.max_bid)
            if profit >= PROFIT_FLOOR:
                opportunities.append(Opportunity(
                    event_slug=ev.slug, event_name=ev.name,
                    event_venue=ev.venue or "", event_city=ev.city or "",
                    event_date=ev.event_date, platform=platform.title(),
                    source_url=event_url,
                    opp_type="new_tier",
                    summary=f"New tier(s): {', '.join(list(added_tiers)[:3])}",
                    detail=(f"New ticket tiers released: "
                            f"{', '.join(added_tiers)}. Lowest currently "
                            f"${new_price:.0f}. Top CV bid: ${ev.max_bid:.0f}."),
                    cost=cost, cv_bid=ev.max_bid, cv_ask=ev.min_ask, profit=profit,
                ))

        # 3. Price drop > 10% on visible tiers
        if (prior_price is not None and new_price is not None
                and new_price < prior_price * 0.9):
            cost = _all_in_cost(new_price, platform)
            profit = _profit_against_cv(cost, ev.max_bid)
            if profit >= PROFIT_FLOOR:
                drop_pct = (1 - new_price / prior_price) * 100
                opportunities.append(Opportunity(
                    event_slug=ev.slug, event_name=ev.name,
                    event_venue=ev.venue or "", event_city=ev.city or "",
                    event_date=ev.event_date, platform=platform.title(),
                    source_url=event_url,
                    opp_type="price_drop",
                    summary=f"Price dropped {drop_pct:.0f}%: ${prior_price:.0f}→${new_price:.0f}",
                    detail=(f"Lowest tier dropped from ${prior_price:.0f} to "
                            f"${new_price:.0f} ({drop_pct:.0f}% off). Top CV bid: "
                            f"${ev.max_bid:.0f}."),
                    cost=cost, cv_bid=ev.max_bid, cv_ask=ev.min_ask, profit=profit,
                ))

        # -- Track 2: code candidates + validation
        # Build per-event candidate set: per-event generic + global pool
        # + scrape-derived codes for this event
        event_specific = set(_generate_event_candidates(ev))
        web_results = _scrape_web_for_event(ev.name, platform)
        for r in web_results:
            event_specific.update(r.get("codes") or [])

        candidates = event_specific | global_codes
        # Strip blacklisted post-merge (defensive)
        candidates = {c for c in candidates if c not in _CODE_BLACKLIST and len(c) >= 3}
        print(f"  {len(candidates)} code candidates to evaluate")

        if platform == "EVENTBRITE":
            base_price = current.get("lowest_visible_price")
            for code in sorted(candidates):
                vkey = f"{ev.slug}:{code}"
                cached = state["validations"].get(vkey)
                if cached:
                    # honor TTL — if cached recently, reuse
                    try:
                        tested = datetime.fromisoformat(cached["tested_at"])
                        if datetime.now() - tested < timedelta(days=VALIDATION_TTL_DAYS):
                            result = cached
                        else:
                            result = None
                    except (KeyError, ValueError):
                        result = None
                else:
                    result = None

                if result is None:
                    result = _validate_eventbrite_code(event_url, code, base_price)
                    result["tested_at"] = datetime.now().isoformat()
                    state["validations"][vkey] = result
                    time.sleep(2)  # rate-limit between validation requests

                if result.get("valid"):
                    applied = result["applied_price"]
                    cost = _all_in_cost(applied, platform)
                    profit = _profit_against_cv(cost, ev.max_bid)
                    if profit >= PROFIT_FLOOR:
                        opportunities.append(Opportunity(
                            event_slug=ev.slug, event_name=ev.name,
                            event_venue=ev.venue or "", event_city=ev.city or "",
                            event_date=ev.event_date, platform="Eventbrite",
                            source_url=event_url,
                            opp_type="code_validated",
                            summary=f"Code {code} → ${applied:.0f} "
                                    f"({result.get('discount_pct', 0):.0f}% off)",
                            detail=(f"Validated discount code **{code}** drops "
                                    f"price from ${base_price:.0f} to ${applied:.0f}. "
                                    f"Top CV bid: ${ev.max_bid:.0f}."),
                            cost=cost, cv_bid=ev.max_bid, cv_ask=ev.min_ask, profit=profit,
                            code=code, discount_pct=result.get("discount_pct"),
                        ))
                        print(f"    ✓ {code}: validated, profit ${profit:.0f}")

        elif platform == "DICE":
            # No validation — alert with the candidate code so the operator
            # can manually try via the DICE app. Only the highest-confidence
            # candidates from public sources (not generic patterns) — too
            # noisy otherwise without validation backstop.
            from_sources = set()
            for r in web_results + sub_results:
                from_sources.update(r.get("codes") or [])
            dice_candidates = (from_sources & candidates) | set(KNOWN_CODES)
            for code in dice_candidates:
                # If DICE event currently has tickets and a code mention
                # was found, surface it. Profit math uses base price as
                # upper bound — actual profit when applied may be better.
                base_price = current.get("lowest_visible_price")
                if base_price is None:
                    continue
                cost = _all_in_cost(base_price, platform)
                profit = _profit_against_cv(cost, ev.max_bid)
                if profit >= PROFIT_FLOOR:
                    opportunities.append(Opportunity(
                        event_slug=ev.slug, event_name=ev.name,
                        event_venue=ev.venue or "", event_city=ev.city or "",
                        event_date=ev.event_date, platform="DICE",
                        source_url=event_url,
                        opp_type="code_candidate",
                        summary=f"Candidate code: {code}",
                        detail=(f"Code **{code}** found in public posts; "
                                f"DICE access codes can't be validated "
                                f"automatically — try it in the DICE app. "
                                f"Current price ${base_price:.0f}, "
                                f"top CV bid ${ev.max_bid:.0f}."),
                        cost=cost, cv_bid=ev.max_bid, cv_ask=ev.min_ask, profit=profit,
                        code=code,
                    ))

        time.sleep(1)  # politeness between events

    # -- Persist state
    _save_state(state)

    # -- Dedup against alert history
    fresh = []
    for opp in opportunities:
        key = f"{opp.event_slug}:{opp.opp_type}:{opp.code or ''}"
        if not _was_recently_alerted(key):
            fresh.append(opp)

    fresh.sort(key=lambda o: o.profit, reverse=True)
    print(f"\n[Radar] {len(opportunities)} opportunities; {len(fresh)} fresh after cooldown")

    if fresh and not dry_run:
        sent = _send_digest(fresh)
        for opp in fresh[:sent]:
            key = f"{opp.event_slug}:{opp.opp_type}:{opp.code or ''}"
            _mark_alerted(key)

    return fresh


# ---------------------------------------------------------------------------
# Discord digest
# ---------------------------------------------------------------------------

def _format_opp_embed(opp: Opportunity) -> dict:
    date_str = opp.event_date.strftime("%b %d") if opp.event_date else "TBD"
    fields = [
        {"name": "Where", "value": f"{opp.event_venue} — {opp.event_city}", "inline": True},
        {"name": "When", "value": date_str, "inline": True},
        {"name": "Platform", "value": opp.platform, "inline": True},
        {"name": "Cost / CV bid",
         "value": f"${opp.cost:.0f} / ${opp.cv_bid:.0f}" if opp.cv_bid
         else f"${opp.cost:.0f}",
         "inline": True},
        {"name": "Est. Profit",
         "value": f"**+${opp.profit:.0f}** after fees", "inline": True},
        {"name": "Links",
         "value": f"[{opp.platform}]({opp.source_url})",
         "inline": False},
    ]
    if opp.code:
        fields.insert(0, {"name": "Code", "value": f"`{opp.code}`", "inline": True})

    color = {
        "availability": 0x4CAF50,    # green
        "new_tier": 0x2196F3,        # blue
        "price_drop": 0xFFC107,      # amber
        "code_validated": 0xFFD700,  # gold
        "code_candidate": 0xFF9800,  # orange
    }.get(opp.opp_type, 0xFFD700)

    return {
        "title": f"🎫 {opp.event_name} — {opp.summary}",
        "description": opp.detail,
        "color": color,
        "fields": fields,
    }


def _send_digest(opps: list[Opportunity]) -> int:
    if not config.DISCORD_WEBHOOK_URL:
        print("[Radar] No Discord webhook configured")
        return 0
    if not opps:
        return 0

    now_nyc = datetime.now(DIGEST_TZ)
    date_str = now_nyc.strftime("%b %d %I%p").lstrip("0").replace(" 0", " ")
    top = opps[:10]
    plural = "ies" if len(opps) != 1 else "y"
    deferred = f"  _({len(opps) - 10} more deferred)_" if len(opps) > 10 else ""
    summary = (f"🎫 **Ticket Radar** — {date_str} — "
               f"{len(opps)} opportunit{plural}{deferred}")

    payload = {
        "username": "Ticket Arb",
        "content": summary,
        "embeds": [_format_opp_embed(o) for o in top],
    }
    try:
        resp = requests.post(
            config.DISCORD_WEBHOOK_URL, json=payload,
            timeout=config.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        print(f"[Radar] Digest sent — {len(top)} embeds")
        return len(top)
    except requests.RequestException as e:
        print(f"[Radar] Digest send failed: {e}")
        return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Ticket Radar — availability + code scanner")
    parser.add_argument("--dry", action="store_true", help="Preview without Discord")
    args = parser.parse_args()
    scan(dry_run=args.dry)


if __name__ == "__main__":
    main()
