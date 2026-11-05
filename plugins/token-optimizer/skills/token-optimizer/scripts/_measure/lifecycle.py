"""Definition-only implementations; mutable state belongs to measure."""
from __future__ import annotations
from . import _compat


def _settings_lock(user_initiated=None):
    """Bounded portable lease for settings.json writes.

    Prevents concurrent writes from silently overwriting each other.
    Contenders skip the mutation after 75ms rather than blocking a hook.

    `user_initiated=True` is an explicit user command: it must not
    lose to the cohort throttle -- it opts out of the herd reservation,
    reclaims a RELEASED lease tombstone at once, and retries briefly (2s, or
    the hook deadline when there is one) while a lease is still held.
    """
    _measure_facade = _compat.facade()
    lease_path = _measure_facade.SETTINGS_PATH.parent / ".settings.lease"
    with _measure_facade.lease_lock(
        lease_path,
        deadline=_measure_facade.current_deadline(),
        acquire_timeout=2.0 if user_initiated else 0.075,
        cohort_throttle=not user_initiated,
        reclaim_released=user_initiated,
    ) as acquired:
        yield acquired
