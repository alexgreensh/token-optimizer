"""Never arm a headless or identifiable delegated Claude parent."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'))


def test_headless_parent_and_redirected_descriptors_are_rejected():
    from _measure.keepwarm_safety import interactive_parent
    assert interactive_parent(['claude'], 'ttys001', ['/dev/ttys001', '/dev/ttys001'], [])
    assert not interactive_parent(['claude', '-p', 'hello'], 'ttys001', ['/dev/ttys001', '/dev/ttys001'], [])
    assert not interactive_parent(['claude', '--print'], 'ttys001', ['/dev/ttys001', '/dev/ttys001'], [])
    assert interactive_parent(['claude'], '??', ['/dev/ttys001', '/dev/ttys001'], [])
    assert not interactive_parent(['claude'], 'ttys001', ['/dev/null', '/dev/ttys001'], [])


def test_outsourcerer_ancestor_is_rejected():
    from _measure.keepwarm_safety import interactive_parent
    assert not interactive_parent(['claude'], 'pts/1', ['/dev/pts/1', '/dev/pts/1'],
                                  ['/opt/outsourcerer/bin/outsourcerer.sh run'])
    assert interactive_parent(['claude'], 'pts/1', ['/dev/pts/1', '/dev/pts/1'], ['/usr/bin/tmux'])


def test_native_versioned_claude_binary_is_recognized():
    from _measure.keepwarm_safety import is_claude_command
    assert is_claude_command('/Users/test/.local/share/claude/versions/2.1.322')
    assert is_claude_command('/usr/local/bin/claude')
    assert not is_claude_command('/other/versions/2.1.322')


def test_tmux_delegate_session_is_rejected_when_detectable():
    from _measure.keepwarm_safety import tmux_parent_allowed
    assert not tmux_parent_allowed('42\toutsourcerer-project-123\n', {42, 43})
    assert not tmux_parent_allowed('42\tosrc-job\n', {42, 43})
    assert tmux_parent_allowed('42\tmy-terminal\n', {42, 43})
    assert not tmux_parent_allowed('80\tmy-terminal\n', {42, 43})
