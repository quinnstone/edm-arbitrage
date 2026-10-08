"""Offline routing checks: no network, notifications, or persistent writes."""
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import config
import crowdvolt
import main
import matcher
import undercut


def event(platform='Ticketmaster'):
    row = crowdvolt.Listing('test', 50, 50, 1, 'GA')
    return crowdvolt.CrowdVoltEvent('test', 'Test Artist', 'Test Club', 'New York',
        event_date=datetime.now() + timedelta(days=10), ticket_platform=platform,
        bids=[row, row], asks=[row, row, row], min_ask=50, max_bid=50)


class SpecPlatformTests(unittest.TestCase):
    def test_only_supported_destinations_can_be_candidates(self):
        cv = event()
        with ExitStack() as stack:
            for name in ('get_bid_trend', 'get_ask_trend', 'get_oldest_bid_age_hours', 'get_oldest_ask_age_hours'):
                stack.enter_context(patch.object(undercut, name, return_value=None))
            for platform in ('TickPick', 'StubHub', 'VividSeats', 'Gametime', 'SeatGeek', 'ResidentAdvisor'):
                with self.subTest(platform=platform):
                    opp = matcher.ArbitrageOpportunity(cv, platform, 200, 'https://example.com', 50, 50, 150, -150)
                    result = undercut.find_opportunities([opp], [cv])
                    self.assertEqual(bool(result), platform in {'TickPick', 'StubHub'})

    def test_disallowed_candidates_cannot_leak_into_digest(self):
        with patch.object(config, 'DISCORD_WEBHOOK_URL', 'https://example.invalid'), \
             patch.object(undercut, '_due_digest_slot', return_value=('2030-01-01', 17)), \
             patch.object(undercut, '_slot_already_sent', return_value=False), \
             patch.object(undercut, '_was_recently_alerted', return_value=False), \
             patch.object(undercut.requests, 'post') as post:
            self.assertEqual(undercut.send_alerts([SimpleNamespace(sell_platform=p) for p in
                ('VividSeats', 'Gametime', 'SeatGeek', 'ResidentAdvisor')]), 0)
            post.assert_not_called()

    def test_provider_routing_and_failure_isolation(self):
        for primary in ('DICE', 'Ticketmaster', 'AXS'):
            with self.subTest(primary=primary), ExitStack() as stack:
                stack.enter_context(patch.object(crowdvolt, 'fetch_all_events', return_value=[event(primary)]))
                calls = {}
                for module in (main.seatgeek, main.tickpick, main.stubhub, main.vividseats, main.gametime):
                    calls[module.__name__] = stack.enter_context(patch.object(module, 'search_events', return_value=[]))
                # One provider failing must not prevent the remaining providers.
                calls['stubhub'].side_effect = RuntimeError('test provider failure')
                stack.enter_context(patch.object(main, '_run_with_timeout', side_effect=lambda fn, **kw: fn()))
                for target in ('skydeck_scanner.scan', 'position_monitor.scan', 'notifier.send_summary',
                               'notifier.send_alert', 'undercut.save_bid_snapshot',
                               'undercut.update_listing_persistence', 'undercut.log_scan_results',
                               'undercut.send_alerts'):
                    stack.enter_context(patch(target))
                main.scan_once()
                for name, mock in calls.items():
                    expected = primary != 'DICE' or name in {'tickpick', 'stubhub'}
                    self.assertEqual(mock.called, expected, (primary, name))


if __name__ == '__main__':
    unittest.main()
