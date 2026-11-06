"""Verify installed-code identity outside user-output bytes.

The frozen guarantee covers BEHAVIOUR: stdout, stderr, exit status, settings,
databases and every other user-visible byte. Installed executable source is
NOT frozen byte-for-byte: adapters install the tree's own modules, so a
deliberate source fix changes those installed bytes. What stays frozen is the
FAITHFULNESS of every installed byte to the source tree that produced it,
proven independently of the captures that observed those bytes:

- Devin's closed payload is rendered by the released installer's
  ``_render_payload``. The renderer must be AST-identical between the trees;
  every differing installed module is re-rendered from BOTH trees and each
  render must equal the captured bytes exactly; a differing
  ``devin_source.py`` is additionally AST-limited to the mandated
  ``_measure`` refusal-root change; and the install manifest is re-verified
  commitment by commitment with only the proven modules' digests masked, so
  every other manifest byte compares exactly.
- The verbatim adapters (antigravity, copilot, cursor, grok, hermes) copy a
  declared module list. The list is read from the BASE installer's AST, the
  candidate installer must be AST-identical (an installer edit -- module
  list, destination, glob, anything -- fails acceptance), the install
  location is anchored in the BASE capture by byte-equality, and the exact
  predicted change set must match the observed differences two ways: each
  differing installed file hashes to exactly the candidate source bytes and
  the BASE capture to the BASE source bytes.

Anything outside those proofs -- an installer or renderer edit, an unmapped
file, a changed destination, a corrupt copy, a missing or extra difference --
fails closed: the raw byte difference stands. An adapter whose installer
changed can never be blessed, and its BASE-declared module list still drives
the vacuity accounting below: a changed BASE-declared source whose install
case leaves no artifacts is an error, not a skip.

``installed_source_proof_error`` is the per-shard accounting twin: on POSIX,
an install case whose sources changed MUST leave verified artifacts -- a
silent pass means the installer refused (sandbox interpreter trust) and the
delta was never exercised.
"""
from __future__ import annotations
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath

import measure_split_golden as golden

CASE = 'cli-devin-install'
SOURCE = 'files/.config/devin/token-optimizer/plugin/devin_source.py'
MANIFEST = 'files/.config/devin/token-optimizer/install.json'

_SCRIPTS = 'skills/token-optimizer/scripts'
_DEVIN_PLUGIN = 'files/.config/devin/token-optimizer/plugin/'
# Verbatim-copy adapters: installer module name -> AST constant holding the
# copied module tuple. The CONTENTS are always read from the BASE installer's
# AST; this inventory only names where each adapter declares its list. Devin
# is excluded: its rendered payload has the stronger manifest-bound proof.
_VERBATIM_ADAPTERS = {
    'antigravity': '_PAYLOAD_MODULES',
    'copilot': '_PAYLOAD_MODULES',
    'cursor': '_PAYLOAD_MODULES',
    'grok': '_PAYLOAD_MODULES',
    'hermes': '_RUNTIME_MODULES',
}


def refusal_root_only(before, after):
    """Only the mandated package refusal root may differ semantically."""
    left, right = ast.parse(before), ast.parse(after)
    for tree, remove in ((left, False), (right, True)):
        node = next(n for n in tree.body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == 'REPO_MODULES' for t in n.targets))
        assert isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == 'frozenset'
        values = node.value.args[0]
        assert isinstance(values, (ast.Tuple, ast.List))
        matches = [n for n in values.elts if isinstance(n, ast.Constant) and n.value == '_measure']
        assert len(matches) == int(remove)
        if remove:
            values.elts.remove(matches[0])
    return ast.dump(left) == ast.dump(right)


def _ast_identical(before, after):
    """Whole-file AST identity: an installer/renderer edit fails acceptance."""
    return ast.dump(ast.parse(before)) == ast.dump(ast.parse(after))


def _module_list(installer_bytes, constant):
    """The adapter's declared copied-module tuple, from the installer AST."""
    tree = ast.parse(installer_bytes)
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == constant for t in node.targets)
                and isinstance(node.value, (ast.Tuple, ast.List))
                and node.value.elts
                and all(isinstance(e, ast.Constant) and isinstance(e.value, str)
                        and '/' not in e.value and e.value.endswith('.py') for e in node.value.elts)):
            return tuple(e.value for e in node.value.elts)
    raise AssertionError('no usable ' + constant + ' module tuple in installer')


def _render_payloads(baseline_source, candidate_source, names):
    """Render each payload module from BOTH trees with the released renderer.

    The candidate ``_render_payload`` must be AST-identical to BASE's; a
    renderer edit fails acceptance. A module unreadable or unrenderable in a
    tree yields None on that side, which never equals captured bytes, so it
    can never be exempted.
    """
    roots = [Path(root) / _SCRIPTS for root in (baseline_source, candidate_source)]
    nodes = [next(node for node in ast.parse((root / 'devin_install.py').read_bytes()).body
                  if isinstance(node, ast.FunctionDef) and node.name == '_render_payload') for root in roots]
    assert ast.dump(nodes[0]) == ast.dump(nodes[1])
    code = compile(ast.Module(body=[nodes[0]], type_ignores=[]), '<released-payload-renderer>', 'exec')
    renders = {}
    for name in names:
        pair = []
        for root in roots:
            namespace = {'ast': ast, 'SCRIPTS': root}
            try:
                exec(code, namespace)
                pair.append(namespace['_render_payload'](name))
            except (OSError, ValueError, KeyError, TypeError, SyntaxError, NameError):
                # NameError: the released renderer reads only ast/SCRIPTS; a
                # renderer reaching for another global is unrenderable here.
                # Fail closed (None never equals captured bytes), never hide
                # it under a blanket Exception catch.
                pair.append(None)
        renders[name] = tuple(pair)
    return renders


def _devin_inventory(case):
    """The installed Devin payload names, from the capture's own manifest."""
    raw = (case / MANIFEST).read_bytes()
    data = json.loads(raw)
    assert data['owner'] == 'devin-output-filter-v1'
    names = set()
    for key in data['files']:
        normalized = key.replace('\\', '/')
        marker = '/.config/devin/token-optimizer/plugin/'
        if marker in normalized:
            names.add(normalized.rsplit('/', 1)[-1])
    assert names
    return names


def verify_manifest(case, masked):
    """Re-verify every manifest commitment, masking only proven modules.

    ``masked`` maps proven module names to THIS side's rendered bytes. Their
    commitments must exist, match the rendered digest, and appear exactly
    once in the raw manifest; those digests are then masked so the remaining
    manifest bytes compare exactly. Every other commitment must hash to the
    case's actual file bytes.
    """
    raw = (case / MANIFEST).read_bytes()
    data = json.loads(raw)
    assert data['owner'] == 'devin-output-filter-v1'
    files = data['files']
    marker = '/.config/devin/token-optimizer/plugin/'
    first = next(iter(files)).replace('\\', '/')
    assert marker in first or '/.config/devin/' in first
    home = first[:first.index('/.config/devin/')]
    tokens = []
    for name, rendered in sorted(masked.items()):
        suffix = marker + name
        entries = [(key.replace('\\', '/'), value) for key, value in files.items()
                   if key.replace('\\', '/').endswith(suffix)]
        assert len(entries) == 1
        key, digest = entries[0]
        assert digest == hashlib.sha256(rendered).hexdigest()
        assert key.startswith(home + '/')
        tokens.append((digest, name))
    for absolute, commitment in files.items():
        absolute = absolute.replace('\\', '/')
        assert absolute.startswith(home + '/')
        relative = PurePosixPath(absolute[len(home)+1:])
        assert not relative.is_absolute() and '..' not in relative.parts
        copied = case / 'files' / Path(*relative.parts)
        assert hashlib.sha256(copied.read_bytes()).hexdigest() == commitment
    for digest, name in tokens:
        assert raw.count(digest.encode()) == 1
        raw = raw.replace(digest.encode(), b'SHA256_VERIFIED_' + name.encode())
    return raw


def _devin_verified(raw, expected, actual, baseline_source, candidate_source):
    """The stronger Devin proof: rendered-payload faithfulness + manifest.

    Blesses exactly the observed plugin-module differences that re-render
    identically from both trees (plus devin_source.py's refusal-root gate and
    the commitment manifest). All-or-nothing: any failure blesses nothing.
    """
    prefix = 'bytes differ: ' + CASE + '/' + _DEVIN_PLUGIN
    manifest_id = 'bytes differ: ' + CASE + '/' + MANIFEST
    observed = sorted(e[len(prefix):] for e in raw if e.startswith(prefix))
    if not observed and manifest_id not in raw:
        return raw, []
    try:
        ecase, acase = Path(expected) / CASE, Path(actual) / CASE
        inventory = _devin_inventory(ecase)
        renders = _render_payloads(baseline_source, candidate_source, sorted(inventory | set(observed)))
        # None on either side never equals the other side: it is a change that
        # can never be verified, so the case blesses nothing below.
        changed = {name for name in inventory if renders[name][0] != renders[name][1]}
        assert changed == set(observed), 'two-way: predicted render changes must equal observed differences'
        assert all(renders[name][0] is not None and renders[name][1] is not None for name in observed)
        if 'devin_source.py' in observed:
            before = (Path(baseline_source) / _SCRIPTS / 'devin_source.py').read_bytes()
            after = (Path(candidate_source) / _SCRIPTS / 'devin_source.py').read_bytes()
            assert refusal_root_only(before, after)
        for name in observed:
            assert (ecase / _DEVIN_PLUGIN / name).read_bytes() == renders[name][0]
            assert (acase / _DEVIN_PLUGIN / name).read_bytes() == renders[name][1]
        blessed = [prefix + name for name in observed]
        if manifest_id in raw:
            assert verify_manifest(ecase, {name: renders[name][0] for name in observed}) == \
                   verify_manifest(acase, {name: renders[name][1] for name in observed})
            blessed.append(manifest_id)
    except (AssertionError, OSError, ValueError, KeyError, TypeError, StopIteration, SyntaxError):
        return raw, []
    return [error for error in raw if error not in blessed], blessed


def _anchor_plugin_dir(case_files, modules, baseline_source):
    """The one directory holding faithful BASE copies of every listed module.

    Anchored from capture evidence, not from install logic: every declared
    module must appear exactly once, byte-equal to its BASE source, all in a
    single directory. Ambiguity or a missing copy fails closed.
    """
    dirs = None
    for name in modules:
        want = (Path(baseline_source) / _SCRIPTS / name).read_bytes()
        hits = {path.parent for path in case_files.rglob(name)
                if path.is_file() and path.read_bytes() == want}
        assert len(hits) == 1
        rel = {path.relative_to(case_files).as_posix() for path in hits}
        dirs = rel if dirs is None else dirs & rel
        assert dirs
    return dirs.pop()


def _verbatim_verified(raw, expected, actual, baseline_source, candidate_source):
    """Generic verbatim-copy faithfulness for the declared-module adapters.

    Per adapter case, all-or-nothing: BASE-anchored install dir, AST-identical
    installer, predicted change set equal to the observed mapped differences,
    and every differing file hashing to exactly its tree's source bytes.
    """
    remaining, blessed = list(raw), []
    for adapter, constant in sorted(_VERBATIM_ADAPTERS.items()):
        case = 'cli-' + adapter + '-install'
        prefix = 'bytes differ: ' + case + '/files/'
        case_files = Path(expected) / case / 'files'
        if not case_files.is_dir():
            continue
        try:
            base_inst = (Path(baseline_source) / _SCRIPTS / (adapter + '_install.py')).read_bytes()
            cand_inst = (Path(candidate_source) / _SCRIPTS / (adapter + '_install.py')).read_bytes()
            assert _ast_identical(base_inst, cand_inst)
            modules = _module_list(base_inst, constant)
            root = _anchor_plugin_dir(case_files, modules, baseline_source)
            mapped = {prefix + root + '/' + name: name for name in modules}
            observed = {error for error in remaining if error in mapped}
            predicted = {identity for identity, name in mapped.items()
                         if (Path(baseline_source) / _SCRIPTS / name).read_bytes()
                         != (Path(candidate_source) / _SCRIPTS / name).read_bytes()}
            assert predicted == observed, 'two-way: predicted source changes must equal observed differences'
            for identity in predicted:
                name = mapped[identity]
                assert (Path(expected) / identity[len('bytes differ: '):]).read_bytes() == \
                       (Path(baseline_source) / _SCRIPTS / name).read_bytes()
                assert (Path(actual) / identity[len('bytes differ: '):]).read_bytes() == \
                       (Path(candidate_source) / _SCRIPTS / name).read_bytes()
        except (AssertionError, OSError, ValueError, KeyError, TypeError, StopIteration, SyntaxError):
            continue
        blessed.extend(sorted(predicted))
        remaining = [error for error in remaining if error not in predicted]
    return remaining, blessed


def compare_candidate(expected, actual, baseline_source, candidate_source):
    """Retain raw evidence; exclude only independently verified code artifacts.

    Behaviour bytes never move: user outputs, settings, databases and errors
    compare raw. Installed executable source is exempted only when proven
    faithful to its tree - Devin's rendered payload with its manifest
    commitments, or a verbatim adapter copy hashing to exactly the source.
    """
    expected, actual = Path(expected), Path(actual)
    raw = golden.compare(expected, actual)
    remaining, devin = _devin_verified(raw, expected, actual, baseline_source, candidate_source)
    remaining, copied = _verbatim_verified(remaining, expected, actual, baseline_source, candidate_source)
    return remaining, devin + copied


def _predict_changes(adapter, baseline_source, candidate_source):
    """The module names whose INSTALLED bytes change between the trees.

    None when prediction is impossible (unparseable sources). An adapter with
    a changed installer is outside the proof: no prediction, no blessing.
    """
    if adapter == 'devin':
        names = _module_list((Path(baseline_source) / _SCRIPTS / 'devin_source.py').read_bytes(), 'PAYLOAD')
        renders = _render_payloads(baseline_source, candidate_source, sorted(names))
        return {name for name in names if renders[name][0] != renders[name][1]}
    base_inst = (Path(baseline_source) / _SCRIPTS / (adapter + '_install.py')).read_bytes()
    cand_inst = (Path(candidate_source) / _SCRIPTS / (adapter + '_install.py')).read_bytes()
    if not _ast_identical(base_inst, cand_inst):
        # A changed installer invalidates the BASE-derived mapping itself:
        # what it copies is unknown, so the delta is UNPREDICTABLE (None),
        # not empty. The shard accounting then requires capture evidence.
        return None
    modules = _module_list(base_inst, _VERBATIM_ADAPTERS[adapter])
    return {name for name in modules
            if (Path(baseline_source) / _SCRIPTS / name).read_bytes()
            != (Path(candidate_source) / _SCRIPTS / name).read_bytes()}


def _case_artifacts_present(adapter, expected, baseline_source):
    """Did the BASE capture's install case actually install? (refusal check)

    Evidence-based: the Devin manifest exists, or the adapter's BASE-declared
    modules anchor (byte-equal) to one installed directory.
    """
    if adapter == 'devin':
        return (Path(expected) / CASE / MANIFEST).is_file()
    case_files = Path(expected) / ('cli-' + adapter + '-install') / 'files'
    if not case_files.is_dir():
        return False
    try:
        base_inst = (Path(baseline_source) / _SCRIPTS / (adapter + '_install.py')).read_bytes()
        _anchor_plugin_dir(case_files, _module_list(base_inst, _VERBATIM_ADAPTERS[adapter]),
                           baseline_source)
        return True
    except (AssertionError, OSError, ValueError, KeyError, TypeError, StopIteration, SyntaxError):
        return False


def installed_source_proof_error(case_ids, raw, identities, baseline_source, candidate_source,
                                 expected, platform=None):
    """Per-shard accounting: a changed, selected install case must be PROVEN.

    On POSIX, when an install case's installed bytes should change, the shard
    must either verify every predicted artifact or show every predicted
    difference as a raw error. When the delta is UNPREDICTABLE (the installer
    changed, so the BASE-derived mapping no longer bounds what gets copied),
    the case must at least have produced its artifacts. Neither means the
    installer refused (the sandbox interpreter trust guard) and the delta was
    never exercised: the green run would be vacuous, even for an installer
    edit that changes no module. Native Windows refusal is product behaviour.
    """
    platform = os.name if platform is None else platform
    if platform != 'posix':
        return None
    problems = []
    for adapter in sorted(tuple(_VERBATIM_ADAPTERS) + ('devin',)):
        case = 'cli-' + adapter + '-install'
        if case not in case_ids:
            continue
        prefix = 'bytes differ: ' + case + '/'
        observed = {e.rsplit('/', 1)[-1] for e in raw if e.startswith(prefix)}
        verified = {e.rsplit('/', 1)[-1] for e in identities if e.startswith(prefix)}
        try:
            predicted = _predict_changes(adapter, baseline_source, candidate_source)
        except Exception:  # an unpredictable case with no other signal must not pass silently
            predicted = None
        if predicted is None:
            if not observed and not verified and not _case_artifacts_present(
                    adapter, expected, baseline_source):
                problems.append(case + ': the candidate installer changed and the case produced '
                                'no installed artifacts to inspect: run the capture with a trusted '
                                'interpreter (root- or self-owned, not world-writable), '
                                'e.g. /usr/bin/python3')
            continue
        if not predicted or predicted <= verified or predicted <= observed:
            continue
        problems.append(case + ' produced no verified installed-source artifacts for its changed '
                        'payload sources: run the capture with a trusted interpreter '
                        '(root- or self-owned, not world-writable), e.g. /usr/bin/python3')
    return '\n'.join(problems) if problems else None


def devin_install_proof_error(case_ids, identities, platform=None):
    """Why a capture failed to exercise the split's Devin code delta, or None.

    POSIX devin-install refuses an untrusted interpreter (hosted-CI toolcache Python is
    world-writable). Then BASE and candidate refuse alike, compare equal, and the one changed
    installed artifact is never produced. Native Windows refusal is the product's behavior.

    Superseded in the driver by installed_source_proof_error, which applies the
    same accounting to every adapter and to partial proofs; kept for callers
    and tests that pin the original Devin-only contract.
    """
    platform = os.name if platform is None else platform
    if platform != 'posix' or CASE not in case_ids or identities:
        return None
    return (CASE + ' produced no verified Devin artifacts: run the capture with a trusted '
            'interpreter (root- or self-owned, not world-writable), e.g. /usr/bin/python3')
