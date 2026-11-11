"""Fail-closed reconstruction and validation of a recorded Claude request."""
import copy
import hashlib
import hmac
import json
import re


class UnsafePrefix(ValueError):
    """The recorded request cannot be matched safely."""


def _digest(value):
    try:
        data = json.dumps(value, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False, allow_nan=False).encode('utf-8')
        return hashlib.sha256(data).hexdigest()
    except (TypeError, ValueError, UnicodeError) as exc:
        raise UnsafePrefix('request cannot be normalized') from exc


def _strip(value):
    if isinstance(value, list):
        return [_strip(item) for item in value]
    if isinstance(value, dict):
        return {key: _strip(item) for key, item in value.items() if key != 'cache_control'}
    return value


def _apply_controls(body, controls):
    for item in controls:
        path = item.get('path')
        if not isinstance(path, list) or not path or path[0] not in ('system', 'tools', 'messages'):
            raise UnsafePrefix('invalid cache checkpoint')
        try:
            target = body
            for key in path:
                target = target[key]
            if not isinstance(target, dict) or not isinstance(item.get('value'), dict):
                raise UnsafePrefix('invalid cache checkpoint')
            target['cache_control'] = copy.deepcopy(item['value'])
        except (KeyError, IndexError, TypeError) as exc:
            raise UnsafePrefix('missing cache checkpoint target') from exc


def _check_tools(tools):
    if not isinstance(tools, list):
        raise UnsafePrefix('missing tool definitions')
    for tool in tools:
        if not isinstance(tool, dict) or tool.get('type', 'custom') != 'custom':
            raise UnsafePrefix('server-side tools cannot be intercepted')


def reconstruct(rows):
    """Verify the latest recorded main-thread request and its content chain."""
    shapes, blobs, chains = {}, {}, {}
    last = None
    last_assistant = None
    try:
        for row in rows:
            if not isinstance(row, dict):
                continue
            typ = row.get('type')
            if typ == 'api-request-shape':
                shapes[row['shapeHash']] = row['shape']
            elif typ == 'api-request-blob':
                blobs[row['hash']] = row['message']
            elif typ == 'assistant' and not row.get('isSidechain'):
                message = row.get('message')
                if isinstance(message, dict) and isinstance(message.get('usage'), dict):
                    last_assistant = row.get('requestRef')
            elif typ == 'api-request':
                keep = row.get('keep', 0)
                if not isinstance(keep, int) or keep < 0:
                    raise UnsafePrefix('invalid request chain')
                base = chains[row['base']] if keep else []
                if keep > len(base):
                    raise UnsafePrefix('incomplete request chain')
                messages = base[:keep] + [blobs[key] for key in row['tail']]
                chains[row['id']] = messages
                if row.get('querySource') == 'repl_main_thread':
                    last = (row, messages)
        if not last:
            raise UnsafePrefix('no recorded parent request')
        row, messages = last
        if last_assistant != row['id']:
            raise UnsafePrefix('parent request is not the completed last turn')
        if row.get('cut') or row.get('withheld') or not row.get('requestDigest'):
            raise UnsafePrefix('request was redacted or incomplete')
        body = copy.deepcopy(row['params'])
        body.update(copy.deepcopy(shapes[row['shapeHash']]))
        body['messages'] = copy.deepcopy(messages)
        _check_tools(body['tools'])
        _apply_controls(body, row.get('cacheControl', []))
        if not hmac.compare_digest(_digest(body), row['requestDigest']):
            raise UnsafePrefix('recorded request digest differs')
        last_content = body['messages'][-1]['content']
        if (not isinstance(last_content, list) or not last_content or not isinstance(last_content[-1], dict) or
                not isinstance(last_content[-1].get('cache_control'), dict) or
                last_content[-1].get('cache_control', {}).get('type') != 'ephemeral' or
                not isinstance(row.get('version'), str)):
            raise UnsafePrefix('cache checkpoint or CLI version unavailable')
        return {'body': body, 'digest': row['requestDigest'], 'version': row['version']}
    except (KeyError, TypeError, IndexError) as exc:
        raise UnsafePrefix('incomplete recorded request') from exc


def prepare(fork, parent, betas):
    """Restore recorded cache checkpoints only after verifying the whole prefix.

    The CLI omits the billing header from its transcript digest. Its prefix and
    version are checked, but the complete header cannot be reconstructed here.
    """
    try:
        output = copy.deepcopy(fork)
        expected = parent['body']
        systems = output['system']
        header = systems[0]
        pattern = r'^x-anthropic-billing-header: cc_version=' + re.escape(parent['version']) + r'\.[A-Za-z0-9]+; cc_entrypoint=cli;'
        if not isinstance(header, dict) or not re.match(pattern, header.get('text', '')):
            raise UnsafePrefix('unrecognized billing header')
        count = len(expected['messages'])
        if len(output['messages']) <= count:
            raise UnsafePrefix('fork has no new ping message')
        _check_tools(output['tools'])
        if (_strip(systems[1:]) != _strip(expected['system']) or
                _strip(output['tools']) != _strip(expected['tools']) or
                _strip(output['messages'][:count]) != _strip(expected['messages'])):
            raise UnsafePrefix('fork prefix differs from parent')
        output['system'][1:] = copy.deepcopy(expected['system'])
        output['tools'] = copy.deepcopy(expected['tools'])
        output['messages'] = copy.deepcopy(expected['messages']) + _strip(output['messages'][count:])
        projected = {key: value for key, value in output.items()
                     if key not in ('metadata', 'stream', 'thread', 'diagnostics', 'safeguards')}
        projected['system'] = projected['system'][1:]
        projected['messages'] = projected['messages'][:count]
        if 'betas' in expected:
            projected['betas'] = betas
        if not hmac.compare_digest(_digest(projected), parent['digest']):
            raise UnsafePrefix('fork request differs from recorded parent')
        return output
    except (KeyError, TypeError, IndexError) as exc:
        raise UnsafePrefix('invalid fork request') from exc
