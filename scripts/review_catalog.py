"""Live catalog validation, with coverage output isolated in a temporary directory."""
from pathlib import Path
import json
import sys
import tempfile
from collections import Counter
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import crowdvolt
from venue_routing import is_seated_event

if __name__ == '__main__':
    scratch=Path(tempfile.mkdtemp(prefix='ticket-review-catalog-'))
    with patch.object(crowdvolt,'Path',return_value=scratch/'crowdvolt.py'):
        events=crowdvolt.fetch_all_events()
    report=json.loads((scratch/'data'/'catalog_coverage.json').read_text())
    print('CATALOG_OUTPUT',scratch)
    print(json.dumps({'sources':report['sources'],'discovery_errors':report['errors'],
        'statuses':dict(Counter(x['status'] for x in report['events'].values())),
        'active':len(events),'with_type_metadata':sum(bool(e.ticket_types) for e in events),
        'books':dict(Counter(e.book_source for e in events)),
        'seated':[(e.slug,e.venue) for e in events if is_seated_event(e)]},indent=2))
