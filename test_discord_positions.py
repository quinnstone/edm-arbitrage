"""Offline tests for optional Discord storage; no real messages or writes."""
import base64
import json
import os
from pathlib import Path
import subprocess
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import position_monitor as pm
import position_store


def position(**extra):
    return dict(event='Example Artist',event_date='2099-08-08',venue='Example Club',
                platform='StubHub',listed_price=100,status='open',match_mode='exact',**extra)


def event(name='Example Artist',venue='Example Club',date=None,ask=102):
    return SimpleNamespace(name=name,venue=venue,city='New York',
        event_date=date or datetime(2099,8,9,2),min_ask=ask,url='https://example.com/event')


class DiscordPositions(unittest.TestCase):
    def test_legacy_and_explicit_id(self):
        p=position();pid=pm._position_id(p)
        self.assertEqual(pid,'example-artist|2099-08-08|stubhub|100')
        p.update(position_id=pid,listed_price=80)
        self.assertEqual(pm._position_id(p),pid)

    def test_exact_identity_does_not_fuzzy_match(self):
        p=position()
        self.assertIsNone(pm._resolve(p,[event(name='Example Artists')]))
        self.assertIsNone(pm._resolve(p,[event(venue='Other Club')]))
        self.assertIsNone(pm._resolve(p,[event(date=datetime(2099,8,10,2))]))
        self.assertIsNotNone(pm._resolve(p,[event()]))

    def test_ambiguous_exact_events_rejected(self):
        self.assertIsNone(pm._resolve(position(),[event(),event()]))

    def test_remote_disabled_uses_existing_file(self):
        with patch.dict(os.environ,{},clear=True),patch.object(pm,'_load_json',return_value={'positions':[position()]}),patch.object(position_store,'load_remote') as remote:
            self.assertEqual(len(pm.load_positions()[0]),1)
            remote.assert_not_called()

    def test_remote_read_and_closed_filter(self):
        p=position();p['status']='closed'
        with patch.dict(os.environ,{'POSITION_REMOTE_REPO':'owner/repo'}),patch.object(position_store,'load_remote',return_value={'positions':[p]}):
            self.assertEqual(pm.load_positions(),([],[]))

    def test_remote_failure_no_stale_fallback_or_crash(self):
        with patch.dict(os.environ,{'POSITION_REMOTE_REPO':'owner/repo'}),patch.object(position_store,'load_remote',side_effect=RuntimeError('offline')),patch.object(pm,'_load_json') as local,patch.object(pm,'_send_alert') as send,patch.object(pm,'_send_problem_alert') as problem:
            self.assertEqual(pm.scan([event()]),[])
            local.assert_not_called();send.assert_not_called();problem.assert_called_once()

    def test_stop_during_scan_suppresses_alert(self):
        with patch.dict(os.environ,{'POSITION_REMOTE_REPO':'owner/repo'}),patch.object(pm,'load_positions',side_effect=[([position()],[]),([],[])]),patch.object(pm,'_load_json',return_value={}),patch.object(pm,'_save_state'),patch.object(pm,'_send_alert') as send:
            self.assertEqual(pm.scan([event()]),[]);send.assert_not_called()

    def test_update_during_scan_suppresses_old_price_alert(self):
        p=position();new={**p,'listed_price':200,'position_id':pm._position_id(p)}
        with patch.dict(os.environ,{'POSITION_REMOTE_REPO':'owner/repo'}),patch.object(pm,'load_positions',side_effect=[([p],[]),([new],[])]),patch.object(pm,'_load_json',return_value={}),patch.object(pm,'_save_state'),patch.object(pm,'_send_alert') as send:
            self.assertEqual(pm.scan([event()]),[]);send.assert_not_called()

    def test_unchanged_remote_position_still_alerts(self):
        with patch.dict(os.environ,{'POSITION_REMOTE_REPO':'owner/repo'}),patch.object(pm,'load_positions',return_value=([position()],[])),patch.object(pm,'_load_json',return_value={}),patch.object(pm,'_save_state'),patch.object(pm,'_send_alert',return_value=True) as send:
            self.assertEqual(len(pm.scan([event()])),1);send.assert_called_once()

    def test_remote_api_decode(self):
        data={'positions':[position()]}
        resp=SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'content':base64.b64encode(json.dumps(data).encode()).decode()})
        with patch.dict(os.environ,{'POSITION_REMOTE_REPO':'o/r','POSITION_READ_TOKEN':'fake'}),patch.object(position_store.requests,'get',return_value=resp) as get:
            self.assertEqual(position_store.load_remote(),data)
            self.assertEqual(get.call_args.kwargs['timeout'],8)

    def test_blank_workflow_branch_defaults_to_main(self):
        data = {'positions': []}
        resp = SimpleNamespace(raise_for_status=lambda: None,
            json=lambda: {'content': base64.b64encode(json.dumps(data).encode()).decode()})
        with patch.dict(os.environ, {'POSITION_REMOTE_REPO': 'o/r',
                                     'POSITION_READ_TOKEN': 'fake', 'POSITION_REMOTE_BRANCH': ''}), \
             patch.object(position_store.requests, 'get', return_value=resp) as get:
            self.assertEqual(position_store.load_remote(), data)
            self.assertEqual(get.call_args.kwargs['params'], {'ref': 'main'})

    def test_main_continues_after_position_store_outage(self):
        from contextlib import ExitStack
        import main
        from test_spec_platforms import event as scan_event
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {'POSITION_REMOTE_REPO': 'o/r'}))
            stack.enter_context(patch.object(position_store, 'load_remote', side_effect=RuntimeError('offline')))
            stack.enter_context(patch.object(pm, '_send_problem_alert'))
            stack.enter_context(patch.object(main.crowdvolt, 'fetch_all_events', return_value=[scan_event()]))
            for module in (main.seatgeek, main.tickpick, main.stubhub, main.vividseats, main.gametime):
                stack.enter_context(patch.object(module, 'search_events', return_value=[]))
            stack.enter_context(patch.object(main, '_run_with_timeout', side_effect=lambda fn, **kw: fn()))
            stack.enter_context(patch('skydeck_scanner.scan'))
            stack.enter_context(patch('notifier.send_alert'))
            summary = stack.enter_context(patch('notifier.send_summary'))
            stack.enter_context(patch('undercut.save_bid_snapshot'))
            stack.enter_context(patch('undercut.update_listing_persistence'))
            spec = stack.enter_context(patch('undercut.find_opportunities', return_value=[]))
            log = stack.enter_context(patch('undercut.log_scan_results'))
            self.assertEqual(main.scan_once(), 0)
            spec.assert_called_once()
            log.assert_called_once()
            summary.assert_called_once()

    def test_malformed_remote_fails(self):
        resp=SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'content':base64.b64encode(b'{}').decode()})
        with patch.dict(os.environ,{'POSITION_REMOTE_REPO':'o/r','POSITION_READ_TOKEN':'fake'}),patch.object(position_store.requests,'get',return_value=resp):
            with self.assertRaises(ValueError):position_store.load_remote()

    def test_margin_boundaries_unchanged(self):
        for ask,severity in [(95,'healthy'),(95.01,'thin'),(100,'thin'),(100.01,'underwater'),(None,'no_supply')]:
            self.assertEqual(pm.assess(position(),event(ask=ask))['severity'],severity)

    def test_listing_price_fee_math_and_cent_boundaries(self):
        for fee, payout in [(15, 109.65), (12.5, 112.88), (0, 129)]:
            p = {**position(), 'listed_price': 129, 'price_basis': 'listing',
                 'seller_fee_pct': fee, 'seller_fee_source': 'default'}
            for ask, severity in [(payout-5, 'healthy'), (payout-4.99, 'thin'),
                                  (payout, 'thin'), (payout+0.01, 'underwater')]:
                result = pm.assess(p, event(ask=round(ask, 2)))
                self.assertEqual(result['payout'], payout)
                self.assertEqual(result['severity'], severity)
                self.assertTrue(result['fees_estimated'])

    def test_legacy_payout_and_override_are_not_double_deducted(self):
        legacy = pm.assess(position(), event(ask=90))
        self.assertEqual(legacy['payout'], 100)
        self.assertEqual(legacy['margin'], 10)
        self.assertFalse(legacy['fees_estimated'])
        p = position(price_basis='listing', seller_fee_pct=15,
                     seller_fee_source='operator', net_payout_override=95)
        result = pm.assess(p, event(ask=90))
        self.assertEqual(result['payout'], 95)
        self.assertEqual(result['margin'], 5)
        self.assertFalse(result['fees_estimated'])

    def test_invalid_pricing_reports_problem_without_dropping_good_positions(self):
        for delta in [{'price_basis': 'gross'}, {'listed_price': float('nan')},
                      {'listed_price': -5}, {'seller_fee_pct': 100},
                      {'seller_fee_pct': float('inf')}, {'seller_fee_pct': None},
                      {'seller_fee_pct': -1}, {'seller_fee_pct': 1.001},
                      {'seller_fee_source': 'unknown'}, {'net_payout_override': 101},
                      {'net_payout_override': 0}]:
            p = {**position(price_basis='listing', seller_fee_pct=15,
                           seller_fee_source='default'), **delta}
            with self.subTest(delta=delta), patch.dict(os.environ, {}, clear=True), \
                 patch.object(pm, '_load_json', return_value={'positions': [p, position()]}):
                good, problems = pm.load_positions()
                self.assertEqual(good, [position()])
                self.assertEqual(len(problems), 1)

    def test_alert_displays_listing_price_payout_fee_and_estimated_margin(self):
        for override in (None, 90):
            p = position(price_basis='listing', seller_fee_pct=15, seller_fee_source='default')
            if override is not None:
                p['net_payout_override'] = override
            result = pm.assess(p, event(ask=96.75))
            with patch.object(pm.config, 'DISCORD_WEBHOOK_URL', 'https://example.invalid'), \
                 patch.object(pm.requests, 'post') as post:
                self.assertTrue(pm._send_alert(p, event(), result))
                embed = post.call_args.kwargs['json']['embeds'][0]
                fields = {f['name']: f['value'] for f in embed['fields']}
                self.assertEqual(fields['Listing price'], '$100.00 on StubHub')
                self.assertEqual(fields['CV cheapest ask'], '$96.75')
                if override is None:
                    self.assertIn('$85.00', fields['Estimated payout'])
                    self.assertIn('15% default fee estimate', fields['Estimated payout'])
                    self.assertIn('Estimated margin if it sells now', fields)
                    self.assertIn('Based on estimated payout', embed['description'])
                else:
                    self.assertIn('$90.00', fields['Payout (fees already accounted)'])
                    self.assertIn('Margin if it sells now', fields)
                    self.assertNotIn('Based on estimated payout', embed['description'])

    def test_js_saved_price_changes_are_used_by_actual_python_monitor(self):
        script = '''
          import {applyCommand,positionId,pricingSummary} from './discord_positions/worker.mjs';
          const d={positions:[]}, snapshots=[];
          const save=()=>snapshots.push({document:structuredClone(d),message:pricingSummary(d.positions[0])});
          applyCommand(d,'add',{event:'Example Artist',date:'2099-08-08',venue:'Example Club',platform:'TickPick',listing_price:'100'},'1');save();
          const id=positionId(d.positions[0]);
          applyCommand(d,'update',{position:id,listing_price:'120'},'2');save();
          applyCommand(d,'update',{position:id,listing_price:'120',net_payout:'110'},'3');save();
          applyCommand(d,'update',{position:id,listing_price:'130'},'4');save();
          applyCommand(d,'stop',{position:id},'5');save();
          console.log(JSON.stringify(snapshots));
        '''
        snapshots = json.loads(subprocess.check_output(['node', '--input-type=module', '-e', script],
                               cwd=Path(__file__).parent, text=True))
        original_id = snapshots[0]['document']['positions'][0]['position_id']
        for index, payout in enumerate((85, 102, 110, 110.5)):
            snapshot = snapshots[index]
            with patch.dict(os.environ, {}, clear=True), \
                 patch.object(pm, '_load_json', return_value=snapshot['document']):
                positions, problems = pm.load_positions()
            self.assertEqual(problems, [])
            p = positions[0]
            self.assertEqual(pm._position_id(p), original_id)
            result = pm.assess(p, pm._resolve(p, [event(ask=90)]))
            self.assertEqual(result['payout'], payout)
            self.assertEqual(result['margin'], payout - 90)
            self.assertIn(f'payout ${payout:.2f}', snapshot['message'])
            with patch.dict(os.environ, {}, clear=True), \
                 patch.object(pm, 'load_positions', return_value=([p], [])), \
                 patch.object(pm, '_load_json', return_value={}), \
                 patch.object(pm, '_save_state'), patch.object(pm, '_send_alert', return_value=True) as send:
                alerts = pm.scan([event(ask=90)])
                self.assertEqual(bool(alerts), index == 0)
                self.assertEqual(send.called, index == 0)
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(pm, '_load_json', return_value=snapshots[-1]['document']):
            self.assertEqual(pm.load_positions(), ([], []))

    def test_hourly_reminders_and_thin_cooldown_remain_unchanged(self):
        for minutes, expected in [(30, False), (61, True)]:
            state = {'p': {'severity': 'underwater',
                          'last_alert': (datetime.now()-timedelta(minutes=minutes)).isoformat()}}
            self.assertEqual(pm._should_alert('p', 'underwater', state), expected)
        self.assertFalse(pm._should_alert('p', 'thin', {'p': {'severity': 'thin'}}))
        self.assertTrue(pm._should_alert('p', 'underwater', {'p': {'severity': 'thin'}}))

if __name__=='__main__': unittest.main()
