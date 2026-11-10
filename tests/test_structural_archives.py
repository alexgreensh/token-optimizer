import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'skills/token-optimizer/scripts'))
from _measure import structural_archives as archives, structural_ledger as ledger


def test_dated_archives_discover_only_missing_items_and_never_write(tmp_path):
    folder = tmp_path / 'archive' / 'agents-2026-10-30'
    folder.mkdir(parents=True)
    (folder / 'old.md').write_text('---\nname: old\ndescription: useful helper\n---\nbody')
    (folder / 'live.md').write_text('---\nname: live\ndescription: still active\n---\nbody')
    live = tmp_path / 'agents'
    live.mkdir()
    (live / 'live.md').write_text('active')
    skill = tmp_path / '_backups' / 'skills-archived-20260826' / 'old-skill'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('---\nname: old-skill\ndescription: useful skill\n---\nbody')
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*'))
    rows = archives.discover(tmp_path, [], lambda text: len(text))
    assert {(r['component'], r['item'], r['ts'][:10], r['tier']) for r in rows} == {
        ('agents', 'old', '2026-10-30', 'estimated'), ('skills', 'old-skill', '2026-08-26', 'estimated')}
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*')) == before


def test_earlier_archive_stops_when_same_item_is_later_recorded(tmp_path):
    folder = tmp_path / 'archive' / 'agents-2026-10-30'
    folder.mkdir(parents=True)
    (folder / 'old.md').write_text('---\nname: old\ndescription: helper\n---')
    later = ledger.make_trim('agents', 'old', 'global', 50, 0, '2026-11-09T00:00:00+00:00', 'audit_action')
    rows = archives.discover(tmp_path, ledger.load_trims_from([later]), lambda text: len(text))
    assert len(rows) == 1 and rows[0]['closed_at'] == later['ts']
