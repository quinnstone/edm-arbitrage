"""Reddit ticket listing scanner — finds arbitrage vs CrowdVolt bids.

Scrapes ticket exchange subreddits (r/avesNYC_tix, etc.) for people
selling tickets at prices below what CrowdVolt buyers are willing to pay.

Usage:
    python reddit_tix_scanner.py          # run once, send Discord digest
    python reddit_tix_scanner.py --dry    # preview without sending

This script is standalone — it shares config, crowdvolt, and matcher
imports but does NOT depend on the main arbitrage pipeline or promo scanner.
"""

import argparse
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import requests
from dateutil import parser as dateparser
from thefuzz import fuzz

import config
import crowdvolt
from matcher import extract_artist_name, _name_similarity, _localize_cv_date

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# Subreddits to scan for ticket listings
TICKET_SUBREDDITS = [
    "avesNYC_tix",
]

# Flair values that indicate a selling post (case-insensitive)
SELLING_FLAIRS = {"selling", "for sale", "sale"}
# Also match selling posts by title keywords when flair is missing
SELLING_TITLE_RE = re.compile(
    r'\b(?:sell(?:ing)?|WTS|for sale|letting go|face value|below face)\b',
    re.IGNORECASE,
)

# Skip posts with these flairs
SKIP_FLAIRS = {"sold", "bought", "buying", "iso", "wtb", "wanted", "trade"}

# Price extraction — "$50", "$50 each", "$50/ea", "$50 obo", "25$ each"
PRICE_RE = re.compile(
    r'(?:'
    r'\$\s*(\d+(?:\.\d{2})?)'        # $50, $ 50
    r'|'
    r'(\d+(?:\.\d{2})?)\s*\$'        # 50$, 25$
    r')'
    r'\s*(?:/?\s*(?:each|ea|per|obo|or best offer|ticket|tix))?',
    re.IGNORECASE,
)

MATCH_THRESHOLD = 70


@dataclass
class RedditListing:
    title: str
    body: str
    price: Optional[float]
    quantity: int
    url: str
    author: str
    created_utc: float
    subreddit: str
    event_name_guess: str  # extracted artist/event name


@dataclass
class RedditArbitrage:
    listing: RedditListing
    crowdvolt_event: crowdvolt.CrowdVoltEvent
    match_score: int
    reddit_price: float
    crowdvolt_bid: float
    profit: float


def _parse_price(text: str) -> Optional[float]:
    """Extract the first price mentioned in text."""
    match = PRICE_RE.search(text)
    if match:
        # group(1) is $N format, group(2) is N$ format
        val = match.group(1) or match.group(2)
        return float(val) if val else None

    # Fallback: bare number followed by "each"/"ea"/"per ticket"
    # Catches "95 each", "45 per ticket"
    bare = re.search(
        r'\b(\d{2,4})\s+(?:each|ea|per|a ticket|per ticket|a pop)\b',
        text, re.IGNORECASE,
    )
    if bare:
        val = float(bare.group(1))
        if 5 <= val <= 2000:  # reasonable ticket price range
            return val

    return None


def _parse_quantity(text: str) -> int:
    """Extract ticket quantity from text. Defaults to 1."""
    # "2x", "3x", "2 tickets", "3 tix"
    m = re.search(r'(\d+)\s*(?:x\b|tickets?|tix)', text, re.IGNORECASE)
    if m:
        qty = int(m.group(1))
        if 1 <= qty <= 10:
            return qty
    return 1


def _extract_event_name(title: str, body: str) -> str:
    """Try to extract the artist/event name from a listing title.

    Strips selling keywords, prices, dates, quantities, and venue names
    to isolate what's being sold.
    """
    text = title

    # Remove common selling prefixes
    text = re.sub(
        r'^(?:selling|WTS|for sale|letting go)\s*[-:—]?\s*',
        '', text, flags=re.IGNORECASE,
    )

    # Remove quantity patterns: "2x", "1x", "3 tickets for", etc.
    text = re.sub(r'\b\d+\s*x\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\b\d+\s*(?:tickets?|tix)\s*(?:for|to)?\s*', '', text, flags=re.IGNORECASE)

    # Remove "ticket(s) for" without a leading number
    text = re.sub(r'\btickets?\s+(?:for|to)\s+', '', text, flags=re.IGNORECASE)

    # Remove price patterns — both "$50" and "50$"
    text = re.sub(r'\$\s*\d+(?:\.\d{2})?\s*(?:each|ea|per|obo|or best offer)?', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\b\d+(?:\.\d{2})?\s*\$\s*(?:each|ea|per|obo)?', '', text, flags=re.IGNORECASE)

    # Remove date patterns: "4/18", "April 18", "tonight", "tomorrow", etc.
    text = re.sub(r'\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b', '', text)
    text = re.sub(
        r'\b(?:tonight|tn|tmrw|tomorrow|this (?:fri|sat|sun|thu|wed|tue|mon)\w*'
        r'|(?:mon|tue|wed|thu|fri|sat|sun)\w*day'
        r'|january|february|march|april|may|june|july|august'
        r'|september|october|november|december)\b',
        '', text, flags=re.IGNORECASE,
    )

    # Truncate at venue/location markers — keep only what's before
    text = re.sub(r'\s+(?:at|@)\s+.*$', '', text, flags=re.IGNORECASE)

    # Remove trailing noise words
    text = re.sub(
        r'\b(?:Will take|OBO|or best offer|face value|below face|each|ea|per)\b.*$',
        '', text, flags=re.IGNORECASE,
    )

    # Remove parentheticals
    text = re.sub(r'\([^)]*\)', '', text)

    # Clean up
    text = re.sub(r'[^\w\s&+:]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()

    # Strip leading/trailing "for" left over from removal
    text = re.sub(r'^for\s+', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+for$', '', text, flags=re.IGNORECASE)

    # Strip leading bare numbers left from quantity removal ("2 Baby J" → "Baby J")
    text = re.sub(r'^\d+\s+', '', text)

    # Strip trailing "ticket(s)" / "tix"
    text = re.sub(r'\s+(?:tickets?|tix)\s*$', '', text, flags=re.IGNORECASE)

    return text.strip()


def _parse_date_from_text(text: str) -> Optional[datetime]:
    """Try to extract an event date from listing text."""
    # Match "4/18", "4/18/26", "April 18"
    date_match = re.search(r'\b(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\b', text)
    if date_match:
        try:
            parsed = dateparser.parse(date_match.group(1), fuzzy=True)
            if parsed:
                if parsed.year < 2026:
                    parsed = parsed.replace(year=2026)
                return parsed
        except (ValueError, TypeError):
            pass

    # Match "tonight", "tomorrow"
    lower = text.lower()
    if "tonight" in lower or "tn" in lower.split():
        return datetime.now()
    if "tomorrow" in lower:
        return datetime.now() + timedelta(days=1)

    return None


def fetch_listings(lookback_days: int = 7) -> list[RedditListing]:
    """Fetch selling posts from ticket exchange subreddits."""
    cutoff = time.time() - (lookback_days * 24 * 60 * 60)
    all_listings = []

    for sub in TICKET_SUBREDDITS:
        try:
            resp = requests.get(
                f"https://www.reddit.com/r/{sub}/new.json",
                params={"limit": 50},
                headers=HEADERS,
                timeout=config.REQUEST_TIMEOUT,
            )
            if resp.status_code != 200:
                print(f"  [Reddit Tix] r/{sub}: HTTP {resp.status_code}")
                continue

            data = resp.json()
            for post in data.get("data", {}).get("children", []):
                d = post.get("data", {})

                # Age filter
                created = d.get("created_utc", 0)
                if created < cutoff:
                    continue

                # Flair filter
                flair = (d.get("link_flair_text") or "").lower().strip()
                if flair in SKIP_FLAIRS:
                    continue

                title = d.get("title", "")
                body = d.get("selftext", "") or ""

                # Must be a selling post — check flair or title
                is_selling = flair in SELLING_FLAIRS or bool(SELLING_TITLE_RE.search(title))
                if not is_selling:
                    continue

                # Extract price from title first, then body
                combined = f"{title} {body}"
                price = _parse_price(title) or _parse_price(body)
                quantity = _parse_quantity(combined)
                event_name = _extract_event_name(title, body)

                if not event_name or len(event_name) < 3:
                    continue

                listing = RedditListing(
                    title=title,
                    body=body[:300],
                    price=price,
                    quantity=quantity,
                    url=f"https://reddit.com{d.get('permalink', '')}",
                    author=d.get("author", ""),
                    created_utc=created,
                    subreddit=sub,
                    event_name_guess=event_name,
                )
                all_listings.append(listing)

        except requests.RequestException as e:
            print(f"  [Reddit Tix] r/{sub} error: {e}")
        time.sleep(1)

    return all_listings


def match_listings(
    listings: list[RedditListing],
    cv_events: list[crowdvolt.CrowdVoltEvent],
) -> list[RedditArbitrage]:
    """Match Reddit listings against CrowdVolt events and find arbitrage."""
    opportunities = []

    # Only consider CV events with active bids
    bid_events = [e for e in cv_events if e.max_bid is not None]

    for listing in listings:
        if listing.price is None:
            continue  # can't calculate arbitrage without a price

        best_match = None
        best_score = 0

        for cv in bid_events:
            score = _name_similarity(listing.event_name_guess, cv.name)
            if score < MATCH_THRESHOLD:
                continue

            # Date check if we can parse a date from the listing
            listing_date = _parse_date_from_text(f"{listing.title} {listing.body}")
            if listing_date and cv.event_date:
                cv_local = _localize_cv_date(cv)
                if cv_local:
                    cv_date = cv_local.date()
                    listing_d = listing_date.date()
                    if abs((cv_date - listing_d).days) > 1:
                        continue

            if score > best_score:
                best_score = score
                best_match = cv

        if best_match and best_match.max_bid is not None:
            profit = best_match.max_bid - listing.price
            if profit > 0:
                opportunities.append(RedditArbitrage(
                    listing=listing,
                    crowdvolt_event=best_match,
                    match_score=best_score,
                    reddit_price=listing.price,
                    crowdvolt_bid=best_match.max_bid,
                    profit=profit,
                ))

    # Sort by profit descending
    opportunities.sort(key=lambda o: o.profit, reverse=True)
    return opportunities


def send_digest(
    opportunities: list[RedditArbitrage],
    total_listings: int,
    total_priced: int,
) -> bool:
    """Send the Reddit ticket arbitrage digest to Discord."""
    if not config.DISCORD_WEBHOOK_URL:
        print("[Reddit Tix] No Discord webhook configured")
        return False

    today = datetime.now().strftime("%b %d, %Y")

    if not opportunities:
        payload = {
            "username": "Ticket Arb",
            "embeds": [{
                "title": f"🎟️ Reddit Ticket Scan — {today}",
                "description": (
                    f"Scanned **{total_listings}** selling posts "
                    f"(**{total_priced}** with prices) across "
                    f"{', '.join(f'r/{s}' for s in TICKET_SUBREDDITS)}.\n\n"
                    f"No arbitrage opportunities found."
                ),
                "color": 0x95A5A6,
            }],
        }
    else:
        fields = []
        for opp in opportunities[:15]:
            listing = opp.listing
            cv = opp.crowdvolt_event
            margin = (opp.profit / opp.reddit_price) * 100

            ago = datetime.now() - datetime.fromtimestamp(listing.created_utc)
            if ago.days > 0:
                age_str = f"{ago.days}d ago"
            else:
                hours = ago.seconds // 3600
                age_str = f"{hours}h ago" if hours > 0 else "just now"

            fields.append({
                "name": (
                    f"{cv.name} — +${opp.profit:.0f} ({margin:.0f}%)"
                ),
                "value": (
                    f"Reddit: **${opp.reddit_price:.0f}** "
                    f"({listing.quantity}x by u/{listing.author}, {age_str})\n"
                    f"CrowdVolt buyer: **${opp.crowdvolt_bid:.0f}**\n"
                    f"[Reddit post]({listing.url}) | [CrowdVolt]({cv.url})"
                ),
                "inline": False,
            })

        payload = {
            "username": "Ticket Arb",
            "embeds": [{
                "title": f"🎟️ Reddit Ticket Scan — {today}",
                "description": (
                    f"Scanned **{total_listings}** selling posts "
                    f"(**{total_priced}** with prices)\n"
                    f"**{len(opportunities)}** arbitrage opportunities found"
                ),
                "color": 0xFF5722,  # deep orange
                "fields": fields[:25],
            }],
        }

    try:
        resp = requests.post(
            config.DISCORD_WEBHOOK_URL,
            json=payload,
            timeout=config.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        print("[Reddit Tix] Digest sent to Discord")
        return True
    except requests.RequestException as e:
        print(f"[Reddit Tix] Failed to send digest: {e}")
        return False


def scan(dry_run: bool = False, cv_events: list = None) -> list[RedditArbitrage]:
    """Run the full Reddit ticket scan."""
    print(f"\n{'='*60}")
    print(f"[Reddit Tix] Starting scan at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    # Fetch CrowdVolt events if not provided
    if cv_events is None:
        cv_events = crowdvolt.fetch_all_events()
    if not cv_events:
        print("[Reddit Tix] No CrowdVolt events found")
        return []

    # Filter to events with bids (need a buyer for arbitrage)
    bid_events = [e for e in cv_events if e.max_bid is not None]
    print(f"[Reddit Tix] {len(bid_events)} CrowdVolt events with active buyers")

    # Fetch Reddit listings
    listings = fetch_listings(lookback_days=7)
    priced = [l for l in listings if l.price is not None]
    print(f"[Reddit Tix] {len(listings)} selling posts found "
          f"({len(priced)} with prices)")

    for l in listings[:10]:
        price_str = f"${l.price:.0f}" if l.price else "no price"
        print(f"  [{l.subreddit}] {l.event_name_guess[:40]} — {price_str} "
              f"({l.quantity}x)")

    # Match against CrowdVolt
    opportunities = match_listings(listings, cv_events)
    print(f"\n[Reddit Tix] {len(opportunities)} arbitrage opportunities")

    for opp in opportunities:
        print(f"  {opp.crowdvolt_event.name}: "
              f"Reddit ${opp.reddit_price:.0f} → CrowdVolt bid ${opp.crowdvolt_bid:.0f} "
              f"(+${opp.profit:.0f})")

    if not dry_run:
        send_digest(opportunities, len(listings), len(priced))

    return opportunities


def main():
    parser = argparse.ArgumentParser(description="Reddit ticket arbitrage scanner")
    parser.add_argument("--dry", action="store_true", help="Preview without sending to Discord")
    args = parser.parse_args()

    scan(dry_run=args.dry)


if __name__ == "__main__":
    main()
