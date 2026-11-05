"""Negative control: a same-domain call that bypasses the patched facade."""
MUTATIONS = [
    ('split pricing bypasses the live same-domain helper',
     ['tests/test_measure_split_compat.py::test_moved_price_lookup_uses_live_helper_and_shared_dictionary'],
     'skills/token-optimizer/scripts/measure.py',
     [{'symbol': '_get_model_cost',
       'old': '(_measure_facade._normalize_openai_model_name,',
       'new': '(lambda _model: None,'}]),
]
