"""Definition-only providers for the measure compatibility facade."""

# Shared by the facade and the bounded hook warmer. No filesystem discovery.
DOMAIN_MODULES = (
    'runtime', 'pricing', 'audit', 'dashboard', 'lifecycle', 'sessions',
    'trends', 'cache_policy', 'daemon', 'quality_archive', 'continuity',
    'savings', 'cli',
)
# Loaded on first use instead of at facade import, only when the importer sets
# HOOK_IMPORT. No module-level facade code and no UserPromptSubmit handler
# uses them (perf attribution, 2026-11-06).
DEFERRED_MODULES = ('audit', 'dashboard', 'sessions', 'savings', 'cli')
RUNTIME_SOURCES = ('__init__.py', '_compat.py') + tuple(name + '.py' for name in DOMAIN_MODULES) + ('dashboard_history.py', 'weekly_activity.py', 'headline.py', 'counted_position.py', 'counted_pricing_v5.py', 'transcript_backfill.py', 'counted_subagents.py', 'lean_resume_threshold.py', 'output_reread.py', 'session_effect.py', 'structural_ledger.py', 'structural_archives.py', 'structural_listing.py', 'structural_report.py', 'structural_seed.py', 'structural_value.py', 'rework_metrics.py', 'session_best.py', 'savings_story.py', 'counted_orphans.py', 'counted_cap.py', 'archive_void_evidence.py', 'cost_per_message_trend.py', 'lean_resume_receivers.py', 'routing_period.py', 'fixture_context.py', 'transcript_product.py', 'periphery_exposure.py', 'structure_funnel.py', 'setup_attachment_counter.py', 'first_read_outcomes.py', 'setup_budget_monitor.py', 'setup_connector_ledger.py', 'first_call_context.py', 'first_read_revive.py', 'mcp_archive_tripwire.py', 'compaction_survival.py', 'archive_void_codex.py', 'compression_fixture_void.py')
# Set True by hooks/userpromptsubmit_runner.py right before `import measure`,
# which consumes it. That process never patches or enumerates the facade.
HOOK_IMPORT = False
