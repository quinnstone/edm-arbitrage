"""Read-only live smoke check. Application output/state is isolated in /tmp."""
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import crowdvolt
import main
import undercut


def run():
    slugs = sys.argv[1:] or [
        'portola-music-festival-sat-sep-26-pier-80-sf',
        'fisher-factory-town-sept-26-miami-florida',
    ]
    events = [crowdvolt.fetch_event(s) for s in slugs]
    events = [e for e in events if e is not None]
    if len(events) != len(slugs):
        raise RuntimeError('A requested live event could not be fetched')
    scratch = Path(tempfile.mkdtemp(prefix='ticket-review-smoke-'))
    print(f'SMOKE_OUTPUT={scratch}', flush=True)
    with patch.object(main.crowdvolt, 'fetch_all_events', return_value=events), \
         patch.object(main, 'Path', return_value=scratch / 'main.py'), \
         patch.object(undercut, '_DATA_DIR', str(scratch)), \
         patch.object(undercut, 'OPPORTUNITY_LOG', str(scratch / 'opportunities.jsonl')):
        result = main.scan_once(dry_run=True)
    print(json.dumps({'candidates': result, 'events': [
        {'slug': e.slug, 'book_source': e.book_source, 'tiers': len(e.ticket_types),
         'comparison_status': e.comparison_status} for e in events]}, indent=2), flush=True)


if __name__ == '__main__':
    run()
