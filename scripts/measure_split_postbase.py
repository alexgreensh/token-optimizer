"""Commands added after the frozen BASE have no BASE bytes to match.

The golden job compares every manifest case byte for byte against BASE. A CLI
command that did not exist at BASE prints the usage text there, so its case
can never match. ``post_base_commands`` in cases.json lists such commands; only
their own cases are exempt from the BASE byte comparison. Their capture still
runs, so a crash or traceback is still caught.

The list may only name commands that are absent from BASE and present in the
current manifest. A BASE command in the list is an error, which keeps the
exemption from ever hiding a real regression.
"""
from __future__ import annotations


def validate(manifest, base_commands):
    """Errors for a bad exemption list (empty list means valid)."""
    listed = manifest.get('post_base_commands', [])
    errors = []
    if not isinstance(listed, list) or any(not isinstance(c, str) for c in listed):
        return ['post_base_commands must be a list of command names']
    if len(set(listed)) != len(listed):
        errors.append('post_base_commands has duplicates')
    spellings = set(manifest.get('spellings', []))
    for command in listed:
        if command in base_commands:
            errors.append(f'{command}: exists at BASE, so it cannot be exempt from the BASE byte comparison')
        if command not in spellings:
            errors.append(f'{command}: not a current command spelling')
    return errors


def exempt_case_ids(manifest):
    """Ids of the cases for the listed commands (plain and every flag variant)."""
    listed = set(manifest.get('post_base_commands', []))
    return {row['id'] for row in manifest['cases']
            if row.get('entry') == 'cli' and row.get('argv') and row['argv'][0] in listed}


def split_errors(errors, exempt_ids):
    """(kept, exempted). Only ``<verb>: <case-id>/<file>`` lines for exempt cases are dropped."""
    kept, exempted = [], []
    for error in errors:
        _, _, name = error.partition(': ')
        if name.split('/', 1)[0] in exempt_ids and '/' in name:
            exempted.append(error)
        else:
            kept.append(error)
    return kept, exempted
