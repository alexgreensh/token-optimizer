"""LaunchAgent management must not churn on macOS.

macOS re-fires the "App Background Activity" banner whenever a background item
(a LaunchAgent plist) is rewritten or the agent is re-registered
(bootout+bootstrap). The daemon and keep-warm installers used to rewrite their
plist and bootout+bootstrap unconditionally on every install/repair, so a user
whose ensure-health touches them repeatedly got a banner every time even when the
on-disk agent was already correct. These guard that:

  - a no-op plist write is skipped (the file, and the background item, is untouched);
  - the reload is skipped when nothing changed and the agent is already loaded.

The daemon auto-update reloads via ``launchctl kickstart``, which restarts the
process WITHOUT re-registering the background item, so it does not fire the banner
and keeps its original version-marker staleness check (do not re-add a version
strip there under the belief that kickstart banners -- it does not).
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_write_file_if_changed_skips_identical(tmp_path):
    import measure

    p = tmp_path / "agent.plist"
    p.write_text("SAME", encoding="utf-8")
    changed = measure._write_file_if_changed(p, "SAME")
    assert changed is False
    assert p.read_text() == "SAME"


def test_write_file_if_changed_writes_on_diff(tmp_path):
    import measure

    p = tmp_path / "agent.plist"
    p.write_text("OLD", encoding="utf-8")
    changed = measure._write_file_if_changed(p, "NEW")
    assert changed is True
    assert p.read_text() == "NEW"


def test_write_file_if_changed_creates_missing(tmp_path):
    import measure

    p = tmp_path / "new.plist"
    assert measure._write_file_if_changed(p, "X") is True
    assert p.read_text() == "X"


def test_write_file_if_changed_applies_mode_even_when_unchanged(tmp_path):
    import measure, os, stat

    p = tmp_path / "s.py"
    p.write_text("BODY", encoding="utf-8")
    os.chmod(p, 0o600)
    assert measure._write_file_if_changed(p, "BODY", 0o755) is False
    # POSIX file modes are not meaningful on Windows: os.chmod only toggles the
    # read-only bit, so S_IMODE reports 0o666 rather than 0o755. The mode-application
    # path being exercised here only exists on POSIX.
    if os.name != "nt":
        assert stat.S_IMODE(p.stat().st_mode) == 0o755


def test_daemon_install_guards_reload_when_current():
    """setup_daemon's launchd path must skip the reload when nothing changed and
    the agent is loaded and serving."""
    src = (SCRIPTS / "measure.py").read_text(encoding="utf-8")
    assert "_write_file_if_changed(daemon_script, _generate_daemon_script()" in src
    assert "_write_file_if_changed(PLIST_PATH, _generate_plist())" in src
    assert "not script_changed and not plist_changed" in src
    assert "_launchagent_loaded(DAEMON_LABEL)" in src


def test_keepwarm_install_guards_reload_when_current():
    src = (SCRIPTS / "measure.py").read_text(encoding="utf-8")
    assert "_write_file_if_changed(plist_path, _keepwarm_generate_scheduler_plist())" in src
    assert "not plist_changed and _launchagent_loaded(_KEEPWARM_SCHEDULER_LABEL)" in src


def test_launchagent_loaded_probe_is_time_bounded():
    """The launchctl print probe on the hot path must use a short timeout."""
    src = (SCRIPTS / "measure.py").read_text(encoding="utf-8")
    m = re.search(r'"launchctl", "print", f"gui/\{getuid\(\)\}/\{label\}"\],\s*\n\s*capture_output=True, text=True, timeout=(\d+)', src)
    assert m, "launchctl print probe not found"
    assert int(m.group(1)) <= 2, "probe timeout must be small (<=2s) on the SessionStart hot path"


def test_both_trees_identical():
    a = (SCRIPTS / "measure.py").read_text(encoding="utf-8")
    b = (ROOT / "plugins" / "token-optimizer" / "skills" / "token-optimizer" / "scripts" / "measure.py").read_text(encoding="utf-8")
    assert a == b, "measure.py drifted between install trees"
