"""Offline tests for optional Discord storage; no real messages or writes."""
import base64
import json
import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import position_monitor as pm
import position_store


def position(**extra):
    return dict(event='Example Artist',event_date='2099-08-08',venue='Example Club',
                platform='StubHub',listed_price=100,status='open',match_mode='exact',**extra)


def event(name='Example Artist',venue='Example Club',date=None,ask=102):
    return SimpleNamespace(name=name,venue=venue,city='New York',
        event_date=date or datetime(2099,8,9,2),min_ask=ask)


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

    def test_new_revision_resets_old_underwater_cooldown(self):
        p = position(monitor_revision=3)
        pid = pm._position_id(p)
        old = {pid: {"severity": "underwater", "last_alert": datetime.now().isoformat(),
                     "monitor_revision": 1}}
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(pm, 'load_positions', return_value=([p], [])), \
             patch.object(pm, '_load_json', return_value=old), \
             patch.object(pm, '_save_state') as save, \
             patch.object(pm, '_send_alert', return_value=True) as send:
            self.assertEqual(len(pm.scan([event()])), 1)
            send.assert_called_once()
            self.assertEqual(save.call_args.args[0][pid]['monitor_revision'], 3)

    def test_unchanged_revision_keeps_underwater_cooldown(self):
        p = position(monitor_revision=3)
        pid = pm._position_id(p)
        old = {pid: {"severity": "underwater", "last_alert": datetime.now().isoformat(),
                     "monitor_revision": 3}}
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(pm, 'load_positions', return_value=([p], [])), \
             patch.object(pm, '_load_json', return_value=old), \
             patch.object(pm, '_save_state'), patch.object(pm, '_send_alert') as send:
            self.assertEqual(pm.scan([event()]), [])
            send.assert_not_called()

    def test_correction_resets_unresolved_miss_count(self):
        p = position(monitor_revision=2)
        pid = pm._position_id(p)
        old = {pid: {'misses': 1, 'monitor_revision': 1}}
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(pm, 'load_positions', return_value=([p], [])), \
             patch.object(pm, '_load_json', return_value=old), \
             patch.object(pm, '_save_state') as save, patch.object(pm, '_send_alert') as send:
            self.assertEqual(pm.scan([]), [])
            self.assertEqual(save.call_args.args[0][pid], {'monitor_revision': 2, 'misses': 1})
            send.assert_not_called()

    def test_resume_during_scan_invalidates_original_snapshot(self):
        p = position(monitor_revision=1)
        resumed = {**p, 'monitor_revision': 3}
        with patch.dict(os.environ, {'POSITION_REMOTE_REPO': 'o/r'}), \
             patch.object(pm, 'load_positions', side_effect=[([p], []), ([resumed], [])]), \
             patch.object(pm, '_load_json', return_value={}), \
             patch.object(pm, '_save_state'), patch.object(pm, '_send_alert') as send:
            self.assertEqual(pm.scan([event()]), [])
            send.assert_not_called()

    def test_real_command_output_can_resume_and_alert_in_python(self):
        import subprocess
        from pathlib import Path
        script = """
            import {applyCommand,positionId} from './discord_positions/worker.mjs';
            const d={positions:[]};
            applyCommand(d,'add',{event:'Example Artist',date:'2099-08-08',venue:'Wrong Club',
                platform:'StubHub',net_payout:'100'},'1');
            const id=positionId(d.positions[0]);
            applyCommand(d,'stop',{position:id},'2');
            applyCommand(d,'update',{position:id,venue:'Example Club'},'3');
            applyCommand(d,'resume',{position:id},'4');
            console.log(JSON.stringify(d));
        """
        doc = json.loads(subprocess.check_output(['node', '--input-type=module', '-e', script],
                                                cwd=Path(__file__).parent, text=True))
        p = doc['positions'][0]
        pid = pm._position_id(p)
        self.assertEqual(pid, 'example-artist|2099-08-08|stubhub|100')
        with patch.dict(os.environ, {'POSITION_REMOTE_REPO': 'o/r'}), \
             patch.object(position_store, 'load_remote', return_value=doc), \
             patch.object(pm, '_load_json', return_value={pid: {
                 'severity': 'underwater', 'last_alert': datetime.now().isoformat(), 'monitor_revision': 1}}), \
             patch.object(pm, '_save_state'), patch.object(pm, '_send_alert', return_value=True) as send:
            self.assertEqual(len(pm.scan([event()])), 1)
            send.assert_called_once()

    def test_margin_boundaries_unchanged(self):
        for ask,severity in [(95,'healthy'),(95.01,'thin'),(100,'thin'),(100.01,'underwater'),(None,'no_supply')]:
            self.assertEqual(pm.assess(position(),event(ask=ask))['severity'],severity)

if __name__=='__main__': unittest.main()
