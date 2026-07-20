"""Marquee Skydeck face-value arbitrage scanner.

Flags CrowdVolt buyer bids that exceed the face value on Tao Group's
primary ticketing for Marquee Skydeck events — if and only if the
primary isn't sold out (so the ticket can actually be bought at face
and the CV bid filled).

Tao event pages live at:
    https://tickets.taogroup.com/e/marquee-skydeck-nyc-s{N}/tickets
where N increments per event. Pages are server-rendered with JSON-LD
(MusicEvent + per-tier offers with availability), so plain HTTP works.
Invalid N returns HTTP 200 with an "Event Not Found" title.

Design constraints (non-interference with the main pipeline):
- Called inline from main.scan_once inside try/except — a Tao failure
  can never break the main scan.
- ZERO Tao requests unless a CV Skydeck event actually has a bid.
- Discovery (s-number enumeration) is cached in data/ and refreshed at
  most every DISCOVERY_TTL_HOURS, not every 15-min scan.
"""

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import requests
from dateutil import parser as dateparser

import config
import matcher

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

_BASE = "https://tickets.taogroup.com/e/marquee-skydeck-nyc-s{n}/tickets"

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CACHE_FILE = os.path.join(_DATA_DIR, "skydeck_events.json")
SENT_FILE = os.path.join(_DATA_DIR, "skydeck_sent.json")

DISCOVERY_TTL_HOURS = 6     # how often the s-number sweep refreshes
NOT_FOUND_RECHECK_DAYS = 3  # re-probe "Event Not Found" slots after this
SWEEP_START = 1             # first-ever sweep lower bound
SWEEP_FORWARD = 10          # how far past max known s-number to probe
ALERT_COOLDOWN_HOURS = 23
# CrowdVolt's fee is already baked into bid.all_in_price (the API returns
# it net of the seller's cut — Rufus front-GA bid price $640 ships as
# all_in_price $618, a ~3.4% reduction matching CV's actual take). So no
# additional fee deduction is needed when computing profit from a bid.

# JSON-LD names Tao uses as placeholders before announcing the artist —
# when seen, fall back to the <title> for the real name.
_PLACEHOLDER_NAMES = {"announce by marquee", "announce", "tba", "tbd"}


@dataclass
class SkydeckEvent:
    s_number: int
    url: str
    name: str
    event_date: Optional[datetime]      # tz-aware, venue-local from JSON-LD
    tiers: list                          # [{name, price, available}]


@dataclass
class SkydeckArb:
    cv_event: object                     # CrowdVoltEvent
    tao: SkydeckEvent
    face_price: float
    face_tier: str
    cv_bid: float
    est_profit: float                    # after CV seller fee


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json(path, data):
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _was_recently_alerted(key: str) -> bool:
    sent = _load_json(SENT_FILE, {})
    last = sent.get(key)
    if not last:
        return False
    try:
        return datetime.now() - datetime.fromisoformat(last) < timedelta(hours=ALERT_COOLDOWN_HOURS)
    except (ValueError, TypeError):
        return False


def _mark_alerted(key: str) -> None:
    sent = _load_json(SENT_FILE, {})
    sent[key] = datetime.now().isoformat()
    cutoff = (datetime.now() - timedelta(hours=ALERT_COOLDOWN_HOURS * 2)).isoformat()
    sent = {k: v for k, v in sent.items() if v > cutoff}
    _save_json(SENT_FILE, sent)


# ---------------------------------------------------------------------------
# Tao page fetch + parse
# ---------------------------------------------------------------------------

def tier_family(name: str) -> str:
    """Classify a ticket/bid type into a product family.

    GA and GA Fast Pass are DIFFERENT products (Fast Pass includes
    expedited-entry wristband) and must never cross-match. Order matters:
    "GA Fast Pass" contains "GA", so fast-pass is checked first.
    """
    n = (name or "").lower()
    if "fast pass" in n or "fastpass" in n:
        return "fastpass"
    if "general admission" in n or re.search(r"\bga\b", n):
        return "ga"
    return "other"


def _parse_visible_tiers(html: str) -> list:
    """Extract live tier state from the server-rendered ticket rows.

    The public rows in #ticket-types-content carry the truth:
    - tier name in <h3 class="name ...">, with "sold-out-text" in the
      class when sold out
    - displayed price is ALL-IN (Tao fees included), e.g. "$130.00"
      with a fee-notice "Incl. $5.00 in fees"

    JSON-LD must NOT be used for availability or pricing — it is
    generated at publish time and goes stale (observed live: LD said
    InStock/base-price while the page showed Sold Out/all-in).
    """
    # Slice to the public section only (skip the access-code unlock area)
    start = html.find('id="ticket-types-content"')
    if start == -1:
        return []
    section = html[start:]

    tiers = []
    blocks = re.split(r'class="ticket-type-item', section)[1:]
    for block in blocks:
        name_m = re.search(
            r'<h3 class="name([^"]*)">\s*(.*?)\s*</h3>', block, re.DOTALL)
        if not name_m:
            continue
        name_classes = name_m.group(1)
        name = re.sub(r"<[^>]+>", "", name_m.group(2)).strip()
        if not name:
            continue

        price_m = re.search(r"\$\s*([\d,]+(?:\.\d{2})?)", block)
        if not price_m:
            continue
        price_allin = float(price_m.group(1).replace(",", ""))
        if price_allin <= 0:
            continue

        sold_out = "sold-out-text" in name_classes or \
                   'class="ticket-sold-out-text"' in block

        tiers.append({
            "name": name,
            "price": price_allin,          # fee-inclusive, what you'd pay
            "available": not sold_out,
            "family": tier_family(name),
        })
    return tiers


def fetch_skydeck_page(s_number: int) -> Optional[SkydeckEvent]:
    """Fetch and parse one Tao Skydeck event page.

    Name + date come from title/JSON-LD; tier availability + all-in
    pricing come from the live server-rendered rows (see
    _parse_visible_tiers for why LD offers are not trusted).

    Returns None when the slot is "Event Not Found" or unparseable.
    """
    url = _BASE.format(n=s_number)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [Skydeck] s{s_number}: fetch error: {e}")
        return None

    html = resp.text

    title_m = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
    title = (title_m.group(1).strip() if title_m else "")
    if "event not found" in title.lower():
        return None
    # "SOFI TUKKER | Tao Group Hospitality" → "SOFI TUKKER"
    name = title.split("|")[0].strip()

    event_date = None
    for block in re.findall(
            r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL):
        try:
            data = json.loads(block)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and data.get("@type") in ("MusicEvent", "Event"):
            if not name:
                ld_name = (data.get("name") or "").strip()
                if ld_name and ld_name.lower() not in _PLACEHOLDER_NAMES:
                    name = ld_name
            start = data.get("startDate")
            if start:
                try:
                    event_date = dateparser.parse(start)
                except (ValueError, TypeError):
                    pass
            break

    if not name:
        return None

    return SkydeckEvent(
        s_number=s_number, url=url, name=name,
        event_date=event_date, tiers=_parse_visible_tiers(html),
    )


# Fulfillment hierarchy: which Tao tier families can fulfill a bid of a
# given family. A Fast Pass is GA + expedited entry — a strict upgrade —
# so it can fulfill a GA bid (the buyer gets more than they asked for).
# The reverse is not true: a plain GA ticket cannot fulfill a Fast Pass
# bid, since the buyer specifically paid for expedited entry.
_FULFILLABLE_BY = {
    "ga": ("ga", "fastpass"),
    "fastpass": ("fastpass",),
}


def cheapest_available_face(ev: SkydeckEvent, family: str) -> Optional[dict]:
    """Cheapest in-stock tier that can FULFILL a bid of this family
    (all-in price), or None if nothing fulfillable is in stock.

    For a GA bid this includes Fast Pass tiers (upgrade fulfillment);
    for a Fast Pass bid only Fast Pass tiers qualify.
    """
    allowed = _FULFILLABLE_BY.get(family, (family,))
    in_stock = [t for t in ev.tiers
                if t["available"] and t["family"] in allowed]
    if not in_stock:
        return None
    return min(in_stock, key=lambda t: t["price"])


# ---------------------------------------------------------------------------
# Discovery — s-number enumeration with cache
# ---------------------------------------------------------------------------

def _discover(cache: dict) -> dict:
    """Refresh the s-number → event mapping.

    Probes: all cached-valid slots with future dates (kept fresh elsewhere
    at match time), stale not-found slots, and SWEEP_FORWARD slots past the
    highest known number to catch newly created events.
    """
    now = datetime.now()
    known = cache.get("slots", {})

    numbers_to_probe = set()
    max_known = SWEEP_START - 1
    for s_str, meta in known.items():
        s = int(s_str)
        max_known = max(max_known, s)
        if meta.get("not_found"):
            checked = meta.get("checked", "2000-01-01")
            try:
                stale = now - datetime.fromisoformat(checked) > timedelta(days=NOT_FOUND_RECHECK_DAYS)
            except (ValueError, TypeError):
                stale = True
            if stale:
                numbers_to_probe.add(s)

    if not known:
        # First-ever sweep — bounded wide scan
        numbers_to_probe.update(range(SWEEP_START, SWEEP_START + 80))
    else:
        numbers_to_probe.update(range(max_known + 1, max_known + 1 + SWEEP_FORWARD))

    slots_to_probe = sorted(numbers_to_probe)
    print(f"  [Skydeck] discovery probing {len(slots_to_probe)} slots")

    # Parallel HTTP fetches. Serial 0.8s-politeness took ~2.8s/slot
    # (fetch + delay), pushing 50-slot discovery to 140s and blowing past
    # the outer wrapper timeout. Empirical measurement showed Tao serves
    # consistent 1.4-1.8s regardless of concurrency, no 429/slowdown at
    # 5 workers even under hammering — see notes below. Result: ~15s
    # for 50 slots, 9x speedup, well under the wrapper budget.
    from concurrent.futures import ThreadPoolExecutor

    def _probe_one(s):
        return s, fetch_skydeck_page(s)

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(_probe_one, slots_to_probe))

    for s, ev in results:
        if ev:
            known[str(s)] = {
                "url": ev.url,
                "name": ev.name,
                "date": ev.event_date.isoformat() if ev.event_date else None,
                "checked": now.isoformat(),
                "not_found": False,
            }
        else:
            known[str(s)] = {"not_found": True, "checked": now.isoformat()}

    cache["slots"] = known
    cache["last_discovery"] = now.isoformat()
    return cache


def _maybe_discover(cache: dict) -> dict:
    last = cache.get("last_discovery")
    if last:
        try:
            if datetime.now() - datetime.fromisoformat(last) < timedelta(hours=DISCOVERY_TTL_HOURS):
                return cache
        except (ValueError, TypeError):
            pass
    return _discover(cache)


# ---------------------------------------------------------------------------
# Matching + scan
# ---------------------------------------------------------------------------

def _match_tao_slot(cv_event, cache: dict) -> Optional[int]:
    """Find the Tao s-number whose nightlife date matches the CV event.

    If multiple Tao events share the date (day party + night), requires a
    name fuzz >= 70 to disambiguate; otherwise skips rather than guessing.
    """
    cv_local = matcher._localize_cv_date(cv_event)
    if not cv_local:
        return None

    candidates = []
    for s_str, meta in cache.get("slots", {}).items():
        if meta.get("not_found") or not meta.get("date"):
            continue
        try:
            tao_date = dateparser.parse(meta["date"])
        except (ValueError, TypeError):
            continue
        if matcher._dates_match(cv_local, tao_date):
            candidates.append((int(s_str), meta))

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][0]

    # Multiple same-night events — disambiguate by name
    best_s, best_score = None, 0
    for s, meta in candidates:
        score = matcher._name_similarity(cv_event.name, meta.get("name", ""))
        if score >= 70 and score > best_score:
            best_s, best_score = s, score
    return best_s


def scan(cv_events: list, dry_run: bool = False) -> list:
    """Check bidded CV Skydeck events against Tao face value, per product
    family.

    GA and GA Fast Pass are matched per the fulfillment hierarchy: a GA
    bid can be fulfilled by GA OR Fast Pass stock (Fast Pass is a strict
    upgrade); a Fast Pass bid only by Fast Pass stock. Alert condition
    per family (operator-specified): highest CV bid in the family >
    cheapest IN-STOCK fulfillable face price. Face prices are all-in
    (Tao fees included, as displayed on the site). When nothing
    fulfillable is in stock, no flag — per the sold-out rule.
    """
    skydeck = [
        e for e in cv_events
        if e.venue and "skydeck" in e.venue.lower() and e.bids
    ]
    if not skydeck:
        return []  # zero Tao traffic when no bidded Skydeck events

    print(f"[Skydeck] {len(skydeck)} bidded Skydeck events on CV")
    cache = _load_json(CACHE_FILE, {})
    cache = _maybe_discover(cache)
    _save_json(CACHE_FILE, cache)

    arbs = []
    for cv in skydeck:
        s = _match_tao_slot(cv, cache)
        if s is None:
            print(f"  [Skydeck] {cv.name!r}: no Tao page matched by date")
            continue

        # Fetch fresh — sold-out state changes faster than the cache
        tao = fetch_skydeck_page(s)
        time.sleep(0.5)
        if tao is None:
            continue

        if not any(t["available"] for t in tao.tiers):
            print(f"  [Skydeck] {tao.name!r} (s{s}): SOLD OUT — no flag per rule")
            continue

        # Highest CV bid per product family
        bids_by_family: dict = {}
        for bid in cv.bids:
            fam = tier_family(bid.ticket_type)
            if fam == "other":
                continue  # tables/VIP etc. — out of scope
            if fam not in bids_by_family or bid.all_in_price > bids_by_family[fam]:
                bids_by_family[fam] = bid.all_in_price

        for fam, top_bid in bids_by_family.items():
            face = cheapest_available_face(tao, fam)
            if face is None:
                print(f"  [Skydeck] {tao.name!r} [{fam}]: nothing fulfillable in stock — no flag")
                continue
            if top_bid > face["price"]:
                # top_bid is bid.all_in_price (already net of CV's seller
                # fee per the API). Subtract the Tao face directly.
                arbs.append(SkydeckArb(
                    cv_event=cv, tao=tao,
                    face_price=face["price"], face_tier=face["name"],
                    cv_bid=top_bid,
                    est_profit=round(top_bid - face["price"], 2),
                ))
                print(f"  [Skydeck] ARB [{fam}]: {tao.name!r} bid ${top_bid:.0f} > "
                      f"face ${face['price']:.0f} ({face['name']})")
            else:
                print(f"  [Skydeck] {tao.name!r} [{fam}]: bid ${top_bid:.0f} <= "
                      f"face ${face['price']:.0f} — no flag")

    # Dedup + send. Key includes tier so GA and Fast Pass alert separately,
    # and a changed bid or face re-alerts.
    fresh = []
    for a in arbs:
        key = f"{a.cv_event.slug}|{a.face_tier}|{a.cv_bid:.0f}|{a.face_price:.0f}"
        if not _was_recently_alerted(key):
            fresh.append((key, a))

    if fresh and not dry_run:
        for key, a in fresh:
            if _send_alert(a):
                _mark_alerted(key)
            time.sleep(1)

    return [a for _, a in fresh]


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def _send_alert(a: SkydeckArb) -> bool:
    if not config.DISCORD_WEBHOOK_URL:
        return False

    cv = a.cv_event
    cv_local = matcher._localize_cv_date(cv)
    date_str = (cv_local or cv.event_date).strftime("%b %d, %Y") if cv.event_date else "TBD"
    profit_note = "" if a.est_profit > 0 else "  ⚠️ negative after CV fee"

    payload = {
        "username": "Ticket Arb",
        "embeds": [{
            "title": f"🌇 Skydeck Face-Value Arb — {a.tao.name}",
            "description": f"Marquee Skydeck — New York — {date_str}",
            "color": 0x00BFFF,  # deep sky blue — distinct from other alert types
            "fields": [
                {"name": "CV buyer offer", "value": f"**${a.cv_bid:.0f}**", "inline": True},
                {"name": "Tao face value",
                 "value": f"**${a.face_price:.0f}** ({a.face_tier})", "inline": True},
                {"name": "Est. profit (after ~10% CV fee)",
                 "value": f"**${a.est_profit:+.0f}**{profit_note}\n"
                          f"_Face is all-in (Tao fees included)_",
                 "inline": False},
                {"name": "Links",
                 "value": f"[Buy at face (Tao)]({a.tao.url}) | [Fill bid (CrowdVolt)]({cv.url})",
                 "inline": False},
            ],
        }],
    }
    try:
        resp = requests.post(config.DISCORD_WEBHOOK_URL, json=payload,
                             timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
        print(f"  [Skydeck] Alert sent: {a.tao.name}")
        return True
    except requests.RequestException as e:
        print(f"  [Skydeck] Alert failed: {e}")
        return False
