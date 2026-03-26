"""Match CrowdVolt events against SeatGeek and Ticketmaster listings."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from thefuzz import fuzz

from crowdvolt import CrowdVoltEvent
from seatgeek import SeatGeekEvent
from ticketmaster import TicketmasterEvent


@dataclass
class ArbitrageOpportunity:
    crowdvolt_event: CrowdVoltEvent
    source_platform: str  # "SeatGeek" or "Ticketmaster"
    source_price: float  # price you'd buy at
    source_url: str
    crowdvolt_ask: Optional[float]  # lowest ask on CrowdVolt (what sellers want)
    crowdvolt_bid: Optional[float]  # highest bid on CrowdVolt (what buyers offer)
    profit_vs_ask: Optional[float]  # if you undercut the lowest ask
    profit_vs_bid: Optional[float]  # if you fill an existing bid


def _extract_artist_name(event_name: str) -> str:
    """Pull the core artist name from an event string.

    Strips common suffixes like venue info, date fragments, and festival qualifiers.
    """
    # Remove common noise words for matching
    name = event_name.lower()
    for noise in [
        " at ", " @ ", " - ", " | ", " tickets", " concert",
        " festival", " music", " live", " tour", " presents",
        " miami", " new york", " brooklyn", " chicago", " los angeles",
        " nyc", " la ",
    ]:
        idx = name.find(noise)
        if idx > 2:  # keep at least a few chars
            name = name[:idx]
    return name.strip()


def _dates_match(dt1, dt2, tolerance_days: int = 1) -> bool:
    """Check if two datetimes are within tolerance of each other."""
    if dt1 is None or dt2 is None:
        return True  # if we can't compare dates, allow the match through
    # Strip timezone info to allow comparison of naive and aware datetimes
    d1 = dt1.replace(tzinfo=None)
    d2 = dt2.replace(tzinfo=None)
    return abs(d1 - d2) <= timedelta(days=tolerance_days)


def _name_similarity(name1: str, name2: str) -> int:
    """Score 0-100 for how similar two event/artist names are."""
    a = _extract_artist_name(name1)
    b = _extract_artist_name(name2)
    return max(
        fuzz.ratio(a, b),
        fuzz.partial_ratio(a, b),
        fuzz.token_sort_ratio(a, b),
    )


MATCH_THRESHOLD = 70  # minimum fuzzy score to consider a match


def match_seatgeek(
    cv_event: CrowdVoltEvent,
    sg_events: list[SeatGeekEvent],
) -> list[ArbitrageOpportunity]:
    """Find SeatGeek listings cheaper than CrowdVolt asks/bids."""
    opportunities = []

    for sg in sg_events:
        # Check name similarity
        score = _name_similarity(cv_event.name, sg.title)
        if score < MATCH_THRESHOLD:
            continue

        # Check date proximity
        if not _dates_match(cv_event.event_date, sg.event_date):
            continue

        # Use lowest SeatGeek price as the buy price
        if sg.lowest_price is None:
            continue

        opp = ArbitrageOpportunity(
            crowdvolt_event=cv_event,
            source_platform="SeatGeek",
            source_price=sg.lowest_price,
            source_url=sg.url,
            crowdvolt_ask=cv_event.min_ask,
            crowdvolt_bid=cv_event.max_bid,
            profit_vs_ask=None,
            profit_vs_bid=None,
        )

        # Calculate profit if you undercut the lowest CrowdVolt ask
        if cv_event.min_ask is not None:
            opp.profit_vs_ask = cv_event.min_ask - sg.lowest_price

        # Calculate profit if you fill an existing CrowdVolt bid
        if cv_event.max_bid is not None:
            opp.profit_vs_bid = cv_event.max_bid - sg.lowest_price

        opportunities.append(opp)

    return opportunities


def match_ticketmaster(
    cv_event: CrowdVoltEvent,
    tm_events: list[TicketmasterEvent],
) -> list[ArbitrageOpportunity]:
    """Find Ticketmaster listings cheaper than CrowdVolt asks/bids."""
    opportunities = []

    for tm in tm_events:
        score = _name_similarity(cv_event.name, tm.name)
        if score < MATCH_THRESHOLD:
            continue

        if not _dates_match(cv_event.event_date, tm.event_date):
            continue

        # Use Ticketmaster min price as the buy price
        if tm.min_price is None:
            continue

        opp = ArbitrageOpportunity(
            crowdvolt_event=cv_event,
            source_platform="Ticketmaster",
            source_price=tm.min_price,
            source_url=tm.url,
            crowdvolt_ask=cv_event.min_ask,
            crowdvolt_bid=cv_event.max_bid,
            profit_vs_ask=None,
            profit_vs_bid=None,
        )

        if cv_event.min_ask is not None:
            opp.profit_vs_ask = cv_event.min_ask - tm.min_price

        if cv_event.max_bid is not None:
            opp.profit_vs_bid = cv_event.max_bid - tm.min_price

        opportunities.append(opp)

    return opportunities
