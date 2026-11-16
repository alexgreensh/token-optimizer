"""`status-bar` shared savings cache: global reuse, backoff, upgrade seeding,
stale last-good results, atomic replace and pricing snapshot.

Split from test_status_bar_command.py to keep each test module under 1,000 lines;
the sandbox fixture and seed helpers live there.

Run: python3 -m pytest tests/test_status_bar_refresh_cache.py -q
"""
import json
import os
import time

import pytest

from test_status_bar_command import SID_A, SID_B, _three_day_fixture, sb  # noqa: F401  (sb is a fixture)


def test_new_session_reuses_global_savings_and_counts_its_own_session(sb):
    _three_day_fixture(sb)
    a = sb.status_bar_payload(SID_A, sync=True)
    b = sb.status_bar_payload(SID_B)
    assert b['savings']['total_30d_tokens'] == a['savings']['total_30d_tokens']
    assert b['savings']['session_tokens'] == 75000


def test_failed_refresh_backs_off_before_retrying(sb, monkeypatch):
    _three_day_fixture(sb)
    sb.status_bar_payload(SID_A, sync=True)
    sb._status_bar_write_cache(SID_A, None, 'failed')
    spawns = []
    monkeypatch.setattr(sb, 'spawn_detached', lambda *a, **k: spawns.append(a) or object())
    out = sb.status_bar_payload(SID_B)
    assert out['savings_state'] == 'stale'
    assert out['refresh_started'] is False
    assert not spawns


def test_failed_refresh_backoff_grows_and_is_capped(sb):
    from _measure import status_bar_refresh
    for n, seconds in [(1, 60), (2, 120), (10, 3600)]:
        assert status_bar_refresh.retry_delay(n) == seconds
    sb._status_bar_write_cache(SID_A, {'session_tokens': 5}, None)
    sb._status_bar_write_cache(SID_A, None, 'failed')
    sb._status_bar_write_cache(SID_A, None, 'failed again')
    cached = sb._status_bar_read_cache(SID_A)
    assert cached['failures'] == 2
    assert cached['savings']['session_tokens'] == 5


def test_upgrade_uses_newest_legacy_savings_as_stale(sb, monkeypatch):
    _three_day_fixture(sb)
    monkeypatch.setattr(sb, 'spawn_detached', lambda *args, **kwargs: object())
    result = sb.status_bar_payload(SID_A, sync=True)
    global_path = sb._status_bar_cache_path(SID_A)
    legacy = global_path.with_name(SID_A + '.json')
    legacy.write_text(global_path.read_text())
    older = json.loads(global_path.read_text())
    older['computed_at'] -= 60
    older['savings']['total_30d_tokens'] = 1
    global_path.with_name(SID_B + '.json').write_text(json.dumps(older))
    global_path.unlink()
    out = sb.status_bar_payload(SID_B)
    assert out['savings']['total_30d_tokens'] == result['savings']['total_30d_tokens']
    assert out['savings_state'] == 'stale'
    assert out['savings_refresh_failed'] is False


def test_no_database_has_an_honest_reason_on_sync(sb):
    out = sb.status_bar_payload(SID_A, sync=True)
    assert out['savings'] is None
    assert out['savings_reason'] == 'no savings recorded yet'


def test_second_session_never_inherits_first_session_on_query_failure(sb, monkeypatch):
    _three_day_fixture(sb)
    sb.status_bar_payload(SID_A, sync=True)
    monkeypatch.setattr(sb, '_open_trends_db_readonly', lambda: (_ for _ in ()).throw(OSError('busy')))
    out = sb.status_bar_payload(SID_B)
    assert out['savings']['session_tokens'] is None


def test_foreground_poll_uses_saved_session_without_query(sb, monkeypatch):
    from _measure import status_bar_refresh
    _three_day_fixture(sb)
    sb.status_bar_payload(SID_A, sync=True)
    path = status_bar_refresh._session_path(sb, SID_A)
    old = time.time() - 61
    os.utime(path, (old, old))
    calls = []
    monkeypatch.setattr(sb, '_open_trends_db_readonly', lambda: calls.append(1))
    out = sb.status_bar_payload(SID_A)
    assert out['savings']['session_tokens'] == 6500
    assert not calls


def test_cache_replace_retries_and_removes_temp_file(sb, monkeypatch):
    real = sb.os.replace
    attempts = []
    def flaky(source, dest):
        attempts.append(1)
        if len(attempts) == 1:
            raise PermissionError('busy')
        return real(source, dest)
    monkeypatch.setattr(sb.os, 'replace', flaky)
    sb._status_bar_write_cache(SID_A, {'session_tokens': 5}, None)
    assert sb._status_bar_read_cache(SID_A)['savings']['session_tokens'] == 5
    assert len(attempts) == 2
    assert not list(sb._status_bar_dir().glob('*.tmp'))


def test_cache_replace_failure_keeps_existing_number_and_cleans_temp(sb, monkeypatch):
    sb._status_bar_write_cache(SID_A, {'session_tokens': 5}, None)
    monkeypatch.setattr(sb.os, 'replace', lambda *args: (_ for _ in ()).throw(PermissionError('busy')))
    with pytest.raises(PermissionError):
        sb._status_bar_write_cache(SID_A, {'session_tokens': 9}, None)
    assert sb._status_bar_read_cache(SID_A)['savings']['session_tokens'] == 5
    assert not list(sb._status_bar_dir().glob('*.tmp'))


def test_locked_global_cache_keeps_new_result_in_pending_file(sb, monkeypatch):
    sb._status_bar_write_cache(SID_A, {'session_tokens': 5}, None)
    real = sb.os.replace
    def locked(source, dest):
        if str(dest).endswith('global.json'):
            raise PermissionError('busy')
        return real(source, dest)
    monkeypatch.setattr(sb.os, 'replace', locked)
    sb._status_bar_write_cache(SID_A, {'session_tokens': 9}, None)
    assert sb._status_bar_read_cache(SID_A)['savings']['session_tokens'] == 9
    assert not list(sb._status_bar_dir().glob('*.tmp'))


@pytest.mark.parametrize("failure", ["returned", "raised"])
def test_failed_sync_keeps_last_good_as_stale(sb, monkeypatch, failure):
    _three_day_fixture(sb)
    good = sb.status_bar_payload(SID_A, sync=True)["savings"]
    def fail(_sid):
        if failure == "raised":
            raise RuntimeError("compute failed")
        return None, "compute failed"
    monkeypatch.setattr(sb, "_status_bar_savings_or_reason", fail)
    result = sb.status_bar_payload(SID_A, sync=True)
    assert result["savings"] == good
    assert result["savings_state"] == "stale"
    assert result["savings_reason"] is None
    assert sb.status_bar_payload(SID_A)["savings_state"] == "stale"


def test_timeout_keeps_last_good_as_stale(sb, monkeypatch):
    _three_day_fixture(sb)
    good = sb.status_bar_payload(SID_A, sync=True)["savings"]
    token = sb._status_bar_acquire_lock(SID_A)
    monkeypatch.setenv(sb._STATUS_BAR_LOCK_TOKEN_ENV, token)
    monkeypatch.setattr(sb, "_install_hook_budget", lambda _seconds, on_timeout: on_timeout())
    monkeypatch.setattr(sb, "status_bar_payload", lambda *a, **kw: {})
    with pytest.raises(SystemExit):
        sb._status_bar_cli(["--session", SID_A, "--sync", "--release-lock"])
    cached = sb._status_bar_read_cache(SID_A)
    assert cached["savings"] == good
    assert cached["stale"] is True
    assert cached["reason"] is None


def test_cli_exception_keeps_last_good(sb, monkeypatch, capsys):
    _three_day_fixture(sb)
    good = sb.status_bar_payload(SID_A, sync=True)['savings']
    def fail(*_args, **_kwargs):
        raise RuntimeError('failed')
    monkeypatch.setattr(sb, 'status_bar_payload', fail)
    with pytest.raises(SystemExit):
        sb._status_bar_cli(['--session', SID_A])
    result = json.loads(capsys.readouterr().out)
    assert result['savings'] == good
    assert result['savings_state'] == 'stale'


def test_pricing_snapshot_reads_configuration_once_per_compute(sb, monkeypatch):
    from _measure import config_cache
    sb._write_config_flag('pricing_tier', 'anthropic')
    original = config_cache.read
    reads = []
    def counted(*args):
        reads.append(1)
        return original(*args)
    monkeypatch.setattr(config_cache, 'read', counted)
    with config_cache.pricing_snapshot(sb):
        assert all(sb._load_pricing_tier() == 'anthropic' for _ in range(20))
    assert len(reads) == 1
    sb._load_pricing_tier()
    assert len(reads) == 2


def test_no_database_reason_recovers_once_the_database_exists(sb, monkeypatch):
    spawns = []
    monkeypatch.setattr(sb, "spawn_detached", lambda argv, **kw: spawns.append(argv) or object())
    assert not os.path.exists(sb.TRENDS_DB)
    first = sb.status_bar_payload(SID_A, sync=True)
    assert first["savings_reason"] == "no savings recorded yet"
    # Still no database: honest reason, no refresh.
    out = sb.status_bar_payload(SID_A)
    assert (out["savings_state"], out["savings_reason"], out["refresh_started"]) == ("unavailable", "no savings recorded yet", False)
    assert spawns == []
    # Savings start being recorded: the very next poll refreshes, for any session.
    _three_day_fixture(sb)
    for sid, started in ((SID_A, True), (SID_B, False)):
        out = sb.status_bar_payload(sid)
        assert out["savings_state"] == "loading"
        assert out["savings_reason"] == "computing savings"
        # One shared refresh: the second session sees it already running.
        assert out["refresh_started"] is started
    assert len(spawns) == 1
    # The refresh it starts replaces the cached reason with real numbers.
    done = sb.status_bar_payload(SID_A, sync=True)
    assert done["savings_state"] == "fresh" and done["savings"]["session_tokens"] == 6500


def test_pending_files_are_removed_after_the_next_shared_write(sb, monkeypatch):
    sb._status_bar_write_cache(SID_A, {'session_tokens': 5}, None)
    real = sb.os.replace
    def locked(source, dest):
        if str(dest).endswith('global.json'):
            raise PermissionError('busy')
        return real(source, dest)
    monkeypatch.setattr(sb.os, 'replace', locked)
    monkeypatch.setattr(sb.time, 'sleep', lambda s: None)
    for value in (6, 7, 8):
        sb._status_bar_write_cache(SID_A, {'session_tokens': value}, None)
    status_dir = sb._status_bar_dir()
    assert len(list(status_dir.glob('global-pending-*.json'))) == 3
    # A newer pending file (another writer, later clock) must survive.
    newer = status_dir / 'global-pending-future.json'
    newer.write_text(json.dumps({'computed_at': time.time() + 3600, 'savings': {'session_tokens': 99}}), encoding='utf-8')
    monkeypatch.setattr(sb.os, 'replace', real)
    sb._status_bar_write_cache(SID_A, {'session_tokens': 10}, None)
    assert [p.name for p in status_dir.glob('global-pending-*.json')] == ['global-pending-future.json']
    assert sb._status_bar_read_cache(SID_A)['savings']['session_tokens'] == 99


def test_one_transient_failure_shows_no_retry_note_but_two_in_a_row_do(sb, monkeypatch):
    _three_day_fixture(sb)
    monkeypatch.setattr(sb, "spawn_detached", lambda argv, **kw: object())
    sb.status_bar_payload(SID_A, sync=True)
    real = sb._status_bar_savings_or_reason
    monkeypatch.setattr(sb, "_status_bar_savings_or_reason", lambda sid: (None, "savings database busy"))
    once = sb.status_bar_payload(SID_A, sync=True)
    assert (once["savings_state"], once["savings_refresh_failed"]) == ("stale", False)
    assert sb.status_bar_payload(SID_B)["savings_refresh_failed"] is False
    twice = sb.status_bar_payload(SID_A, sync=True)
    assert (twice["savings_state"], twice["savings_refresh_failed"]) == ("stale", True)
    assert sb.status_bar_payload(SID_B)["savings_refresh_failed"] is True
    # Success clears it.
    monkeypatch.setattr(sb, "_status_bar_savings_or_reason", real)
    assert sb.status_bar_payload(SID_A, sync=True)["savings_refresh_failed"] is False
    assert sb.status_bar_payload(SID_B)["savings_refresh_failed"] is False


def test_first_ever_compute_failing_once_is_quiet(sb, monkeypatch):
    _three_day_fixture(sb)
    monkeypatch.setattr(sb, "_status_bar_savings_or_reason", lambda sid: (None, "savings database busy"))
    once = sb.status_bar_payload(SID_A, sync=True)
    assert (once["savings_state"], once["savings_refresh_failed"]) == ("loading", False)
    assert sb.status_bar_payload(SID_A, sync=True)["savings_refresh_failed"] is True
