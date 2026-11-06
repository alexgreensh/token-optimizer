"""Verify the required Devin refusal-source change outside user-output bytes."""
from __future__ import annotations
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath

import measure_split_golden as golden

CASE = 'cli-devin-install'
SOURCE = 'files/.config/devin/token-optimizer/plugin/devin_source.py'
MANIFEST = 'files/.config/devin/token-optimizer/install.json'


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


def verify_manifest(case, source_bytes):
    raw = (case / MANIFEST).read_bytes()
    data = json.loads(raw)
    assert data['owner'] == 'devin-output-filter-v1'
    files = data['files']
    suffix = '/.config/devin/token-optimizer/plugin/devin_source.py'
    entries = [(key.replace('\\', '/'), value) for key, value in files.items()
               if key.replace('\\', '/').endswith(suffix)]
    assert len(entries) == 1
    key, digest = entries[0]
    assert digest == hashlib.sha256(source_bytes).hexdigest()
    home = key[:-len(suffix)]
    for absolute, commitment in files.items():
        absolute = absolute.replace('\\', '/')
        assert absolute.startswith(home + '/')
        relative = PurePosixPath(absolute[len(home)+1:])
        assert not relative.is_absolute() and '..' not in relative.parts
        copied = case / 'files' / Path(*relative.parts)
        assert hashlib.sha256(copied.read_bytes()).hexdigest() == commitment
    assert raw.count(digest.encode()) == 1
    return raw.replace(digest.encode(), b'SHA256_VERIFIED_DEVIN_SOURCE')


def rendered_pair(baseline_source, candidate_source):
    """Reuse the unchanged released installer renderer for each input tree."""
    roots = [Path(root) / 'skills/token-optimizer/scripts' for root in (baseline_source, candidate_source)]
    nodes = [next(node for node in ast.parse((root / 'devin_install.py').read_bytes()).body
                  if isinstance(node, ast.FunctionDef) and node.name == '_render_payload') for root in roots]
    assert ast.dump(nodes[0]) == ast.dump(nodes[1])
    code = compile(ast.Module(body=[nodes[0]], type_ignores=[]), '<released-payload-renderer>', 'exec')
    results = []
    for root in roots:
        namespace = {'ast': ast, 'SCRIPTS': root}
        exec(code, namespace)
        results.append(namespace['_render_payload']('devin_source.py'))
    return results


def compare_candidate(expected, actual, baseline_source, candidate_source):
    """Retain raw evidence; exclude only two independently verified code artifacts.

    Copied source and its integrity commitment change with the refusal root.
    All user-output bytes and all other manifest bytes remain raw.
    """
    expected, actual = Path(expected), Path(actual)
    raw = golden.compare(expected, actual)
    identities = ['bytes differ: ' + CASE + '/' + path for path in (SOURCE, MANIFEST)]
    if not set(identities).issubset(raw):
        return raw, []
    try:
        relative = 'skills/token-optimizer/scripts/devin_source.py'
        before = (Path(baseline_source) / relative).read_bytes()
        after = (Path(candidate_source) / relative).read_bytes()
        assert refusal_root_only(before, after)
        before, after = rendered_pair(baseline_source, candidate_source)
        assert (expected / CASE / SOURCE).read_bytes() == before
        assert (actual / CASE / SOURCE).read_bytes() == after
        assert verify_manifest(expected / CASE, before) == verify_manifest(actual / CASE, after)
    except (AssertionError, OSError, ValueError, KeyError, TypeError, StopIteration, SyntaxError):
        return raw, []
    return [error for error in raw if error not in identities], identities


def devin_install_proof_error(case_ids, identities, platform=None):
    """Why a capture failed to exercise the split's Devin code delta, or None.

    POSIX devin-install refuses an untrusted interpreter (hosted-CI toolcache Python is
    world-writable). Then BASE and candidate refuse alike, compare equal, and the one changed
    installed artifact is never produced. Native Windows refusal is the product's behavior.
    """
    import os
    platform = os.name if platform is None else platform
    if platform != 'posix' or CASE not in case_ids or identities:
        return None
    return (CASE + ' produced no verified Devin artifacts: run the capture with a trusted '
            'interpreter (root- or self-owned, not world-writable), e.g. /usr/bin/python3')
