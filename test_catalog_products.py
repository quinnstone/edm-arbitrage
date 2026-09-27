"""Offline regressions for catalog completeness and product-safe comparisons."""
import json
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import crowdvolt
import matcher
import undercut
from ticket_products import SourceListing, compatible, is_admission
from listing_metadata import parse_tickpick, parse_stubhub
from venue_routing import is_seated_event


def listing(tier, price=50, qty=1, **kwargs):
    return crowdvolt.Listing('person', price, price, qty, tier, **kwargs)


def event(**kwargs):
    return crowdvolt.CrowdVoltEvent('test', 'Artist', 'Club', 'New York',
        event_date=datetime.now() + timedelta(days=10), **kwargs)


def base(cv, platform='TickPick'):
    return matcher.ArbitrageOpportunity(cv, platform, 100, 'https://example.com', None, None, None, None)


class ProductTests(unittest.TestCase):
    def test_addons_rejected(self):
        for name in ['Ferry Pass - 2-Day', 'Parking', 'GA + Shuttle', 'Locker']:
            self.assertFalse(is_admission(name))
            self.assertFalse(compatible(name, name))

    def test_equivalent_labels(self):
        self.assertTrue(compatible('General Admission', 'GA'))
        self.assertTrue(compatible('GA (Anytime Entry)', 'GA Entry Anytime'))
        self.assertTrue(compatible('GA (Sunday)', 'GA', '', 'Artist - Sunday Pass Only'))

    def test_distinct_products_never_collapse(self):
        for left, right in [('GA (2-Day)', 'GA (Sunday)'), ('GA Before 12AM', 'GA Anytime'),
                            ('GA Floor', 'GA Bowl'), ('VIP', 'GA'), ('GA+', 'GA'), ('', 'GA'),
                            ('GA before 1AM after 3AM', 'GA before 3AM after 1AM')]:
            self.assertFalse(compatible(left, right), (left, right))

    def test_no_aggregate_price_fallback(self):
        cv = event(bids=[listing('GA', 200)])
        with patch('tickpick.enrich_with_listings', return_value=False):
            opps = matcher._tier_opportunities(base(cv), SimpleNamespace(listings=[], name='Artist'))
            self.assertEqual(len(opps), 1)
            self.assertFalse(opps[0].tier_verified)
            self.assertIsNone(opps[0].profit_vs_bid)

    def test_cheapest_same_product_and_quantity_only(self):
        cv = event(bids=[listing('GA (Sunday)', 120), listing('GA (2-Day)', 300)],
                   asks=[listing('GA (Sunday)', 60), listing('Ferry', 10)])
        source = SimpleNamespace(name='Artist Sunday', listings=[
            SourceListing('GA', 100, 1), SourceListing('GA', 80, 1), SourceListing('GA', 50, 2)])
        with patch('tickpick.enrich_with_listings', return_value=True):
            results = matcher._tier_opportunities(base(cv), source)
        self.assertEqual(len(results), 1)
        self.assertEqual((results[0].crowdvolt_bid, results[0].source_price, results[0].profit_vs_bid), (120, 80, 40))

    def test_unknown_restrictions_and_estimated_cv_payout_excluded(self):
        cv = event(bids=[listing('GA', 200, price_verified=False)])
        source = SimpleNamespace(name='Artist', listings=[SourceListing('GA', 50, 1)])
        with patch('tickpick.enrich_with_listings', return_value=True):
            self.assertEqual(matcher._tier_opportunities(base(cv), source), [])
        cv.bids[0].price_verified = True
        source.listings[0].notes = 'Entry before midnight'
        with patch('tickpick.enrich_with_listings', return_value=True):
            self.assertEqual(matcher._tier_opportunities(base(cv), source), [])

    def test_tickpick_row_restrictions_preserved(self):
        rows = parse_tickpick({'listings': [{'sid':'GA','p':30,'q':2,'r':'Before 11PM'}]})
        self.assertEqual(rows[0].notes, 'Before 11PM')
        self.assertFalse(rows[0].can_buy(1))
        self.assertTrue(rows[0].can_buy(2))

    def test_stubhub_requires_all_in_listing_not_marketing_floor(self):
        self.assertEqual(parse_stubhub({'section':'GA','price':50,'quantity':1}), [])
        rows = parse_stubhub({'items':[{'section':'GA','priceWithFees':'$55.50','availableTickets':2}]})
        self.assertEqual(rows[0].all_in_price,55.5)


class CatalogTests(unittest.TestCase):
    def test_flight_decoder_keeps_full_book_and_unicode(self):
        payload = {'initialBook':{'buy':[], 'sell':[{'ticket_type':'GA','user_first':'A } " B'}]},
                   'tt_data':{'types':[{'name':'GA','uqid':'id'}]}}
        html = '<script>self.__next_f.push([1,' + json.dumps('1:' + json.dumps(payload)) + '])</script>'
        self.assertEqual(crowdvolt._extract_book_json(html), payload['initialBook'])
        self.assertEqual(crowdvolt._extract_summary_book(html)['types'], payload['tt_data']['types'])

    def test_nested_sitemap_dedup_and_fragment(self):
        def response(url, **kwargs):
            if url.endswith('sitemap.xml'):
                xml='<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://www.crowdvolt.com/child.xml</loc></sitemap></sitemapindex>'
            else:
                xml='<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://www.crowdvolt.com/event/a#event</loc></url><url><loc>https://www.crowdvolt.com/event/a</loc></url></urlset>'
            return SimpleNamespace(content=xml, raise_for_status=lambda:None)
        with patch.object(crowdvolt.cf_requests,'get',side_effect=response):
            self.assertEqual(crowdvolt.fetch_sitemap(), ['a'])

    def test_catalog_pagination_and_union(self):
        offsets=[]
        def response(url, **kwargs):
            if 'event/list' in url:
                offset=kwargs['params']['offset'];offsets.append((kwargs['params']['area_uqid'], offset))
                rows=[{'uqid':f'e{i}'} for i in range(offset,min(offset+20,25))]
                return SimpleNamespace(json=lambda:{'events':rows},raise_for_status=lambda:None)
            return SimpleNamespace(text='<a href="/event/in-progress#event">' + json.dumps({'allFeaturedEvents':{'NY':[], 'SF':[]}}),raise_for_status=lambda:None)
        with patch.object(crowdvolt,'fetch_sitemap',return_value=['sitemap-only']), patch.object(crowdvolt.cf_requests,'get',side_effect=response):
            slugs,report=crowdvolt.discover_events()
        self.assertEqual(offsets,[('NY',0),('NY',20),('NY',40),('SF',0),('SF',20),('SF',40)])
        self.assertEqual(len(slugs),27)
        self.assertTrue(report['sources']['catalog_complete'])

    def test_partial_catalog_records_failure(self):
        with patch.object(crowdvolt,'fetch_sitemap',return_value=['sitemap']), patch.object(crowdvolt.cf_requests,'get',side_effect=RuntimeError('blocked')):
            slugs,report=crowdvolt.discover_events()
        self.assertEqual(slugs,['sitemap'])
        self.assertFalse(report['sources']['catalog_complete'])
        self.assertTrue(report['errors'])

    def test_fisher_routed_by_venue_not_slug(self):
        cv=event();cv.slug='fisher-fri-oct-16-forest-hill-new-york';cv.venue='Forest Hills Stadium'
        self.assertTrue(is_seated_event(cv))

    def test_summary_retains_ids_and_flags_estimated_payout(self):
        bids,asks=crowdvolt._listings_from_summary({'max_bid':100,'max_bid_all_in':96,'types':[
            {'name':'GA','uqid':'ga','highest_bid_price':50,'highest_bid_qty':2,'lowest_ask_price':30,'all_in_lowest_ask_price':32,'lowest_ask_qty':1}]})
        self.assertEqual(asks[0].ticket_type_id,'ga')
        self.assertTrue(asks[0].price_verified)
        self.assertFalse(bids[0].price_verified)


class ReverseTests(unittest.TestCase):
    def test_only_requested_platforms_use_matching_ask_without_bids(self):
        cv=event(asks=[listing('GA',50) for _ in range(3)],min_ask=1)
        opps=[]
        for platform in ['TickPick','StubHub','Gametime','VividSeats','SeatGeek']:
            o=base(cv,platform);o.source_price=200;o.crowdvolt_ask=50;o.tier_verified=True
            o.ticket_type='GA';o.quantity=1;o.matched_ask_count=3
            opps.append(o)
        with patch.object(undercut,'get_bid_trend'),patch.object(undercut,'get_ask_trend'),patch.object(undercut,'get_oldest_bid_age_hours'),patch.object(undercut,'get_oldest_ask_age_hours'):
            result=undercut.find_opportunities(opps,[cv])
        self.assertEqual(len(result),1)
        self.assertEqual(result[0].sell_platform,'TickPick')
        self.assertEqual(result[0].cv_ask,50)
        self.assertEqual(result[0].est_profit,130)

    def test_unverified_or_insufficient_same_tier_supply_rejected(self):
        cv=event(asks=[listing('GA')]);o=base(cv);o.source_price=300;o.crowdvolt_ask=50
        self.assertEqual(undercut.find_opportunities([o],[cv]),[])
        o.tier_verified=True;o.matched_ask_count=1
        self.assertEqual(undercut.find_opportunities([o],[cv]),[])


class PipelineTests(unittest.TestCase):
    def test_sellers_only_queries_tickpick_stubhub_only(self):
        import main
        import tempfile
        from contextlib import ExitStack
        from pathlib import Path
        cv=event(asks=[listing('GA')],min_ask=50)
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            stack.enter_context(patch.object(main.crowdvolt,'fetch_all_events',return_value=[cv]))
            stack.enter_context(patch.object(main,'Path',return_value=Path(tmp)/'main.py'))
            from provider_runner import search_provider
            stack.enter_context(patch.object(main, 'run_provider', side_effect=lambda p,e,q,d,**kw: search_provider(p,e,q,d)))
            calls={}
            for name in ['seatgeek','tickpick','stubhub','vividseats','gametime','resident_advisor']:
                calls[name]=stack.enter_context(patch.object(getattr(main,name),'search_events',return_value=[]))
            for name in ['save_bid_snapshot','update_listing_persistence','log_scan_results']:
                stack.enter_context(patch.object(main.undercut,name))
            stack.enter_context(patch.object(main.undercut,'find_opportunities',return_value=[]))
            stack.enter_context(patch.object(main.notifier,'send_summary'))
            stack.enter_context(patch.object(main.notifier,'send_alert'))
            stack.enter_context(patch('skydeck_scanner.scan'))
            stack.enter_context(patch('position_monitor.scan'))
            stack.enter_context(patch.object(main.time,'sleep'))
            main.scan_once()
            for name in ['tickpick','stubhub']:
                self.assertTrue(calls[name].called,name)
            for name in ['seatgeek','vividseats','gametime','resident_advisor']:
                calls[name].assert_not_called()
            self.assertTrue((Path(tmp)/'data'/'comparison_coverage.json').exists())


if __name__ == '__main__':
    unittest.main()
