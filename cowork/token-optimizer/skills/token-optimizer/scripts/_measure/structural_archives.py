"""Discover dated setup archives without changing the trim ledger."""
import re
from datetime import datetime
from pathlib import Path

from . import structural_ledger as ledger


def _listing_text(path):
    try:
        lines = path.read_text(encoding='utf-8', errors='replace').lstrip('\ufeff').splitlines()
    except OSError:
        return ''
    if not lines or lines[0].strip() != '---':
        return ''
    fields, current = {}, None
    for line in lines[1:]:
        if line.strip() == '---':
            break
        match = re.match(r'^(name|description):\s*(.*)$', line)
        if match:
            current = match.group(1)
            fields[current] = match.group(2).strip(' \"\'')
        elif line[:1].isspace() and current:
            fields[current] += ' ' + line.strip()
        else:
            current = None
    return (fields.get('name', '') + ' ' + fields.get('description', '')).strip()


def discover(claude_dir, existing, estimate):
    """Estimate missing historic listing trims from dated, still-archived items."""
    root = Path(claude_dir)
    records, seen = [], set()
    previous = {}
    for item in existing:
        previous.setdefault((item.get('component'), item.get('item'), item.get('scope')), []).append(item)
    patterns = ((root / 'archive', 'agents-', 'agents'),
                (root / '_backups', 'skills-archived-', 'skills'))
    folders = []
    for parent, prefix, component in patterns:
        try:
            for folder in parent.iterdir():
                suffix = folder.name[len(prefix):] if folder.name.startswith(prefix) else ''
                if not folder.is_dir() or not re.fullmatch(r'\d{4}-?\d{2}-?\d{2}', suffix):
                    continue
                try:
                    date = datetime.strptime(suffix.replace('-', ''), '%Y%m%d')
                except ValueError:
                    continue
                folders.append((date, component, folder))
        except OSError:
            continue
    for date, component, folder in sorted(folders):
        candidates = sorted(folder.glob('*.md')) if component == 'agents' else sorted(folder.glob('*/SKILL.md'))
        for path in candidates:
            name = path.stem if component == 'agents' else path.parent.name
            live = root / component / (name + '.md' if component == 'agents' else name + '/SKILL.md')
            key = (component, name, 'global')
            if key in seen or live.exists():
                continue
            seen.add(key)
            text = _listing_text(path)
            if not text:
                continue
            tokens = int(estimate(text) or 0)
            if tokens <= 0:
                continue
            ts = date.isoformat() + '+00:00'
            later = [t for t in previous.get(key, []) if t.get('ts_epoch') is not None]
            if any(t['ts_epoch'] <= ledger.to_epoch(ts) for t in later):
                continue
            record = ledger.make_trim(component, name, 'global', tokens, 0, ts, 'dated_archive',
                                      evidence=str(path), origin='dated_archive')
            records.append(record)
            if later:
                earliest = min(later, key=lambda t: t['ts_epoch'])
                records.append({'kind': 'close', 'trim_id': record['id'], 'ts': earliest['ts'], 'reason': 'later recorded trim'})
    return ledger.load_trims_from(records)
