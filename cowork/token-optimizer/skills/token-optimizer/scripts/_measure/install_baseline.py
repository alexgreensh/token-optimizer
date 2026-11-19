"""Frozen baseline from the user's own activity around installation."""
from datetime import datetime, timedelta
import json
import math
import shutil

from . import _compat, baseline_provenance


def compute():
    f = _compat.facade()
    try:
        conn = f._init_trends_db()
        f.ensure_activity_view(conn)
        try:
            rows = conn.execute(
                "SELECT date,id,input_tokens,output_tokens,cache_create_5m_tokens,"
                "cache_create_1h_tokens,cache_hit_rate FROM session_activity "
                "WHERE input_tokens IS NOT NULL AND COALESCE(is_sidechain,0)=0 ORDER BY date,id"
            ).fetchall()
            if not rows:
                return None
            evidence = baseline_provenance.install_evidence(f)
            install = datetime.fromisoformat(evidence['date']) if evidence.get('date') else None
            end = install if install and evidence.get('source') in ('snapshot', 'first_event') else None
            start = end - timedelta(days=30) if end else None
            selected = [r for r in rows if start and start <= datetime.fromisoformat(str(r[0])[:10]) < end]
            coverage = 'pre_install'
            if len({r[1] for r in selected}) < 30:
                start = datetime.fromisoformat(str(rows[0][0])[:10]) + timedelta(days=f._BASELINE_ONBOARDING_DAYS)
                end = start + timedelta(days=f._BASELINE_EARLY_WINDOW_DAYS)
                selected = [r for r in rows if start <= datetime.fromisoformat(str(r[0])[:10]) < end]
                coverage = 'post_install'
            vectors = {}
            for row in selected:
                acc = vectors.setdefault(row[1], [0.0] * 5)
                for i in range(4):
                    acc[i] += row[i + 2] or 0
                acc[4] += (row[2] or 0) * (row[6] or 0)
            window_rows = [(*v[:4], v[4] / v[0] if v[0] else 0.0) for v in vectors.values()]
            if len(window_rows) < max(30, f._BASELINE_MIN_STABLE_SESSIONS):
                return None
            vec, used, total = f._winsorized_mean_session(window_rows)
            if not all(math.isfinite(x) for x in vec):
                return None
            try:
                model_rows = conn.execute(
                    'SELECT model, SUM(total_tokens) FROM model_daily WHERE date >= ? AND date < ? GROUP BY model',
                    (start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'))).fetchall()
            except f.sqlite3.Error:
                model_rows = []
            if not model_rows:
                try:
                    raw_rows = conn.execute(
                        'SELECT model_usage_json, all_model_usage_json FROM session_activity '
                        'WHERE date >= ? AND date < ? AND COALESCE(is_sidechain,0)=0',
                        (start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'))).fetchall()
                    for parent, combined in raw_rows:
                        usage = f._safe_json_dict(parent) or f._safe_json_dict(combined)
                        model_rows.extend(usage.items())
                except f.sqlite3.Error:
                    pass
        finally:
            conn.close()
        totals = {}
        for model, tokens in model_rows:
            family = f._normalize_model_name(str(model or ''))
            if family and tokens and tokens > 0 and f._routing_class_rates(model):
                totals[family] = totals.get(family, 0) + tokens
        grand = sum(totals.values())
        shares = {k: v / grand for k, v in totals.items()} if grand else (f._pretool_baseline_mix() or {})
        source = 'robust_earliest' if grand else ('pretool_baseline' if shares else 'unavailable')
        snap = baseline_provenance._read_json(f.SNAPSHOT_DIR / 'snapshot_before.json') or {}
        return {'version': f._BASELINE_VERSION, 'activity_basis': 1,
                'typical_session': dict(zip(('fresh_input', 'cache_write', 'cache_read', 'output'), (round(x, 1) for x in vec))),
                'opus_share': round(float(shares.get('opus', 0)), 4), 'opus_share_source': source,
                'model_shares': {k: round(float(v), 4) for k, v in shares.items() if v},
                'window': {'start': start.strftime('%Y-%m-%d'), 'end': (end - timedelta(days=1)).strftime('%Y-%m-%d'),
                           'sessions_used': used, 'sessions_total': total, 'elapsed': datetime.now() >= end, 'coverage': coverage},
                'method': 'winsorized_mean', 'winsor_pct': f._BASELINE_WINSOR_PCT,
                'structural_overhead_tokens': int((snap.get('totals') or {}).get('estimated_total', 0) or 0)}
    except (f.sqlite3.Error, OSError, ValueError, TypeError, OverflowError):
        return None


def get(freeze=True):
    f = _compat.facade()
    path = f.SNAPSHOT_DIR / 'baseline_state.json'
    try:
        old = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(old, dict) and old.get('version') == f._BASELINE_VERSION and old.get('activity_basis') == 1:
            return old
    except (OSError, ValueError, TypeError):
        old = None
    if isinstance(old, dict) and old.get('version') not in f._BASELINE_KNOWN_VERSIONS:
        return None
    if isinstance(old, dict) and old.get('activity_basis') == 1 and old.get('typical_session') and baseline_provenance.install_evidence(f).get('source') not in ('snapshot', 'first_event'):
        return old
    state = f._compute_baseline_state()
    if not state or not (state.get('window') or {}).get('elapsed'):
        if isinstance(old, dict) and old.get('activity_basis') == 1 and old.get('typical_session'):
            return old
        adopted = f._adopt_orphaned_baseline() if old is None else None
        if adopted and adopted.get('activity_basis') == 1:
            return adopted
        return state
    # A shrinking baseline means the HISTORY thinned, not that the user got cheaper.
    # Keep the incumbent active, park the candidate beside it, and record the
    # rejection rather than letting the reported saving quietly collapse.
    incumbent = old if isinstance(old, dict) and old.get('activity_basis') == 1 and old.get('typical_session') else None
    if incumbent is not None and f._baseline_shrink_is_material(incumbent, state):
        try:
            f._write_baseline_state(path.with_name('baseline_state.candidate.json'), dict(
                state,
                rejected_reason='materially smaller than the existing baseline',
                rejected_at=datetime.now().isoformat(),
                incumbent_cost_usd=round(f._baseline_priced_cost(incumbent), 4),
                candidate_cost_usd=round(f._baseline_priced_cost(state), 4)))
        except (OSError, AttributeError):
            pass
        return incumbent
    if freeze and state['window']['elapsed']:
        try:
            if isinstance(old, dict):
                backup = path.with_name('baseline_state.v%s.json' % old.get('version', 'unknown'))
                if not backup.exists():
                    shutil.copy2(path, backup)
            state['captured_at'] = datetime.now().isoformat()
            state['source'] = 'frozen_from_history'
            f._write_baseline_state(path, state)
        except OSError:
            pass
    return state


def workload_anchor(f, conn, tier):
    """Price the frozen window when its transcript-based anchor is unavailable."""
    try:
        state = json.loads((f.SNAPSHOT_DIR / 'baseline_state.json').read_text(encoding='utf-8'))
        if state.get('version') != f._BASELINE_VERSION:
            return None
        window = state.get('window') or {}
        start, end = window.get('start'), window.get('end')
        if not start or not end:
            return None
        metrics = f._price_parent_window(conn, 'AND date >= ? AND date <= ?', (start, end), tier)
        return {'metrics': metrics, 'label': 'baseline (%s to %s)' % (start, end)} if metrics else None
    except (OSError, ValueError, TypeError):
        return None


def has_install_anchor(f):
    try:
        state = json.loads((f.SNAPSHOT_DIR / 'baseline_state.json').read_text(encoding='utf-8'))
        return state.get('version') == f._BASELINE_VERSION
    except (OSError, ValueError, TypeError, AttributeError):
        return False
