"""Conservative admission-product identity. Unknown metadata never means GA."""
import re
import math
from dataclasses import dataclass
from typing import Optional


@dataclass
class SourceListing:
    ticket_type: str
    all_in_price: float
    qty: int
    listing_id: str = ""
    allowed_quantities: tuple = ()
    notes: str = ""
    fees_estimated: bool = False

    def quantities(self) -> tuple:
        values = self.allowed_quantities or (self.qty,)
        return tuple(sorted({q for q in values if isinstance(q, int) and not isinstance(q, bool)
                             and 0 < q <= self.qty}))

    def can_buy(self, qty: int) -> bool:
        # Without split metadata only the full lot is known to be available.
        return qty > 0 and qty <= self.qty and qty in self.quantities()


def is_admission(label: str) -> bool:
    return bool(label and label.strip()) and not re.search(
        r'\b(ferry|parking|shuttle|locker|merch(?:andise)?|camping|coat check|add[ -]?on)\b',
        label, re.I)


def product_key(label: str, event_name: str = "") -> Optional[str]:
    if not is_admission(label):
        return None
    text = label.lower().strip()
    text = re.sub(r'general\s+admission', 'ga', text)
    text = re.sub(r'\bentry anytime\b|\banytime entry\b', 'anytime', text)
    text = re.sub(r'\bsec(?:tion)?\.?\s*(?=\d)', '', text)
    text = re.sub(r'(\d)\s*[- ]\s*day\b', r'\1day', text)
    text = re.sub(r'(\d)\s*:\s*00\s*(am|pm)', r'\1\2', text)
    text = re.sub(r'(\d)\s+(am|pm)', r'\1\2', text)
    # Only day/pass context is inherited from the source event title.
    # Never infer a tier from a venue or an event-wide minimum price.
    day_pattern = r'\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d+[- ]?day)\b'
    # A festival's title/start day must not override an explicitly labeled pass.
    if not re.search(day_pattern, text):
        qualifiers = re.findall(day_pattern, event_name.lower())
        for q in qualifiers:
            q = re.sub(r'[- ]', '', q) if q[0].isdigit() else q
            if q not in text:
                text += ' ' + q
    text = re.sub(r'[^a-z0-9+]+', ' ', text).strip()
    return ' '.join(text.split()) or None


def compatible(left: str, right: str, left_event: str = "", right_event: str = "") -> bool:
    a, b = product_key(left, left_event), product_key(right, right_event)
    return a is not None and a == b


def positive_price(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def restriction_notes(*values) -> str:
    """Keep human notes; numeric rows describe seat location, not restrictions."""
    return ' '.join(str(v).strip() for v in values if v and str(v).strip())


def row_restrictions(row) -> str:
    text = str(row or '').strip()
    if re.search(r'\b(vip|ada|box|pit)\b|\d+\+', text, re.I):
        return text
    if not text or re.fullmatch(r'[A-Za-z]{1,3}|[0-9]+|GA|N/A', text, re.I):
        return ''
    return text


def positive_quantity(value) -> int:
    """No truncation of fractional or boolean quantities."""
    if isinstance(value, bool):
        raise ValueError('boolean quantity')
    number = float(value)
    if not math.isfinite(number) or number <= 0 or int(number) != number:
        raise ValueError('invalid quantity')
    return int(number)
