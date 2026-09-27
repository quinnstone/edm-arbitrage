"""Adversarial regressions found during the second implementation review."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import ExitStack
from datetime import datetime, timedelta

import crowdvolt
import matcher
import notifier
import undercut
from ticket_products import SourceListing, compatible
from listing_metadata import parse_tickpick, parse_stubhub
from test_catalog_products import event, listing, base


class ReviewRegressions(unittest.TestCase):
    def test_seated_rejects_restricted_rows_and_nonfinite_prices(self):
        import seated_scanner as seated
        cv = event(bids=[listing('GA', 100)])
        for row, price in [('ADA', 50), ('21+', 50), ('Limited view', 50), ('A', float('nan'))]:
            source = seated.SectionListing('GA', 'ga', row, price, 1)
            self.assertEqual(seated.find_section_arbitrage(cv, [source], 'https://example.com'), [])
        source = seated.SectionListing('GA', 'ga', 'A', 50, 1)
        self.assertEqual(len(seated.find_section_arbitrage(cv, [source], 'https://example.com')), 1)

    def test_explicit_day_wins_over_festival_start_day(self):
        self.assertTrue(compatible('GA Sunday', 'GA Sunday', 'Festival Saturday', 'Festival Sunday'))

    def test_restrictions_do_not_disappear_from_stubhub_row(self):
        rows=parse_stubhub({'section':'GA','row':'VIP Before 11PM','priceWithFees':'$50','availableTickets':1})
        self.assertTrue(not rows or not compatible('GA',rows[0].ticket_type+' '+rows[0].notes))

    def test_non_usd_dollar_symbol_rejected(self):
        self.assertEqual(parse_stubhub({'section':'GA','priceWithFees':'$50','currencyCode':'CAD','availableTickets':1}),[])

    def test_nan_and_infinity_rejected(self):
        for value in [float('nan'),float('inf')]:
            self.assertEqual(parse_tickpick({'listings':[{'sid':'GA','p':value,'q':1}]}),[])

    def test_known_split_quantity_is_used(self):
        cv=event(bids=[listing('GA',100,qty=2)])
        source=SimpleNamespace(name='Artist',listings=[SourceListing('GA',50,4,allowed_quantities=(2,4))])
        with patch('tickpick.enrich_with_listings',return_value=True):
            opps=matcher._tier_opportunities(base(cv),source)
        self.assertEqual(len(opps),1)
        self.assertEqual(opps[0].quantity,2)

    def test_flight_object_can_span_chunks(self):
        raw='0:'+json.dumps({'initialBook':{'buy':[],'sell':[{'ticket_type':'GA'}]}})
        chunks=[raw[:35],raw[35:]]
        html=''.join('self.__next_f.push([1,'+json.dumps(c)+'])' for c in chunks)
        self.assertEqual(crowdvolt._extract_book_json(html),{'buy':[],'sell':[{'ticket_type':'GA'}]})

    def test_alert_best_means_best_profit_not_cheapest_different_product(self):
        cv=event()
        ga=base(cv);ga.ticket_type='GA';ga.quantity=1;ga.tier_verified=True
        ga.source_price=10;ga.crowdvolt_bid=11;ga.profit_vs_bid=1
        vip=base(cv);vip.ticket_type='VIP';vip.quantity=1;vip.tier_verified=True
        vip.source_price=100;vip.crowdvolt_bid=150;vip.profit_vs_bid=50
        embed=notifier._format_opportunity([ga,vip])
        field=next(f for f in embed['fields'] if f['name']=='Best Arbitrage')
        self.assertIn('VIP',field['value'])

    def test_logging_keeps_all_products_and_premium_only_events(self):
        cv=event(bids=[listing('VIP',200)])
        opps=[]
        for tier in ['GA','VIP']:
            o=base(cv);o.ticket_type=tier;o.quantity=1;o.tier_verified=True;opps.append(o)
        with tempfile.TemporaryDirectory() as tmp, patch.object(undercut,'_DATA_DIR',tmp),patch.object(undercut,'OPPORTUNITY_LOG',str(Path(tmp)/'log.jsonl')):
            undercut.log_scan_results([cv],opps,0)
            rows=[json.loads(x) for x in (Path(tmp)/'log.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows),1)
        self.assertEqual({p['ticket_type'] for p in rows[0]['product_comparisons']},{'GA','VIP'})




class DataBoundaryTests(unittest.TestCase):
    def test_reserved_blocked_malformed_rows_do_not_reappear(self):
        rows=[{'ticket_type':'GA','qty':1,'price':50,'all_in_price':55,'in_cart':True},
              {'ticket_type':'GA','qty':1,'price':50,'all_in_price':55,'is_pending':True},
              {'ticket_type':'GA','qty':1,'price':50,'all_in_price':float('inf')},
              {'ticket_type':'GA','qty':1.5,'price':50,'all_in_price':55},
              {'ticket_type':'GA','qty':2,'qty_open_to_cart':1,'price':50,'all_in_price':55,'uqid':'good'}]
        parsed=crowdvolt._parse_listings(rows,side='sell')
        self.assertEqual([(x.listing_id,x.qty) for x in parsed],[('good',1)])

    def test_empty_authoritative_book_does_not_use_stale_summary(self):
        data={'name':'Artist','doors_open_time':'2026-12-01 21:00:00','venue':'Club','area_name':'New York',
              'initialBook':{'buy':[],'sell':[]},
              'tt_data':{'types':[{'name':'GA','lowest_ask_price':10,'all_in_lowest_ask_price':11,'lowest_ask_qty':1}]}}
        html='<script>self.__next_f.push([1,'+json.dumps(json.dumps(data))+'])</script>'
        response=SimpleNamespace(status_code=200,text=html)
        with patch.object(crowdvolt.cf_requests,'get',return_value=response):
            cv=crowdvolt.fetch_event('test')
        self.assertEqual(cv.asks,[])
        self.assertEqual(cv.book_source,'embedded')

    def test_same_seller_multiple_rows_not_independent_supply(self):
        asks=[listing('GA',50,seller_id='same') for _ in range(3)]
        cv=event(asks=asks)
        source=SimpleNamespace(name='Artist',listings=[SourceListing('GA',100,1)])
        with patch('tickpick.enrich_with_listings',return_value=True):
            opps=matcher._tier_opportunities(base(cv),source)
        self.assertEqual(opps[0].matched_ask_count,1)
        self.assertEqual(undercut.find_opportunities(opps,[cv]),[])

    def test_missing_event_identity_cannot_be_verified(self):
        import tickpick
        cv=event(bids=[listing('GA',200)])
        src=tickpick.TickPickEvent('Artist','','',None,100,150,'https://example.com',
                                  [SourceListing('GA',100,1)],True)
        opps=matcher.match_tickpick(cv,[src])
        self.assertTrue(opps)
        self.assertFalse(any(o.tier_verified for o in opps))
        self.assertIsNone(opps[0].profit_vs_bid)

    def test_gametime_uses_priced_lot_and_keeps_disclosures(self):
        import gametime
        payload={'listings':[{'id':'x','section':'GA','row':'GA','lots':[1,2,3,4],
            'priced_from_lot':2,'price':{'total':5500},'spot':{'disclosures':['Before 11PM']}}]}
        response=SimpleNamespace(status_code=200,json=lambda:payload)
        with patch.object(gametime.requests,'get',return_value=response):
            rows=gametime.fetch_listings('id')
        self.assertEqual(rows[0].qty,4)
        self.assertEqual(rows[0].allowed_quantities,(2,))
        self.assertIn('Before 11PM',rows[0].notes)

    def test_unknown_gametime_priced_lot_not_guessed(self):
        import gametime
        response=SimpleNamespace(status_code=200,json=lambda:{'listings':[{'section':'GA','lots':[2,4],'price':{'total':5500}}]})
        with patch.object(gametime.requests,'get',return_value=response):
            self.assertEqual(gametime.fetch_listings('id'),[])

    def test_provider_results_without_aggregate_price_are_usable(self):
        import stubhub
        cv=event(bids=[listing('GA',200)])
        src=stubhub.StubHubEvent('Artist','Club','New York',matcher._localize_cv_date(cv),None,'https://example.com',
                                listings=[SourceListing('GA',100,1)])
        opps=matcher.match_stubhub(cv,[src])
        self.assertEqual(opps[0].profit_vs_bid,100)


class ProcessBoundaryTests(unittest.TestCase):
    def test_real_worker_serialization_without_network(self):
        import provider_runner,os
        cv=event()
        with patch.dict(os.environ,{'SEATGEEK_CLIENT_ID':''}):
            opps,status,errors=provider_runner.run_provider('SeatGeek',cv,['Artist'],None,timeout=10)
        self.assertEqual((opps,status,errors),([], 'not_configured', []))

    def test_timeout_terminates_process(self):
        import subprocess,sys,os,time
        import provider_runner
        p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)'],stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,start_new_session=os.name=='posix')
        start=time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            provider_runner.communicate_bounded(p,'',0.1)
        self.assertIsNotNone(p.poll())
        self.assertLess(time.monotonic()-start,3)


class FullPipelineTests(unittest.TestCase):
    def setup_scan(self, stack, tmp, events, runner):
        import main
        stack.enter_context(patch.object(main.crowdvolt,'fetch_all_events',return_value=events))
        stack.enter_context(patch.object(main,'Path',return_value=Path(tmp)/'main.py'))
        stack.enter_context(patch.object(main,'run_provider',side_effect=lambda p,e,q,d,**kw: runner(p,e,q,d)))
        stack.enter_context(patch.object(main.time,'sleep'))
        stack.enter_context(patch.object(undercut,'_DATA_DIR',tmp))
        stack.enter_context(patch.object(undercut,'OPPORTUNITY_LOG',str(Path(tmp)/'log.jsonl')))
        for name in ['save_bid_snapshot','update_listing_persistence']:
            stack.enter_context(patch.object(undercut,name))
        for name in ['get_bid_trend','get_ask_trend','get_oldest_bid_age_hours','get_oldest_ask_age_hours']:
            stack.enter_context(patch.object(undercut,name,return_value=None))
        stack.enter_context(patch('skydeck_scanner.scan'))
        stack.enter_context(patch('position_monitor.scan'))
        return main

    def test_product_profit_reaches_alerts_digest_and_log(self):
        import tickpick,main
        from provider_runner import search_provider
        cv=event(bids=[listing('GA',200)],asks=[listing('GA',20,seller_id=str(i)) for i in range(3)],min_ask=20,max_bid=200)
        src=tickpick.TickPickEvent('Artist','Club','New York',matcher._localize_cv_date(cv),100,100,'https://example.com',
                                  [SourceListing('GA',100,1)],True)
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            self.setup_scan(stack,tmp,[cv],search_provider)
            for name in ['seatgeek','stubhub','vividseats','gametime','resident_advisor']:
                stack.enter_context(patch.object(getattr(main,name),'search_events',return_value=[]))
            stack.enter_context(patch.object(main.tickpick,'search_events',return_value=[src]))
            send=stack.enter_context(patch.object(main.notifier,'send_alert',return_value=True))
            digest=stack.enter_context(patch.object(undercut,'send_alerts',return_value=1))
            summary=stack.enter_context(patch.object(main.notifier,'send_summary'))
            self.assertEqual(main.scan_once(),1)
            self.assertEqual(send.call_args.args[0][0].profit_vs_bid,100)
            self.assertEqual(digest.call_args.args[0][0].est_profit,70)
            self.assertEqual(summary.call_args.args[1],1)
            rows=[json.loads(x) for x in (Path(tmp)/'log.jsonl').read_text().splitlines()]
            self.assertEqual(rows[0]['product_comparisons'][0]['quantity'],1)
            self.assertEqual(rows[0]['comparison_status']['TickPick'],'verified_products')

    def test_late_provider_exception_preserves_earlier_opportunity(self):
        cv=event(bids=[listing('GA',200)])
        o=base(cv);o.ticket_type='GA';o.quantity=1;o.tier_verified=True;o.crowdvolt_bid=200;o.profit_vs_bid=100
        def runner(platform,*args):
            if platform=='TickPick':return [o],'verified_products',[]
            if platform=='StubHub':raise RuntimeError('broken browser')
            return [],'no_results_or_fetch_failure',[]
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            main=self.setup_scan(stack,tmp,[cv],runner)
            send=stack.enter_context(patch.object(main.notifier,'send_alert',return_value=False))
            summary=stack.enter_context(patch.object(main.notifier,'send_summary'))
            self.assertEqual(main.scan_once(),1)
            send.assert_called_once()
            self.assertEqual(summary.call_args.args[1],0) # failed delivery is not a sent alert
            self.assertEqual(summary.call_args.args[2],1)
            self.assertEqual(cv.comparison_status['StubHub'],'error')

    def test_dice_reverse_only_and_dry_run_has_no_notifications(self):
        cv=event(ticket_platform='DICE',bids=[listing('GA',200)],asks=[listing('GA',20) for _ in range(3)])
        calls=[]
        def runner(platform,*args):
            calls.append(platform)
            return [],'tier_metadata_unavailable',[]
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            main=self.setup_scan(stack,tmp,[cv],runner)
            alerts=stack.enter_context(patch.object(main.notifier,'send_alert'))
            summary=stack.enter_context(patch.object(main.notifier,'send_summary'))
            digest=stack.enter_context(patch.object(undercut,'send_alerts'))
            main.scan_once(dry_run=True)
            self.assertEqual(set(calls),{'TickPick','StubHub'})
            alerts.assert_not_called();summary.assert_not_called();digest.assert_not_called()

    def test_seated_handoff_processes_fisher_metadata(self):
        import seated_scanner
        cv=event(bids=[listing('General Admission',100)],max_bid=100)
        cv.slug='fisher-fri-oct-16-forest-hill-new-york';cv.venue='Forest Hills Stadium'
        source=SimpleNamespace(url='https://example.com')
        row=seated_scanner.SectionListing('GA','ga','GA',50,1)
        with patch.object(seated_scanner.crowdvolt,'fetch_all_events',return_value=[cv]), \
             patch.object(seated_scanner,'_find_tickpick_event',return_value=source), \
             patch.object(seated_scanner,'fetch_section_listings',return_value=[row]), \
             patch.object(seated_scanner.time,'sleep'),patch.object(seated_scanner,'send_alert') as send:
            self.assertEqual(seated_scanner.scan_once(dry_run=True),1)
            send.assert_not_called()


class OperationalTests(unittest.TestCase):
    def test_budget_exhaustion_is_visible_and_makes_no_provider_calls(self):
        helper=FullPipelineTests()
        cv=event(asks=[listing('GA',20)])
        def runner(*args):raise AssertionError('provider must not run after deadline')
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            main=helper.setup_scan(stack,tmp,[cv],runner)
            stack.enter_context(patch.object(main.config,'COMPARISON_BUDGET_SECONDS',0))
            main.scan_once(dry_run=True)
        self.assertEqual(cv.comparison_status['TickPick'],'scan_budget_exhausted')
        self.assertEqual(cv.comparison_status['StubHub'],'scan_budget_exhausted')

    def test_notification_fields_fit_discord_with_many_products(self):
        cv=event();opps=[]
        for i in range(60):
            o=base(cv);o.ticket_type='GA '+str(i);o.quantity=1;o.tier_verified=True
            o.crowdvolt_bid=120;o.profit_vs_bid=20;o.source_url='https://example.com/'+('x'*150)+str(i)
            opps.append(o)
        embed=notifier._format_opportunity(opps)
        self.assertTrue(all(len(f['value'])<=1024 for f in embed['fields']))
        self.assertEqual(notifier._format_opportunity([base(cv)]),None)

    def test_catalog_worker_failure_does_not_abort_other_events(self):
        def fetch(slug):
            if slug=='bad':raise ValueError('bad record')
            if slug=='empty':return event()
            cv=event(asks=[listing('GA')]);cv.slug=slug;return cv
        with tempfile.TemporaryDirectory() as tmp,patch.object(crowdvolt,'Path',return_value=Path(tmp)/'crowdvolt.py'), \
             patch.object(crowdvolt,'discover_events',return_value=(['bad','empty','active'],{'sources':{},'errors':[]})), \
             patch.object(crowdvolt,'fetch_event',side_effect=fetch),patch.object(crowdvolt.time,'sleep'):
            result=crowdvolt.fetch_all_events()
            report=json.loads((Path(tmp)/'data'/'catalog_coverage.json').read_text())
        self.assertEqual(len(result),1)
        self.assertEqual(report['events']['bad']['status'],'parse_or_fetch_error')
        self.assertEqual(report['events']['empty']['status'],'empty')
        self.assertEqual(report['events']['active']['status'],'active')

    def test_real_worker_populated_result_roundtrip(self):
        import provider_runner,subprocess,sys
        from dataclasses import asdict
        cv=event();opp=base(cv);opp.ticket_type='GA';opp.quantity=2;opp.tier_verified=True
        row=asdict(opp);del row['crowdvolt_event']
        payload={'opportunities':[row],'status':'verified_products','errors':[]}
        real_popen=subprocess.Popen
        def fixture_process(args,**kwargs):
            code='import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('+repr(json.dumps(payload))+')'
            return real_popen([sys.executable,'-c',code,args[-1]],**kwargs)
        with patch.object(provider_runner.subprocess,'Popen',side_effect=fixture_process):
            results,status,errors=provider_runner.run_provider('TickPick',cv,['Artist'],None,timeout=10)
        self.assertIs(results[0].crowdvolt_event,cv)
        self.assertEqual(results[0].quantity,2)
        self.assertEqual(status,'verified_products')
        self.assertEqual(errors,[])

    def test_timeout_closes_descendant_stdout_pipe(self):
        import provider_runner,subprocess,sys,os,time
        if os.name!='posix':self.skipTest('POSIX process-group regression')
        code="import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)']); print('ready',flush=True); time.sleep(20)"
        p=subprocess.Popen([sys.executable,'-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,text=True,start_new_session=True)
        self.assertEqual(p.stdout.readline().strip(),'ready')
        start=time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            provider_runner.communicate_bounded(p,'',0.1)
        self.assertLess(time.monotonic()-start,3)

    def test_stubhub_response_can_supply_price_without_dom_floor(self):
        import stubhub
        rows=[SourceListing('GA',75,1)]
        class Page:
            def on(self,*args):pass
            def goto(self,*args,**kwargs):pass
            def wait_for_timeout(self,*args):pass
            def content(self):return 'x'*6000
            def wait_for_function(self,*args,**kwargs):pass
            def inner_text(self,*args):raise AssertionError('should use typed listing price')
        self.assertEqual(stubhub._fetch_event_price(Page(),'https://www.stubhub.com/example/event/12345/',rows),(75,True))

    def test_partial_city_failure_keeps_other_cities(self):
        def response(url,**kwargs):
            if 'event/list' not in url:
                return SimpleNamespace(text=json.dumps({'allFeaturedEvents':{'first':[],'second':[]}}),raise_for_status=lambda:None)
            area=kwargs['params']['area_uqid'];offset=kwargs['params']['offset']
            if area=='first':raise RuntimeError('blocked')
            return SimpleNamespace(json=lambda:{'events':[{'uqid':'from-second'}] if offset==0 else []},raise_for_status=lambda:None)
        with patch.object(crowdvolt,'fetch_sitemap',return_value=['from-sitemap']),patch.object(crowdvolt.cf_requests,'get',side_effect=response):
            slugs,report=crowdvolt.discover_events()
        self.assertEqual(set(slugs),{'from-sitemap','from-second'})
        self.assertFalse(report['sources']['catalog_complete'])
        self.assertEqual(report['sources']['catalog_areas']['second'],1)


if __name__=='__main__':unittest.main()
