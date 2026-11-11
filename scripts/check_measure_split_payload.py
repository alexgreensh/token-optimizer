#!/usr/bin/env python3
"""Verify the candidate's actual installed Python payload, including mirrors."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from measure_split import REPO, DOMAINS

SOURCES = ('__init__.py', '_compat.py') + tuple(name + '.py' for name in DOMAINS) + ('dashboard_history.py', 'weekly_activity.py', 'headline.py', 'structural_ledger.py', 'structural_archives.py', 'structural_listing.py', 'structural_report.py', 'structural_seed.py', 'structural_value.py', 'counted_position.py', 'counted_pricing_v5.py', 'transcript_backfill.py', 'counted_subagents.py', 'lean_resume_threshold.py', 'output_reread.py', 'session_effect.py', 'rework_metrics.py', 'fixture_context.py', 'counted_orphans.py', 'counted_cap.py', 'archive_void_evidence.py', 'cost_per_message_trend.py', 'lean_resume_receivers.py', 'routing_period.py', 'session_best.py', 'savings_story.py', 'transcript_product.py', 'periphery_exposure.py', 'structure_funnel.py', 'setup_attachment_counter.py', 'first_read_outcomes.py', 'setup_budget_monitor.py', 'setup_connector_ledger.py', 'first_call_context.py', 'timestamp_cache.py', 'token_type_split.py', 'evidence_parallel.py', 'ttl_period.py', 'first_read_revive.py', 'mcp_archive_tripwire.py', 'compaction_survival.py', 'archive_void_codex.py', 'compression_fixture_void.py', 'feature_health.py', 'archive_health.py', 'coverage_report.py', 'file_refetch.py', 'holdout_report.py', 'structural_dedupe.py', 'setup_step.py')


SOURCES += ('transcript_stream.py',)


def verify(payload, canonical=REPO):
    relative = Path('skills/token-optimizer/scripts/_measure')
    errors = []
    for name in SOURCES:
        actual = payload / relative / name
        expected = canonical / relative / name
        if not actual.is_file():
            errors.append('missing installed provider: ' + str(actual))
        elif actual.read_bytes() != expected.read_bytes():
            errors.append('provider bytes differ: ' + str(actual))
    extra = {path.name for path in (payload / relative).glob('*.py')} - set(SOURCES)
    if extra:
        errors.append('unmanifested installed providers: ' + repr(sorted(extra)))
    return errors


def smoke(payload, work):
    home = work / 'home é with spaces'
    home.mkdir(parents=True, exist_ok=True)
    cwd = work / 'outside source'
    cwd.mkdir(parents=True, exist_ok=True)
    env = {key: value for key, value in os.environ.items()
           if key != 'PYTHONDONTWRITEBYTECODE' and not key.startswith(('CLAUDE', 'CODEX', 'TOKEN_OPTIMIZER', 'HERMES', 'COPILOT', 'CURSOR', 'GROK', 'ANTIGRAVITY', 'DEVIN'))}
    env.update({'HOME': str(home), 'USERPROFILE': str(home), 'CLAUDE_CONFIG_DIR': str(home),
                'XDG_CACHE_HOME': str(home / '.cache'),
                # Compile the 3 MB facade once, outside the payload, not once per call.
                'PYTHONPYCACHEPREFIX': str(work / 'pycache'),
                'TOKEN_OPTIMIZER_REMOTE_LEAN': '1', 'TOKEN_OPTIMIZER_NO_PROC_SCAN': '1',
                'TOKEN_OPTIMIZER_COLD_COMPILE_WAIT_S': '-1', 'PYTHONUTF8': '1'})
    measure = payload / 'skills/token-optimizer/scripts/measure.py'
    commands = ([sys.executable, str(measure), '--version'],
                [sys.executable, str(payload / 'hooks/run.py'), 'skills/token-optimizer/scripts/measure.py', '--version'])
    outputs = []
    for argv in commands:
        proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=30)
        if proc.returncode or not proc.stdout.strip() or proc.stderr:
            raise RuntimeError(f'{argv}: exit={proc.returncode}, stdout={proc.stdout!r}, stderr={proc.stderr!r}')
        outputs.append(proc.stdout)
    if outputs[0] != outputs[1]:
        raise RuntimeError('direct CLI and real hook launcher version bytes differ')
    for name in ('direct', 'hook'):
        normal_home = work / name / 'home é'
        normal_home.mkdir(parents=True)
        settings = normal_home / 'settings.json'
        settings.write_text(json.dumps({'unrelated': True, 'mcpServers': {'fixture': {'command': 'fixture-mcp'}}}),
                            encoding='utf-8')
        normal_env = {**env, 'HOME': str(normal_home), 'USERPROFILE': str(normal_home),
                      'CLAUDE_CONFIG_DIR': str(normal_home), 'TOKEN_OPTIMIZER_RUNTIME': 'claude',
                      'XDG_CACHE_HOME': str(normal_home / '.cache'),
                      'TOKEN_OPTIMIZER_SNAPSHOT_DIR': str(normal_home / 'data')}
        argv = [sys.executable, str(measure)] if name == 'direct' else [
            sys.executable, str(payload / 'hooks/run.py'), 'skills/token-optimizer/scripts/measure.py']
        proc = subprocess.run([*argv, 'api-manage', 'mcp/disable', 'fixture'], cwd=cwd,
                              env=normal_env, input=b'{}', capture_output=True, timeout=30)
        if proc.returncode or proc.stderr or json.loads(proc.stdout).get('ok') is not True:
            raise RuntimeError(f'installed {name} JSON management failed: {proc.stdout!r} {proc.stderr!r}')
        saved = json.loads(settings.read_text(encoding='utf-8'))
        if not saved.get('unrelated') or 'fixture' not in saved.get('_disabledMcpServers', {}):
            raise RuntimeError('installed management did not preserve and update settings')
        code = ('import sys, json; sys.path.insert(0, sys.argv[1]); import measure as m; '
                's=m._get_savings_summary(); print(json.dumps({'
                '"cost":m._get_model_cost("claude-sonnet-4-20250514",1000000,1000000,tier="anthropic"),'
                '"integer":m._safe_int("7"),"tokens":s["total_tokens"],"saved":s["total_cost_usd"]}))')
        proc = subprocess.run([sys.executable, '-c', code, str(measure.parent)], cwd=cwd,
                              env=normal_env, capture_output=True, timeout=30)
        if proc.returncode or proc.stderr or json.loads(proc.stdout) != {'cost': 18, 'integer': 7, 'tokens': 0, 'saved': 0}:
            raise RuntimeError(f'installed pricing/accounting failed: {proc.stdout!r} {proc.stderr!r}')
    return outputs[0].decode().strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--payload', type=Path)
    parser.add_argument('--work', type=Path)
    parser.add_argument('--mirrors', action='store_true')
    args = parser.parse_args()
    payloads = [args.payload.resolve()] if args.payload else []
    if args.mirrors:
        payloads += [REPO / 'plugins/token-optimizer', REPO / 'cowork/token-optimizer']
    errors = [error for payload in payloads for error in verify(payload)]
    if not payloads:
        parser.error('select --payload or --mirrors')
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        return 1
    if args.payload and args.work:
        print('installed CLI/hook smoke: ' + smoke(args.payload.resolve(), args.work.resolve()))
    print(f'{len(payloads)} complete payload(s), {len(SOURCES)} provider files each')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
