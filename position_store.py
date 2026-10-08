"""Optional fresh GitHub read for Discord-managed positions; never silently use stale data."""
import base64
import json
import os
import re

import requests


def remote_enabled():
    return bool(os.getenv('POSITION_REMOTE_REPO'))


def load_remote():
    repo = os.environ['POSITION_REMOTE_REPO']
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', repo):
        raise ValueError('Invalid position repository')
    token = os.getenv('POSITION_READ_TOKEN')
    if not token:
        raise ValueError('Position reader token is not configured')
    response = requests.get(
        f'https://api.github.com/repos/{repo}/contents/positions.json',
        params={'ref': os.getenv('POSITION_REMOTE_BRANCH') or 'main'},
        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                 'X-GitHub-Api-Version': '2022-11-28'}, timeout=8)
    response.raise_for_status()
    document = json.loads(base64.b64decode(response.json()['content'], validate=False))
    if not isinstance(document, dict) or not isinstance(document.get('positions'), list):
        raise ValueError('Remote positions document is malformed')
    return document
