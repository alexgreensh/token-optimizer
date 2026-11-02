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
    ("Opus 5.5 billed at the opus family card, not its generation card",
     [HAND],
     MEASURE,
     [("            for key in ((f\"{fam}_{major}_{minor}\", f\"{fam}_{major}\") if minor else (f\"{fam}_{major}\",)):",
       "            for key in ():")]),
    ("session-cost breakdown prices input at the family card",
     [HAND],
     MEASURE,
     [("        c_in = _get_model_cost(model, fi, 0, 0, 0, tier=tier)",
       "        c_in = _get_model_cost(_normalize_model_name(model), fi, 0, 0, 0, tier=tier)")]),
    ("subagent cost added on top of a total that already holds it",
     [HAND, PARITY, TEXT],
     MEASURE,
     [("    total = float(priced[\"total_cost\"])\n",
       "    total = float(priced[\"total_cost\"]) + max(0.0, float(priced[\"total_cost\"]) - float(priced[\"main_cost\"]))\n")]),
    ("session total drops the subagents (main thread only)",
     [HAND, TEXT],
     MEASURE,
     [("        total_cost = sum(sl[1] for sl in slices)\n",
       "        total_cost = session_cost\n")]),
    ("a session stored under two paths is counted twice",
     [TWICE],
     MEASURE,
     [("        key = _session_cost_row_id(r)\n        rank = _session_rank(",
       "        key = (_session_cost_row_id(r), r[\"id\"])\n        rank = _session_rank(")]),
    ("session-cost reads the legacy ~/.claude/token-optimizer/trends.db",
     [PLUGIN_DB, CONFIG_DB],
     MEASURE,
     [("    if not TRENDS_DB.exists():\n        out[\"error\"] = (\"No Token Optimizer history yet",
       "    TRENDS_DB = RUNTIME_DIR / \"token-optimizer\" / \"trends.db\"\n"
       "    if not TRENDS_DB.exists():\n        out[\"error\"] = (\"No Token Optimizer history yet"),
      ("        \"db\": str(TRENDS_DB),\n        \"basis\": SESSION_COST_BASIS",
       "        \"db\": str(RUNTIME_DIR / \"token-optimizer\" / \"trends.db\"),\n        \"basis\": SESSION_COST_BASIS")]),
    ("session-cost opens trends.db through the collector's opener (schema, migrations, backfills)",
     [NO_WRITE, OLD_DB],
     MEASURE,
     [("    conn, mode = _session_cost_open_db(TRENDS_DB)\n",
       "    conn, mode = _init_trends_db(), \"read-only\"\n    conn.row_factory = sqlite3.Row\n")]),
    ("short words match inside longer ones ('tr' in 'trends')",
     [WORDS],
     MEASURE,
     [("        if (w in tokens) if len(w) <= 3 else (w in hay):",
       "        if w in hay:")]),
    ("any one word is enough to match a session",
     [WORDS],
     MEASURE,
     [("    matched = [r for r in scoped if not words or _session_cost_word_hits(words, r) == len(words)]",
       "    matched = [r for r in scoped if not words or _session_cost_word_hits(words, r) > 0]")]),
    ("token-dashboard hidden from Claude again (plain questions never reach it)",
     [FRONT],
     SKILL,
     [("name: token-dashboard\n", "name: token-dashboard\ndisable-model-invocation: true\n")]),
    ("skill tells Claude to run a session-cost command that does not exist",
     [ENTRY],
     SKILL,
     [("\"$PY\" \"$MEASURE_PY\" session-cost <the user's words> --json",
       "\"$PY\" \"$MEASURE_PY\" session-costs <the user's words> --json")]),
    ("a multi-day session's 'yesterday' share is the whole session",
     [DAY],
     MEASURE,
     [("            sum(sl[1] for sl in priced[\"slices\"] if sl[0] == date_filter), 4)",
       "            sum(sl[1] for sl in priced[\"slices\"]), 4)")]),
]
