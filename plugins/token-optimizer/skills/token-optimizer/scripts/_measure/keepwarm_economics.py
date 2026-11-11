"""Conservative return estimates from this installation's session history."""
from bisect import bisect_right
import hashlib
import json


def feature_settings():
    return {'keepwarm': {'env_var': 'TOKEN_OPTIMIZER_KEEPWARM',
            'config_key': 'v5_keepwarm', 'default': True,
            'label': 'Keep-warm', 'what': 'Keeps a paused session cache warm when the expected return exceeds the ping cost.',
            'value': 'Can avoid rebuilding cached context when you return.', 'impact_pct': 0,
            'how': 'Runs only when the safety and cost checks pass.',
            'risk': 'A ping uses plan limits or API credits.', 'risk_level': 'low', 'recommended': True}}


def toggle(facade, enabled):
    """The paused release does not expose an active setting."""
    return False


class GapHistory:
    """Index fully observed windows so replays do not rescan all transcripts."""

    def __init__(self, sessions):
        self.sessions = sessions
        self.index = {}

    def counts(self, now, elapsed, horizon):
        key = (elapsed, horizon)
        if key not in self.index:
            at_risk, returned = [], []
            for session in self.sessions:
                times = sorted(t for t in session if isinstance(t, (int, float)))
                for index, start in enumerate(times):
                    end = times[index + 1] if index + 1 < len(times) else None
                    if end is not None and end <= start + elapsed:
                        continue
                    cutoff = start + elapsed + horizon
                    at_risk.append(cutoff)
                    if end is not None and end <= cutoff:
                        returned.append(cutoff)
            self.index[key] = (sorted(at_risk), sorted(returned))
        at_risk, returned = self.index[key]
        return bisect_right(at_risk, now), bisect_right(returned, now)


def return_probability(sessions, now, elapsed, horizon, minimum=20, prior=10):
    """Estimate returns in the cover window for pauses surviving to elapsed.

    Each session is an ordered sequence of turn timestamps. A terminal pause is
    counted as no return once its whole observation window has elapsed. Windows
    not yet fully observed are excluded, avoiding look-ahead and censor bias.
    Ten prior no-return observations shrink small samples; cold starts return 0.
    """
    if elapsed < 0 or horizon <= 0:
        return 0.0
    if isinstance(sessions, GapHistory):
        candidates, successes = sessions.counts(now, elapsed, horizon)
        return successes / (candidates + prior) if candidates >= minimum else 0.0
    candidates = successes = 0
    for session in sessions:
        times = sorted(t for t in session if isinstance(t, (int, float)) and t <= now)
        for index, start in enumerate(times):
            end = times[index + 1] if index + 1 < len(times) else None
            cutoff = start + elapsed + horizon
            if cutoff > now or (end is not None and end <= start + elapsed):
                continue
            candidates += 1
            if end is not None and end <= cutoff:
                successes += 1
    if candidates < minimum:
        return 0.0
    return successes / (candidates + prior)


def decision(history, now, elapsed, ttl_kind, since, reload_value, ping_cost, phase):
    ttl = 3600 if ttl_kind == '1h' else 300
    cover = max(0, ttl - since) + ttl
    probability = return_probability((history or {}).get('gap_sessions', []),
                                     float(now), elapsed, cover)
    if probability * reload_value <= 10 * ping_cost:
        return {'action': 'stop', 'reason': 'insufficient-return-value', 'phase': phase}
    return {'action': 'ping', 'reason': 'due', 'phase': phase}


def load_history(facade, supplied, records):
    """Read local main-session transcripts only when an active tick needs them."""
    if supplied is not None or not records:
        return supplied
    sessions = []
    try:
        paths = facade._find_all_jsonl_files(30)
        for path, _, _ in paths:
            turns = facade.parse_session_turns(path)
            times = [facade._keepwarm_iso_to_epoch(t.get('timestamp')) for t in turns]
            sessions.append([t for t in times if t is not None])
    except (OSError, ValueError, TypeError, KeyError):
        return {'gap_sessions': []}
    return {'gap_sessions': GapHistory(sessions)}


def with_gaps(stats, sessions):
    return dict(stats, gap_sessions=sessions or [])


def gate_reason(facade):
    try:
        summary = verified_summary(facade, 30, facade.time.time())
        if summary['pings'] and summary['net_usd'] < 0:
            return 'negative-net'
        return 'live-resume-unverified'
    except Exception:
        return 'accounting-unavailable'


def verified_summary(facade, days, now):
    conn = facade._open_trends_db_readonly(timeout=1.0)
    try:
        keys = {row[0] for row in conn.execute(
            "SELECT pause_key FROM savings_events WHERE event_type = 'keepwarm_estimated' AND pause_key IS NOT NULL")}
    finally:
        conn.close()
    rows = [row for row in facade._keepwarm_load_ledger_rows()
            if row.get('status') != 'realized' or row.get('pause_key') in keys]
    return facade.keepwarm_spend_summary(days=days, now=now, rows=rows)


def headline_pool(facade, cutoff):
    """Use the spend ledger, including pings that never led to a return."""
    now = facade.time.time()
    start = facade._keepwarm_iso_to_epoch(cutoff)
    if start is None:
        return None
    try:
        summary = verified_summary(facade, max(0, now - start) / 86400, now)
    except Exception:
        return None
    if not summary['pings'] and not summary['realized_count']:
        return None
    return {'events': summary['pings'] + summary['realized_count'],
            'tokens_saved': 0, 'cost_saved_usd': summary['net_usd']}


def claim_outcome(outcomes, firing_time):
    """Match each completed ping to at most one reserved attempt."""
    for index, outcome_time in enumerate(outcomes):
        if outcome_time >= firing_time - 0.001:
            outcomes.pop(index)
            return True
    return False


def attempt_key(record):
    try:
        session, armed = str(record['session_id']), float(record['ts'])
        attempts = record.get('pings_fired') or []
        if not session or armed <= 0 or not isinstance(attempts, list):
            return None
        raw = json.dumps([session, armed, len(attempts)], separators=(',', ':')).encode()
        return hashlib.sha256(raw).hexdigest()
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def already_attempted(rows, key):
    return not key or any(row.get('status') == 'firing' and row.get('attempt_key') == key
                          for row in rows)


def reconcile_while_paused(facade, reason, now, dry_run):
    if reason in ('negative-net', 'kill-switch', 'settings-disabled', 'consent-declined',
                  'tripwire-off', 'live-resume-unverified', 'accounting-unavailable') and not dry_run:
        try:
            facade.keepwarm_detect_realized(now=now)
        except Exception:
            pass


def transcript_changed(record):
    try:
        from pathlib import Path
        stat = Path(record['transcript_path']).stat()
        return stat.st_size != int(record['byte_size']) or stat.st_mtime != float(record['transcript_mtime'])
    except (KeyError, TypeError, ValueError, OSError):
        return False
