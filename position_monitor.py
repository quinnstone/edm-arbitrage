"""Open-position risk monitor for naked speculative listings.

The operator lists a ticket on a 3P marketplace (TickPick, StubHub, ...)
that they still need to source from CrowdVolt when it sells. If CV's
cheapest ask rises above the after-fee payout of that listing, a sale
would force them to buy high and lose money.

Positions live in positions.json at the repo ROOT — tracked in git on
purpose. The data/ Actions cache can be evicted without warning, which
is fine for dedup state but unacceptable for records of real money at
risk.

Severity model (margin = payout − CV cheapest ask; payout = listed
price net of the 3P seller fee):

    healthy     margin >= $5     silent
    thin        $0 <= margin < 5 alert once on entry
    underwater  margin < $0      alert on entry + daily reminder
    no_supply   zero CV asks     alert on entry + daily (can't fulfill)
    unresolved  event not found  alert after 2 consecutive scan misses

Escalation always alerts immediately; recovery is silent (operator
wants problem-alerts only). Checked every 15 min via main.scan_once.
"""

import json
import os
import re
from datetime import datetime, timedelta
from typing import Optional

import requests
from dateutil import parser as dateparser

import config
import matcher
from undercut import SELLER_FEES

THIN_MARGIN = 5.0           # dollars — operator-specified
REMINDER_HOURS = 23         # daily reminder cadence for underwater/no-supply
UNRESOLVED_MISSES = 2       # consecutive scans before "not found" alerts

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
POSITIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "positions.json")
STATE_FILE = os.path.join(_DATA_DIR, "position_state.json")

# Severity ranks — alerts fire on any rank increase.
_SEVERITY_RANK = {"healthy": 0, "thin": 1, "underwater": 2, "no_supply": 3,
                  "unresolved": 3}

_SEVERITY_STYLE = {
    "thin":       {"emoji": "⚠️", "color": 0xF1C40F,
                   "label": "Margin thinning"},
    "underwater": {"emoji": "🚨", "color": 0xE74C3C,
                   "label": "UNDERWATER — a sale now loses money"},
    "no_supply":  {"emoji": "🔻", "color": 0x992D22,
                   "label": "NO CV SUPPLY — cannot fulfill at any price"},
    "unresolved": {"emoji": "🔻", "color": 0x992D22,
                   "label": ("No visible CV market — the event has no active "
                             "bids/asks (or was delisted). There is currently "
                             "nothing to source from if your listing sells.")},
}


def _seller_fee(platform: str) -> Optional[float]:
    for name, fee in SELLER_FEES.items():
        if name.lower() == (platform or "").lower():
            return fee
    return None


def _position_id(p: dict) -> str:
    base = f"{p.get('event','')}|{p.get('event_date','')}|{p.get('platform','')}|{p.get('listed_price','')}"
    return re.sub(r"[^a-z0-9|.-]+", "-", base.lower())


def load_positions() -> tuple:
    """Returns (open_positions, problems). Malformed entries become
    problems instead of being silently dropped — silent monitoring
    failure on a real position is the dangerous case."""
    data = _load_json(POSITIONS_FILE, {})
    raw = data.get("positions", [])
    open_positions, problems = [], []
    for i, p in enumerate(raw):
        if not isinstance(p, dict):
            problems.append(f"entry #{i} is not an object")
            continue
        if (p.get("status") or "open").lower() != "open":
            continue
        missing = [k for k in ("event", "event_date", "platform", "listed_price")
                   if not p.get(k)]
        if missing:
            problems.append(f"entry #{i} ({p.get('event','?')!r}) missing: {', '.join(missing)}")
            continue
        if _seller_fee(p["platform"]) is None:
            problems.append(
                f"entry #{i} ({p['event']!r}): unknown platform {p['platform']!r} "
                f"(known: {', '.join(SELLER_FEES)})")
            continue
        try:
            float(p["listed_price"])
            dateparser.parse(str(p["event_date"]))
        except (TypeError, ValueError):
            problems.append(f"entry #{i} ({p['event']!r}): bad listed_price or event_date")
            continue
        open_positions.append(p)
    return open_positions, problems


def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_state(state: dict) -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ---------------------------------------------------------------------------
# Resolution + assessment
# ---------------------------------------------------------------------------

def _resolve(position: dict, cv_events: list):
    """Match a position to a CV event by name fuzz (>=70) + nightlife date."""
    try:
        target_date = dateparser.parse(str(position["event_date"]))
    except (TypeError, ValueError):
        return None

    best, best_score = None, 0
    for ev in cv_events:
        cv_local = matcher._localize_cv_date(ev)
        if cv_local is None or not matcher._dates_match(cv_local, target_date):
            continue
        score = matcher._name_similarity(position["event"], ev.name)
        if score >= 70 and score > best_score:
            best, best_score = ev, score
    return best


def assess(position: dict, cv_event) -> dict:
    """Compute payout / margin / severity for a resolved position."""
    fee = _seller_fee(position["platform"])
    payout = float(position["listed_price"]) * (1 - fee)

    if cv_event is None:
        return {"severity": "unresolved", "payout": round(payout, 2),
                "min_ask": None, "margin": None}
    if cv_event.min_ask is None:
        return {"severity": "no_supply", "payout": round(payout, 2),
                "min_ask": None, "margin": None}

    margin = payout - cv_event.min_ask
    if margin < 0:
        severity = "underwater"
    elif margin < THIN_MARGIN:
        severity = "thin"
    else:
        severity = "healthy"
    return {"severity": severity, "payout": round(payout, 2),
            "min_ask": cv_event.min_ask, "margin": round(margin, 2)}


# ---------------------------------------------------------------------------
# Alert decision — state machine
# ---------------------------------------------------------------------------

def _should_alert(pid: str, severity: str, state: dict) -> bool:
    """Escalation alerts immediately; thin alerts once on entry only;
    underwater/no_supply/unresolved remind daily; recovery is silent."""
    if severity == "healthy":
        return False

    entry = state.get(pid, {})
    prev = entry.get("severity", "healthy")
    prev_rank = _SEVERITY_RANK.get(prev, 0)
    rank = _SEVERITY_RANK[severity]

    if rank > prev_rank:
        return True  # escalation — always immediate

    if rank < prev_rank:
        return False  # improvement (even if still problematic, the daily
                      # reminder cycle below handles continued reminders)

    # Same severity as last scan:
    if severity == "thin":
        return False  # one alert on entry only
    # underwater / no_supply / unresolved → daily reminder
    last_alert = entry.get("last_alert")
    if not last_alert:
        return True
    try:
        return datetime.now() - datetime.fromisoformat(last_alert) >= timedelta(hours=REMINDER_HOURS)
    except (ValueError, TypeError):
        return True


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def scan(cv_events: list, dry_run: bool = False) -> list:
    positions, problems = load_positions()
    state = _load_json(STATE_FILE, {})
    alerts_sent = []

    # Malformed-entry warnings (deduped daily via the same state machine)
    for prob in problems:
        pid = f"_invalid|{prob}"
        if _should_alert(pid, "unresolved", state):
            if not dry_run and _send_problem_alert(prob):
                state[pid] = {"severity": "unresolved",
                              "last_alert": datetime.now().isoformat()}
        print(f"  [Positions] INVALID: {prob}")

    if not positions:
        if problems:
            _save_state(state)
        return []

    print(f"[Positions] monitoring {len(positions)} open position(s)")

    for p in positions:
        pid = _position_id(p)
        cv = _resolve(p, cv_events)
        entry = state.get(pid, {})

        # Transient-miss guard for resolution: the CV catalog scrape
        # occasionally drops 1-3 pages, so require consecutive misses
        # before raising "not found".
        if cv is None:
            misses = entry.get("misses", 0) + 1
            if misses < UNRESOLVED_MISSES:
                state[pid] = {**entry, "misses": misses}
                print(f"  [Positions] {p['event']!r}: not resolved "
                      f"(miss {misses}/{UNRESOLVED_MISSES}, holding)")
                continue
        else:
            entry.pop("misses", None)

        result = assess(p, cv)
        severity = result["severity"]
        margin_str = f"${result['margin']:+.2f}" if result["margin"] is not None else "n/a"
        print(f"  [Positions] {p['event']!r} [{p['platform']} @ ${p['listed_price']}] "
              f"payout=${result['payout']:.2f} ask="
              f"{('$%.0f' % result['min_ask']) if result['min_ask'] else 'NONE'} "
              f"margin={margin_str} → {severity.upper()}")

        if _should_alert(pid, severity, state):
            sent = True
            if not dry_run:
                sent = _send_alert(p, cv, result)
            if sent:
                alerts_sent.append((p, result))
                state[pid] = {"severity": severity,
                              "last_alert": datetime.now().isoformat()}
        else:
            state[pid] = {**state.get(pid, {}), "severity": severity}
            state[pid].pop("misses", None)

    _save_state(state)
    return alerts_sent


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def _send_alert(position: dict, cv_event, result: dict) -> bool:
    if not config.DISCORD_WEBHOOK_URL:
        return False
    style = _SEVERITY_STYLE[result["severity"]]

    fields = [
        {"name": "Listed",
         "value": f"${float(position['listed_price']):.0f} on {position['platform']}",
         "inline": True},
        {"name": "Payout after fee",
         "value": f"${result['payout']:.2f}", "inline": True},
    ]
    if result["min_ask"] is not None:
        fields.append({"name": "CV cheapest ask",
                       "value": f"${result['min_ask']:.0f}", "inline": True})
        fields.append({"name": "Margin if it sells now",
                       "value": f"**${result['margin']:+.2f}**", "inline": True})
    if cv_event is not None:
        fields.append({"name": "Links",
                       "value": f"[CrowdVolt]({cv_event.url})", "inline": False})

    payload = {
        "username": "Ticket Arb",
        "embeds": [{
            "title": f"{style['emoji']} Position Risk — {position['event']}",
            "description": f"{style['label']}\n"
                           f"Event date: {position['event_date']}",
            "color": style["color"],
            "fields": fields,
        }],
    }
    try:
        resp = requests.post(config.DISCORD_WEBHOOK_URL, json=payload,
                             timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
        print(f"  [Positions] Alert sent: {position['event']} → {result['severity']}")
        return True
    except requests.RequestException as e:
        print(f"  [Positions] Alert failed: {e}")
        return False


def _send_problem_alert(problem: str) -> bool:
    if not config.DISCORD_WEBHOOK_URL:
        return False
    payload = {
        "username": "Ticket Arb",
        "embeds": [{
            "title": "🔧 positions.json problem — a position is NOT being monitored",
            "description": problem,
            "color": 0x992D22,
        }],
    }
    try:
        resp = requests.post(config.DISCORD_WEBHOOK_URL, json=payload,
                             timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
        return True
    except requests.RequestException:
        return False
