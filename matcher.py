"""Match CrowdVolt events against SeatGeek, TickPick, StubHub, and VividSeats."""

import unicodedata
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from thefuzz import fuzz

import config
from crowdvolt import CrowdVoltEvent
from seatgeek import SeatGeekEvent
from stubhub import StubHubEvent
from tickpick import TickPickEvent
from vividseats import VividSeatsEvent


@dataclass
class ArbitrageOpportunity:
    crowdvolt_event: CrowdVoltEvent
    source_platform: str  # "SeatGeek", "TickPick", "StubHub", or "VividSeats"
    source_price: float  # estimated all-in price (base + fees)
    source_url: str
    crowdvolt_ask: Optional[float]  # lowest ask on CrowdVolt (what sellers want)
    crowdvolt_bid: Optional[float]  # highest bid on CrowdVolt (what buyers offer)
    profit_vs_ask: Optional[float]  # if you undercut the lowest ask
    profit_vs_bid: Optional[float]  # if you fill an existing bid
    fees_estimated: bool = False  # True when source_price includes an estimated fee


def _is_junk(name: str) -> bool:
    """Return True if an event name looks like parking, merch, etc."""
    lower = name.lower()
    return any(kw in lower for kw in JUNK_KEYWORDS)


def _strip_accents(text: str) -> str:
    """Normalize accented characters to ASCII equivalents.

    "rüfüs" → "rufus", "böhmer" → "bohmer", "naté" → "nate"
    """
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def extract_artist_name(event_name: str) -> str:
    """Pull the core artist name from an event string.

    Strips common suffixes like venue info, date fragments, and festival qualifiers.
    Used both for fuzzy matching and as the search query for external platforms.
    """
    name = _strip_accents(event_name.lower())

    # Truncate at venue/location delimiters — keep everything before.
    # The idx >= 5 guard prevents over-stripping short names like
    # "Wire Festival" (idx=4) down to "wire".
    for noise in [
        " at ", " @ ", " - ", " | ", " presents", " festival",
        " miami", " new york", " brooklyn", " chicago", " los angeles",
        " nyc", " la ",
    ]:
        idx = name.find(noise)
        if idx >= 5:  # keep enough chars for a meaningful query
            name = name[:idx]

    # Strip trailing qualifiers that don't identify the artist.
    # Safe for short names because we only remove known non-artist words.
    for suffix in [
        " tickets", " concert", " music", " live",
        " tour", " dj set", " dj", " set",
        # City names as suffixes — catches "Zedd Brooklyn" where the
        # truncation approach can't help (idx < 5 for short names).
        " brooklyn", " new york", " nyc", " manhattan",
        " chicago", " los angeles", " la", " miami",
        " san francisco", " sf", " las vegas", " denver",
        " seattle", " boston", " atlanta", " houston",
        " dallas", " detroit", " philadelphia", " phoenix",
        " portland", " nashville", " austin", " dc",
        " washington", " minneapolis", " tampa",
    ]:
        if name.endswith(suffix):
            name = name[: -len(suffix)]

    return name.strip()


def _dates_match(dt1, dt2, tolerance_days: int = 1) -> bool:
    """Check if two dates are within tolerance of each other.

    Compares calendar dates (not full datetimes) to avoid timezone
    artifacts — CrowdVolt stores UTC, so a 10pm ET show becomes
    2am UTC the next day.

    Returns True if either date is missing (allows match through).
    """
    if dt1 is None or dt2 is None:
        return True  # if we can't compare dates, allow the match through
    d1 = dt1.date() if hasattr(dt1, 'date') else dt1
    d2 = dt2.date() if hasattr(dt2, 'date') else dt2
    return abs((d1 - d2).days) <= tolerance_days


def _dates_confirmed(dt1, dt2, tolerance_days: int = 1) -> bool:
    """Like _dates_match but returns False when either date is missing.

    Used for high-confidence matching: only skip city checks when we
    can positively verify the dates line up.
    """
    if dt1 is None or dt2 is None:
        return False
    d1 = dt1.date() if hasattr(dt1, 'date') else dt1
    d2 = dt2.date() if hasattr(dt2, 'date') else dt2
    return abs((d1 - d2).days) <= tolerance_days


def _name_similarity(name1: str, name2: str) -> int:
    """Score 0-100 for how similar two event/artist names are."""
    a = extract_artist_name(name1)
    b = extract_artist_name(name2)
    base_scores = [fuzz.ratio(a, b), fuzz.token_sort_ratio(a, b)]
    best_base = max(base_scores)
    # partial_ratio inflates scores when one name is very short
    # (e.g. "ale" scores 100 against "alex") — only trust it
    # when the shorter name is long enough to be distinctive.
    if min(len(a), len(b)) >= 5:
        partial = fuzz.partial_ratio(a, b)
        # Only trust partial_ratio when it's within 25 points of the
        # base scores.  A big gap means partial matched a substring
        # but the names are otherwise very different (e.g. "baby jane"
        # partially matching "baby j & belters only": partial=80 but
        # ratio=47).
        if partial - best_base <= 25:
            base_scores.append(partial)
    return max(base_scores)


MATCH_THRESHOLD = 70  # minimum fuzzy score to consider a match
HIGH_CONFIDENCE_THRESHOLD = 85  # skip city check when name+date match this well

# Event names containing these words are not real tickets
JUNK_KEYWORDS = {"parking", "merch", "merchandise", "shuttle", "camping", "locker"}

# Cities that should be treated as equivalent
CITY_ALIASES = {
    "nyc": "new york", "brooklyn": "new york", "queens": "new york",
    "bronx": "new york", "manhattan": "new york", "staten island": "new york",
    "la": "los angeles", "hollywood": "los angeles", "inglewood": "los angeles",
    "pasadena": "los angeles", "east los angeles": "los angeles",
    "miami beach": "miami", "south beach": "miami", "miami gardens": "miami",
    "sf": "san francisco", "oakland": "san francisco",
    "arlington": "dallas", "fort worth": "dallas", "irving": "dallas",
    "rosemont": "chicago", "tinley park": "chicago", "hoffman estates": "chicago",
    "foxborough": "boston", "foxboro": "boston",
    "east rutherford": "new york", "newark": "new york",
    "atlantic city": "atlantic city",  # keep distinct from NYC
    "national harbor": "washington", "dc": "washington",
    "paradise": "las vegas", "henderson": "las vegas",
    "tempe": "phoenix", "scottsdale": "phoenix", "glendale": "phoenix",
    "noblesville": "indianapolis",
    "maryland heights": "st. louis",
    "auburn": "seattle",
}


def _normalize_city(raw: str) -> str:
    """Normalize a city string: strip state/country suffix, apply aliases."""
    # "Brooklyn, NY, US" → "brooklyn"
    city = raw.lower().strip()
    city = city.split(",")[0].strip()
    return CITY_ALIASES.get(city, city)


def _cities_match(city1: str, city2: str) -> bool:
    """Check if two city strings refer to the same metro area."""
    if not city1 or not city2:
        return True  # if either is missing, allow through

    c1 = _normalize_city(city1)
    c2 = _normalize_city(city2)

    if c1 == c2 or c1 in c2 or c2 in c1:
        return True

    # Fuzzy fallback for cities with slight name variations
    return fuzz.ratio(c1, c2) >= 80


def match_seatgeek(
    cv_event: CrowdVoltEvent,
    sg_events: list[SeatGeekEvent],
) -> list[ArbitrageOpportunity]:
    """Find the cheapest matching SeatGeek listing for a CrowdVolt event."""
    best = None
    fee_rate = config.PLATFORM_FEES.get("SeatGeek", 0)

    for sg in sg_events:
        if _is_junk(sg.title):
            continue
        score = _name_similarity(cv_event.name, sg.title)
        if score < MATCH_THRESHOLD:
            continue
        if not _dates_match(cv_event.event_date, sg.event_date):
            continue
        high_conf = score >= HIGH_CONFIDENCE_THRESHOLD and _dates_confirmed(cv_event.event_date, sg.event_date)
        if not high_conf and not _cities_match(cv_event.city, sg.city):
            continue
        if sg.lowest_price is None:
            continue

        all_in = sg.lowest_price * (1 + fee_rate)

        opp = ArbitrageOpportunity(
            crowdvolt_event=cv_event,
            source_platform="SeatGeek",
            source_price=round(all_in, 2),
            source_url=sg.url,
            crowdvolt_ask=cv_event.min_ask,
            crowdvolt_bid=cv_event.max_bid,
            profit_vs_ask=None,
            profit_vs_bid=None,
            fees_estimated=fee_rate > 0,
        )

        if cv_event.min_ask is not None:
            opp.profit_vs_ask = round(cv_event.min_ask - all_in, 2)
        if cv_event.max_bid is not None:
            opp.profit_vs_bid = round(cv_event.max_bid - all_in, 2)

        if best is None or opp.source_price < best.source_price:
            best = opp

    return [best] if best else []


def match_tickpick(
    cv_event: CrowdVoltEvent,
    tp_events: list[TickPickEvent],
) -> list[ArbitrageOpportunity]:
    """Find the cheapest matching TickPick listing for a CrowdVolt event."""
    best = None
    fee_rate = config.PLATFORM_FEES.get("TickPick", 0)

    for tp in tp_events:
        if _is_junk(tp.name):
            continue
        score = _name_similarity(cv_event.name, tp.name)
        if score < MATCH_THRESHOLD:
            continue
        if not _dates_match(cv_event.event_date, tp.event_date):
            continue
        high_conf = score >= HIGH_CONFIDENCE_THRESHOLD and _dates_confirmed(cv_event.event_date, tp.event_date)
        if not high_conf and not _cities_match(cv_event.city, tp.city):
            continue
        if tp.low_price is None:
            continue

        all_in = tp.low_price * (1 + fee_rate)

        opp = ArbitrageOpportunity(
            crowdvolt_event=cv_event,
            source_platform="TickPick",
            source_price=round(all_in, 2),
            source_url=tp.url,
            crowdvolt_ask=cv_event.min_ask,
            crowdvolt_bid=cv_event.max_bid,
            profit_vs_ask=None,
            profit_vs_bid=None,
            fees_estimated=fee_rate > 0,
        )

        if cv_event.min_ask is not None:
            opp.profit_vs_ask = round(cv_event.min_ask - all_in, 2)
        if cv_event.max_bid is not None:
            opp.profit_vs_bid = round(cv_event.max_bid - all_in, 2)

        if best is None or opp.source_price < best.source_price:
            best = opp

    return [best] if best else []


def match_stubhub(
    cv_event: CrowdVoltEvent,
    sh_events: list[StubHubEvent],
) -> list[ArbitrageOpportunity]:
    """Find the cheapest matching StubHub listing for a CrowdVolt event."""
    best = None
    fee_rate = config.PLATFORM_FEES.get("StubHub", 0)

    for sh in sh_events:
        if _is_junk(sh.name):
            continue
        score = _name_similarity(cv_event.name, sh.name)
        if score < MATCH_THRESHOLD:
            continue
        if not _dates_match(cv_event.event_date, sh.event_date):
            continue
        high_conf = score >= HIGH_CONFIDENCE_THRESHOLD and _dates_confirmed(cv_event.event_date, sh.event_date)
        if not high_conf and not _cities_match(cv_event.city, sh.city):
            continue
        if sh.min_price is None:
            continue

        # Use actual all-in price when available, otherwise estimate fees
        if sh.price_is_all_in:
            all_in = sh.min_price
            estimated = False
        else:
            all_in = sh.min_price * (1 + fee_rate)
            estimated = True

        opp = ArbitrageOpportunity(
            crowdvolt_event=cv_event,
            source_platform="StubHub",
            source_price=round(all_in, 2),
            source_url=sh.url,
            crowdvolt_ask=cv_event.min_ask,
            crowdvolt_bid=cv_event.max_bid,
            profit_vs_ask=None,
            profit_vs_bid=None,
            fees_estimated=estimated,
        )

        if cv_event.min_ask is not None:
            opp.profit_vs_ask = round(cv_event.min_ask - all_in, 2)
        if cv_event.max_bid is not None:
            opp.profit_vs_bid = round(cv_event.max_bid - all_in, 2)

        if best is None or opp.source_price < best.source_price:
            best = opp

    return [best] if best else []


def match_vividseats(
    cv_event: CrowdVoltEvent,
    vs_events: list[VividSeatsEvent],
) -> list[ArbitrageOpportunity]:
    """Find the cheapest matching VividSeats listing for a CrowdVolt event."""
    best = None
    fee_rate = config.PLATFORM_FEES.get("VividSeats", 0)

    for vs in vs_events:
        if _is_junk(vs.name):
            continue
        score = _name_similarity(cv_event.name, vs.name)
        if score < MATCH_THRESHOLD:
            continue
        if not _dates_match(cv_event.event_date, vs.event_date):
            continue
        high_conf = score >= HIGH_CONFIDENCE_THRESHOLD and _dates_confirmed(cv_event.event_date, vs.event_date)
        if not high_conf and not _cities_match(cv_event.city, vs.city):
            continue
        if vs.min_price is None:
            continue

        # Use actual all-in price when available, otherwise estimate fees
        if vs.price_is_all_in:
            all_in = vs.min_price
            estimated = False
        else:
            all_in = vs.min_price * (1 + fee_rate)
            estimated = True

        opp = ArbitrageOpportunity(
            crowdvolt_event=cv_event,
            source_platform="VividSeats",
            source_price=round(all_in, 2),
            source_url=vs.url,
            crowdvolt_ask=cv_event.min_ask,
            crowdvolt_bid=cv_event.max_bid,
            profit_vs_ask=None,
            profit_vs_bid=None,
            fees_estimated=estimated,
        )

        if cv_event.min_ask is not None:
            opp.profit_vs_ask = round(cv_event.min_ask - all_in, 2)
        if cv_event.max_bid is not None:
            opp.profit_vs_bid = round(cv_event.max_bid - all_in, 2)

        if best is None or opp.source_price < best.source_price:
            best = opp

    return [best] if best else []
