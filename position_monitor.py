"""Open-position risk monitor for naked speculative listings.

The operator lists a ticket on a 3P marketplace (TickPick, StubHub, ...)
that they still need to source from CrowdVolt when it sells. If CV's
cheapest ask rises above the after-fee payout of that listing, a sale
would force them to buy high and lose money.

Positions live in positions.json at the repo ROOT — tracked in git on
purpose. The data/ Actions cache can be evicted without warning, which
is fine for dedup state but unacceptable for records of real money at
risk.

Severity model (margin = payout − CV cheapest ask). New Discord entries
use price_basis="listing": listed_price is the asking price before seller
fees. Payout uses the saved fee percentage or an explicit payout override.
Legacy entries without price_basis already contain a net payout; their
values must never have fees deducted again.

    healthy     margin >= $5     silent
    thin        $0 <= margin < 5 alert once on entry
    underwater  margin < $0      alert on entry + hourly reminder
    no_supply   zero CV asks     alert on entry + daily (can't fulfill)
    unresolved  event not found  alert after 2 consecutive scan misses

Underwater reminders are hourly (operator-set) because the loss can
deepen silently — Bob Moses walked from -$7 to -$59 over 32h in June
with the old 23h cadence firing only twice, both during sleep hours.
no_supply / unresolved stay daily — those signal a system condition
(scrape gap, delisting) rather than active loss escalation.

Escalation always alerts immediately; recovery is silent (operator
wants problem-alerts only). Checked when main.scan_once reaches this monitor.
"""

import json
import os
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional

import requests
from dateutil import parser as dateparser

import config
import matcher
import position_store
from undercut import SELLER_FEES

THIN_MARGIN = 5.0                # dollars — operator-specified
REMINDER_HOURS = 23              # default reminder cadence (no_supply, unresolved)
UNDERWATER_REMINDER_HOURS = 1    # operator-set: keep underwater loud
UNRESOLVED_MISSES = 2            # consecutive scans before "not found" alerts

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
    if p.get("position_id"):
        return p["position_id"]
    base = f"{p.get('event','')}|{p.get('event_date','')}|{p.get('platform','')}|{p.get('listed_price','')}"
    return re.sub(r"[^a-z0-9|.-]+", "-", base.lower())


def load_positions() -> tuple:
    """Returns (open_positions, problems). Malformed entries become
    problems instead of being silently dropped — silent monitoring
    failure on a real position is the dangerous case."""
    data = (position_store.load_remote() if position_store.remote_enabled()
            else _load_json(POSITIONS_FILE, {}))
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
            _position_pricing(p)
            dateparser.parse(str(p["event_date"]))
        except (TypeError, ValueError, InvalidOperation, KeyError):
            problems.append(f"entry #{i} ({p['event']!r}): invalid price, fee, payout override, or event_date")
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

    # Discord additions require exact names/venue and a unique date match.
    # Existing manually-managed positions retain their prior resolver.
    if position.get("match_mode") == "exact":
        matches = []
        for ev in cv_events:
            local = matcher._localize_cv_date(ev)
            if (local is not None and matcher._dates_match(local, target_date)
                    and ev.name.strip().casefold() == position["event"].strip().casefold()
                    and ev.venue.strip().casefold() == position.get("venue", "").strip().casefold()):
                matches.append(ev)
        return matches[0] if len(matches) == 1 else None

    best, best_score = None, 0
    for ev in cv_events:
        cv_local = matcher._localize_cv_date(ev)
        if cv_local is None or not matcher._dates_match(cv_local, target_date):
            continue
        score = matcher._name_similarity(position["event"], ev.name)
        if score >= 70 and score > best_score:
            best, best_score = ev, score
    return best


def _money(value) -> Decimal:
    amount = Decimal(str(value))
    if (not amount.is_finite() or amount <= 0 or amount > 1000000
            or amount != amount.quantize(Decimal("0.01"))):
        raise ValueError("Invalid USD amount")
    return amount


def _position_pricing(position: dict) -> dict:
    """Explicit price basis prevents double-deducting fees on old positions."""
    price = _money(position["listed_price"])
    basis = position.get("price_basis", "net")
    if basis == "net":
        return {"payout": float(price), "fees_estimated": False,
                "pricing_note": "Net payout supplied; fees already accounted"}
    if basis != "listing":
        raise ValueError("Unknown price basis")
    fee = Decimal(str(position["seller_fee_pct"]))
    if (not fee.is_finite() or not 0 <= fee < 100
            or fee != fee.quantize(Decimal("0.01"))):
        raise ValueError("Invalid seller fee")
    source = position.get("seller_fee_source")
    if source not in ("operator", "default"):
        raise ValueError("Missing seller fee source")
    if "net_payout_override" in position:
        payout = _money(position["net_payout_override"])
        if payout > price:
            raise ValueError("Payout exceeds listing price")
        return {"payout": float(payout), "fees_estimated": False,
                "pricing_note": "Your payout override; no further fee deduction"}
    payout = (price * (1 - fee / 100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    fee_label = "your fee setting" if source == "operator" else "default fee estimate"
    return {"payout": float(payout), "fees_estimated": True,
            "pricing_note": f"{float(fee):g}% {fee_label}; actual payout may differ"}


def assess(position: dict, cv_event) -> dict:
    """Compare the net or estimated payout to the existing CV all-in GA ask."""
    pricing = _position_pricing(position)

    if cv_event is None:
        return {**pricing, "severity": "unresolved",
                "min_ask": None, "margin": None}
    if cv_event.min_ask is None:
        return {**pricing, "severity": "no_supply",
                "min_ask": None, "margin": None}

    margin = Decimal(str(pricing["payout"])) - Decimal(str(cv_event.min_ask))
    if margin < 0:
        severity = "underwater"
    elif margin < THIN_MARGIN:
        severity = "thin"
    else:
        severity = "healthy"
    return {**pricing, "severity": severity,
            "min_ask": cv_event.min_ask, "margin": float(round(margin, 2))}


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
    # underwater → hourly reminder; no_supply / unresolved → daily.
    last_alert = entry.get("last_alert")
    if not last_alert:
        return True
    cadence = UNDERWATER_REMINDER_HOURS if severity == "underwater" else REMINDER_HOURS
    try:
        return datetime.now() - datetime.fromisoformat(last_alert) >= timedelta(hours=cadence)
    except (ValueError, TypeError):
        return True


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def scan(cv_events: list, dry_run: bool = False) -> list:
    try:
        positions, problems = load_positions()
    except Exception:
        # Keep the overall arbitrage scan alive; don't misreport storage failure
        # as no positions or resurrect positions from an old checkout.
        print("[Positions] ERROR: current position store unavailable; risk checks skipped", flush=True)
        if not dry_run:
            _send_problem_alert("Position storage unavailable: risk checks could not run. Saved positions were not changed.")
        return []
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
        estimate_note = " (estimated payout)" if result["fees_estimated"] else ""
        print(f"  [Positions] {p['event']!r} [{p['platform']} @ ${p['listed_price']}] "
              f"payout=${result['payout']:.2f} ask="
              f"{('$%.0f' % result['min_ask']) if result['min_ask'] else 'NONE'} "
              f"margin={margin_str} → {severity.upper()}{estimate_note}")

        if _should_alert(pid, severity, state):
            sent = True
            if not dry_run:
                if position_store.remote_enabled():
                    # A command may close/update a position during a long scan.
                    try:
                        latest, _ = load_positions()
                    except Exception:
                        print("[Positions] ERROR: cannot recheck position status; alert skipped", flush=True)
                        continue
                    if not any(_position_id(q) == pid and q == p for q in latest):
                        print(f"[Positions] Changed or closed during scan: {pid}; alert skipped")
                        continue
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
    estimated = result["fees_estimated"]

    fields = [
        {"name": "Listing price" if position.get("price_basis") == "listing" else "Supplied net payout",
         "value": f"${float(position['listed_price']):.2f} on {position['platform']}",
         "inline": True},
        {"name": "Estimated payout" if estimated else "Payout (fees already accounted)",
         "value": f"${result['payout']:.2f}\n{result['pricing_note']}", "inline": True},
    ]
    if result["min_ask"] is not None:
        fields.append({"name": "CV cheapest ask",
                       "value": f"${result['min_ask']:.2f}", "inline": True})
        fields.append({"name": "Estimated margin if it sells now" if estimated else "Margin if it sells now",
                       "value": f"**${result['margin']:+.2f}**", "inline": True})
    if cv_event is not None:
        fields.append({"name": "Links",
                       "value": f"[CrowdVolt]({cv_event.url})", "inline": False})

    payload = {
        "username": "Ticket Arb",
        "embeds": [{
            "title": f"{style['emoji']} Position Risk — {position['event']}",
            "description": ("Based on estimated payout: " if estimated else "") + f"{style['label']}\n"
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
