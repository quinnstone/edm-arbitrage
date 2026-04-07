"""Scan GroupMe group chat for ticket buy requests and match against CrowdVolt."""

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import requests
from dateutil import parser as dateparser

import config
from crowdvolt import CrowdVoltEvent
from matcher import _name_similarity, _dates_match, _cities_match

# Higher threshold for GroupMe — messages are unstructured text so we need
# stronger name evidence.  Cross-platform matchers use 70 but have date+city
# as backup; GroupMe messages often lack both.
_GM_MATCH_THRESHOLD = 80
_GM_CONFIRMED_THRESHOLD = 70  # OK to lower when date OR city confirms

# Patterns indicating someone wants to buy a ticket.
# "anyone selling" is buy intent (asking the group), not a sell post.
_BUY_RE = re.compile(
    r"\b(?:looking for|lf|iso|wtb|anyone selling|anyone got|need)\b",
    re.IGNORECASE,
)

# Patterns indicating someone is selling — skip these (from buy-side),
# also used to detect sell intent in sell-side parsing.
_SELL_RE = re.compile(r"\b(?:selling|sell|wts|for sale)\b", re.IGNORECASE)

# Broader sell-intent patterns that capture contextual selling.
# These handle cases where the event name appears *before* the sell keyword,
# e.g. "Bought zedds dead... Looking to sell at face value"
_SELL_CONTEXT_RE = re.compile(
    r"\b(?:looking to sell|trying to sell|want to sell|need to sell|"
    r"have .{3,40} (?:for sale|to sell)|"
    r"got .{3,40} (?:for sale|to sell)|"
    r"bought .{3,40}(?:looking to sell|want to sell|need to sell|for sale))\b",
    re.IGNORECASE,
)

# Words to strip from the extracted query (not part of the event name).
_NOISE_WORDS = [
    "tickets", "ticket", "tix", "tkt", "tkts",
    "please", "plz", "pls", "hmu", "dm me", "dm",
    "asap", "urgent", "desperately",
    "extra", "spare", "an extra",
    "one", "two", "three", "four",
    "willing to pay", "rsvp",
]


@dataclass
class BuyRequest:
    user: str
    text: str
    event_query: str
    message_id: str
    created_at: int  # unix timestamp
    mentioned_date: Optional[datetime] = None  # date extracted from message


@dataclass
class SellListing:
    user: str
    text: str
    event_query: str
    price: Optional[float]  # None if seller didn't list a price
    qty: int
    message_id: str
    created_at: int  # unix timestamp
    mentioned_date: Optional[datetime] = None  # date extracted from message


@dataclass
class GroupMeMatch:
    buy_requests: list[BuyRequest]
    crowdvolt_event: CrowdVoltEvent


@dataclass
class GroupMeSellMatch:
    sell_listings: list[SellListing]
    crowdvolt_event: CrowdVoltEvent


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def fetch_recent_messages(minutes: int = 10080) -> list[dict]:
    """Fetch messages from the last *minutes*, paginating as needed.

    The GroupMe API returns at most 100 messages per request (newest
    first).  For a 7-day window we may need several pages.  A safety
    cap of 20 pages (2 000 messages) prevents runaway requests.
    """
    if not config.GROUPME_TOKEN or not config.GROUPME_GROUP_ID:
        return []

    url = f"https://api.groupme.com/v3/groups/{config.GROUPME_GROUP_ID}/messages"
    cutoff = time.time() - (minutes * 60)
    messages = []
    before_id = None

    for _ in range(20):  # safety cap
        params = {"token": config.GROUPME_TOKEN, "limit": 100}
        if before_id:
            params["before_id"] = before_id

        try:
            resp = requests.get(url, params=params, timeout=config.REQUEST_TIMEOUT)
            if resp.status_code != 200:
                print(f"[GroupMe] API returned {resp.status_code}")
                break
        except requests.RequestException as e:
            print(f"[GroupMe] Request failed: {e}")
            break

        page = resp.json().get("response", {}).get("messages", [])
        if not page:
            break

        reached_cutoff = False
        for m in page:
            if m.get("created_at", 0) < cutoff:
                reached_cutoff = True
                break
            if m.get("text") and not m.get("system", False):
                messages.append(m)

        if reached_cutoff:
            break

        before_id = page[-1]["id"]
        time.sleep(0.3)  # respect rate limits between pages

    return messages


# ---------------------------------------------------------------------------
# Date extraction
# ---------------------------------------------------------------------------

# Day-of-week and relative date patterns
_DAY_NAMES = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3,
    "thurs": 3, "fri": 4, "sat": 5, "sun": 6,
}

def _extract_date_from_text(text: str) -> Optional[datetime]:
    """Try to extract an event date from a GroupMe message.

    Handles patterns like:
    - "April 4", "Apr 18", "4/4", "4/18"
    - "Saturday", "this Friday", "tomorrow", "tonight"
    - Falls back to dateutil fuzzy parsing
    """
    lower = text.lower()

    # "tonight" / "today"
    if re.search(r"\b(?:tonight|today)\b", lower):
        return datetime.now()

    # "tomorrow"
    if re.search(r"\btomorrow\b", lower):
        return datetime.now() + timedelta(days=1)

    # Day names: "Saturday", "this Friday", "next Saturday"
    for day_name, day_num in _DAY_NAMES.items():
        if re.search(r"\b" + day_name + r"\b", lower):
            today = datetime.now()
            days_ahead = (day_num - today.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7  # next occurrence
            return today + timedelta(days=days_ahead)

    # Explicit dates: "April 4", "Apr 18", "4/4", "4/18/26"
    # Try dateutil fuzzy parsing — it handles most formats
    try:
        parsed = dateparser.parse(text, fuzzy=True)
        if parsed and parsed.year >= 2025:
            # If the parsed date is in the past and no year was explicit,
            # it might be next year — but for a 7-day window, past dates
            # within a few days are probably correct
            return parsed
    except (ValueError, TypeError, OverflowError):
        pass

    return None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _extract_buy_query(text: str) -> Optional[str]:
    """Return the event/artist name if *text* is a buy request, else None."""
    lower = text.lower().strip()

    # Look for buy intent
    buy_match = _BUY_RE.search(lower)
    if not buy_match:
        return None

    # If a sell word appears *before* the buy intent, it's a sell post
    # e.g. "Selling 1 Adam Ten, anyone need one?"
    sell_match = _SELL_RE.search(lower)
    if sell_match and sell_match.start() < buy_match.start():
        return None

    # Everything after the buy-intent phrase is the query
    query = lower[buy_match.end():].strip()

    # Strip leading quantity: "2x", "2 ", "1x ", etc.
    query = re.sub(r"^\d+\s*x?\s+", "", query)

    # Strip phone numbers
    query = re.sub(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b", "", query)

    # Strip price mentions
    query = re.sub(r"\$\d+", "", query)

    # Strip noise words
    for word in _NOISE_WORDS:
        query = re.sub(r"\b" + re.escape(word) + r"\b", "", query, flags=re.IGNORECASE)

    # Clean up whitespace and punctuation
    query = re.sub(r"\s+", " ", query).strip(" ?!.,;:-")

    if len(query) < 3:
        return None

    return query


def _extract_sell_listing(text: str) -> Optional[tuple[str, Optional[float], int]]:
    """Return (event_query, price, qty) if *text* is a sell post, else None.

    Handles two patterns:
    1. Standard: "Selling 2 Adam Ten for $90" — event name after sell keyword
    2. Contextual: "Bought zedds dead... looking to sell at face" — event
       name appears *before* the sell keyword, extracted from the full message
    """
    lower = text.lower().strip()

    # Check for sell intent (standard pattern first)
    sell_match = _SELL_RE.search(lower)
    context_match = _SELL_CONTEXT_RE.search(lower)

    if not sell_match and not context_match:
        return None

    # If a buy word appears *before* the sell intent, it's a buy post
    # e.g. "ISO Adam Ten" with "sell" later is buy-first intent
    if sell_match:
        buy_match = _BUY_RE.search(lower)
        if buy_match and buy_match.start() < sell_match.start():
            return None

    # Decide where the event name lives: after sell keyword, or in the
    # full message context when the sell phrase leaves nothing useful
    query = ""
    if sell_match:
        # Standard: take everything after the sell keyword
        query = lower[sell_match.end():].strip()

    # Extract price from the full message (before cleaning the query)
    price = None
    price_match = re.search(r'\$(\d+(?:,\d{3})*(?:\.\d{2})?)', lower)
    if price_match:
        price = float(price_match.group(1).replace(",", ""))
    else:
        # Try "90$" or "for 90"
        price_match2 = re.search(r'(?:for\s+)?(\d+)\s*\$', lower)
        if price_match2:
            price = float(price_match2.group(1))

    # Extract quantity from after sell keyword
    qty = 1
    qty_match = re.match(r"^(\d+)\s*x?\s+", query)
    if qty_match:
        qty = int(qty_match.group(1))
        query = query[qty_match.end():]
    else:
        word_qtys = {"one": 1, "two": 2, "three": 3, "four": 4}
        for word, n in word_qtys.items():
            if query.startswith(word + " "):
                qty = n
                query = query[len(word):].strip()
                break

    # Remove price strings from the query
    query = re.sub(r'\$\d+(?:,\d{3})*(?:\.\d{2})?', '', query)
    query = re.sub(r'\d+\s*\$', '', query)

    # Strip noise words
    sell_noise = _NOISE_WORDS + ["each", "obo", "or best offer", "face value",
                                  "face", "below face", "above face", "per",
                                  "text", "call", "dm me", "dm",
                                  "another", "anyone need", "anyone want",
                                  "lmk", "let me know", "hit me up",
                                  "for", "at"]
    for word in sell_noise:
        query = re.sub(r"\b" + re.escape(word) + r"\b", "", query, flags=re.IGNORECASE)

    # Strip phone numbers
    query = re.sub(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b", "", query)

    # Clean up
    query = re.sub(r"\s+", " ", query).strip(" ?!.,;:-")

    # If the after-keyword query is too short, try extracting the event
    # name from the full message (contextual sell pattern).
    # e.g. "Bought zedds dead bowl... looking to sell at face value"
    if len(query) < 3:
        query = _extract_event_from_context(lower)

    if len(query) < 3:
        return None

    return (query, price, qty)


def _extract_event_from_context(text: str) -> str:
    """Extract an event name from a message where sell intent is contextual.

    Handles patterns like:
    - "Bought zedds dead bowl 7/18 forest hills thinking they were floor.
       Looking to sell at face value"
    - "Have 2 Chris Lake tickets, want to sell"
    - "Got extra Solid Grooves for sale"

    Strategy: strip sell-intent phrases, buy-context phrases, noise, and
    return what's left as the event query.
    """
    # Remove sell-intent phrases
    cleaned = re.sub(
        r"\b(?:looking to sell|trying to sell|want to sell|need to sell|"
        r"for sale|selling|sell|wts)\b",
        " ", text, flags=re.IGNORECASE,
    )
    # Remove buy-context phrases (how they got the ticket)
    cleaned = re.sub(
        r"\b(?:bought|purchased|got|have|had)\b",
        " ", cleaned, flags=re.IGNORECASE,
    )
    # Remove filler/reasoning
    cleaned = re.sub(
        r"\b(?:thinking they were|thought they were|thought it was|"
        r"turns out|but|and|so|at face value|face value|face|"
        r"below face|above face|obo|or best offer)\b",
        " ", cleaned, flags=re.IGNORECASE,
    )
    # Remove dates like "7/18", "April 4", "Saturday"
    cleaned = re.sub(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", " ", cleaned)
    cleaned = re.sub(
        r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        " ", cleaned, flags=re.IGNORECASE,
    )
    # Remove prices
    cleaned = re.sub(r'\$\d+(?:,\d{3})*(?:\.\d{2})?', '', cleaned)
    # Remove phone numbers
    cleaned = re.sub(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b", "", cleaned)
    # Remove common noise
    for word in _NOISE_WORDS:
        cleaned = re.sub(r"\b" + re.escape(word) + r"\b", "", cleaned, flags=re.IGNORECASE)
    # Remove seating words
    cleaned = re.sub(
        r"\b(?:floor|ga|general admission|pit|balcony|mezzanine|vip|section|row|seat)\b",
        " ", cleaned, flags=re.IGNORECASE,
    )

    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ?!.,;:-")
    # Strip leading quantity: "2 chris lake" → "chris lake"
    cleaned = re.sub(r"^\d+\s*x?\s+", "", cleaned)
    return cleaned


def parse_sell_listings(messages: list[dict]) -> list[SellListing]:
    """Extract sell listings from a list of GroupMe messages."""
    out = []
    for msg in messages:
        result = _extract_sell_listing(msg.get("text", ""))
        if result:
            query, price, qty = result
            out.append(SellListing(
                user=msg.get("name", "Unknown"),
                text=msg.get("text", ""),
                event_query=query,
                price=price,
                qty=qty,
                message_id=msg.get("id", ""),
                created_at=msg.get("created_at", 0),
                mentioned_date=_extract_date_from_text(msg.get("text", "")),
            ))
    return out


def parse_buy_requests(messages: list[dict]) -> list[BuyRequest]:
    """Extract buy requests from a list of GroupMe messages."""
    out = []
    for msg in messages:
        query = _extract_buy_query(msg.get("text", ""))
        if query:
            out.append(BuyRequest(
                user=msg.get("name", "Unknown"),
                text=msg.get("text", ""),
                event_query=query,
                message_id=msg.get("id", ""),
                created_at=msg.get("created_at", 0),
                mentioned_date=_extract_date_from_text(msg.get("text", "")),
            ))
    return out


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _is_confirmed(request_date, event, default_city: str = "New York") -> bool:
    """Check if date or city from the message confirms the CrowdVolt match.

    GroupMe is NYC-based, so messages without a city are assumed NYC.
    A recent message (last 48h) for an event within 7 days also counts
    as soft confirmation — recency implies the event is upcoming and local.
    """
    # Date confirmation
    if request_date and event.event_date:
        if _dates_match(request_date, event.event_date, tolerance_days=1):
            return True

    # City confirmation — assume NYC for this group
    if event.city and _cities_match(default_city, event.city):
        return True

    return False


def match_demand(
    buy_requests: list[BuyRequest],
    cv_events: list[CrowdVoltEvent],
) -> list[GroupMeMatch]:
    """Match buy requests against CrowdVolt events (including DICE).

    Uses tiered thresholds:
    - Score >= 80: match (strong name match alone is enough)
    - Score 70-79: match only if date OR city confirms

    Skips past events. Returns one GroupMeMatch per matched event,
    with all matching buy requests grouped together.
    """
    today = datetime.now().date()
    # Filter to future events with active sellers — no seller means
    # nothing to buy on CrowdVolt, so no actionable opportunity.
    active_events = [
        e for e in cv_events
        if e.min_ask is not None
        and (e.event_date is None or e.event_date.date() >= today)
    ]

    matches: dict[str, GroupMeMatch] = {}

    for req in buy_requests:
        best_score = 0
        best_event = None

        for event in active_events:
            score = _name_similarity(req.event_query, event.name)
            if score <= best_score:
                continue

            if score >= _GM_MATCH_THRESHOLD:
                best_score = score
                best_event = event
            elif score >= _GM_CONFIRMED_THRESHOLD:
                if _is_confirmed(req.mentioned_date, event):
                    best_score = score
                    best_event = event

        if best_event:
            # Final date contradiction check — if the message mentions a
            # specific date that does NOT match the event, skip it
            if (req.mentioned_date and best_event.event_date
                    and not _dates_match(req.mentioned_date, best_event.event_date, tolerance_days=1)):
                continue

            slug = best_event.slug
            if slug in matches:
                matches[slug].buy_requests.append(req)
            else:
                matches[slug] = GroupMeMatch(
                    buy_requests=[req],
                    crowdvolt_event=best_event,
                )

    return list(matches.values())


def match_supply(
    sell_listings: list[SellListing],
    cv_events: list[CrowdVoltEvent],
) -> list[GroupMeSellMatch]:
    """Match sell listings against CrowdVolt events with active bids.

    Only considers events where someone on CrowdVolt is already offering
    to buy — no bid means no guaranteed buyer, so no alert.

    Uses tiered thresholds (same as demand):
    - Score >= 80: match
    - Score 70-79: match only if date OR city confirms

    Skips past events. Returns one match per event, with all matching
    sell listings grouped.
    """
    today = datetime.now().date()
    events_with_bids = [
        e for e in cv_events
        if e.max_bid is not None
        and (e.event_date is None or e.event_date.date() >= today)
    ]

    matches: dict[str, GroupMeSellMatch] = {}

    for listing in sell_listings:
        best_score = 0
        best_event = None

        for event in events_with_bids:
            score = _name_similarity(listing.event_query, event.name)
            if score <= best_score:
                continue

            if score >= _GM_MATCH_THRESHOLD:
                best_score = score
                best_event = event
            elif score >= _GM_CONFIRMED_THRESHOLD:
                if _is_confirmed(listing.mentioned_date, event):
                    best_score = score
                    best_event = event

        if best_event:
            if (listing.mentioned_date and best_event.event_date
                    and not _dates_match(listing.mentioned_date, best_event.event_date, tolerance_days=1)):
                continue

            slug = best_event.slug
            if slug in matches:
                matches[slug].sell_listings.append(listing)
            else:
                matches[slug] = GroupMeSellMatch(
                    sell_listings=[listing],
                    crowdvolt_event=best_event,
                )

    return list(matches.values())
