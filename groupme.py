"""Scan GroupMe group chat for ticket buy requests and match against CrowdVolt."""

import re
import time
from dataclasses import dataclass
from typing import Optional

import requests

import config
from crowdvolt import CrowdVoltEvent
from matcher import _name_similarity, MATCH_THRESHOLD

# Patterns indicating someone wants to buy a ticket.
# "anyone selling" is buy intent (asking the group), not a sell post.
_BUY_RE = re.compile(
    r"\b(?:looking for|lf|iso|wtb|anyone selling|anyone got|need)\b",
    re.IGNORECASE,
)

# Patterns indicating someone is selling — skip these.
_SELL_RE = re.compile(r"\b(?:selling|sell|wts|for sale)\b", re.IGNORECASE)

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


@dataclass
class GroupMeMatch:
    buy_requests: list[BuyRequest]
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
            ))
    return out


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def match_demand(
    buy_requests: list[BuyRequest],
    cv_events: list[CrowdVoltEvent],
) -> list[GroupMeMatch]:
    """Match buy requests against CrowdVolt events (including DICE).

    Returns one GroupMeMatch per matched event, with all matching buy
    requests grouped together.
    """
    matches: dict[str, GroupMeMatch] = {}  # keyed by event slug

    for req in buy_requests:
        best_score = 0
        best_event = None

        for event in cv_events:
            score = _name_similarity(req.event_query, event.name)
            if score > best_score and score >= MATCH_THRESHOLD:
                best_score = score
                best_event = event

        if best_event:
            slug = best_event.slug
            if slug in matches:
                matches[slug].buy_requests.append(req)
            else:
                matches[slug] = GroupMeMatch(
                    buy_requests=[req],
                    crowdvolt_event=best_event,
                )

    return list(matches.values())
