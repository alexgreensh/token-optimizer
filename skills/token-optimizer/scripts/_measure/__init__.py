"""Definition-only providers for the measure compatibility facade."""

# Shared by the facade and the bounded hook warmer. No filesystem discovery.
DOMAIN_MODULES = (
    'runtime', 'pricing', 'audit', 'dashboard', 'lifecycle', 'sessions',
    'trends', 'cache_policy', 'daemon', 'quality_archive', 'continuity',
    'savings', 'cli',
)
RUNTIME_SOURCES = ('__init__.py', '_compat.py') + tuple(name + '.py' for name in DOMAIN_MODULES)
