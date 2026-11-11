"""Recover and check the parent's request before a fork can send a ping."""
import copy
import hashlib
import json
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _fixture():
    shape = {'system': [{'type': 'text', 'text': 'system'}],
             'tools': [{'name': 'Read', 'input_schema': {'type': 'object'}}]}
    message = {'role': 'user', 'content': [{'type': 'text', 'text': 'parent'}]}
    params = {'model': 'synthetic-model', 'thinking': {'type': 'enabled'}, 'betas': ['test']}
    normalized = dict(params, **copy.deepcopy(shape), messages=[copy.deepcopy(message)])
    normalized['messages'][0]['content'][0]['cache_control'] = {'type': 'ephemeral'}
    rows = [{'type': 'api-request-shape', 'shapeHash': 'shape', 'shape': shape},
            {'type': 'api-request-blob', 'hash': 'message', 'message': message},
            {'type': 'api-request', 'id': 'request', 'shapeHash': 'shape', 'version': '1.2.3',
             'querySource': 'repl_main_thread', 'keep': 0, 'tail': ['message'], 'params': params,
             'cacheControl': [{'path': ['messages', 0, 'content', 0], 'value': {'type': 'ephemeral'}}],
             'requestDigest': _digest(normalized)}]
    rows.append({'type': 'assistant', 'requestRef': 'request', 'message': {'usage': {'output_tokens': 1}}})
    fork = dict(params, **copy.deepcopy(shape), messages=[copy.deepcopy(message),
        {'role': 'assistant', 'content': [{'type': 'text', 'text': 'parent response'}]},
        {'role': 'user', 'content': [{'type': 'text', 'text': 'ping', 'cache_control': {'type': 'ephemeral'}}]}])
    fork['system'].insert(0, {'type': 'text', 'text': 'x-anthropic-billing-header: cc_version=1.2.3.abc; cc_entrypoint=cli;'})
    return rows, fork, normalized


def test_rebuilds_parent_and_restores_exact_fork_prefix():
    from _measure.keepwarm_prefix import reconstruct, prepare
    rows, fork, expected = _fixture()
    parent = reconstruct(rows)
    assert parent['body'] == expected
    before = copy.deepcopy(fork)
    outbound = prepare(fork, parent, ['test'])
    assert fork == before
    assert outbound['messages'][:1] == expected['messages']
    assert outbound['system'][1:] == expected['system']
    assert outbound['tools'] == expected['tools']
    assert outbound['thinking'] == expected['thinking']


def test_rejects_changed_parent_and_untrusted_tool_types():
    from _measure.keepwarm_prefix import reconstruct, prepare, UnsafePrefix
    rows, fork, _ = _fixture()
    rows[2]['requestDigest'] = 'wrong'
    with pytest.raises(UnsafePrefix): reconstruct(rows)
    rows, fork, _ = _fixture()
    parent = reconstruct(rows)
    fork['tools'][0]['name'] = 'Bash'
    with pytest.raises(UnsafePrefix): prepare(fork, parent, ['test'])
    rows, fork, _ = _fixture()
    rows[0]['shape']['tools'][0]['type'] = 'web_search_20250305'
    with pytest.raises(UnsafePrefix): reconstruct(rows)


def test_rejects_missing_final_message_cache_checkpoint():
    from _measure.keepwarm_prefix import reconstruct, UnsafePrefix
    rows, _, body = _fixture()
    rows[2]['cacheControl'] = [{'path': ['system', 0], 'value': {'type': 'ephemeral'}}]
    del body['messages'][0]['content'][0]['cache_control']
    body['system'][0]['cache_control'] = {'type': 'ephemeral'}
    rows[2]['requestDigest'] = _digest(body)
    with pytest.raises(UnsafePrefix): reconstruct(rows)


def test_reconstructs_incremental_request_chain_and_rejects_redaction():
    from _measure.keepwarm_prefix import reconstruct, UnsafePrefix
    rows, _, original = _fixture()
    assistant = {'role': 'assistant', 'content': [{'type': 'text', 'text': 'answer'}]}
    followup = {'role': 'user', 'content': [{'type': 'text', 'text': 'next'}]}
    rows.extend([{'type': 'api-request-blob', 'hash': 'answer', 'message': assistant},
                 {'type': 'api-request-blob', 'hash': 'followup', 'message': followup}])
    body = copy.deepcopy(original)
    del body['messages'][0]['content'][0]['cache_control']
    body['messages'].extend([copy.deepcopy(assistant), copy.deepcopy(followup)])
    body['messages'][-1]['content'][0]['cache_control'] = {'type': 'ephemeral'}
    rows.append({'type': 'api-request', 'id': 'next', 'base': 'request', 'keep': 1,
                 'tail': ['answer', 'followup'], 'shapeHash': 'shape', 'version': '1.2.3',
                 'querySource': 'repl_main_thread', 'params': rows[2]['params'],
                 'cacheControl': [{'path': ['messages', 2, 'content', 0], 'value': {'type': 'ephemeral'}}],
                 'requestDigest': _digest(body)})
    rows.append({'type': 'assistant', 'requestRef': 'next', 'message': {'usage': {'output_tokens': 1}}})
    assert reconstruct(rows)['body'] == body
    rows[-2]['cut'] = {'kind': 'truncated'}
    with pytest.raises(UnsafePrefix): reconstruct(rows)


def test_rejects_stale_or_incomplete_parent_request():
    from _measure.keepwarm_prefix import reconstruct, UnsafePrefix
    rows, _, _ = _fixture()
    rows[-1]['requestRef'] = 'different-request'
    with pytest.raises(UnsafePrefix): reconstruct(rows)
