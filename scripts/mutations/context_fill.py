"""Context fill shown to the model (5.13.52). With a reduced auto-compact window,
messages led with the share of the compact window ("Context 76%") while the
user's meter showed the share of the model window (38%); sessions then claimed
to be nearly full. Each planted bug brings that back; the listed tests must go red.

Shape: (name, [test node ids], file_relpath, [(old, new), ...]).
"""

MEASURE = "skills/token-optimizer/scripts/measure.py"
T = "tests/test_context_window_disclosure.py"

MUTATIONS = [
]
