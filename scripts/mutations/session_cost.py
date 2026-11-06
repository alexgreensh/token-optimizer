"""`measure.py session-cost` (5.13.51): plain-language "what did that session
cost?" answers. Each planted bug is a way the answer has gone, or would go,
confidently wrong; scripts/check_regression_guards.py plants it and the listed
tests must turn red.

Shape: (name, [test node ids], file_relpath, [(old, new), ...]).
"""

MEASURE = "skills/token-optimizer/scripts/measure.py"
SKILL = "skills/token-dashboard/SKILL.md"

T = "tests/test_session_cost.py"
HAND = T + "::test_garden_cost_matches_the_hand_worked_dollars"
PARITY = T + "::test_same_number_as_the_dashboard"
TEXT = T + "::test_text_output_reads_like_an_answer"
TWICE = T + "::test_a_session_stored_twice_counts_once"
PLUGIN_DB = T + "::test_finds_the_plugin_data_db_not_a_legacy_one"
CONFIG_DB = T + "::test_finds_the_db_under_claude_config_dir"
NO_WRITE = T + "::test_never_writes_the_db_even_while_a_writer_holds_the_lock"
BYTES = T + "::test_read_only_run_leaves_db_bytes_and_mtime_alone"
OLD_DB = T + "::test_an_older_install_db_is_read_as_is_never_migrated"
WORDS = T + "::test_all_words_must_match_and_short_words_are_whole_tokens"
FRONT = T + "::test_skill_frontmatter_lets_claude_pick_it_for_cost_questions"
ENTRY = T + "::test_skill_preamble_plus_command_answers_the_question"
DAY = T + "::test_a_multi_day_session_gives_the_asked_days_share"

MUTATIONS = [
    ("token-dashboard hidden from Claude again (plain questions never reach it)",
     [FRONT],
     SKILL,
     [("name: token-dashboard\n", "name: token-dashboard\ndisable-model-invocation: true\n")]),
    ("skill tells Claude to run a session-cost command that does not exist",
     [ENTRY],
     SKILL,
     [("\"$PY\" \"$MEASURE_PY\" session-cost <the user's words> --json",
       "\"$PY\" \"$MEASURE_PY\" session-costs <the user's words> --json")]),
]
