"""Read listing-level metadata from observed public browser responses.

An aggregate offer or an unlabeled price is deliberately not a listing.
Blocked/unrecognized responses return no verified inventory.
"""
import re
from ticket_products import SourceListing, positive_price, row_restrictions, restriction_notes, positive_quantity


def parse_tickpick(payload):
    if not isinstance(payload, dict):
        return []
    result = []
    for item in payload.get('listings', []):
        if not isinstance(item, dict):
            continue
        try:
            label = str(item.get('sid') or '')
            price, qty = float(item['p']), positive_quantity(item['q'])
            if not label or not positive_price(price) or qty <= 0:
                continue
            # Preserve restrictions instead of using section alone.
            notes = restriction_notes(item.get('n'), item.get('notes'), row_restrictions(item.get('r')))
            result.append(SourceListing(label, price, qty,
                                        str(item.get('id') or ''), notes=notes))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return result


def parse_stubhub(payload):
    """Only accept same-object section, explicit all-in price and quantity."""
    result = []
    if isinstance(payload, list):
        for item in payload:
            result.extend(parse_stubhub(item))
    elif isinstance(payload, dict):
        label = payload.get('section') or payload.get('sectionName')
        price = payload.get('priceWithFees')
        qty = payload.get('availableTickets') or payload.get('quantity')
        if isinstance(label, str) and price is not None and qty is not None:
            try:
                if payload.get('currencyCode', 'USD') != 'USD':
                    return []
                if isinstance(price, dict):
                    if price.get('currency') != 'USD':
                        return []
                    price = price.get('amount')
                # Currency must be explicit in the amount or sibling field.
                elif isinstance(price, str):
                    if not price.startswith('$'):
                        return []
                    price = price[1:].replace(',', '')
                elif payload.get('currencyCode') != 'USD':
                    return []
                price, qty = float(price), positive_quantity(qty)
                if positive_price(price) and qty > 0:
                    notes = restriction_notes(payload.get('listingNotes'), payload.get('notes'),
                                              row_restrictions(payload.get('row') or payload.get('rowName')))
                    result.append(SourceListing(label, price, qty,
                                                str(payload.get('id') or ''), notes=notes))
            except (TypeError, ValueError, OverflowError):
                pass
        else:
            for value in payload.values():
                if isinstance(value, (dict, list)):
                    result.extend(parse_stubhub(value))
    return result


def fetch_tickpick(event_url):
    from playwright.sync_api import sync_playwright
    found = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                def on_response(response):
                    if 'listings/internal/event-v2' in response.url and response.status == 200:
                        try:
                            found.extend(parse_tickpick(response.json()))
                        except Exception:
                            pass
                page.on('response', on_response)
                page.goto(event_url, wait_until='networkidle', timeout=30000)
            finally:
                browser.close()
    except Exception as exc:
        print(f'  [TickPick] Tier fetch unavailable: {exc}')
    return found
