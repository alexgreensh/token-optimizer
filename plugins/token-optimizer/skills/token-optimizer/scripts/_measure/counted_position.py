"""Recognize cache rebuilds that reach the original removal position."""


def is_rebuild(cache_class, tokens, context, removal_context):
    if not cache_class or len(cache_class) < 4:
        return False
    written, _, _, read = cache_class[:4]
    if not isinstance(written, int) or not isinstance(read, int):
        return False
    try:
        size = abs(int(tokens))
    except (TypeError, ValueError):
        return False
    if written <= 0 or size <= 0 or written < size:
        return False
    if context is None or removal_context is None:
        return read == 0
    return (isinstance(context, int) and isinstance(removal_context, int)
            and removal_context > 0 and context >= removal_context
            and read < removal_context * 0.97)
