"""Definition-only implementations; mutable state belongs to measure."""
from __future__ import annotations
from . import _compat


def _read_codex_config() -> dict:
    _measure_facade = _compat.facade()
    pass
    path = _measure_facade.RUNTIME_DIR / "config.toml"
    if _measure_facade.tomllib is None:
        return {}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if _measure_facade._codex_config_cache is not None and _measure_facade._codex_config_cache[0] == mtime:
        return _measure_facade._codex_config_cache[1]
    try:
        with path.open("rb") as handle:
            data = _measure_facade.tomllib.load(handle)
        result = data if isinstance(data, dict) else {}
    except (OSError, _measure_facade.tomllib.TOMLDecodeError):
        return {}
    _measure_facade._codex_config_cache = (mtime, result)
    return result
