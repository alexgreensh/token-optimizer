"""Correct CPython 3.9 multiline f-string expression columns before text edits."""
from __future__ import annotations
import ast
import copy
import io
import sys
import re
from functools import lru_cache
import tokenize


@lru_cache(maxsize=1)
def _fstring_tokens(source):
    return tuple(t for t in tokenize.generate_tokens(io.StringIO(source).readline)
                 if t.type == tokenize.STRING and re.match(r"(?i)^[rubf]*f[rubf]*['\"]", t.string))


@lru_cache(maxsize=1)
def _source_lines(source):
    return tuple(line.encode('utf-8') for line in source.splitlines(keepends=True))


def _expression_text(source, node):
    lines = _source_lines(source)
    first, last = node.lineno - 1, node.end_lineno - 1
    if first == last:
        return lines[first][node.col_offset:node.end_col_offset].decode('utf-8')
    return (lines[first][node.col_offset:] + b''.join(lines[first+1:last])
            + lines[last][:node.end_col_offset]).decode('utf-8')


def _same_expression(source, node, expected):
    try:
        text = _expression_text(source, node)
        parsed = ast.parse('(' + text + ')', mode='eval').body
    except (SyntaxError, TypeError, UnicodeError, IndexError):
        return False
    return ast.dump(parsed) == ast.dump(expected)


def _inside_field(source, node, token):
    """A matching expression slice must be executable, never escaped text."""
    lines = token.string.encode('utf-8').splitlines(keepends=True)
    opening = len(_source_lines(source)[token.start[0]-1].decode('utf-8')[:token.start[1]].encode('utf-8'))
    def relative(line, column):
        row = line - token.start[0]
        return sum(map(len, lines[:row])) + column - (opening if row == 0 else 0)
    start = relative(node.lineno, node.col_offset)
    end = relative(node.end_lineno, node.end_col_offset)
    raw = token.string.encode('utf-8')
    if not 0 <= start < end <= len(raw):
        return False
    marker = '_measure_position_probe'
    while marker in token.string:
        marker += '_'
    try:
        probe = (raw[:start] + marker.encode() + raw[end:]).decode('utf-8')
        tree = ast.parse('(' + probe + ')', mode='eval')
    except (SyntaxError, UnicodeError):
        return False
    return sum(isinstance(n, ast.Name) and n.id == marker for n in ast.walk(tree)) == 1


def _tokens_with_nested_literals(source, joined):
    tokens = list(_fstring_tokens(source))
    lines = _source_lines(source)
    for node in ast.walk(joined):
        if not isinstance(node, ast.JoinedStr) or node is joined or not _same_expression(source, node, node):
            continue
        text = _expression_text(source, node)
        prefix = len(lines[node.lineno-1][:node.col_offset].decode('utf-8'))
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type != tokenize.STRING or not re.match(r"(?i)^[rubf]*f[rubf]*['\"]", token.string):
                continue
            def absolute(position):
                line, column = position
                return node.lineno + line - 1, column + (prefix if line == 1 else 0)
            tokens.append(tokenize.TokenInfo(token.type, token.string,
                          absolute(token.start), absolute(token.end), token.line))
    return tokens


def repair_fstring_positions(source, joined):
    """Use actual string-token bounds and reparse each corrected expression.

    Python 3.9 adds the opening token's column plus one to expression columns
    on later physical lines. Continuation lines within an expression are not
    shifted. Never guess an edit location: the corrected slice must parse to
    exactly the original expression AST, with one unambiguous span.
    """
    if sys.version_info >= (3, 10):
        return
    lines = _source_lines(source)
    tokens = _tokens_with_nested_literals(source, joined)
    for field in ast.walk(joined):
        if not isinstance(field, ast.FormattedValue) or getattr(field, '_measure_positions_verified', False):
            continue
        value = field.value
        matches = {}
        for token in tokens:
            if not token.start[0] < value.lineno <= token.end[0]:
                continue
            delta = len(lines[token.start[0]-1].decode('utf-8')[:token.start[1]].encode('utf-8')) + 1
            candidate = copy.deepcopy(value)
            for child in ast.walk(candidate):
                if getattr(child, 'lineno', None) == value.lineno:
                    child.col_offset -= delta
                if getattr(child, 'end_lineno', None) == value.lineno:
                    child.end_col_offset -= delta
            if candidate.col_offset < 0 or candidate.end_col_offset < 0:
                continue
            start = (candidate.lineno, candidate.col_offset)
            end = (candidate.end_lineno, candidate.end_col_offset)
            token_start = (token.start[0], len(lines[token.start[0]-1].decode('utf-8')[:token.start[1]].encode('utf-8')))
            token_end = (token.end[0], len(lines[token.end[0]-1].decode('utf-8')[:token.end[1]].encode('utf-8')))
            if not token_start <= start <= end <= token_end:
                continue
            if _same_expression(source, candidate, value) and _inside_field(source, candidate, token):
                matches[start, end] = candidate
        if len(matches) == 1:
            fixed = next(iter(matches.values()))
            for original, corrected in zip(ast.walk(value), ast.walk(fixed)):
                for name in ('lineno', 'col_offset', 'end_lineno', 'end_col_offset'):
                    if hasattr(corrected, name):
                        setattr(original, name, getattr(corrected, name))
        elif matches or not (_same_expression(source, value, value) and
                            any(_inside_field(source, value, token) for token in tokens
                                if token.start[0] <= value.lineno <= token.end[0])):
            raise ValueError('unverified Python 3.9 f-string expression positions')
        field._measure_positions_verified = True
