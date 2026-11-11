"""Recover recent user direction and standing constraints from Claude transcripts."""
import json
import re
from collections import deque

_RULE = re.compile(r"(?:\b(?:never|always|must|don['’]t|do not|only|cap)\b|^no\s+(?!no\b)\w+)", re.I)
_PING = re.compile(r"^(?:ok(?:ay)?|yes|yep|no|nope|thanks|thank you|continue|proceed|go on|remove|status(?:\s+(?:plz|please))?|/\S+)[.!?\s]*$", re.I)


def scan(path, user_text, redact, checkpoint_state, max_line):
    first = None
    recent = deque(maxlen=3)
    constraints = []
    try:
        with open(path, encoding='utf-8', errors='replace') as stream:
            for line in stream:
                if len(line) > max_line:
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(record, dict) or record.get('type') != 'user':
                    continue
                raw = user_text(record)
                if not raw.strip():
                    continue
                text = redact(raw).strip()
                ident = record.get('uuid')
                if first is None:
                    first = {'text': text, 'source': {'kind': 'user_message', 'id': ident}}
                # Keep original line breaks and punctuation after redaction.
                if not _PING.fullmatch(text) and len(text.split()) >= 2:
                    recent.append({'text': text[:400], 'source': {'kind': 'user_message', 'id': ident}})
                for sentence in checkpoint_state._sentences(text):
                    cue = _RULE.search(sentence)
                    if not cue or sentence.rstrip().endswith('?'):
                        continue
                    start = max(0, cue.start() - 60) if cue.end() > 300 else 0
                    excerpt = checkpoint_state._clip(('… ' if start else '') + sentence[start:], 300)
                    key = checkpoint_state._norm(excerpt)
                    constraints = [c for c in constraints if checkpoint_state._norm(c['text']) != key]
                    constraints.append({'text': excerpt, 'source': {'kind': 'user_message', 'id': ident}})
                    constraints = constraints[-8:]
    except OSError:
        pass
    return first, list(recent), constraints
