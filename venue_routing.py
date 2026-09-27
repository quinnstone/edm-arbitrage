"""Shared venue routing: discover events first, classify using metadata."""
import re

SEATED_VENUES = {
    'barclays center', 'madison square garden', 'msg', 'forest hills stadium',
    'huntington bank pavilion', 'wrigley field',
}


def is_seated_event(event) -> bool:
    venue = (event.venue or '').lower()
    return any(re.search(r'\b' + re.escape(v) + r'\b', venue) for v in SEATED_VENUES) or any(
        t.get('is_section_based') for t in event.ticket_types if isinstance(t, dict))
