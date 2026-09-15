"""Codex command rewrite adapter. Execute once, retain failures and archive originals.

Security model (hardened relative to the original design):

- ``tool_input`` execution-context fields (``shell``, ``env``, ``cwd`` and any
  sibling) are IGNORED, never propagated. A prompt-injected model could
  otherwise point this auto-approved wrapper at an attacker-named interpreter
  or redirect a confined read to a sensitive directory. Both the rewrite
  quoting and the ``--run`` exec always use runtime defaults resolved here, and
  ``updatedInput`` carries only the rewritten ``command`` so no model-supplied
  field can survive into the tool call. ``--run`` also refuses to execute when
  the ambient working directory differs from the rewrite-time cwd recorded in
  the plan.
- ``permissionDecision: 'allow'`` is emitted ONLY for commands whose file
  operands are provably confined to the working directory (non-hidden,
  non-sensitive, non-absolute, glob-free, no symlink escape). Eligible but
  non-confined commands are passed through UNREWRITTEN -- no updatedInput and
  no decision -- so Codex's own consent evaluates the literal command text.
  Rewriting an unvetted read would launder e.g. ``head ~/.ssh/id_rsa`` into an
  opaque ``python ... --run <blob>`` that path-based rules cannot see; under a
  permissive ``python`` policy that would execute silently. Pass-through keeps
  Codex's consent fully informed regardless of whether it checks the original
  or the rewritten command.
- ``--run`` revalidates eligibility AND confinement at exec time and re-derives
  the shell, so a forged plan can never widen what this wrapper executes.
"""
import base64
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid

from bash_whitelist import has_dangerous_chars, is_whitelisted

MAX_COMPRESS_BYTES = 8 * 1024 * 1024

# --- Confinement gate for the 'allow' decision + rewrite ----------------------
# A command is wrapped (and auto-allowed) only when every file operand stays
# provably inside the working directory and names nothing sensitive. Failing
# the gate costs only the compression: the command reaches Codex verbatim.
# A single leading backslash is also absolute on Windows (drive-relative,
# e.g. ``\Users\x\.ssh\id_rsa``). Catching it fail-closes the confinement
# gate for rootless Windows paths that shlex would otherwise mangle.
_ABSOLUTE_OPERAND_RE = re.compile(r'^(?:/|\\|[A-Za-z]:[\\/])')
_GLOB_OPERAND_RE = re.compile(r'[*?\[\]{}!]')
# Path components that hold credential/key material or secret stores. Applied
# per-component so `src/.ssh/config` and `.env.local` are caught, not just
# top-level names.
_SENSITIVE_COMPONENT_RE = re.compile(
    r'(?i)(?:^id_(?:rsa|dsa|ecdsa|ed25519)$|^\.env(?:\..*)?$|.*\.(?:pem|key|p12|'
    r'pfx|jks|keystore|kdbx?|ppk|asc|gpg)$|^\.netrc$|^\.pgpass$|^\.npmrc$|'
    r'^\.pypirc$|^credentials(?:\.[a-z]+)?$|^secrets?\.(?:json|ya?ml|toml|env|txt)$|'
    r'^\.ssh$|^\.gnupg$|^\.aws$|^\.azure$|^\.kube$|^\.docker$|^\.config$)'
)
# Flags that widen a search's read scope into hidden/ignored files. `rg` is
# recursive-by-default but skips hidden+ignored files unless one of these is
# given; `grep` only recurses (and thereby reads in-tree dotfiles) with -r/-R.
_RG_SCOPE_FLAGS = frozenset({
    '-u', '-uu', '-uuu', '--hidden', '--no-ignore', '--no-ignore-vcs',
    '--no-ignore-dot', '--no-ignore-parent', '--files', '--no-require-git',
})
# Flags whose following argument is itself a file operand (`rg -f pats`,
# `tree --fromfile f`). The value must pass the path check like any operand,
# and a pattern-supplying flag (-e/-f) means there is no pattern positional.
_PATTERN_FLAGS = frozenset({'-e', '--regexp', '-f', '--file', '--fromfile'})
_FILE_VALUE_FLAGS = frozenset({'-f', '--file', '--fromfile', '--ignore-file',
                               '--files0-from'})
# Flags that widen a listing to hidden files (``ls -a``, ``ls -A``,
# ``ls -la``, ``tree -a``, ``--all``, ``--almost-all``). Confinement checks
# operands, but these flags reveal dotfile names inside the cwd.
_HIDDEN_LISTING_RE = re.compile(r'-[a-zA-Z]*[aA][a-zA-Z]*')
_HIDDEN_LISTING_LONG = frozenset({'--all', '--almost-all'})
# First positional of these commands is a pattern, not a path.
_PATTERN_FIRST_COMMANDS = frozenset({'rg', 'grep'})
# PowerShell switches that widen a literal Get-Content/Get-ChildItem into
# recursion or hidden-file reads.
_PS_SCOPE_FLAGS = frozenset({'-recurse', '-force', '-hidden'})


def eligible(command):
    if not isinstance(command, str) or not command.strip() or len(command) > 16000:
        return False
    if has_dangerous_chars(command) or 'codex_command_compress' in command:
        return False
    # Literal read-only PowerShell cmdlets. No script blocks, expressions or
    # profile functions; the original text is passed intact to the same shell.
    if re.match(r'^(Get-Content|Get-ChildItem)\s', command, re.I):
        return not re.search(r'[{}()]|\s-(?:Wait|Stream)\b', command, re.I)
    try:
        args = shlex.split(command)
    except ValueError:
        return False
    if not args:
        return False
    # The shared Claude list also contains builds, tests and write-capable
    # subcommands. Codex 'allow' must only rewrite inspection commands.
    if args[0] == 'git':
        return len(args) > 1 and args[1] in ('status', 'log') and not any(
            a.startswith(('--output', '--ext-diff', '--textconv')) for a in args[2:])
    if args[0] in ('rg', 'grep', 'ls', 'tree', 'wc', 'head', 'tail'):
        return not any(a.startswith(('--pre', '--hostname-bin')) for a in args[1:]) and is_whitelisted(command)
    return False


def _default_shell():
    """The shell this runtime would use. ``tool_input.shell`` is never read:
    it is model-controlled text and must not steer an auto-approved hook.

    PATH is walked entry by entry and every candidate must resolve under a
    system directory. ``shutil.which`` would return the first PATH hit, so a
    model-controlled ``env.PATH`` could otherwise point this wrapper at an
    attacker-placed ``bash``/``pwsh``. An environment with no validated shell
    yields ``None`` (fail closed: the command passes through unrewritten).
    """
    if os.name == 'nt':
        names = ('pwsh.exe', 'powershell.exe')
        roots = []
        for var, fallback in (('SystemRoot', r'C:\Windows'),
                              ('ProgramFiles', r'C:\Program Files'),
                              ('ProgramFiles(x86)', r'C:\Program Files (x86)')):
            raw = os.environ.get(var) or fallback
            try:
                roots.append(Path(raw).resolve())
            except OSError:
                continue
    else:
        names = ('bash', 'sh')
        roots = []
        for directory in ('/bin', '/sbin', '/usr/bin', '/usr/sbin',
                          '/usr/local/bin', '/usr/local/sbin',
                          '/opt/homebrew/bin', '/opt/homebrew/sbin'):
            try:
                roots.append(Path(directory).resolve())
            except OSError:
                continue
    for name in names:
        for directory in os.environ.get('PATH', '').split(os.pathsep):
            if not directory:
                continue
            try:
                candidate = (Path(directory) / name).resolve()
                if (candidate.name.lower() == name and candidate.is_file()
                        and os.access(candidate, os.X_OK)
                        and any(candidate.is_relative_to(root)
                                for root in roots)):
                    return str(candidate)
            except OSError:
                # Unreadable PATH entries (e.g. another user's ~/.cargo/bin on
                # a shared machine) must not crash shell resolution -- skip
                # them like any other non-match and keep scanning.
                continue
    return None


def _operand_confined(arg, base):
    """True when a single operand is a provably in-cwd, non-hidden,
    non-sensitive path -- or not path-shaped at all."""
    arg = arg.strip('"\'')  # posix=False shlex keeps quote characters
    if (not arg or arg.startswith(('~', '$')) or _ABSOLUTE_OPERAND_RE.match(arg)
            or _GLOB_OPERAND_RE.search(arg)):
        return False
    components = [c for c in re.split(r'[\\/]+', arg) if c and c != '.']
    if any(c.startswith('.') or _SENSITIVE_COMPONENT_RE.search(c) for c in components):
        return False
    try:
        return (base / arg).resolve().is_relative_to(base)
    except OSError:
        return False


def _confined(command, base):
    """True when every file operand of `command` is confined to `base`.

    `base` is always the real process cwd at call time -- never the
    model-influenced payload cwd, which cannot be trusted as a confinement
    boundary.

    Tokenization uses ``posix=False`` so backslashes in Windows path
    operands (``C:\\Users\\x\\.ssh\\id_rsa``) are preserved for the
    absolute-path and sensitive-component checks. POSIX-mode shlex would
    consume the backslashes, collapsing the path to a relative-looking
    token that bypasses confinement. Quote characters retained by
    ``posix=False`` are stripped before every flag/operand comparison
    (mirroring the PowerShell branch and ``_operand_confined``).
    """
    if re.match(r'^(Get-Content|Get-ChildItem)\s', command, re.I):
        # PowerShell quoting differs from POSIX; flag-looking tokens are still
        # dashed and the rest are operands.
        try:
            args = shlex.split(command, posix=False)[1:]
        except ValueError:
            return False
        if any(a.strip('"\'').lower() in _PS_SCOPE_FLAGS for a in args):
            return False
        cmd0 = ''
    else:
        try:
            args = shlex.split(command, posix=False)
        except ValueError:
            return False
        cmd0 = args[0].strip('"\'') if args else ''
        args = args[1:]
    if cmd0 == 'rg' and any(a.strip('\'"') in _RG_SCOPE_FLAGS for a in args):
        return False
    if cmd0 == 'grep':
        stripped = [a.strip('\'"') for a in args]
        if any(a == '--recursive' or a == '--dereference-recursive'
               or re.fullmatch(r'-[a-zA-Z]*[rR][a-zA-Z]*', a) for a in stripped):
            return False
        # ``grep -d recurse`` / ``--directories=recurse`` is functionally -r:
        # it recurses into directories and reads dotfiles in the cwd. The
        # value following ``-d``/``--directories`` is the action keyword.
        for i, a in enumerate(stripped):
            if a == '-d' or a == '--directories':
                nxt = stripped[i + 1] if i + 1 < len(stripped) else ''
                if nxt == 'recurse':
                    return False
            elif a.startswith('--directories='):
                if a.split('=', 1)[1] == 'recurse':
                    return False
    if cmd0 in ('ls', 'tree') and any(
            _HIDDEN_LISTING_RE.fullmatch(a.strip('\'"')) or a.strip('\'"') in _HIDDEN_LISTING_LONG
            for a in args):
        return False
    expect_pattern = cmd0 in _PATTERN_FIRST_COMMANDS
    file_value_next = False
    for arg in args:
        tok = arg.strip('\'"')  # posix=False keeps quote characters
        if file_value_next:
            file_value_next = False
        elif tok.startswith('-') and tok != '-':
            name, sep, value = tok.partition('=')
            if name in _FILE_VALUE_FLAGS:
                if sep:
                    # Attached ``--flag=VALUE``: the value is a file operand
                    # too, so it gets the same confinement check.
                    if not _operand_confined(value, base):
                        return False
                else:
                    file_value_next = True
            elif not sep and len(tok) > 2 and tok[:2] in _FILE_VALUE_FLAGS:
                # Glued short form: ``grep -fFILE`` / ``rg -fFILE``.
                if not _operand_confined(tok[2:], base):
                    return False
            if name in _PATTERN_FLAGS or (
                    not sep and len(tok) > 2 and tok[:2] in _PATTERN_FLAGS):
                expect_pattern = False
            continue
        elif expect_pattern:
            expect_pattern = False
            continue
        if not _operand_confined(arg, base):
            return False
    return True


def rewrite(payload):
    from plugin_env import is_v5_flag_enabled
    if not is_v5_flag_enabled('v5_bash_compress', 'TOKEN_OPTIMIZER_BASH_COMPRESS', default=True):
        return None
    if not isinstance(payload, dict) or payload.get('tool_name') != 'Bash':
        return None
    tool_input = payload.get('tool_input')
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get('command')
    shell = _default_shell()
    if not eligible(command) or not shell:
        return None
    if not _confined(command, Path.cwd().resolve()):
        # Provably-safe reads only. Anything else passes through untouched so
        # Codex's consent sees the literal command (e.g. `head ~/.ssh/id_rsa`
        # prompts exactly as it would without this hook).
        return None
    plan = {'command': command, 'session_id': payload.get('session_id'),
            'model': payload.get('model'), 'cwd': str(Path.cwd().resolve())}
    encoded = base64.b64encode(json.dumps(plan).encode()).decode()
    argv = [sys.executable, str(Path(__file__).resolve()), '--run', encoded]
    if Path(shell).stem.lower() in ('pwsh', 'powershell'):
        rewritten = '& ' + ' '.join("'" + a.replace("'", "''") + "'" for a in argv) + '; exit $LASTEXITCODE'
    else:
        rewritten = shlex.join(argv)
    # updatedInput carries ONLY the rewritten command. Every other tool_input
    # field (shell, env, cwd, and any future execution-context sibling) is
    # model-controlled and must never reach an auto-approved tool call: env
    # could steer PATH resolution, cwd could redirect the confined read.
    updated = {'command': rewritten}
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'allow',
                                  'updatedInput': updated}}


def run(plan):
    command = plan.get('command') if isinstance(plan, dict) else None
    # Revalidate at execution time: never turn a trusted wrapper into a generic
    # command launcher. Ineligible or non-confined commands are not executed by
    # this wrapper -- a forged plan cannot exceed the inspection envelope.
    if not eligible(command) or not _confined(command, Path.cwd().resolve()):
        print('Token Optimizer: command is not eligible', file=sys.stderr)
        return 2
    # The plan pins the rewrite-time working directory. Confinement was proven
    # against THAT cwd; if the ambient cwd differs (e.g. a model-controlled
    # tool_input.cwd survived into the tool call), re-running the confined
    # command here would read files in a different directory. Refuse.
    expected_cwd = plan.get('cwd')
    if expected_cwd:
        try:
            same_cwd = Path.cwd().resolve() == Path(str(expected_cwd)).resolve()
        except OSError:
            same_cwd = False
        if not same_cwd:
            print('Token Optimizer: working directory changed since approval',
                  file=sys.stderr)
            return 2
    shell = _default_shell()
    if not shell:
        print('Token Optimizer: no usable default shell', file=sys.stderr)
        return 2
    # Scope runtime markers to the child environment only. Mutating the
    # parent's os.environ leaks 'codex' into any process that calls run()
    # in-process (and poisons detect_runtime() for everything after it).
    child_env = dict(os.environ, TOKEN_OPTIMIZER_RUNTIME='codex')
    if plan.get('session_id'):
        child_env['TOKEN_OPTIMIZER_SESSION_ID'] = str(plan['session_id'])
    if Path(shell).stem.lower() in ('pwsh', 'powershell'):
        tail = '; $toSucceeded=$?; $toExit=$LASTEXITCODE; if ($null -ne $toExit) { exit $toExit }; if (-not $toSucceeded) { exit 1 }'
        argv = [shell, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', command + tail]
    else:
        argv = [shell, '-c', command]
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            result = subprocess.run(argv, stdout=output, stderr=errors, env=child_env,
                                    timeout=6,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except subprocess.TimeoutExpired:
            # Stream whatever was captured before the timeout, then exit.
            output.seek(0)
            shutil.copyfileobj(output, sys.stdout.buffer)
            errors.seek(0)
            shutil.copyfileobj(errors, sys.stderr.buffer)
            print('Token Optimizer: command timed out', file=sys.stderr)
            return 124
        except OSError:
            # Spawn failed (shell binary deleted between check and exec, etc.).
            print('Token Optimizer: failed to spawn command', file=sys.stderr)
            return 127
        output.seek(0, 2)
        size = output.tell()
        output.seek(0)
        errors.seek(0)
        # Failure/oversized output is streamed verbatim, never buffered in RAM.
        if result.returncode != 0 or size > MAX_COMPRESS_BYTES:
            shutil.copyfileobj(output, sys.stdout.buffer)
            shutil.copyfileobj(errors, sys.stderr.buffer)
            return result.returncode
        raw_bytes = output.read()
        error_bytes = errors.read(MAX_COMPRESS_BYTES + 1)
        if error_bytes:  # preserve warnings too; no simplification on stderr
            sys.stdout.buffer.write(raw_bytes)
            sys.stderr.buffer.write(error_bytes)
            shutil.copyfileobj(errors, sys.stderr.buffer)
            return result.returncode
        try:
            raw = raw_bytes.decode('utf-8', errors='strict')
        except UnicodeError:
            sys.stdout.buffer.write(raw_bytes)
            return result.returncode
        try:
            from bash_compress import compress
            from plugin_env import resolve_snapshot_dir
            from token_estimate import estimate_tokens
            short = compress(command, raw)
            if short != raw and len(short) < len(raw) * 0.9:
                archive = resolve_snapshot_dir() / 'codex-command-output'
                archive.mkdir(parents=True, exist_ok=True)
                target = archive / (uuid.uuid4().hex + '.txt')
                try:
                    with target.open('xb') as handle:
                        handle.write(raw_bytes)
                    # Restrict to owner-only read: the archive may hold command
                    # output that touched sensitive in-cwd files. Mirrors
                    # archive_result._chmod_private_file on the tool-archive path.
                    try:
                        os.chmod(target, 0o600)
                    except OSError:
                        pass
                except OSError:
                    target = None  # disk full or permission denied; skip archive
                if target:
                    short += f'\n[Token Optimizer: full command output saved to {target}]\n'
                if estimate_tokens(short) < estimate_tokens(raw):
                    sys.stdout.buffer.write(short.encode('utf-8'))
                    sys.stdout.buffer.flush()
                    try:
                        from compression_log import log_compression_event
                        log_compression_event(feature='codex_command_compress', original_text=raw,
                            compressed_text=short, session_id=plan.get('session_id'),
                            model=plan.get('model') or 'unknown', command_pattern='read-only command',
                            verified=True, tier='measured')
                    except Exception:
                        pass
                    return 0
        except Exception:
            pass
        sys.stdout.buffer.write(raw_bytes)
        return result.returncode


def main():
    from utf8_io import enforce_utf8_io
    enforce_utf8_io()
    if len(sys.argv) == 3 and sys.argv[1] == '--run':
        try:
            plan = json.loads(base64.b64decode(sys.argv[2]))
        except (ValueError, UnicodeError):
            print('Token Optimizer: malformed plan', file=sys.stderr)
            return 2
        return run(plan)
    from hook_io import read_stdin_hook_input
    result = rewrite(read_stdin_hook_input() or {})
    if result:
        print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
