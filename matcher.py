"""Match CrowdVolt events against SeatGeek, TickPick, StubHub, VividSeats, and Gametime."""

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from thefuzz import fuzz

import config
from crowdvolt import CrowdVoltEvent
from seatgeek import SeatGeekEvent
from stubhub import StubHubEvent
from tickpick import TickPickEvent
from vividseats import VividSeatsEvent
from gametime import GametimeEvent


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

    # Strip day-of-week and qualifier parentheticals that CrowdVolt adds
    # e.g., "Chris Lake (Saturday)" → "Chris Lake"
    # Also strip age qualifiers: "(21+ Event)", "(18+)", "(All Ages)"
    name = re.sub(
        r'\s*\((saturday|friday|thursday|sunday|monday|tuesday|wednesday'
        r'|afters|2-day pass|day \d+'
        r'|\d+\+(?:\s*event)?|all\s*ages)\)\s*',
        '', name, flags=re.IGNORECASE,
    )

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


def search_queries(event_name: str) -> list[str]:
    """Generate candidate search queries for an event name.

    Unlike extract_artist_name (which aggressively strips for fuzzy matching),
    this preserves featured artists so platform searches actually find them.

    Returns queries ordered best-first. Callers should try each until one
    returns results.

    Examples:
        "Factory 93 Presents: Seth Troxler" → ["seth troxler", "factory 93"]
        "Teksupport: Adriatique"            → ["adriatique", "teksupport"]
        "Chris Lake + Fisher"               → ["chris lake fisher", "chris lake"]
        "Chris Lake (Saturday)"             → ["chris lake"]
        "Bob Moses"                         → ["bob moses"]
    """
    raw = _strip_accents(event_name.lower())

    # Strip parentheticals (day-of-week, age qualifiers)
    raw = re.sub(
        r'\s*\((saturday|friday|thursday|sunday|monday|tuesday|wednesday'
        r'|afters|2-day pass|day \d+'
        r'|\d+\+(?:\s*event)?|all\s*ages)\)\s*',
        '', raw, flags=re.IGNORECASE,
    )

    queries = []

    # Split on "presents" or ":" — right side is usually the featured artist
    split = re.split(r'\s+presents\s*:?\s*|\s*:\s+', raw, maxsplit=1)
    if len(split) == 2:
        left, right = split
        right_clean = extract_artist_name(right)
        left_clean = extract_artist_name(left)
        # Featured artist (right side) is the better search query
        if right_clean and len(right_clean) >= 3:
            queries.append(right_clean)
        if left_clean and left_clean != right_clean:
            queries.append(left_clean)
    else:
        # No promoter/featured split — check for multi-artist "+"/"&"
        parts = re.split(r'\s*[+&]\s+', raw)
        if len(parts) > 1:
            # Try full combined query first (some platforms handle it well),
            # then try individual artists
            combined = " ".join(extract_artist_name(p) for p in parts if extract_artist_name(p))
            if combined:
                queries.append(combined)
            for p in parts:
                cleaned = extract_artist_name(p)
                if cleaned and cleaned not in queries:
                    queries.append(cleaned)
        else:
            # Simple single-artist name
            queries.append(extract_artist_name(raw))

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for q in queries:
        if q and q not in seen:
            seen.add(q)
            unique.append(q)

    return unique if unique else [extract_artist_name(event_name)]


# Map CrowdVolt cities to their local timezone for date normalization.
# CrowdVolt stores event times in UTC — a 10pm ET show becomes 2am UTC
# the next day. Converting to local time lets us compare calendar dates
# with zero tolerance, fixing consecutive-night cross-matching.
CITY_TIMEZONES = {
    "new york": ZoneInfo("America/New_York"),
    "brooklyn": ZoneInfo("America/New_York"),
    "chicago": ZoneInfo("America/Chicago"),
    "los angeles": ZoneInfo("America/Los_Angeles"),
    "miami": ZoneInfo("America/New_York"),
    "las vegas": ZoneInfo("America/Los_Angeles"),
    "denver": ZoneInfo("America/Denver"),
    "phoenix": ZoneInfo("America/Phoenix"),
    "nashville": ZoneInfo("America/Chicago"),
    "atlanta": ZoneInfo("America/New_York"),
    "detroit": ZoneInfo("America/Detroit"),
    "seattle": ZoneInfo("America/Los_Angeles"),
    "boston": ZoneInfo("America/New_York"),
    "houston": ZoneInfo("America/Chicago"),
    "dallas": ZoneInfo("America/Chicago"),
    "philadelphia": ZoneInfo("America/New_York"),
    "washington": ZoneInfo("America/New_York"),
    "minneapolis": ZoneInfo("America/Chicago"),
    "san francisco": ZoneInfo("America/Los_Angeles"),
    "austin": ZoneInfo("America/Chicago"),
    "portland": ZoneInfo("America/Los_Angeles"),
    "tampa": ZoneInfo("America/New_York"),
}


def _localize_cv_date(cv_event) -> Optional[datetime]:
    """Convert a CrowdVolt event's UTC datetime to local time.

    Returns the datetime shifted to the show's local timezone so that
    calendar-date comparison works correctly. Falls back to UTC if
    the city isn't mapped.
    """
    dt = cv_event.event_date
    if dt is None:
        return None

    city_key = _normalize_city(cv_event.city) if cv_event.city else ""
    tz = CITY_TIMEZONES.get(city_key)

    if tz is None:
        return dt  # unknown city — return as-is

    # Ensure the datetime is timezone-aware before converting
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(tz)


# Hour at which a "nightlife day" ends. Events starting before this hour
# are treated as continuations of the previous evening rather than belonging
# to the calendar day they technically fall on. The 4–7am band is empty
# in practice (no real shows start there), so the exact value is insensitive;
# 6am is the safe upper bound.
NIGHTLIFE_END_HOUR = 6


def _nightlife_date(dt):
    """Return the 'nightlife date' for a datetime.

    Events starting before NIGHTLIFE_END_HOUR are continuations of the
    previous evening (e.g. a 3am Saturday afters set is really a Friday
    night event). Maps those times to the previous calendar day so that
    a Friday-night-into-Saturday-morning show doesn't collide with the
    actual Saturday-night show at the same venue.

    Exception: exactly 00:00:00 is treated as a date-only timestamp,
    not a real midnight start. Some platforms (notably Gametime) encode
    JSON-LD startDate without a time component, which dateparser parses
    as 00:00:00. Shifting those to the previous day produces off-by-one
    false matches against actual previous-evening events. Real midnight
    launches are rare; bias toward calendar-day correctness.
    """
    if dt is None:
        return None
    d = dt.date() if hasattr(dt, 'date') else dt
    if hasattr(dt, 'hour'):
        if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
            return d  # date-only encoding — keep calendar day
        if dt.hour < NIGHTLIFE_END_HOUR:
            return d - timedelta(days=1)
    return d


def _dates_match(dt1, dt2, tolerance_days: int = 0) -> bool:
    """Check if two dates fall on the same nightlife date.

    CrowdVolt dates should be pre-localized via _localize_cv_date()
    so we can compare with zero tolerance. Other platforms already
    store local times.

    Returns True if either date is missing (allows match through).
    """
    if dt1 is None or dt2 is None:
        return True  # if we can't compare dates, allow the match through
    d1 = _nightlife_date(dt1)
    d2 = _nightlife_date(dt2)
    return abs((d1 - d2).days) <= tolerance_days


def _extract_segments(name: str) -> list[str]:
    """Split a multi-artist event name into individual artist segments.

    Works on the raw name BEFORE extract_artist_name truncation, so that
    "Factory 93 Presents: Seth Troxler" splits into segments before
    " presents" truncation would discard "Seth Troxler".

    Handles patterns like:
    - "Head Trip: Calvin Harris & Swedish House Mafia" → ["head trip", "calvin harris", "swedish house mafia"]
    - "Factory 93 Presents: Seth Troxler" → ["factory 93", "seth troxler"]
    - "Chris Lake + Fisher" → ["chris lake", "fisher"]
    """
    raw = _strip_accents(name.lower())
    # Strip parentheticals first (age qualifiers, day-of-week)
    raw = re.sub(
        r'\s*\((saturday|friday|thursday|sunday|monday|tuesday|wednesday'
        r'|afters|2-day pass|day \d+'
        r'|\d+\+(?:\s*event)?|all\s*ages)\)\s*',
        '', raw, flags=re.IGNORECASE,
    )
    # Split on multi-artist delimiters including "presents:"
    parts = re.split(r'\s*[+&]\s*|\s*:\s*|\s+(?:b2b|x|vs\.?|presents)\s+', raw)
    # Clean each segment through extract_artist_name for suffix stripping
    segments = []
    for p in parts:
        cleaned = extract_artist_name(p)
        if cleaned:
            segments.append(cleaned)
    return segments


def _name_similarity(name1: str, name2: str) -> int:
    """Score 0-100 for how similar two event/artist names are.

    For multi-artist names (containing ":", "+", "&"), also tries matching
    individual segments. For short names (< 8 chars), requires exact match
    to prevent "Baby J" matching "Baby Jane".
    """
    a = extract_artist_name(name1)
    b = extract_artist_name(name2)

    shorter = min(len(a), len(b))
    base_scores = [fuzz.ratio(a, b), fuzz.token_sort_ratio(a, b)]
    best_base = max(base_scores)

    # partial_ratio inflates scores when one name is very short
    # (e.g. "ale" scores 100 against "alex") — only trust it
    # when the shorter name is long enough to be distinctive.
    if min(len(a), len(b)) >= 5:
        partial = fuzz.partial_ratio(a, b)
        # Only trust partial_ratio when it's within 25 points of the
        # base scores.  A big gap means partial matched a substring
        # but the names are otherwise very different.
        if partial - best_base <= 25:
            base_scores.append(partial)

    best = max(base_scores)

    # If the full-name comparison fails, try individual segments.
    # "Factory 93 Presents: Seth Troxler" full-name vs "Seth Troxler"
    # scores poorly, but the segment "seth troxler" matches perfectly.
    if best < MATCH_THRESHOLD:
        for seg_a in _extract_segments(name1):
            for seg_b in _extract_segments(name2):
                if len(seg_a) < 3 or len(seg_b) < 3:
                    continue
                seg_score = max(fuzz.ratio(seg_a, seg_b), fuzz.token_sort_ratio(seg_a, seg_b))
                if seg_score > best:
                    best = seg_score

    # Short names (< 6 chars) are too ambiguous for the standard threshold.
    # "hamdi" vs "handi" scores 80 — close enough to pass 70, but clearly
    # wrong.  Require near-exact (85+) for short names; cap others below
    # the match threshold so callers reject them uniformly.
    if min(len(a), len(b)) < 6 and best < 85:
        return min(best, MATCH_THRESHOLD - 1)

    return best


MATCH_THRESHOLD = 70  # minimum fuzzy score to consider a match

# Event names containing these words are not real tickets
JUNK_KEYWORDS = {"parking", "merch", "merchandise", "shuttle", "camping", "locker"}

# Cities that should be treated as equivalent.
# Source platforms (TickPick, Gametime, etc.) often use neighborhood-level
# city names while CrowdVolt uses metro-level. Each entry maps a
# neighborhood/satellite to its metro canonical form.
CITY_ALIASES = {
    # NYC metro — including Queens neighborhoods and NJ urban core
    "nyc": "new york", "brooklyn": "new york", "queens": "new york",
    "bronx": "new york", "manhattan": "new york", "staten island": "new york",
    "forest hills": "new york",        # Forest Hills (Queens), e.g. neighborhood-only labels
    "long island city": "new york",    # LIC venues like Knockdown Center
    "lic": "new york",
    "flushing": "new york",            # Citi Field area
    "astoria": "new york",
    "harlem": "new york",
    "east rutherford": "new york",     # MetLife
    "newark": "new york",              # Prudential Center
    "jersey city": "new york",
    "hoboken": "new york",
    # LA metro
    "la": "los angeles", "hollywood": "los angeles", "inglewood": "los angeles",
    "pasadena": "los angeles", "east los angeles": "los angeles",
    "santa monica": "los angeles", "long beach": "los angeles",
    "culver city": "los angeles", "burbank": "los angeles",
    "anaheim": "los angeles",          # Honda Center / House of Blues Anaheim
    # Other metros
    "miami beach": "miami", "south beach": "miami", "miami gardens": "miami",
    "sf": "san francisco", "oakland": "san francisco",
    "berkeley": "san francisco",       # Greek Theatre Berkeley
    "arlington": "dallas", "fort worth": "dallas", "irving": "dallas",
    "rosemont": "chicago", "tinley park": "chicago", "hoffman estates": "chicago",
    "foxborough": "boston", "foxboro": "boston",
    "cambridge": "boston",
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
        return True  # missing data — can't disprove, allow through

    c1 = _normalize_city(city1)
    c2 = _normalize_city(city2)

    if c1 == c2 or c1 in c2 or c2 in c1:
        return True

    # Fuzzy fallback for cities with slight name variations
    return fuzz.ratio(c1, c2) >= 80


# Venues that are the same physical place under different platform-facing names.
# Each tuple lists strings that should be treated as equivalent. Substring
# matching is the primary signal; this table covers cross-platform aliases that
# don't share a substring (e.g. "Brooklyn Mirage" is a stage inside the
# "Avant Gardner" complex but they're listed separately on different platforms).
_VENUE_ALIASES = [
    {"brooklyn mirage", "avant gardner"},
    {"msg", "madison square garden"},
    {"the forum", "kia forum"},
    {"radio city", "radio city music hall"},
]


def _venues_match(venue1: str, venue2: str) -> bool:
    """Check if two venue names refer to the same physical place.

    Substring matching first ("Brooklyn Mirage" vs "The Brooklyn Mirage"
    both refer to the same venue), then a small alias table for known
    cross-platform synonyms.

    Returns True when either side is missing — we don't reject when
    upstream data is incomplete, but we do reject when both sides have
    venues and they clearly differ.
    """
    if not venue1 or not venue2:
        return True  # missing data — can't disprove, allow through

    v1 = venue1.lower().strip()
    v2 = venue2.lower().strip()

    if v1 == v2 or v1 in v2 or v2 in v1:
        return True

    for alias_set in _VENUE_ALIASES:
        v1_match = any(a in v1 for a in alias_set)
        v2_match = any(a in v2 for a in alias_set)
        if v1_match and v2_match:
            return True

    return False


def match_seatgeek(
    cv_event: CrowdVoltEvent,
    sg_events: list[SeatGeekEvent],
) -> list[ArbitrageOpportunity]:
    """Find the cheapest matching SeatGeek listing for a CrowdVolt event."""
    best = None
    fee_rate = config.PLATFORM_FEES.get("SeatGeek", 0)
    cv_local_date = _localize_cv_date(cv_event)

    for sg in sg_events:
        if _is_junk(sg.title):
            continue
        score = _name_similarity(cv_event.name, sg.title)
        if score < MATCH_THRESHOLD:
            continue
        if not _dates_match(cv_local_date, sg.event_date):
            continue
        if not _cities_match(cv_event.city, sg.city):
            continue
        if not _venues_match(cv_event.venue, sg.venue):
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
    cv_local_date = _localize_cv_date(cv_event)

    for tp in tp_events:
        if _is_junk(tp.name):
            continue
        score = _name_similarity(cv_event.name, tp.name)
        if score < MATCH_THRESHOLD:
            continue
        if not _dates_match(cv_local_date, tp.event_date):
            continue
        if not _cities_match(cv_event.city, tp.city):
            continue
        if not _venues_match(cv_event.venue, tp.venue):
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
    cv_local_date = _localize_cv_date(cv_event)

    for sh in sh_events:
        if _is_junk(sh.name):
            continue
        score = _name_similarity(cv_event.name, sh.name)
        if score < MATCH_THRESHOLD:
            continue
        if not _dates_match(cv_local_date, sh.event_date):
            continue
        if not _cities_match(cv_event.city, sh.city):
            continue
        if not _venues_match(cv_event.venue, sh.venue):
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
    cv_local_date = _localize_cv_date(cv_event)

    for vs in vs_events:
        if _is_junk(vs.name):
            continue
        score = _name_similarity(cv_event.name, vs.name)
        if score < MATCH_THRESHOLD:
            continue
        if not _dates_match(cv_local_date, vs.event_date):
            continue
        if not _cities_match(cv_event.city, vs.city):
            continue
        if not _venues_match(cv_event.venue, vs.venue):
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


def match_gametime(
    cv_event: CrowdVoltEvent,
    gt_events: list[GametimeEvent],
) -> list[ArbitrageOpportunity]:
    """Find the cheapest matching Gametime listing for a CrowdVolt event."""
    best = None
    fee_rate = config.PLATFORM_FEES.get("Gametime", 0)
    cv_local_date = _localize_cv_date(cv_event)

    for gt in gt_events:
        if _is_junk(gt.name):
            continue
        score = _name_similarity(cv_event.name, gt.name)
        if score < MATCH_THRESHOLD:
            continue
        if not _dates_match(cv_local_date, gt.event_date):
            continue
        if not _cities_match(cv_event.city, gt.city):
            continue
        if not _venues_match(cv_event.venue, gt.venue):
            continue
        if gt.min_price is None:
            continue

        if gt.price_is_all_in:
            all_in = gt.min_price
            estimated = False
        else:
            all_in = gt.min_price * (1 + fee_rate)
            estimated = True

        opp = ArbitrageOpportunity(
            crowdvolt_event=cv_event,
            source_platform="Gametime",
            source_price=round(all_in, 2),
            source_url=gt.url,
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
