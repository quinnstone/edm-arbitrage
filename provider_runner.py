"""Isolated provider work: deadlines kill scraper processes, not just waiters.

Workers only search and compare. They never send notifications or write
application state. Parent-owned event objects are reconstructed on return.
"""
import importlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from dataclasses import asdict
from datetime import datetime

PROVIDERS = {
    'SeatGeek': 'seatgeek', 'TickPick': 'tickpick', 'StubHub': 'stubhub',
    'VividSeats': 'vividseats', 'Gametime': 'gametime', 'ResidentAdvisor': 'resident_advisor',
}
TIMEOUTS = {'SeatGeek': 45, 'TickPick': 90, 'StubHub': 90,
            'VividSeats': 60, 'Gametime': 60, 'ResidentAdvisor': 60}


def search_provider(platform, cv, queries, date_str):
    """Pure search boundary, also usable with mocked clients in offline tests."""
    import matcher
    module = importlib.import_module(PROVIDERS[platform])
    match = getattr(matcher, 'match_' + PROVIDERS[platform])
    results, errors = [], []
    cv.comparison_status.pop(platform, None)
    seen = set()
    if platform == 'SeatGeek':
        import config
        if not config.SEATGEEK_CLIENT_ID:
            return [], 'not_configured', []
    for query in queries:
        try:
            kwargs = {'city_hint': cv.city} if platform == 'ResidentAdvisor' else {}
            events = module.search_events(query, date_str, **kwargs)
            if events:
                fresh = []
                for source in events:
                    identity = getattr(source, 'url', '')
                    if identity and identity in seen:
                        continue
                    if identity:
                        seen.add(identity)
                    fresh.append(source)
                results.extend(match(cv, fresh))
            if any(o.tier_verified for o in results):
                break
            if results and platform in {'SeatGeek', 'VividSeats', 'ResidentAdvisor'}:
                break
        except Exception as exc:
            errors.append(f'{type(exc).__name__}: {exc}')
    status = cv.comparison_status.get(platform, 'no_results_or_fetch_failure')
    if any(o.tier_verified for o in results):
        status = 'verified_products'
    elif errors:
        status = 'error'
    return results, status, errors


def communicate_bounded(process, payload, timeout):
    """Terminate the process group on timeout, including browser descendants."""
    try:
        return process.communicate(input=payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == 'posix':
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.communicate()
        raise


def run_provider(platform, cv, queries, date_str, timeout=None):
    if platform not in PROVIDERS:
        raise ValueError(f'Unsupported provider: {platform}')
    request = {'platform': platform, 'event': asdict(cv), 'queries': queries, 'date_str': date_str}
    request['event']['event_date'] = cv.event_date.isoformat() if cv.event_date else None
    with tempfile.TemporaryDirectory(prefix='ticket-provider-') as tmp:
        result_path = Path(tmp) / 'result.json'
        process = subprocess.Popen(
            [sys.executable, '-B', str(Path(__file__).resolve()), str(result_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, start_new_session=(os.name == 'posix'))
        try:
            output, _ = communicate_bounded(process, json.dumps(request),
                                             TIMEOUTS[platform] if timeout is None else timeout)
        except subprocess.TimeoutExpired:
            return [], 'timeout', [f'{platform} process exceeded its deadline']
        if output:
            print(output.rstrip(), flush=True)
        if process.returncode != 0 or not result_path.exists():
            return [], 'error', [f'{platform} worker failed (exit {process.returncode})']
        try:
            from matcher import ArbitrageOpportunity
            data = json.loads(result_path.read_text())
            opps = [ArbitrageOpportunity(crowdvolt_event=cv, **row) for row in data['opportunities']]
            return opps, data['status'], data['errors']
        except (ValueError, KeyError, TypeError) as exc:
            return [], 'error', [f'{platform} invalid worker result: {exc}']


def main():
    from crowdvolt import CrowdVoltEvent, Listing
    request = json.load(sys.stdin)
    data = request['event']
    data['event_date'] = datetime.fromisoformat(data['event_date']) if data['event_date'] else None
    for side in ('asks', 'bids'):
        data[side] = [Listing(**row) for row in data[side]]
    cv = CrowdVoltEvent(**data)
    opps, status, errors = search_provider(request['platform'], cv, request['queries'], request['date_str'])
    rows = []
    for opp in opps:
        row = asdict(opp)
        del row['crowdvolt_event']
        rows.append(row)
    Path(sys.argv[1]).write_text(json.dumps({'opportunities': rows, 'status': status, 'errors': errors}, allow_nan=False))


if __name__ == '__main__':
    main()
