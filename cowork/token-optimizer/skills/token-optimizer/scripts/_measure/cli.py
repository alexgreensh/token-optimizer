"""Definition-only implementations; mutable state belongs to measure."""
from __future__ import annotations
from . import _compat, coverage_report


def main():
    _measure_facade = _compat.facade()
    # On a non-UTF-8 host locale, re-exec under Python UTF-8 mode FIRST so every
    # subprocess(text=True) decode (ps/git session detection), default open(), and
    # stdio become UTF-8 -- closing the whole non-ASCII crash class. No-op for
    # UTF-8 users and for scripts already launched with PYTHONUTF8=1 by run.py.
    _measure_facade.reexec_in_utf8_mode()
    # Belt-and-suspenders for the case where re-exec was unavailable.
    _measure_facade.enforce_utf8_io()
    _measure_facade.args = _measure_facade.sys.argv[1:]

    # Parse global --context-size flag (applies to all commands)
    _measure_facade._filtered_args = []
    _measure_facade.i = 0
    while _measure_facade.i < len(_measure_facade.args):
        if _measure_facade.args[_measure_facade.i] == "--context-size" and _measure_facade.i + 1 < len(_measure_facade.args):
            try:
                _measure_facade._cli_context_size = int(_measure_facade.args[_measure_facade.i + 1])
            except ValueError:
                print(f"[Error] Invalid --context-size value: {_measure_facade.args[_measure_facade.i + 1]}")
                _measure_facade.sys.exit(1)
            _measure_facade.i += 2
        else:
            _measure_facade._filtered_args.append(_measure_facade.args[_measure_facade.i])
            _measure_facade.i += 1
    _measure_facade.args = _measure_facade._filtered_args

    # Windows Codex: hooks saved by older versions start with a quote, which
    # cmd.exe mangles, so they fail before reaching this script and cannot
    # fix themselves (issue #276). Any Token Optimizer run on that machine
    # repairs them in place. One small JSON read; never raises.
    if _measure_facade.sys.platform == "win32" and _measure_facade.detect_runtime() == "codex":
        try:
            import codex_install as _measure_import_0; _measure_facade._ci = _measure_import_0; del _measure_import_0
            _measure_facade._healed = _measure_facade._ci.heal_quoted_launcher_commands()
            if _measure_facade._healed:
                print(f"[Token Optimizer] Repaired {_measure_facade._healed} Codex hook command(s) that Windows could not run. Restart Codex to load them.", file=_measure_facade.sys.stderr)
        except Exception:
            pass

    # --- Foreign-runtime guardrail (cross-platform universality) -
    # OpenCode loads ~/.claude/skills by default, Copilot and Hermes can be
    # invoked from their own sessions, so any of them can reach this skill even
    # though the user is NOT in Claude Code. The Claude audit/fix commands below
    # scan and (for skill/mcp/plugin-cleanup) MUTATE ~/.claude — the wrong
    # target in that situation. Redirect those to a safe per-runtime notice
    # instead of touching ~/.claude. Runtime-data commands are unaffected: they
    # use RUNTIME_DIR, which resolves to the foreign runtime's own home.
    #
    # Per-runtime exemptions (_FOREIGN_RUNTIME_EXEMPTIONS): a foreign runtime
    # may exempt a _CLAUDE_TARGET_CMDS subcommand when a NATIVE flow invokes it
    # as a top-level command AND the command is runtime-aware (no ~/.claude
    # scan). Hermes exempts `dashboard` (hermes_hook_bridge.run_dashboard shells
    # to `measure.py dashboard`; the dashboard is hermes-aware — see the
    # exemption table docstring). Exempted commands fall through to their
    # normal handler; everything else gets the audit notice + exit 0.
    if _measure_facade._is_foreign_runtime() and (not _measure_facade.args or _measure_facade.args[0] in _measure_facade._CLAUDE_TARGET_CMDS):
        _measure_facade._runtime = _measure_facade.detect_runtime()
        _measure_facade._exempt = _measure_facade._FOREIGN_RUNTIME_EXEMPTIONS.get(_measure_facade._runtime, frozenset())
        if _measure_facade.args and _measure_facade.args[0] in _measure_facade._exempt:
            pass  # exempt — fall through to the normal command handler
        else:
            _measure_facade._foreign_audit_notice()
            _measure_facade.sys.exit(0)

    if not _measure_facade.args or _measure_facade.args[0] == "report":
        _measure_facade.full_report()
    elif _measure_facade.args[0] == "quick":
        _measure_facade.output_json = "--json" in _measure_facade.args
        _measure_facade.quick_scan(as_json=_measure_facade.output_json)
    elif _measure_facade.args[0] == "route":
        _measure_facade._cmd_route(_measure_facade.args)
    elif _measure_facade.args[0] == "cloud-setup-script":
        import cloud_kit as _measure_import_1; _measure_facade.cloud_kit = _measure_import_1; del _measure_import_1
        _measure_facade.sys.exit(_measure_facade.cloud_kit.main_setup_script(_measure_facade.args[1:]))
    elif _measure_facade.args[0] == "cloud-doctor":
        import cloud_kit as _measure_import_2; _measure_facade.cloud_kit = _measure_import_2; del _measure_import_2
        _measure_facade.sys.exit(_measure_facade.cloud_kit.main_doctor(_measure_facade.args[1:]))
    elif _measure_facade.args[0] == "doctor":
        _measure_facade.output_json = "--json" in _measure_facade.args
        if _measure_facade.detect_runtime() == "codex":
            import codex_doctor as _measure_import_3; _measure_facade.codex_doctor = _measure_import_3; del _measure_import_3
            _measure_facade.sys.exit(_measure_facade.codex_doctor.main(_measure_facade.args[1:]))
        _measure_facade.doctor(as_json=_measure_facade.output_json)
    elif _measure_facade.args[0] == "codex-doctor":
        import codex_doctor as _measure_import_4; _measure_facade.codex_doctor = _measure_import_4; del _measure_import_4
        _measure_facade.sys.exit(_measure_facade.codex_doctor.main(_measure_facade.args[1:]))
    elif _measure_facade.args[0] == "codex-compact-prompt":
        import codex_compact_prompt as _measure_import_5; _measure_facade.codex_compact_prompt = _measure_import_5; del _measure_import_5
        _measure_facade.sys.exit(_measure_facade.codex_compact_prompt.main(_measure_facade.args[1:]))
    elif _measure_facade.args[0] == "codex-status-line":
        import codex_statusline as _measure_import_6; _measure_facade.codex_statusline = _measure_import_6; del _measure_import_6
        _measure_facade.sys.exit(_measure_facade.codex_statusline.main(_measure_facade.args[1:]))
    elif _measure_facade.args[0] == "codex-install":
        import codex_install as _measure_import_7; _measure_facade.codex_install = _measure_import_7; del _measure_import_7
        _measure_facade.sys.exit(_measure_facade.codex_install.main(_measure_facade.args[1:]))
    elif _measure_facade.args[0] == "copilot-doctor":
        import copilot_doctor as _measure_import_8; _measure_facade.copilot_doctor = _measure_import_8; del _measure_import_8
        _measure_facade.sys.exit(_measure_facade.copilot_doctor.main(_measure_facade.args[1:]))
    elif _measure_facade.args[0] == "antigravity-doctor":
        import antigravity_doctor as _measure_import_9; _measure_facade.antigravity_doctor = _measure_import_9; del _measure_import_9
        _measure_facade.sys.exit(_measure_facade.antigravity_doctor.main(_measure_facade.args[1:]))
    elif _measure_facade.args[0] == "antigravity-install":
        import antigravity_install as _measure_import_10; _measure_facade.antigravity_install = _measure_import_10; del _measure_import_10
        _measure_facade.sys.exit(_measure_facade.antigravity_install.main(["install"] + _measure_facade.args[1:]))
    elif _measure_facade.args[0] == "antigravity-uninstall":
        import antigravity_install as _measure_import_11; _measure_facade.antigravity_install = _measure_import_11; del _measure_import_11
        _measure_facade.sys.exit(_measure_facade.antigravity_install.main(["uninstall"] + _measure_facade.args[1:]))
    elif _measure_facade.args[0] == "antigravity-home":
        from runtime_env import antigravity_home as _measure_import_12; _measure_facade.antigravity_home = _measure_import_12; del _measure_import_12  # noqa: PLC0415
        print(str(_measure_facade.antigravity_home()))
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] == "antigravity-rollup":
        # Ingest recent Antigravity sessions into trends.db. Fired by the stop
        # hook via antigravity_hook_bridge.handle_stop(). Wall-clock backstop:
        # same 60s rationale as copilot-rollup — the bridge spawns this
        # fire-and-forget, so cap it and fail open (the next Stop re-collects
        # idempotently).
        _measure_facade.quiet = "--quiet" in _measure_facade.args or "-q" in _measure_facade.args
        _measure_facade._tok_hook_deadline = _measure_facade._install_hook_budget(60)
        try:
            _measure_facade._collect_antigravity_sessions(days=90, quiet=_measure_facade.quiet)
        except _measure_facade._HookTimeout:
            pass
        except Exception:
            pass
        finally:
            _measure_facade._clear_hook_budget(_measure_facade._tok_hook_deadline)
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] == "antigravity-summary":
        _measure_facade._antigravity_summary()
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] == "copilot-install":
        # Intercept copilot-install to support a --home <path> flag that
        # overrides the copilot home. copilot_install.main() uses
        # argparse with a fixed action+dry-run signature, so we parse --home
        # out here and call copilot_install.install(home=...) directly,
        # mirroring main()'s output format. Without this, a WSL-root user who
        # sets COPILOT_HOME=/mnt/c/Users/<you>/.copilot would see the banner
        # (from copilot-home) show the right path but the actual install
        # still writes to /root/.copilot (runtime_env._is_safe_home_dir
        # rejects /mnt/c/... because it's not under $HOME=/root). The --home
        # flag lets install.sh forward the WSL-aware resolved path so the
        # install and the banner agree.
        import copilot_install as _measure_import_13; _measure_facade.copilot_install = _measure_import_13; del _measure_import_13  # noqa: PLC0415

        _measure_facade.rest = list(_measure_facade.args[1:])
        _measure_facade.override_home: str | None = None
        _measure_facade.dry_run = False
        _measure_facade.i = 0
        while _measure_facade.i < len(_measure_facade.rest):
            _measure_facade.tok = _measure_facade.rest[_measure_facade.i]
            if _measure_facade.tok == "--home" and _measure_facade.i + 1 < len(_measure_facade.rest):
                _measure_facade.override_home = _measure_facade.rest[_measure_facade.i + 1]
                _measure_facade.i += 2
                continue
            if _measure_facade.tok == "--dry-run":
                _measure_facade.dry_run = True
            _measure_facade.i += 1
        if _measure_facade.override_home is not None:
            try:
                _measure_facade.result = _measure_facade.copilot_install.install(dry_run=_measure_facade.dry_run, home=_measure_facade.Path(_measure_facade.override_home))
                _measure_facade.verb = "Would install" if _measure_facade.dry_run else "Installed"
                print(f"{_measure_facade.verb} Token Optimizer for GitHub Copilot CLI.")
                print(f"  Hook config: {_measure_facade.result['hook_file']}")
                print(f"  Modules: {len(_measure_facade.result['copied'])} copied"
                      + (f", {len(_measure_facade.result['skipped'])} missing: {_measure_facade.result['skipped']}" if _measure_facade.result["skipped"] else ""))
                print(_measure_facade._hint("  Run `python3 measure.py copilot-doctor` to verify readiness."))
                _measure_facade.sys.exit(0)
            except RuntimeError as _measure_exception_14:
                _measure_facade.exc = _measure_exception_14
                try:
                    print(f"[Token Optimizer] {_compat.read_global(_measure_facade, 'exc')}", file=_measure_facade.sys.stderr)
                    _measure_facade.sys.exit(1)
                finally:
                    _measure_facade.exc = None
                    del _measure_facade.exc
        _measure_facade.sys.exit(_measure_facade.copilot_install.main(["install"] + _measure_facade.args[1:]))
    elif _measure_facade.args[0] == "copilot-uninstall":
        import copilot_install as _measure_import_15; _measure_facade.copilot_install = _measure_import_15; del _measure_import_15
        _measure_facade.sys.exit(_measure_facade.copilot_install.main(["uninstall"] + _measure_facade.args[1:]))
    elif _measure_facade.args[0] == "copilot-home":
        # Print the resolved Copilot home directory. Honors COPILOT_HOME via
        # runtime_env.copilot_home → _safe_home_from_env (confined under $HOME
        # by the safe-home guard, plus the WSL-root /mnt/ opt-in). Used by
        # install.sh to show the TRUE hook destination in its success banner
        # instead of a hardcoded ~/.copilot path (WSL-root installs
        # wrote to /root/.copilot which the Windows Copilot CLI never reads).
        #
        # WSL-root /mnt/ opt-in: when running as root
        # under WSL, $HOME=/root, so the strict _is_safe_home_dir guard
        # REJECTS a COPILOT_HOME=/mnt/c/Users/<you>/.copilot path (it's not
        # under /root). runtime_env._safe_home_from_env now accepts such a
        # value when ALL of: WSL context detected (gated on /proc markers so
        # native-Linux /mnt stays strict), path absolute, under /mnt/, not a
        # symlink, exists and is a directory. The opt-in lives in
        # runtime_env so copilot_home(), codex_home(), hermes_home(), and the
        # opencode_*_home() resolvers all share it (single source of truth).
        # This subcommand delegates via _resolve_copilot_home_wsl_aware().
        from runtime_env import copilot_home as _measure_import_16; _measure_facade._ch = _measure_import_16; del _measure_import_16  # noqa: PLC0415

        _measure_facade.resolved = _measure_facade._resolve_copilot_home_wsl_aware()
        print(str(_measure_facade.resolved))
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] == "copilot-rollup":
        # Ingest recent Copilot sessions (CLI + VS Code planes) into trends.db.
        # Fired by the sessionEnd hook via copilot_hook_bridge.handle_session_end(); extra
        # flags are silently consumed (INSERT OR IGNORE keeps this idempotent).
        # Wall-clock backstop: this dispatch is spawned fire-and-forget by the
        # Stop hook bridge (start_new_session=True, DEVNULL, no PID tracking),
        # so the parent hook's 20s ceiling never reaches the detached
        # grandchild. A stuck SQLite / pathological session dir would otherwise
        # leave a permanent orphan per Stop event, silently contending for
        # trends.db. Cap it at 60s and fail open: a skipped rollup on a single
        # Stop is invisible (the next Stop re-collects idempotently), a frozen
        # orphan is not. Mirrors the keepwarm-arm / checkpoint budgets above.
        _measure_facade.quiet = "--quiet" in _measure_facade.args or "-q" in _measure_facade.args
        _measure_facade._tok_hook_deadline = _measure_facade._install_hook_budget(60)
        try:
            _measure_facade._collect_copilot_sessions(days=90, quiet=_measure_facade.quiet)
        except _measure_facade._HookTimeout:
            pass
        except Exception:
            pass
        finally:
            _measure_facade._clear_hook_budget(_measure_facade._tok_hook_deadline)
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] == "copilot-summary":
        _measure_facade._copilot_summary()
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] == "devin-install":
        import devin_install as _measure_import_17; _measure_facade.devin_install = _measure_import_17; del _measure_import_17
        _measure_facade.sys.exit(_measure_facade.devin_install.main(["install"] + _measure_facade.args[1:]))
    elif _measure_facade.args[0] == "devin-uninstall":
        import devin_install as _measure_import_18; _measure_facade.devin_install = _measure_import_18; del _measure_import_18
        _measure_facade.sys.exit(_measure_facade.devin_install.main(["uninstall"] + _measure_facade.args[1:]))
    elif _measure_facade.args[0] == "devin-doctor":
        import devin_doctor as _measure_import_19; _measure_facade.devin_doctor = _measure_import_19; del _measure_import_19
        _measure_facade.sys.exit(_measure_facade.devin_doctor.main(_measure_facade.args[1:]))
    elif _measure_facade.args[0] == "devin-summary":
        import devin_state as _measure_import_20; _measure_facade.devin_state = _measure_import_20; del _measure_import_20
        try:
            print(_measure_facade.json.dumps(_measure_facade.devin_state.summary(), indent=2))
            _measure_facade.sys.exit(0)
        except (OSError, ValueError):
            print("Devin local estimates unavailable; no provider usage inferred.", file=_measure_facade.sys.stderr)
            _measure_facade.sys.exit(1)
    elif _measure_facade.args[0] == "devin-filter":
        import devin_compress as _measure_import_21; _measure_facade.devin_compress = _measure_import_21; del _measure_import_21
        _measure_facade.sys.exit(_measure_facade.devin_compress.main(_measure_facade.args[1:]))
    elif _measure_facade.args[0] == "cursor-doctor":
        import cursor_doctor as _measure_import_22; _measure_facade.cursor_doctor = _measure_import_22; del _measure_import_22
        _measure_facade.sys.exit(_measure_facade.cursor_doctor.main(_measure_facade.args[1:]))
    elif _measure_facade.args[0] == "cursor-install":
        import cursor_install as _measure_import_23; _measure_facade.cursor_install = _measure_import_23; del _measure_import_23
        _measure_facade.sys.exit(_measure_facade.cursor_install.main(["install"] + _measure_facade.args[1:]))
    elif _measure_facade.args[0] == "cursor-uninstall":
        import cursor_install as _measure_import_24; _measure_facade.cursor_install = _measure_import_24; del _measure_import_24
        _measure_facade.sys.exit(_measure_facade.cursor_install.main(["uninstall"] + _measure_facade.args[1:]))
    elif _measure_facade.args[0] == "cursor-home":
        # Print the resolved Cursor home directory (honors
        # TOKEN_OPTIMIZER_CURSOR_HOME via runtime_env.cursor_home() → the
        # safe-home guard plus the WSL-root /mnt opt-in). Used by install.sh
        # to show the TRUE hook destination in its banner instead of a
        # hardcoded ~/.cursor path (WSL-root installs wrote to /root/.cursor
        # which the Windows Cursor never reads).
        from runtime_env import cursor_home as _measure_import_ch; _measure_facade._clh = _measure_import_ch; del _measure_import_ch  # noqa: PLC0415

        print(str(_measure_facade._clh()))
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] == "cursor-rollup":
        # The bridge always pins TOKEN_OPTIMIZER_RUNTIME=cursor. A bare manual
        # run must refuse (not silently collect under the default runtime).
        if _measure_facade.detect_runtime() != "cursor":
            print(
                "[Token Optimizer] cursor-rollup runs only under the Cursor runtime; "
                "set TOKEN_OPTIMIZER_RUNTIME=cursor.",
                file=_measure_facade.sys.stderr,
            )
            _measure_facade.sys.exit(0)
        _measure_facade.quiet = "--quiet" in _measure_facade.args or "-q" in _measure_facade.args
        _measure_facade._tok_hook_deadline = _measure_facade._install_hook_budget(60)
        try:
            _measure_facade._collect_cursor_sessions(days=90, quiet=_measure_facade.quiet)
        except _measure_facade._HookTimeout:
            pass
        except Exception:
            pass
        finally:
            _measure_facade._clear_hook_budget(_measure_facade._tok_hook_deadline)
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] == "grok-doctor":
        import grok_doctor as _measure_import_25; _measure_facade.grok_doctor = _measure_import_25; del _measure_import_25
        _measure_facade.sys.exit(_measure_facade.grok_doctor.main(_measure_facade.args[1:]))
    elif _measure_facade.args[0] == "grok-install":
        import grok_install as _measure_import_26; _measure_facade.grok_install = _measure_import_26; del _measure_import_26
        _measure_facade.sys.exit(_measure_facade.grok_install.main(["install"] + _measure_facade.args[1:]))
    elif _measure_facade.args[0] == "grok-uninstall":
        import grok_install as _measure_import_27; _measure_facade.grok_install = _measure_import_27; del _measure_import_27
        _measure_facade.sys.exit(_measure_facade.grok_install.main(["uninstall"] + _measure_facade.args[1:]))
    elif _measure_facade.args[0] == "grok-home":
        # Print the resolved Grok home directory (honors GROK_HOME via
        # runtime_env.grok_home() → TOKEN_OPTIMIZER_GROK_HOME → GROK_HOME →
        # ~/.grok). Used by install.sh to show the TRUE hook destination.
        from runtime_env import grok_home as _measure_import_28; _measure_facade._gh = _measure_import_28; del _measure_import_28  # noqa: PLC0415

        print(str(_measure_facade._gh()))
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] == "grok-rollup":
        # Ingest recent Grok Build sessions into trends.db. Fired by the stop
        # hook via grok_hook_bridge.handle_stop(). Mirrors copilot-rollup:
        # idempotent (INSERT OR IGNORE), 60s wall-clock cap, fail open.
        _measure_facade.quiet = "--quiet" in _measure_facade.args or "-q" in _measure_facade.args
        _measure_facade._tok_hook_deadline = _measure_facade._install_hook_budget(60)
        try:
            _measure_facade._collect_grok_sessions(days=90, quiet=_measure_facade.quiet)
        except _measure_facade._HookTimeout:
            pass
        except Exception:
            pass
        finally:
            _measure_facade._clear_hook_budget(_measure_facade._tok_hook_deadline)
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] == "cursor-summary":
        if _measure_facade.detect_runtime() != "cursor":
            print(
                "[Token Optimizer] cursor-summary reads only the Cursor runtime; "
                "set TOKEN_OPTIMIZER_RUNTIME=cursor.",
                file=_measure_facade.sys.stderr,
            )
            _measure_facade.sys.exit(0)
        _measure_facade._cursor_summary()
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] == "cursor-coach":
        # Native coach data for the Cursor runtime (Slice D): tallies,
        # per-signal quality, and rollup trends - never a ~/.claude scan.
        if _measure_facade.detect_runtime() != "cursor":
            print(
                "[Token Optimizer] cursor-coach reads only the Cursor runtime; "
                "set TOKEN_OPTIMIZER_RUNTIME=cursor.",
                file=_measure_facade.sys.stderr,
            )
            _measure_facade.sys.exit(0)
        import cursor_coach as _measure_import_cc
        _measure_facade.cursor_coach = _measure_import_cc
        del _measure_import_cc
        # Forward --days/--focus like `coach` does; the generator accepts both
        # (window_days / focus_area in the snapshot).
        _measure_facade._cc_days = 30
        _measure_facade._cc_focus = None
        _measure_facade._i = 1
        while _measure_facade._i < len(_measure_facade.args):
            _measure_facade._a = _measure_facade.args[_measure_facade._i]
            if _measure_facade._a == "--days" and _measure_facade._i + 1 < len(_measure_facade.args):
                try:
                    _measure_facade._cc_days = int(_measure_facade.args[_measure_facade._i + 1])
                except ValueError:
                    pass
                _measure_facade._i += 2
            elif _measure_facade._a == "--focus" and _measure_facade._i + 1 < len(_measure_facade.args):
                _measure_facade._cc_focus = _measure_facade.args[_measure_facade._i + 1]
                _measure_facade._i += 2
            else:
                _measure_facade._i += 1
        _measure_facade._cc_data = _measure_facade.cursor_coach.generate_cursor_coach_data(
            days=_measure_facade._cc_days, focus=_measure_facade._cc_focus
        )
        if "--json" in _measure_facade.args:
            print(_measure_facade.json.dumps(_measure_facade._cc_data, indent=2))
        else:
            _measure_facade._csnap = _measure_facade._cc_data["snapshot"]
            print("Token Optimizer - Cursor coach data")
            _measure_facade._chs = _measure_facade._cc_data["health_score"]
            print(f"  Health score: {_measure_facade._chs if _measure_facade._chs is not None else 'no scored sessions'}")
            print(f"  Sessions analyzed: {_measure_facade._csnap['sessions_analyzed']} "
                  f"({_measure_facade._csnap['total_turns']} turns, "
                  f"{_measure_facade._csnap['total_tool_calls']} tool calls, "
                  f"{_measure_facade._csnap['total_compactions']} compactions)")
            print(f"  Cost: {_measure_facade._csnap['cost']['detail']}")
            if _measure_facade._cc_data["patterns_bad"]:
                print("  Issues detected:")
                for _measure_facade._p in _measure_facade._cc_data["patterns_bad"]:
                    _measure_facade._sev = {"high": "!!!", "medium": "!!", "low": "!"}.get(_measure_facade._p["severity"], "!")
                    print(f"    [{_measure_facade._sev}] {_measure_facade._p['name']}: {_measure_facade._p['detail']}")
            if _measure_facade._cc_data["patterns_good"]:
                print("  Good practices:")
                for _measure_facade._p in _measure_facade._cc_data["patterns_good"]:
                    print(f"    [OK] {_measure_facade._p['name']}: {_measure_facade._p['detail']}")
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] == "grok-summary":
        _measure_facade._grok_summary()
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] == "hermes-doctor":
        import hermes_doctor as _measure_import_29; _measure_facade.hermes_doctor = _measure_import_29; del _measure_import_29
        _measure_facade.sys.exit(_measure_facade.hermes_doctor.main(_measure_facade.args[1:]))
    elif _measure_facade.args[0] == "hermes-install":
        import hermes_install as _measure_import_30; _measure_facade.hermes_install = _measure_import_30; del _measure_import_30
        _measure_facade.sys.exit(_measure_facade.hermes_install.main(_measure_facade.args[1:]))
    elif _measure_facade.args[0] in _measure_facade._HERMES_SUBCOMMAND_HELP and ("--help" in _measure_facade.args[1:] or "-h" in _measure_facade.args[1:]):
        # Print usage, never run: `hermes-rollup --help` used to start a rollup.
        print(_measure_facade._HERMES_SUBCOMMAND_HELP[_measure_facade.args[0]])
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] == "hermes-rollup":
        # Ingest recent Hermes sessions into trends.db.  Called by the Hermes
        # plugin's on_session_end hook via hermes_hook_bridge.run_rollup().
        # --session <id> and --platform / --reason are accepted and silently
        # consumed so the bridge's exact call signature never causes an error;
        # a full recent collect is always run (idempotent: rows are keyed per
        # session and only refreshed when state.db has moved further along).
        # Wall-clock backstop: same rationale as copilot-rollup above -- the
        # Hermes bridge spawns this fire-and-forget with start_new_session=True
        # and no PID tracking, so the parent hook deadline never propagates.
        # Cap at 60s and fail open; a skipped rollup is invisible (the next
        # session re-collects idempotently), a frozen orphan is not.
        _measure_facade.quiet = "--quiet" in _measure_facade.args or "-q" in _measure_facade.args
        _measure_facade._HERMES_ROLLUP_CONTEXT = _measure_facade._parse_hermes_rollup_context(_measure_facade.args[1:])
        _measure_facade._HERMES_ROLLUP_SKILLS = _measure_facade._parse_hermes_rollup_skills(_measure_facade.args[1:])
        _measure_facade._tok_hook_deadline = _measure_facade._install_hook_budget(60)
        try:
            _measure_facade._collect_hermes_sessions(days=90, quiet=_measure_facade.quiet)
        except _measure_facade._HookTimeout:
            pass
        except Exception:
            pass
        finally:
            _measure_facade._clear_hook_budget(_measure_facade._tok_hook_deadline)
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] == "hermes-checkpoint":
        # Session checkpoint for a Hermes session, built from Hermes's own
        # state.db messages (read-only). Spawned detached by the Hermes plugin
        # at fill bands, before compaction, before subagent fan-out, after edit
        # batches, at turn end and at session end. Same 60s backstop as
        # hermes-rollup: a skipped checkpoint is invisible, a frozen orphan is not.
        _measure_facade._tok_hook_deadline = _measure_facade._install_hook_budget(60)
        try:
            _measure_facade._hermes_checkpoint_cli(_measure_facade.args[1:])
        except _measure_facade._HookTimeout:
            pass
        except Exception:
            pass
        finally:
            _measure_facade._clear_hook_budget(_measure_facade._tok_hook_deadline)
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] == "hermes-turn-context":
        # One child process per Hermes turn for continuity + lean-output (see
        # hermes_turn_context). Reads a JSON payload on stdin, prints one JSON
        # object. Same 8s budget as the Claude Code prompt hooks.
        _measure_facade._tok_hook_deadline = _measure_facade._install_hook_budget(8)
        try:
            print(_measure_facade.json.dumps(_measure_facade.hermes_turn_context(_measure_facade._read_stdin_hook_input())))
        except _measure_facade._HookTimeout:
            pass
        except Exception:
            pass
        finally:
            _measure_facade._clear_hook_budget(_measure_facade._tok_hook_deadline)
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] == "hermes-summary":
        # Print a brief usage summary for the most recent Hermes sessions.
        # Called by hermes_hook_bridge.run_summary() on /token-optimizer command.
        # --session <id> is accepted but treated as "show recent" (Hermes state.db
        # does not expose a single-session fast path that avoids a full collect).
        _measure_facade.quiet = False
        _measure_facade.days = 7
        _measure_facade.i = 1
        while _measure_facade.i < len(_measure_facade.args):
            if _measure_facade.args[_measure_facade.i] == "--session" and _measure_facade.i + 1 < len(_measure_facade.args):
                _measure_facade.i += 2  # accept arg, ignore value — we always show recent
            elif _measure_facade.args[_measure_facade.i] in ("--quiet", "-q"):
                _measure_facade.quiet = True
                _measure_facade.i += 1
            elif _measure_facade.args[_measure_facade.i] == "--days" and _measure_facade.i + 1 < len(_measure_facade.args):
                try:
                    _measure_facade.days = int(_measure_facade.args[_measure_facade.i + 1])
                except ValueError:
                    pass
                _measure_facade.i += 2
            else:
                _measure_facade.i += 1
        _measure_facade.new_count = _measure_facade._collect_hermes_sessions(days=_measure_facade.days, quiet=True)
        if not _measure_facade.quiet:
            print(f"[Token Optimizer / Hermes] {_measure_facade.new_count} new session(s) ingested.")
        _measure_facade.usage_trends(days=_measure_facade.days, as_json=False)
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] == "codex-state":
        _measure_facade.codex_state_report(as_json="--json" in _measure_facade.args)
    elif _measure_facade.args[0] == "drift":
        _measure_facade.output_json = "--json" in _measure_facade.args
        _measure_facade.drift_check(as_json=_measure_facade.output_json)
    elif _measure_facade.args[0] == "git-context":
        _measure_facade.output_json = "--json" in _measure_facade.args
        _measure_facade.git_context(as_json=_measure_facade.output_json)
    elif _measure_facade.args[0] == "snapshot" and len(_measure_facade.args) > 1:
        _measure_facade.take_snapshot(_measure_facade.args[1])
    elif _measure_facade.args[0] == "set-baseline":
        _measure_facade.set_pretool_baseline(_measure_facade.args[1:])
    elif _measure_facade.args[0] == "compare":
        _measure_facade.compare_snapshots()
    elif _measure_facade.args[0] == "coverage-replay":
        # Re-run the Bash hook's gates over this user's own transcripts. Read-only:
        # prints a gate table, stores nothing, never prints command or output text.
        _measure_facade.sys.exit(coverage_report.replay_cli(_measure_facade.args[1:], _measure_facade.CLAUDE_DIR / "projects"))
    elif _measure_facade.args[0] == "dashboard":
        _measure_facade._dispatch_dashboard(_measure_facade.args)
    elif _measure_facade.args[0] == "status-bar":
        # Desktop status band: one JSON read. See STATUS_BAR_HELP.
        _measure_facade._status_bar_cli(_measure_facade.args[1:])
    elif _measure_facade.args[0] == "compact-advice":
        # Replay session history against candidate compact windows. Read-only.
        _measure_facade._compact_advice_cli(_measure_facade.args[1:])
    elif _measure_facade.args[0] == "runway-json":
        # Machine-readable runway snapshot so a non-Python dashboard (the OpenClaw
        # / OpenCode TypeScript surfaces) can render the "Your plan goes further"
        # card WITHOUT reimplementing runway_snapshot's multiplier/savings logic --
        # keeping this the single source of truth. Prints the dict as JSON, or {}
        # when there is no story. Never raises -> prints {} on any error.
        try:
            _measure_facade._rj = _measure_facade.runway_snapshot()
            print(_measure_facade.json.dumps(_measure_facade._rj if _measure_facade._rj else {}))
        except Exception:
            print("{}")
    elif _measure_facade.args[0] == "conversation":
        # Per-turn token breakdown for a session
        _measure_facade.output_json = "--json" in _measure_facade.args
        _measure_facade.sid = None
        for _measure_facade.a in _measure_facade.args[1:]:
            if _measure_facade.a.startswith("--"):
                continue
            _measure_facade.sid = _measure_facade.a
            break
        if not _measure_facade.sid:
            # Use current session
            _measure_facade.fp = _measure_facade._find_current_session_jsonl()
            if not _measure_facade.fp:
                print("[Error] No session ID provided and no active session found.")
                _measure_facade.sys.exit(1)
        else:
            _measure_facade.fp = _measure_facade._find_session_jsonl_by_id(_measure_facade.sid)
            if not _measure_facade.fp:
                print(f"[Error] Session '{_measure_facade.sid}' not found.")
                _measure_facade.sys.exit(1)
        _measure_facade.turns = _measure_facade.parse_session_turns(_measure_facade.fp)
        if _measure_facade.output_json:
            print(_measure_facade.json.dumps(_measure_facade.turns, indent=2))
        else:
            _measure_facade.tier = _measure_facade._load_pricing_tier()
            _measure_facade.tier_label = _measure_facade.PRICING_TIERS[_measure_facade.tier]["label"]
            print(f"\n  Per-Turn Token Breakdown ({len(_measure_facade.turns)} API calls)")
            print(f"  Pricing: {_measure_facade.tier_label}")
            print(f"  {'#':>3}  {'Input':>8}  {'Output':>8}  {'Cache R':>8}  {'Cache W':>8}  {'Cost':>8}  Model")
            print(f"  {'':->3}  {'':->8}  {'':->8}  {'':->8}  {'':->8}  {'':->8}  {'':->10}")
            _measure_facade.total_cost = 0
            for _measure_facade.t in _measure_facade.turns:
                _measure_facade.cost_str = f"${_measure_facade.t['cost_usd']:.4f}" if _measure_facade.t['cost_usd'] > 0 else "$0"
                _measure_facade.total_cost += _measure_facade.t['cost_usd']
                _measure_facade.model_short = _measure_facade._normalize_model_name(_measure_facade.t['model']) or _measure_facade.t['model'][:12]
                _measure_facade.tools_str = f"  [{', '.join(_measure_facade.t['tools_used'][:3])}]" if _measure_facade.t['tools_used'] else ""
                print(f"  {_measure_facade.t['turn_index']:>3}  {_measure_facade.t['input_tokens']:>8,}  {_measure_facade.t['output_tokens']:>8,}  {_measure_facade.t['cache_read']:>8,}  {_measure_facade.t['cache_creation']:>8,}  {_measure_facade.cost_str:>8}  {_measure_facade.model_short}{_measure_facade.tools_str}")
            print(f"\n  Total cost: ${_measure_facade.total_cost:.4f}")
            print()
    elif _measure_facade.args[0] == "pricing-tier":
        if len(_measure_facade.args) > 1 and _measure_facade.args[1] in _measure_facade.PRICING_TIERS:
            _measure_facade._save_pricing_tier(_measure_facade.args[1])
            print(f"[Token Optimizer] Pricing tier set to: {_measure_facade.PRICING_TIERS[_measure_facade.args[1]]['label']}")
        elif len(_measure_facade.args) > 1:
            print(f"[Error] Unknown tier '{_measure_facade.args[1]}'. Available: {', '.join(_measure_facade.PRICING_TIERS.keys())}")
            _measure_facade.sys.exit(1)
        else:
            _measure_facade.current = _measure_facade._load_pricing_tier()
            print(f"\n  Current pricing tier: {_measure_facade.PRICING_TIERS[_measure_facade.current]['label']}")
            print("\n  Available tiers:")
            for _measure_facade.key, _measure_facade.val in _measure_facade.PRICING_TIERS.items():
                _measure_facade.marker = " (active)" if _measure_facade.key == _measure_facade.current else ""
                print(f"    {_measure_facade.key:20s} {_measure_facade.val['label']}{_measure_facade.marker}")
            print("\n  Set with: measure.py pricing-tier <tier-name>")
            print()
    elif _measure_facade.args[0] == "collect":
        _measure_facade._dispatch_collect(_measure_facade.args)
    elif _measure_facade.args[0] == "health":
        _measure_facade.session_health()
    elif _measure_facade.args[0] == "health-selfcheck":
        _measure_facade.health_selfcheck()
    elif _measure_facade.args[0] == "dashboard-diagnose":
        # Bug 2 diagnostic: validate the schema of the JSON that would
        # be injected into dashboard.html. Prints PRESENT / MISSING /
        # UNEXPECTED-TYPE for every top-level key the template expects.
        # Lets users (and us) rule out "data shape changed" as the cause
        # of a silent empty-tab render before touching the dashboard.
        _measure_facade.expected = {
            "snapshot": dict,
            "plan": (str, type(None)),
            "trends": (dict, type(None)),
            "health": (dict, type(None)),
            "coach": (dict, type(None)),
            "quality": (dict, type(None)),
            "manage": (dict, type(None)),
            "hooks": (dict, type(None)),
            "standalone": bool,
            "auto_plan": bool,
            "generated_at": str,
            "pricing_tier": str,
            "pricing_tier_label": str,
            "pricing_tiers": dict,
            "ttl_period_summary": list,
            "session_turns": dict,
            "memory_review": (dict, type(None)),
            "claude_md_health": (dict, type(None)),
            "v5_recommendation": (dict, type(None)),
            "version": str,
        }
        try:
            _measure_facade.components = _measure_facade.measure_components()
            _measure_facade.totals = _measure_facade.calculate_totals(_measure_facade.components)
            _measure_facade.baselines = _measure_facade.get_session_baselines(5)
            _measure_facade.calibration = _measure_facade.detect_calibration_gap(_measure_facade.components, _measure_facade.totals, _measure_facade.baselines)
            _measure_facade.snapshot = {
                "components": _measure_facade.components,
                "totals": _measure_facade.totals,
                "session_baselines": _measure_facade.baselines,
                "calibration": _measure_facade.calibration,
                "context_window": _measure_facade.detect_context_window()[0],
            }
            try:
                _measure_facade.trends = _measure_facade._collect_trends_data(days=30)
            except Exception:
                _measure_facade.trends = None
            _measure_facade.data = {
                "snapshot": _measure_facade.snapshot,
                "plan": None,
                "trends": _measure_facade.trends,
                "health": _measure_facade._collect_health_data(),
                "coach": None,
                "quality": {},
                "manage": None,
                "hooks": None,
                "standalone": True,
                "auto_plan": False,
                "generated_at": _measure_facade.datetime.now().isoformat(),
                "pricing_tier": _measure_facade._load_pricing_tier(),
                "pricing_tier_label": _measure_facade._pricing_tier_label(_measure_facade._load_pricing_tier()),
                "pricing_tiers": {} if _measure_facade.detect_runtime() == "codex" else {k: v["label"] for k, v in _measure_facade.PRICING_TIERS.items()},
                "ttl_period_summary": [],
                "session_turns": {},
                "memory_review": None,
                "claude_md_health": None,
                "v5_recommendation": None,
                "version": _measure_facade.TOKEN_OPTIMIZER_VERSION,
            }
        except Exception as _measure_exception_31:
            _measure_facade.e = _measure_exception_31
            try:
                print(f"[ERROR] data construction failed: {_compat.read_global(_measure_facade, 'e')!r}")
                _measure_facade.sys.exit(1)
            finally:
                _measure_facade.e = None
                del _measure_facade.e

        print("\nDASHBOARD DATA SCHEMA CHECK")
        print("=" * 60)
        _measure_facade.ok = 0
        _measure_facade.bad = 0
        for _measure_facade.key, _measure_facade.expected_type in _measure_facade.expected.items():
            if _measure_facade.key not in _measure_facade.data:
                print(f"  [MISSING]  {_measure_facade.key}  (expected {_measure_facade.expected_type})")
                _measure_facade.bad += 1
            else:
                _measure_facade.value = _measure_facade.data[_measure_facade.key]
                if not isinstance(_measure_facade.value, _measure_facade.expected_type):
                    _measure_facade.actual = type(_measure_facade.value).__name__
                    print(f"  [WRONG]    {_measure_facade.key}  expected {_measure_facade.expected_type}, got {_measure_facade.actual}")
                    _measure_facade.bad += 1
                else:
                    _measure_facade.tag = type(_measure_facade.value).__name__ if _measure_facade.value is not None else "None"
                    print(f"  [OK]       {_measure_facade.key}  ({_measure_facade.tag})")
                    _measure_facade.ok += 1
        _measure_facade.extras = [k for k in _measure_facade.data.keys() if k not in _measure_facade.expected]
        for _measure_facade.k in _measure_facade.extras:
            print(f"  [EXTRA]    {_measure_facade.k}  ({type(_measure_facade.data[_measure_facade.k]).__name__})  -- not in expected schema")
        print()
        print(f"  {_measure_facade.ok} keys valid, {_measure_facade.bad} issues, {len(_measure_facade.extras)} extras")
        if _measure_facade.bad > 0:
            _measure_facade.sys.exit(1)
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] in ("version", "--version", "-v"):
        # Simple sanity check users can run to confirm which Token
        # Optimizer they're actually running after /plugin update.
        print(_measure_facade.TOKEN_OPTIMIZER_VERSION)
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] == "daemon-consent":
        # v5.3.3: persistent consent for the bookmarkable dashboard URL.
        # SKILL.md checks --get before prompting; Claude writes --set
        # yes|no after the user answers. Non-interactive runs
        # (SessionStart, CI) never touch this file, so consent stays
        # explicit and user-owned.
        _measure_facade.consent_path = _measure_facade.SNAPSHOT_DIR / "daemon-consent.json"
        if "--get" in _measure_facade.args:
            if not _measure_facade.consent_path.exists():
                print("{}")
                _measure_facade.sys.exit(0)
            try:
                _measure_facade.raw = _measure_facade.consent_path.read_text(encoding="utf-8").strip()
            except OSError:
                # Read error: return "{}" so the skill treats it as
                # unrecorded and re-prompts rather than branching on
                # garbage. Exit 0 -- the CLI still succeeded, file
                # recovery is the user's follow-up action.
                print("{}")
                _measure_facade.sys.exit(0)
            # Validate JSON shape. A half-written file
            # from a crash between truncate and flush could print
            # partial JSON here and mislead the skill's branch logic.
            if not _measure_facade.raw:
                print("{}")
                _measure_facade.sys.exit(0)
            try:
                _measure_facade.parsed = _measure_facade.json.loads(_measure_facade.raw)
                if not isinstance(_measure_facade.parsed, dict):
                    print("{}")
                else:
                    print(_measure_facade.json.dumps(_measure_facade.parsed))
            except (ValueError, TypeError):
                print("{}")
            _measure_facade.sys.exit(0)
        if "--set" in _measure_facade.args:
            try:
                _measure_facade.value = _measure_facade.args[_measure_facade.args.index("--set") + 1].strip().lower()
            except (IndexError, AttributeError):
                print("[Error] --set requires yes|no|unset")
                _measure_facade.sys.exit(1)
            if _measure_facade.value in ("yes", "y", "true", "1"):
                _measure_facade.consent = True
            elif _measure_facade.value in ("no", "n", "false", "0"):
                _measure_facade.consent = False
            elif _measure_facade.value == "unset":
                if _measure_facade.consent_path.exists():
                    try:
                        _measure_facade.consent_path.unlink()
                    except OSError:
                        pass
                print("consent cleared")
                _measure_facade.sys.exit(0)
            else:
                print(f"[Error] --set requires yes|no|unset, got {_measure_facade.value!r}")
                _measure_facade.sys.exit(1)
            _measure_facade.record = {
                "prompted": True,
                "consent": _measure_facade.consent,
                "ts": _measure_facade.datetime.now().isoformat(),
                "platform": _measure_facade.platform.system(),
            }
            # Atomic write: tempfile + os.replace so a mid-write crash
            # cannot leave the consent file in a half-written state that
            # would confuse future --get readers.
            try:
                _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
                _measure_facade.tmp_path = _measure_facade.consent_path.with_suffix(".json.tmp")
                _measure_facade.tmp_path.write_text(_measure_facade.json.dumps(_measure_facade.record) + "\n", encoding="utf-8")
                _measure_facade.os.replace(str(_measure_facade.tmp_path), str(_measure_facade.consent_path))
            except OSError as _measure_exception_32:
                _measure_facade.e = _measure_exception_32
                try:
                    print(f"[Error] Could not write consent file: {_compat.read_global(_measure_facade, 'e')}")
                    _measure_facade.sys.exit(1)
                finally:
                    _measure_facade.e = None
                    del _measure_facade.e
            print(_measure_facade.json.dumps(_measure_facade.record))
            _measure_facade.sys.exit(0)
        # No flag: print usage
        print("Usage: measure.py daemon-consent --get | --set yes|no|unset")
        _measure_facade.sys.exit(1)
    elif _measure_facade.args[0] == "consent":
        # Enterprise consent management
        _measure_facade.sub = _measure_facade.args[1] if len(_measure_facade.args) > 1 else "--show"
        if _measure_facade.sub == "--show":
            _measure_facade.shown = _measure_facade._read_config_flag("enterprise_consent_shown", False)
            _measure_facade.v5 = _measure_facade._read_config_flag("v5_welcome_shown", False)
            print(f"Enterprise consent: {'granted' if _measure_facade.shown else 'not yet granted'}")
            print(f"V5 welcome shown: {'yes' if _measure_facade.v5 else 'no'}")
            if not _measure_facade.shown and _measure_facade.v5:
                print("Note: v5 welcome was shown; consent will be backfilled on next hook invocation")
        elif _measure_facade.sub == "--reset":
            _measure_facade._write_config_flag("enterprise_consent_shown", False)
            # Also clear v5_welcome_shown: run.py's backfill treats a present
            # v5_welcome_shown as implied consent, so leaving it True would
            # silently re-grant consent on the next hook.
            # Clearing it is also what re-triggers the data notice on the next
            # session start, matching the message below.
            _measure_facade._write_config_flag("v5_welcome_shown", False)
            print("Consent reset. Data notice will appear on next session start.")
        elif _measure_facade.sub == "--grant":
            _measure_facade._write_config_flag("enterprise_consent_shown", True)
            print("Consent granted.")
        else:
            print(f"Usage: consent [--show|--reset|--grant]", file=_measure_facade.sys.stderr)
            _measure_facade.sys.exit(1)
    elif _measure_facade.args[0] == "security-report":
        _measure_facade.as_json = "--json" in _measure_facade.args
        _measure_facade._security_report(as_json=_measure_facade.as_json)
    elif _measure_facade.args[0] == "session-end-flush":
        # Single sequential entry point for the SessionEnd hook. Runs
        # collect -> dashboard -> compact-capture in one process so the
        # three phases can't race on trends.db (SQLite serialises within
        # a process but locks out other processes, so three async hook
        # entries would have corrupted the DB). Keeps exit 0 regardless.
        #
        # Defer by DEFAULT. A legacy bare `session-end-flush` hook
        # fossilized in settings.json (pre-5.11.77 script installs, no --defer
        # flag) otherwise runs this flush synchronously inline and wedges
        # Windows stop-hooks at 3/4 -- and neither self-heal path rewrites that
        # settings.json entry. Deferring unless --no-defer is passed makes every
        # such fossil non-blocking the moment the scripts update, with no
        # settings.json surgery. --no-defer keeps the synchronous path for
        # manual refresh / tests; `--defer` stays accepted (now redundant).
        _measure_facade._dispatch_session_end_flush(_measure_facade.args)
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] == "session-end-flush-worker":
        _measure_facade._run_session_end_flush_worker(_measure_facade.args)
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] == "daemon-status":
        # Cross-platform identity probe of 127.0.0.1:24842. A bare TCP
        # connect would mark any foreign service on 24842 as "ours" and
        # lead the SKILL.md to advertise a URL that returns someone
        # else's content. Instead we GET /__to_ping and require the
        # magic string reply. Foreign listener -> DAEMON_FOREIGN,
        # surfaced separately so the skill can guide remediation.
        import urllib.error; _measure_facade.urllib = urllib; del urllib
        import urllib.request; _measure_facade.urllib = urllib; del urllib

        _measure_facade.magic = _measure_facade.DAEMON_IDENTITY_MAGIC.encode("utf-8")
        _measure_facade.status = "DAEMON_NOT_RUNNING"
        for _measure_facade.host in ("127.0.0.1", "[::1]"):
            _measure_facade.url = f"http://{_measure_facade.host}:{_measure_facade.DAEMON_PORT}/__to_ping"
            try:
                with _measure_facade.urllib.request.urlopen(_measure_facade.url, timeout=1) as _measure_facade.resp:
                    _measure_facade.body = _measure_facade.resp.read(len(_measure_facade.magic) + 8).strip()
                    if _measure_facade.body == _measure_facade.magic:
                        _measure_facade.status = "DAEMON_RUNNING"
                        break
                    _measure_facade.status = "DAEMON_FOREIGN"
            except _measure_facade.urllib.error.URLError as _measure_exception_35:
                _measure_facade.e = _measure_exception_35
                try:
                    _measure_facade.reason = getattr(_compat.read_global(_measure_facade, 'e'), 'reason', None)
                    if isinstance(_measure_facade.reason, ConnectionRefusedError):
                        continue
                    if isinstance(_measure_facade.reason, OSError):
                        _measure_facade.errno_attr = getattr(_measure_facade.reason, 'errno', None)
                        if _measure_facade.errno_attr in (61, 111):
                            continue
                    _measure_facade.status = 'DAEMON_FOREIGN'
                finally:
                    _measure_facade.e = None
                    del _measure_facade.e
            except (OSError, ValueError):
                continue
        print(_measure_facade.status)
        _measure_facade.sys.exit(0 if _measure_facade.status == "DAEMON_RUNNING" else 1)
    elif _measure_facade.args[0] == "kill-stale":
        _measure_facade.hours, _measure_facade.dry, _measure_facade.include_orphans = _measure_facade._parse_kill_stale_args(_measure_facade.args)
        if _measure_facade.hours < 1:
            print("[Error] --hours must be >= 1")
            _measure_facade.sys.exit(1)
        _measure_facade.kill_stale_sessions(threshold_hours=_measure_facade.hours, dry_run=_measure_facade.dry, include_orphans=_measure_facade.include_orphans)
    elif _measure_facade.args[0] == "check-hook":
        _measure_facade.check_hook()
    elif _measure_facade.args[0] == "setup-hook":
        _measure_facade.dry = "--dry-run" in _measure_facade.args
        _measure_facade.uninstall = "--uninstall" in _measure_facade.args
        _measure_facade.setup_hook(dry_run=_measure_facade.dry, uninstall=_measure_facade.uninstall, user_initiated=True)
    elif _measure_facade.args[0] == "setup-all-hooks":
        _measure_facade.dry = "--dry-run" in _measure_facade.args
        _measure_facade.verbose = "--verbose" in _measure_facade.args or "-v" in _measure_facade.args
        _measure_facade.output_json = "--json" in _measure_facade.args
        _measure_facade.result = _measure_facade.setup_all_hooks(dry_run=_measure_facade.dry, verbose=_measure_facade.verbose)
        if _measure_facade.output_json:
            print(_measure_facade.json.dumps(_measure_facade.result, indent=2))
        else:
            _measure_facade.added = _measure_facade.result.get("added", 0)
            _measure_facade.skipped = _measure_facade.result.get("skipped", 0)
            _measure_facade.root = _measure_facade.result.get("plugin_root", "?")
            _measure_facade.err = _measure_facade.result.get("error")
            if _measure_facade.err:
                print(f"  [Error] {_measure_facade.err}")
                _measure_facade.sys.exit(1)
            elif _measure_facade.dry:
                print(f"  [Dry-run] would add {_measure_facade.added} hook(s), skip {_measure_facade.skipped} (plugin root: {_measure_facade.root})")
            elif _measure_facade.added == 0:
                print(f"  [setup-all-hooks] All hooks already present. (plugin root: {_measure_facade.root})")
            else:
                print(f"  [setup-all-hooks] Added {_measure_facade.added} hook(s), skipped {_measure_facade.skipped}. (plugin root: {_measure_facade.root})")
                print("  Restart Claude Code to activate the new hooks.")
    elif _measure_facade.args[0] == "cleanup-duplicate-hooks":
        # v5.0.2: Remove token-optimizer hooks from settings.json that the
        # installed plugin already provides. Safe to run anytime; idempotent.
        _measure_facade.dry = "--dry-run" in _measure_facade.args
        _measure_facade.output_json = "--json" in _measure_facade.args
        _measure_facade.result = _measure_facade._cleanup_duplicate_plugin_hooks_from_settings(dry_run=_measure_facade.dry)
        if _measure_facade.output_json:
            print(_measure_facade.json.dumps(_measure_facade.result, indent=2))
        else:
            _measure_facade.removed = _measure_facade.result.get("removed", 0)
            _measure_facade.reason = _measure_facade.result.get("reason", "")
            if _measure_facade.reason == "plugin_not_installed":
                print("  [cleanup-duplicate-hooks] Plugin is not installed — nothing to clean up.")
                print("  (This command only applies when the marketplace plugin is active.)")
            elif _measure_facade.reason == "plugin_hooks_json_not_found":
                print("  [cleanup-duplicate-hooks] Plugin hooks.json not found. Cannot determine duplicates.")
                _measure_facade.sys.exit(1)
            elif _measure_facade.reason == "no_duplicates_found":
                print("  [cleanup-duplicate-hooks] settings.json has no token-optimizer duplicates. All clean.")
            elif _measure_facade.dry:
                print(f"  [Dry-run] Would remove {_measure_facade.removed} duplicate hook(s) from settings.json.")
                print("  Run without --dry-run to apply.")
            elif _measure_facade.reason == "success":
                print(f"  [cleanup-duplicate-hooks] Removed {_measure_facade.removed} duplicate hook(s) from settings.json.")
                print("  Plugin hooks.json remains the single source of truth. Restart Claude Code to fully apply.")
            else:
                print(f"  [cleanup-duplicate-hooks] {_measure_facade.reason}")
                if "fail" in _measure_facade.reason or "error" in _measure_facade.reason:
                    _measure_facade.sys.exit(1)
    elif _measure_facade.args[0] == "setup-daemon":
        _measure_facade.sys.exit(_measure_facade._setup_daemon_cli(_measure_facade.args))
    elif _measure_facade.args[0] == "cleanup":
        # Confirm gate. `dry = "--dry-run" in args` meant a
        # typo ("--dryrun", "--dry_run", "-dry-run") silently performed the
        # REAL destructive run -- the flag you reach for to stay safe is
        # exactly the one whose typo costs you. Unknown flags are now rejected,
        # and mutating requires an explicit --confirm.
        _measure_facade._known = {"--dry-run", "--this-install-only", "--confirm"}
        _measure_facade._unknown = [a for a in _measure_facade.args[1:] if a.startswith("-") and a not in _measure_facade._known]
        if _measure_facade._unknown:
            print(f"[Token Optimizer] Unknown flag(s): {', '.join(_measure_facade._unknown)}")
            print("  Usage: measure.py cleanup [--dry-run] [--confirm] "
                  "[--this-install-only]")
            print("  Nothing was changed.")
            _measure_facade.sys.exit(2)
        _measure_facade.dry = "--dry-run" in _measure_facade.args
        _measure_facade.this_install_only = "--this-install-only" in _measure_facade.args
        if not _measure_facade.dry and "--confirm" not in _measure_facade.args:
            print("[Token Optimizer] cleanup removes installed components "
                  "(daemon, statusLine, hooks, manifest entries).")
            print("  Session data, checkpoints, and trends are PRESERVED.")
            print("  Preview first:  measure.py cleanup --dry-run")
            print("  Then run:       measure.py cleanup --confirm")
            print("  Nothing was changed.")
            _measure_facade.sys.exit(1)
        _measure_facade.cleanup(dry_run=_measure_facade.dry, this_install_only=_measure_facade.this_install_only)
    elif _measure_facade.args[0] in ("inject-routing", "inject-coach", "setup-coach-injection"):
        from injection import inject_managed_block as _measure_import_36; _measure_facade.inject_managed_block = _measure_import_36; del _measure_import_36; from injection import remove_managed_block as _measure_import_37; _measure_facade.remove_managed_block = _measure_import_37; del _measure_import_37

        def _managed_instruction_candidates() -> list[Path]:
            _measure_facade = _compat.facade()
            cwd = _measure_facade.Path.cwd()
            if _measure_facade.detect_runtime() == "codex":
                return [cwd / "AGENTS.md", cwd / "AGENTS.override.md", _measure_facade.runtime_home() / "AGENTS.md"]
            return [cwd / "CLAUDE.md", cwd / ".claude" / "CLAUDE.md", _measure_facade.CLAUDE_DIR / "CLAUDE.md"]
        _measure_facade._managed_instruction_candidates = _compat.bind(_managed_instruction_candidates, _measure_facade.__name__)
        _measure_facade._managed_instruction_candidates.__qualname__ = '_managed_instruction_candidates'
        del _managed_instruction_candidates

        def _resolve_instruction_file(cli_args):
            _measure_facade = _compat.facade()
            for i, a in enumerate(cli_args):
                if a == "--file" and i + 1 < len(cli_args):
                    return cli_args[i + 1]
            candidates = _measure_facade._managed_instruction_candidates()
            return str(next((c for c in candidates if c.exists()), candidates[0]))
        _measure_facade._resolve_instruction_file = _compat.bind(_resolve_instruction_file, _measure_facade.__name__)
        _measure_facade._resolve_instruction_file.__qualname__ = '_resolve_instruction_file'
        del _resolve_instruction_file

        if _measure_facade.args[0] == "setup-coach-injection" and "--uninstall" in _measure_facade.args:
            for _measure_facade.candidate in _measure_facade._managed_instruction_candidates():
                if _measure_facade.candidate.exists():
                    _measure_facade.r = _measure_facade.remove_managed_block(str(_measure_facade.candidate), "COACH")
                    if _measure_facade.r["action"] == "removed":
                        print(f"  Removed COACH block from {_measure_facade.candidate}")
            print("  Coach injection uninstalled.")
        else:
            # Determine section and generator
            if _measure_facade.args[0] == "inject-routing":
                _measure_facade.section, _measure_facade.gen_fn, _measure_facade.err = "MODEL_ROUTING", _measure_facade.generate_model_routing_block, "No trends data available."
            else:
                _measure_facade.section, _measure_facade.gen_fn, _measure_facade.err = "COACH", _measure_facade.generate_coach_block, "No coaching data available yet."

            _measure_facade.block = _measure_facade.gen_fn()
            if not _measure_facade.block:
                print(f"[Error] {_measure_facade.err} Run some sessions first.")
                _measure_facade.sys.exit(1)

            _measure_facade.target = _measure_facade._resolve_instruction_file(_measure_facade.args)
            _measure_facade.dry = "--dry-run" in _measure_facade.args and _measure_facade.args[0] != "setup-coach-injection"
            _measure_facade.result = _measure_facade.inject_managed_block(_measure_facade.target, _measure_facade.section, _measure_facade.block, dry_run=_measure_facade.dry)

            if _measure_facade.dry:
                print(f"\n  [DRY RUN] Would {_measure_facade.result['action']} {_measure_facade.section} block in {_measure_facade.result['filepath']}")
                print(f"\n{_measure_facade.result['diff']}\n")
            else:
                print(f"\n  {_measure_facade.result['action'].upper()} {_measure_facade.section} block in {_measure_facade.result['filepath']}")
                if _measure_facade.result["action"] != "unchanged":
                    print(f"\n{_measure_facade.result['diff']}\n")
                if _measure_facade.args[0] == "setup-coach-injection":
                    print("  Block will auto-remove after 48h if not refreshed.")
                    print("  Uninstall: measure.py setup-coach-injection --uninstall")
    elif _measure_facade.args[0] == "check-staleness":
        from injection import check_staleness as _measure_import_38; _measure_facade.check_staleness = _measure_import_38; del _measure_import_38; from injection import remove_managed_block as _measure_import_39; _measure_facade.remove_managed_block = _measure_import_39; del _measure_import_39
        _measure_facade.section = _measure_facade.args[1] if len(_measure_facade.args) > 1 else "COACH"
        _measure_facade.cwd = _measure_facade.Path.cwd()
        if _measure_facade.detect_runtime() == "codex":
            _measure_facade.candidates = [_measure_facade.cwd / "AGENTS.md", _measure_facade.cwd / "AGENTS.override.md", _measure_facade.runtime_home() / "AGENTS.md"]
        else:
            _measure_facade.candidates = [_measure_facade.cwd / "CLAUDE.md", _measure_facade.cwd / ".claude" / "CLAUDE.md", _measure_facade.CLAUDE_DIR / "CLAUDE.md"]
        for _measure_facade.candidate in _measure_facade.candidates:
            if _measure_facade.candidate.exists():
                _measure_facade.s = _measure_facade.check_staleness(str(_measure_facade.candidate), _measure_facade.section)
                if _measure_facade.s["exists"] and _measure_facade.s["stale"]:
                    _measure_facade.remove_managed_block(str(_measure_facade.candidate), _measure_facade.section)
                    print(f"[Token Optimizer] Removed stale {_measure_facade.section} block from {_measure_facade.candidate.name} "
                          f"(age: {_measure_facade.s['age_hours']:.0f}h, TTL: 48h)", file=_measure_facade.sys.stderr)
    elif _measure_facade.args[0] == "coach":
        _measure_facade._coach_cli(_measure_facade.args)
    elif _measure_facade.args[0] == "deterministic-candidates":
        _measure_facade._cmd_deterministic_candidates(_measure_facade.args[1:])
    elif _measure_facade.args[0] == "validate-impact":
        _measure_facade.output_json = "--json" in _measure_facade.args
        _measure_facade.strat = "auto"
        _measure_facade.val_days = 30
        for _measure_facade.i, _measure_facade.a in enumerate(_measure_facade.args):
            if _measure_facade.a == "--strategy" and _measure_facade.i + 1 < len(_measure_facade.args):
                _measure_facade.strat = _measure_facade.args[_measure_facade.i + 1]
            elif _measure_facade.a == "--days" and _measure_facade.i + 1 < len(_measure_facade.args):
                try:
                    _measure_facade.val_days = int(_measure_facade.args[_measure_facade.i + 1])
                except ValueError:
                    pass
        if _measure_facade.strat not in ("auto", "halves"):
            print(f"[Error] Unknown strategy '{_measure_facade.strat}'. Use 'auto' or 'halves'.")
            _measure_facade.sys.exit(1)
        _measure_facade.validate_impact(strategy=_measure_facade.strat, days=_measure_facade.val_days, as_json=_measure_facade.output_json)
    elif _measure_facade.args[0] == "quality":
        _measure_facade.sid = None
        _measure_facade.output_json = "--json" in _measure_facade.args
        for _measure_facade.a in _measure_facade.args[1:]:
            if _measure_facade.a not in ("--json",):
                _measure_facade.sid = _measure_facade.a
                break
        _measure_facade.quality_analyzer(session_id=_measure_facade.sid, as_json=_measure_facade.output_json)
    elif _measure_facade.args[0] == "compact-capture":
        # Called by PreCompact/Stop/SessionEnd hooks
        # Reads hook input from stdin (JSON with session_id, transcript_path, etc.)
        _measure_facade.trigger = "auto"
        _measure_facade.transcript = None
        _measure_facade.sid = None
        for _measure_facade.i, _measure_facade.a in enumerate(_measure_facade.args):
            if _measure_facade.a == "--trigger" and _measure_facade.i + 1 < len(_measure_facade.args):
                _measure_facade.trigger = _measure_facade.args[_measure_facade.i + 1]
        # Wall-clock backstop. compact_capture reads the transcript, which on a very
        # large session or a slow/contended disk can run long. This dispatch is wired
        # to the synchronous Stop hook, so an unbounded call surfaces as Claude Code
        # "hanging" on the stop hook (run.py's 120s subprocess cap is far too coarse to
        # feel like anything but a freeze). Cap it and fail open: a skipped checkpoint on
        # a single Stop is invisible (progressive checkpoints already cover the session),
        # a frozen hook is not. Mirrors the keepwarm-arm budget in the block below.
        # Hooks keep the 14s default. A caller that waits on the result (the desktop
        # Start fresh button) passes --budget-seconds N, capped at 60.
        _measure_facade._tok_hook_old_sig = _measure_facade._install_hook_budget(_measure_facade._compact_capture_budget_seconds(_measure_facade.args))
        try:
            # Read hook input from stdin (JSON with session_id, transcript_path, etc.)
            _measure_facade.hook_input = _measure_facade._read_stdin_hook_input()
            _measure_facade.transcript = _measure_facade.hook_input.get("transcript_path") or _measure_facade.transcript
            _measure_facade.sid = _measure_facade.hook_input.get("session_id") or _measure_facade.sid
            _measure_facade.result = _measure_facade.compact_capture(transcript_path=_measure_facade.transcript, session_id=_measure_facade.sid, trigger=_measure_facade.trigger)
            if _measure_facade.result and "--quiet" not in _measure_facade.args:
                # Only print for non-hook invocations (hooks should be quiet)
                print(f"[Token Optimizer] Checkpoint saved: {_measure_facade.result}")
            elif not _measure_facade.result:
                _measure_facade._notice = _measure_facade._redaction_skip_notice(_measure_facade.sid)
                if _measure_facade._notice:
                    # Sole hook output: one JSON object is a valid envelope on every host.
                    print(_measure_facade.json.dumps({"systemMessage": _measure_facade._notice}))
        except _measure_facade._HookTimeout:
            pass
        except Exception:
            pass
        finally:
            _measure_facade._clear_hook_budget(_measure_facade._tok_hook_old_sig)
    elif _measure_facade.args[0] == "keepwarm-arm":
        # Called by the Stop hook (additive). Appends one arm record to the
        # keep-warm sidecar so the keepwarm-tick loop can later decide whether to
        # ping. Dumb + fast: a SIGALRM budget plus the writer's own fail-open
        # backstop keep it well inside the hook hot path; any failure is silent so
        # the hook chain is never broken. NO eligibility/policy here.
        # Arming is token-FREE (it just records that a session paused, so the
        # watcher can measure cache/cash drops). It stays ON. The LLM ping is the
        # only token-spending step and is gated at the fire point.
        _measure_facade._tok_hook_old_sig = _measure_facade._install_hook_budget(2)
        try:
            _measure_facade.hook_input = _measure_facade._read_stdin_hook_input()
            _measure_facade.sid = _measure_facade.hook_input.get("session_id")
            _measure_facade.transcript = _measure_facade.hook_input.get("transcript_path")
            _measure_facade.rec = _measure_facade.write_keepwarm_arm_record(_measure_facade.sid, _measure_facade.transcript)
            if _measure_facade.rec and "--quiet" not in _measure_facade.args:
                print(f"[Token Optimizer] keep-warm armed: {_measure_facade.rec.get('session_id')}")
        except _measure_facade._HookTimeout:
            _measure_facade.sys.exit(0)
        except Exception:
            _measure_facade.sys.exit(0)
        finally:
            _measure_facade._clear_hook_budget(_measure_facade._tok_hook_old_sig)
    elif _measure_facade.args[0] == "keepwarm-enable":
        # Explicit opt-in. Refuses on subscription billing with the honest
        # message (no per-write premium to recover). On API billing it records
        # consent='enabled'; the scheduler install is a SEPARATE step.
        # --limits-lab: dogfood-only subscription enrollment (EXPERIMENTAL),
        # never offered by consent pitch surfaces — operator CLI only.
        _measure_facade.ok, _measure_facade.msg = _measure_facade.keepwarm_enable(limits_lab="--limits-lab" in _measure_facade.args)
        print(f"[Token Optimizer] {_measure_facade.msg}")
        _measure_facade.sys.exit(0 if _measure_facade.ok else 1)
    elif _measure_facade.args[0] == "keepwarm-disable":
        # Explicit opt-out from any state. Sets consent='declined'
        # (terminal); a declined user is never re-asked and never pinged.
        _measure_facade._, _measure_facade.msg = _measure_facade.keepwarm_disable()
        print(f"[Token Optimizer] {_measure_facade.msg}")
    elif _measure_facade.args[0] == "keepwarm-consent-status":
        # Machine-readable gate inputs for the first-run ASK surface. The
        # TO skill / dashboard reads should_ask to decide whether to show the
        # one-time pitch. JSON to stdout only — no human prose to confuse a
        # parser; nothing here ever prints a secret value.
        print(_measure_facade.json.dumps(_measure_facade.keepwarm_consent_status()))
    elif _measure_facade.args[0] == "keepwarm-consent-asked":
        # Idempotent marker: record that the pitch was shown so it is shown
        # exactly once (unasked -> asked; terminal states untouched). The
        # resulting state is echoed for the caller's confirmation.
        _measure_facade.state = _measure_facade.keepwarm_mark_asked()
        if "--quiet" not in _measure_facade.args:
            print(f"[Token Optimizer] keep-warm consent state: {_measure_facade.state}")
    elif _measure_facade.args[0] == "keepwarm-tick":
        # The keep-warm brain+trigger loop. Run by the scheduler every
        # ~5min. Gates via keepwarm_gate() FIRST -- exits 0 silently when not
        # allowed (subscription / unconsented / ping-process). Single-instance
        # guarded; per-record isolated; fires the CLI ping for any 'ping'
        # decision. --dry-run prints decisions JSON without firing anything.
        _measure_facade.dry_run = "--dry-run" in _measure_facade.args
        # Log rotation guard: truncate the scheduler's
        # combined stdout/stderr log if it has grown past 1MB. Cheap, best-effort,
        # never blocks the tick. Skipped under --dry-run (no real run is logged).
        if not _measure_facade.dry_run:
            try:
                _measure_facade._keepwarm_rotate_log()
            except Exception:
                pass
        try:
            _measure_facade.summary = _measure_facade.keepwarm_tick(dry_run=_measure_facade.dry_run)
        except Exception:
            _measure_facade.sys.exit(0)
        if _measure_facade.dry_run:
            # Machine-readable decisions to stdout; nothing fired.
            print(_measure_facade.json.dumps(_measure_facade.summary))
        elif "--quiet" not in _measure_facade.args:
            if _measure_facade.summary.get("allowed"):
                print(
                    f"[Token Optimizer] keep-warm tick: "
                    f"{_measure_facade.summary.get('fired', 0)} pinged, "
                    f"{_measure_facade.summary.get('skipped', 0)} skipped "
                    f"({len(_measure_facade.summary.get('decisions', []))} armed)")
            # Gate-refused ticks stay silent (exit 0).
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] == "keepwarm-scheduler":
        # Install/uninstall/status the keep-warm launchd interval agent.
        # install REFUSES unless keepwarm_gate() allows (consent drives install);
        # uninstall always works; status prints JSON. Fully decoupled from
        # the dashboard daemon (distinct label + own install lock; never touches
        # the dashboard plist/lock/regen loop).
        _measure_facade.sub = _measure_facade.args[1] if len(_measure_facade.args) > 1 else ""
        if _measure_facade.sub == "install":
            _measure_facade.ok, _measure_facade.msg = _measure_facade.keepwarm_scheduler_install(quiet="--quiet" in _measure_facade.args)
            print(f"[Token Optimizer] {_measure_facade.msg}")
            _measure_facade.sys.exit(0 if _measure_facade.ok else 1)
        elif _measure_facade.sub == "uninstall":
            _measure_facade.ok, _measure_facade.msg = _measure_facade.keepwarm_scheduler_uninstall(quiet="--quiet" in _measure_facade.args)
            print(f"[Token Optimizer] {_measure_facade.msg}")
            _measure_facade.sys.exit(0 if _measure_facade.ok else 1)
        elif _measure_facade.sub == "status":
            print(_measure_facade.json.dumps(_measure_facade.keepwarm_scheduler_status()))
            _measure_facade.sys.exit(0)
        else:
            print("[Token Optimizer] usage: keepwarm-scheduler install|uninstall|status")
            _measure_facade.sys.exit(1)
    elif _measure_facade.args[0] == "keepwarm-backfill":
        # Replay the LIVE keep-warm policy over the last 30d of real history in
        # three modes (probe-only / predictor-sustain / oracle-sustain) and
        # decide the sustain PROMOTION FENCE. READ-ONLY on transcripts; the
        # only write is the 0600 promotion sidecar (and only when the gate
        # passes / to record the honest current verdict). --json prints the full
        # result+reconcile+decision; otherwise a human summary. --no-fence skips
        # the sidecar write (inspection only).
        _measure_facade.as_json = "--json" in _measure_facade.args
        _measure_facade.write_fence = "--no-fence" not in _measure_facade.args
        _measure_facade.days = 30
        for _measure_facade.i, _measure_facade.a in enumerate(_measure_facade.args):
            if _measure_facade.a == "--days" and _measure_facade.i + 1 < len(_measure_facade.args):
                try:
                    _measure_facade.days = int(_measure_facade.args[_measure_facade.i + 1])
                except ValueError:
                    pass
        try:
            _measure_facade.run = _measure_facade.keepwarm_backfill_run(days=_measure_facade.days, write_fence=_measure_facade.write_fence)
        except Exception as _measure_exception_40:
            _measure_facade.exc = _measure_exception_40
            try:
                print(_measure_facade.json.dumps({'error': str(_compat.read_global(_measure_facade, 'exc'))}) if _measure_facade.as_json else f"[Token Optimizer] keep-warm backfill failed: {_compat.read_global(_measure_facade, 'exc')}")
                _measure_facade.sys.exit(1)
            finally:
                _measure_facade.exc = None
                del _measure_facade.exc
        if _measure_facade.as_json:
            print(_measure_facade.json.dumps(_measure_facade.run))
            _measure_facade.sys.exit(0)
        _measure_facade.modes = _measure_facade.run["result"]["modes"]
        _measure_facade.dec = _measure_facade.run["decision"]
        _measure_facade.rec = _measure_facade.run["reconcile"]
        print(f"[Token Optimizer] keep-warm backfill ({_measure_facade.days}d, "
              f"{_measure_facade.run['result']['scanned_sessions']} sessions):")
        for _measure_facade.label in ("probe-only", "predictor-sustain", "oracle-sustain"):
            _measure_facade.m = _measure_facade.modes[_measure_facade.label]
            print(f"  {_measure_facade.label:<18} net {_measure_facade._recs_usd(_measure_facade.m['net_usd']):>10}  "
                  f"(saving ${_measure_facade.m['saving_usd']:.2f} - ping ${_measure_facade.m['ping_cost_usd']:.2f}; "
                  f"{_measure_facade.m['pings']} pings, {_measure_facade.m['avoided_writes']} writes avoided)")
        print(f"  reconcile: probe_net={_measure_facade._recs_usd(_measure_facade.rec['probe_net'])} "
              f"(in-band={_measure_facade.rec['probe_in_band']}), "
              f"oracle_net={_measure_facade._recs_usd(_measure_facade.rec['oracle_net'])} "
              f"(in-band={_measure_facade.rec['oracle_in_band']})")
        _measure_facade.verdict = "PROMOTED (sustain ON)" if _measure_facade.dec["sustain_allowed"] else "probe-only (sustain OFF)"
        print(f"  fence: {_measure_facade.verdict} -- {_measure_facade.dec['reason']}"
              + ("  [sidecar written]" if _measure_facade.run["wrote_fence"] else ""))
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] == "keepwarm-report":
        # The honest end-to-end money story. Human summary by default;
        # --json for the machine-readable block. Mode, consent+billing,
        # scheduler, 7d+30d pings/spend/realized/NET, tripwire state+ratio,
        # top-5 sessions by realized. Honest empty-state when no pings yet.
        _measure_facade.as_json = "--json" in _measure_facade.args
        try:
            _measure_facade.rep = _measure_facade.keepwarm_report()
        except Exception as _measure_exception_41:
            _measure_facade.exc = _measure_exception_41
            try:
                print(_measure_facade.json.dumps({'error': str(_compat.read_global(_measure_facade, 'exc'))}) if _measure_facade.as_json else f"[Token Optimizer] keep-warm report failed: {_compat.read_global(_measure_facade, 'exc')}")
                _measure_facade.sys.exit(1)
            finally:
                _measure_facade.exc = None
                del _measure_facade.exc
        if _measure_facade.as_json:
            print(_measure_facade.json.dumps(_measure_facade.rep))
            _measure_facade.sys.exit(0)
        print("[Token Optimizer] keep-warm report:")
        print(f"  mode: {_measure_facade.rep['mode']}  (billing={_measure_facade.rep['billing']}, "
              f"consent={_measure_facade.rep['consent']}, gate={_measure_facade.rep['gate_reason']})")
        _measure_facade.sched = _measure_facade.rep.get("scheduler") or {}
        print(f"  scheduler: {'installed' if _measure_facade.sched.get('installed') else 'not installed'}"
              + (f" ({_measure_facade.sched.get('label')})" if _measure_facade.sched.get('label') else ""))
        print(f"  tripwire: {_measure_facade.rep['tripwire_mode']}"
              + (f"  ratio_7d={_measure_facade.rep['tripwire_ratio_7d']:.2f}"
                 if _measure_facade.rep.get('tripwire_ratio_7d') is not None else "  ratio_7d=n/a"))
        if not _measure_facade.rep["has_pings"]:
            print(f"  {_measure_facade.rep['empty_state']}")
            _measure_facade.sys.exit(0)
        for _measure_facade.label, _measure_facade.w in (("7d", _measure_facade.rep["window_7d"]), ("30d", _measure_facade.rep["window_30d"])):
            print(f"  {_measure_facade.label}: {_measure_facade.w['pings']} pings "
                  f"({_measure_facade.w.get('ok_count', 0)} ok, {_measure_facade.w.get('error_count', 0)} error, "
                  f"{_measure_facade.w.get('timeout_count', 0)} timeout), "
                  f"spend ${_measure_facade.w['spend_usd']:.2f}, realized ${_measure_facade.w['realized_usd']:.2f}, "
                  f"NET {_measure_facade._recs_usd(_measure_facade.w['net_usd'])} "
                  f"({_measure_facade.w['realized_count']} realized / {_measure_facade.w['loss_count']} loss)")
            _measure_facade.orphans = _measure_facade.w.get("orphan_firing_count", 0)
            if _measure_facade.orphans:
                print(f"      WARN: {_measure_facade.orphans} orphaned firing rows "
                      f"(${_measure_facade.w.get('orphan_firing_usd', 0.0):.2f} spent, never resolved "
                      f"-- outcome booking may be broken)")
            _measure_facade.errs = _measure_facade.w.get("error_count", 0) + _measure_facade.w.get("timeout_count", 0)
            if _measure_facade.w["pings"] > 0 and _measure_facade.errs > _measure_facade.w["pings"] * 0.5:
                print(f"      ALARM: {_measure_facade.errs}/{_measure_facade.w['pings']} pings failed "
                      f"-- every ping may be erroring (check model/auth/cwd)")
        _measure_facade.h = _measure_facade.rep["health"]
        print(f"  health: pings-on-never-resumed "
              f"{_measure_facade.h['pings_on_never_resumed']}/{_measure_facade.h['total_pings']} "
              f"({_measure_facade.h['fraction']*100:.0f}%{'  ALARM' if _measure_facade.h['alarm'] else ''})")
        _measure_facade.sched_age = _measure_facade.rep.get("last_tick_age")
        if _measure_facade.sched_age is not None:
            _measure_facade.mins = int(_measure_facade.sched_age // 60)
            _measure_facade.ok_str = ("ok" if _measure_facade.rep.get("last_tick_ok") else "ISSUE")
            print(f"  last tick: {_measure_facade.mins}m ago ({_measure_facade.ok_str})")
        _measure_facade.tops = _measure_facade.rep.get("top_sessions") or []
        if _measure_facade.tops:
            print("  top sessions by realized:")
            for _measure_facade.t in _measure_facade.tops:
                print(f"    {_measure_facade.t['session_id'][:24]:<24}  realized ${_measure_facade.t['realized_usd']:.2f}  "
                      f"spend ${_measure_facade.t['spend_usd']:.2f}  ({_measure_facade.t['pings']} pings)")
        _measure_facade.qv = _measure_facade.rep.get("quota_value")
        if _measure_facade.qv and _measure_facade.rep.get("billing") != "api":
            _measure_facade.m = _measure_facade.qv.get("meters") or {}
            _measure_facade.fh = _measure_facade.m.get("five_hour_pct")
            _measure_facade.sd = _measure_facade.m.get("seven_day_pct")
            _measure_facade.meter_str = (f"5h={_measure_facade.fh:.0f}% 7d={_measure_facade.sd:.0f}%"
                         if isinstance(_measure_facade.fh, (int, float)) and isinstance(_measure_facade.sd, (int, float))
                         else "unavailable")
            print(f"  quota (subscription): {_measure_facade.qv.get('verdict')} "
                  f"[{_measure_facade.qv.get('confidence')}]  meters: {_measure_facade.meter_str}")
            print(f"    {_measure_facade.qv.get('recommendation')}")
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] == "keepwarm-quota-value":
        # Subscription QUOTA value of keep-warm:
        # meter SAVED vs meter SPENT, the right currency on a subscription where a
        # ping "saves $0" in dollars. Advisory + read-only; never flips the gate.
        _measure_facade.as_json = "--json" in _measure_facade.args
        try:
            _measure_facade.qv = _measure_facade.keepwarm_quota_value_summary(days=7)
        except Exception as _measure_exception_42:
            _measure_facade.exc = _measure_exception_42
            try:
                print(_measure_facade.json.dumps({'error': str(_compat.read_global(_measure_facade, 'exc'))}) if _measure_facade.as_json else f"[Token Optimizer] keep-warm quota-value failed: {_compat.read_global(_measure_facade, 'exc')}")
                _measure_facade.sys.exit(1)
            finally:
                _measure_facade.exc = None
                del _measure_facade.exc
        if _measure_facade.as_json:
            print(_measure_facade.json.dumps(_measure_facade.qv))
            _measure_facade.sys.exit(0)
        _measure_facade.mt = _measure_facade.qv.get("meters") or {}
        _measure_facade.md = _measure_facade.qv.get("modeled") or {}
        _measure_facade.meas = (_measure_facade.qv.get("measured") or {}).get("seven_day") or {}
        _measure_facade.fh, _measure_facade.sd = _measure_facade.mt.get("five_hour_pct"), _measure_facade.mt.get("seven_day_pct")
        _measure_facade.meter_str = (f"5h={_measure_facade.fh:.0f}%  7d={_measure_facade.sd:.0f}%"
                     + ("  (STALE)" if _measure_facade.mt.get("stale") else "")
                     if isinstance(_measure_facade.fh, (int, float)) and isinstance(_measure_facade.sd, (int, float))
                     else "unavailable")
        print("[Token Optimizer] keep-warm subscription quota-value (7d):")
        print(f"  currency: rate-limit quota (the right currency on subscription)")
        print(f"  meters: {_measure_facade.meter_str}")
        print(f"  verdict: {_measure_facade.qv.get('verdict')}  (confidence: {_measure_facade.qv.get('confidence')})")
        _measure_facade.ratio = _measure_facade.md.get("ratio")
        _measure_facade.ratio_str = f"{_measure_facade.ratio:.1f}x" if isinstance(_measure_facade.ratio, (int, float)) else "n/a"
        print(f"  modeled: {_measure_facade.md.get('ok_pings', 0)} ok pings, "
              f"{_measure_facade.md.get('realized_count', 0)} realized; "
              f"saved/spent ratio (dollar-proxy) {_measure_facade.ratio_str}")
        if _measure_facade.meas.get("median_pct") is not None:
            print(f"  measured: per-ping 7d-meter ~{_measure_facade.meas['median_pct']:.3f} pct-pts "
                  f"(n={_measure_facade.meas.get('n', 0)})")
        else:
            print(f"  measured: no completed-ping meter samples yet "
                  f"(n={_measure_facade.meas.get('n', 0)})")
        print(f"  gate changed: {_measure_facade.qv.get('gate_changed')}  (subscription stays hard-off)")
        print(f"  -> {_measure_facade.qv.get('recommendation')}")
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] == "keepwarm-forecast":
        # PERSONALIZED projected savings from the user's OWN history -- the
        # before-you-opt-in pitch (nobody enables a spending feature blind).
        # Replays the conservative probe-only floor; READ-ONLY (no fence write).
        _measure_facade.as_json = "--json" in _measure_facade.args
        _measure_facade.days = 30
        for _measure_facade.i, _measure_facade.a in enumerate(_measure_facade.args):
            if _measure_facade.a == "--days" and _measure_facade.i + 1 < len(_measure_facade.args):
                try:
                    _measure_facade.days = int(_measure_facade.args[_measure_facade.i + 1])
                except ValueError:
                    pass
        try:
            _measure_facade.fc = _measure_facade.keepwarm_forecast(days=_measure_facade.days)
        except Exception as _measure_exception_43:
            _measure_facade.exc = _measure_exception_43
            try:
                print(_measure_facade.json.dumps({'error': str(_compat.read_global(_measure_facade, 'exc'))}) if _measure_facade.as_json else f"[Token Optimizer] keep-warm forecast failed: {_compat.read_global(_measure_facade, 'exc')}")
                _measure_facade.sys.exit(1)
            finally:
                _measure_facade.exc = None
                del _measure_facade.exc
        if _measure_facade.as_json:
            print(_measure_facade.json.dumps(_measure_facade.fc))
            _measure_facade.sys.exit(0)
        if not _measure_facade.fc.get("available"):
            print(f"[Token Optimizer] keep-warm forecast unavailable: "
                  f"{_measure_facade.fc.get('error', 'no data')}")
            _measure_facade.sys.exit(1)

        def _human_tokens(n):
            _measure_facade = _compat.facade()
            n = int(n or 0)
            if n >= 1_000_000:
                return f"{n / 1_000_000:.1f}M"
            if n >= 1_000:
                return f"{n / 1_000:.0f}K"
            return str(n)
        _measure_facade._human_tokens = _compat.bind(_human_tokens, _measure_facade.__name__)
        _measure_facade._human_tokens.__qualname__ = '_human_tokens'
        del _human_tokens

        _measure_facade.toks = _measure_facade._human_tokens(_measure_facade.fc["tokens_saved"])
        print(f"[Token Optimizer] keep-warm -- what it would save YOU "
              f"(last {_measure_facade.fc['period_days']}d, {_measure_facade.fc['sessions']} sessions):")
        print(f"  ~{_measure_facade.toks} cache-write tokens spared  "
              f"({_measure_facade.fc['avoided_writes']} cold re-writes avoided)")
        print(f"  API-equivalent: ${_measure_facade.fc['saving_usd']:.2f} saved  -  "
              f"${_measure_facade.fc['ping_cost_usd']:.2f} keep-warm cost  =  "
              f"{_measure_facade._recs_usd(_measure_facade.fc['net_usd'], plus=True)}/month net")
        if _measure_facade.fc["positive"]:
            print(f"  Verdict: WORTH IT -- nets you ~${_measure_facade.fc['net_usd']:.0f}/mo in "
                  f"avoided token cost.")
        else:
            print(f"  Verdict: not worth it on your history "
                  f"(net {_measure_facade._recs_usd(_measure_facade.fc['net_usd'])}/mo).")
        print("  Note: on a subscription you pay keep-warm's cost in rate-limit "
              "quota, not cash; the $ is the API-equivalent size of the win.")
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] == "resume-lean":
        # Cold-resume-lean (keep-warm v2): reopen a forgotten/cold session by
        # reconstructing a LEAN context from checkpoints + session_log, token-free.
        # No arg -> picker list. <session_id|index> -> build the block.
        # --print emits ONLY the block (for `claude "$(... --print)"`); --json
        # emits structured data. Pure read; spends nothing.
        _measure_facade.as_json = "--json" in _measure_facade.args
        _measure_facade.as_print = "--print" in _measure_facade.args
        _measure_facade.positional = [a for a in _measure_facade.args[1:] if not a.startswith("-")]
        _measure_facade.target = _measure_facade.positional[0] if _measure_facade.positional else None

        # Build the candidate list once; an integer target indexes into it.
        _measure_facade.days = 30
        for _measure_facade.i, _measure_facade.a in enumerate(_measure_facade.args):
            if _measure_facade.a == "--days" and _measure_facade.i + 1 < len(_measure_facade.args):
                try:
                    _measure_facade.days = int(_measure_facade.args[_measure_facade.i + 1])
                except ValueError:
                    pass
        try:
            _measure_facade.cands = _measure_facade.resume_lean_candidates(limit=20, days=_measure_facade.days)
        except Exception as _measure_exception_44:
            _measure_facade.exc = _measure_exception_44
            try:
                print(_measure_facade.json.dumps({'error': str(_compat.read_global(_measure_facade, 'exc'))}) if _measure_facade.as_json else f"[Token Optimizer] resume-lean failed: {_compat.read_global(_measure_facade, 'exc')}")
                _measure_facade.sys.exit(1)
            finally:
                _measure_facade.exc = None
                del _measure_facade.exc

        if _measure_facade.target is None:
            # Picker mode.
            if _measure_facade.as_json:
                print(_measure_facade.json.dumps({"candidates": _measure_facade.cands}))
                _measure_facade.sys.exit(0)
            if not _measure_facade.cands:
                print("[Token Optimizer] resume-lean: no recent sessions found "
                      f"in the last {_measure_facade.days}d.")
                _measure_facade.sys.exit(0)
            print(f"[Token Optimizer] Cold sessions you can reopen lean "
                  f"(last {_measure_facade.days}d, token-free):")
            for _measure_facade.i, _measure_facade.c in enumerate(_measure_facade.cands, 1):
                _measure_facade.cp = (f"cp {_measure_facade.c['checkpoint_age_min'] // 60}h ago"
                      if _measure_facade.c["has_checkpoint"] and _measure_facade.c["checkpoint_age_min"] is not None
                      else "thin")
                _measure_facade.topic = _measure_facade._strip_ansi(_measure_facade.c["topic"] or "(no topic)")
                print(f"  {_measure_facade.i:>2}. [{_measure_facade.c['date']}] {_measure_facade.topic[:60]:<60} "
                      f"({_measure_facade.cp}, {_measure_facade.c['session_id'][:8]})")
            print("  Reopen:  measure.py resume-lean <#|session_id> --print")
            print("  Seed a fresh session:  claude \"$(measure.py resume-lean <#> --print)\"")
            _measure_facade.sys.exit(0)

        # Resolve target: integer index into the candidate list, else session_id.
        _measure_facade.session_id = None
        if _measure_facade.target.isdigit():
            _measure_facade.idx = int(_measure_facade.target) - 1
            if 0 <= _measure_facade.idx < len(_measure_facade.cands):
                _measure_facade.session_id = _measure_facade.cands[_measure_facade.idx]["session_id"]
            else:
                print(_measure_facade.json.dumps({"error": "index out of range"}) if _measure_facade.as_json
                      else f"[Token Optimizer] resume-lean: index {_measure_facade.target} out of "
                           f"range (1-{len(_measure_facade.cands)}).")
                _measure_facade.sys.exit(1)
        else:
            _measure_facade.session_id = _measure_facade.target

        try:
            _measure_facade.block = _measure_facade.build_lean_resume_context(_measure_facade.session_id)
        except Exception as _measure_exception_45:
            _measure_facade.exc = _measure_exception_45
            try:
                print(_measure_facade.json.dumps({'error': str(_compat.read_global(_measure_facade, 'exc'))}) if _measure_facade.as_json else f"[Token Optimizer] resume-lean reconstruction failed: {_compat.read_global(_measure_facade, 'exc')}")
                _measure_facade.sys.exit(1)
            finally:
                _measure_facade.exc = None
                del _measure_facade.exc

        if not _measure_facade.block:
            print(_measure_facade.json.dumps({"error": "session not found"}) if _measure_facade.as_json
                  else f"[Token Optimizer] resume-lean: no checkpoint or session_log "
                       f"data for {_measure_facade.session_id[:8] if _measure_facade.session_id else '?'}.")
            _measure_facade.sys.exit(1)

        if _measure_facade.as_json:
            print(_measure_facade.json.dumps({"session_id": _measure_facade.session_id, "context": _measure_facade.block}))
        else:
            # --print and default both emit the block; default adds nothing extra
            # so it stays pipe-safe.
            print(_measure_facade.block)
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] == "checkpoint-trigger":
        _measure_facade.quiet = "--quiet" in _measure_facade.args or "-q" in _measure_facade.args
        _measure_facade.milestone = None
        for _measure_facade.i, _measure_facade.a in enumerate(_measure_facade.args):
            if _measure_facade.a == "--milestone" and _measure_facade.i + 1 < len(_measure_facade.args):
                _measure_facade.milestone = _measure_facade.args[_measure_facade.i + 1]
        _measure_facade.checkpoint_trigger(milestone=_measure_facade.milestone, quiet=_measure_facade.quiet)
    elif _measure_facade.args[0] == "compact-restore":
        # Called by SessionStart hook (two variants)
        _measure_facade.hook_input = _measure_facade._read_stdin_hook_input()
        _measure_facade.sid = _measure_facade.hook_input.get("session_id")
        _measure_facade.new_session_only = "--new-session-only" in _measure_facade.args
        _measure_facade.source = str(_measure_facade.hook_input.get("source") or _measure_facade.hook_input.get("hook_event_name") or "").lower()
        _measure_facade.is_compact = bool(_measure_facade.hook_input.get("is_compact", False)) or _measure_facade.source == "compact" or "--compact" in _measure_facade.args
        # Cowork parity: the --new-session-only pointer is wired onto both
        # SessionStart (native, --once-mark) and UserPromptSubmit (Cowork,
        # --once-per-session). SessionStart always runs and (re)writes the
        # marker so a resume/compact re-fire is NOT latched out;
        # only the UserPromptSubmit copy checks-then-skips, so it does not
        # re-inject the billed checkpoint pointer on every prompt in Cowork.
        # Only the new-session pointer is guarded -- the --compact restore must
        # run on each compaction.
        if _measure_facade.is_compact or _measure_facade.source == "clear":
            _measure_facade._clear_resume_dedup_markers(_measure_facade.sid)
        if _measure_facade.new_session_only and "--once-mark" in _measure_facade.args:
            _measure_facade._mark_ran_this_session("compact-restore-new-session", _measure_facade.sid)
        elif ("--once-per-session" in _measure_facade.args and _measure_facade.new_session_only
                and _measure_facade._ran_once_this_session("compact-restore-new-session", _measure_facade.sid)):
            _measure_facade.sys.exit(0)

        def _run_compact_restore():
            _measure_facade = _compat.facade()
            if _measure_facade.new_session_only:
                _measure_facade.compact_restore(session_id=_measure_facade.sid, new_session_only=True)
            else:
                _measure_facade.compact_restore(session_id=_measure_facade.sid, is_compact=_measure_facade.is_compact)
        _measure_facade._run_compact_restore = _compat.bind(_run_compact_restore, _measure_facade.__name__)
        _measure_facade._run_compact_restore.__qualname__ = '_run_compact_restore'
        del _run_compact_restore

        # Cap this SessionStart hook's wall-clock like every other hook. It was the
        # ONLY synchronous first-prompt hook with no internal budget -- just the
        # harness 20s timeout -- so under disk/CPU contention (multiple concurrent
        # sessions sharing one project dir) it was the dominant contributor to a ~36s
        # stall. Its output is a best-effort re-orientation hint, so a budget-exit
        # (no hint) degrades gracefully. Env-overridable.
        _measure_facade._tok_hook_deadline = _measure_facade._install_hook_budget(
            _measure_facade._int_env("TOKEN_OPTIMIZER_COMPACT_RESTORE_BUDGET", 8))
        try:
            # Codex requires SessionStart stdout to be empty or ONE valid JSON
            # object; a raw "[Token Optimizer] ..." line reads to it as the start
            # of a JSON array and fails the whole hook ("hook returned invalid
            # session start JSON output"). Under the Codex marketplace
            # plugin the shared hooks.json calls this directly, NOT via
            # codex_hook_bridge -- and Codex sets neither CODEX_HOME nor
            # TOKEN_OPTIMIZER_RUNTIME in the hook subprocess (it only sets
            # CLAUDE_PLUGIN_ROOT/CLAUDE_PLUGIN_DATA), so detect_runtime() returns
            # "claude" there and the old `detect_runtime() == "codex"` gate was
            # dead code on the exact host it existed to protect. The envelope is
            # the DOCUMENTED SessionStart contract on Claude Code too, so it is now
            # emitted unconditionally rather than guessing the host.
            #
            # The envelope event comes from the firing hook's stdin payload so the
            # emitted hookEventName always matches the hook that actually fired
            # (Claude Code rejects "expected SessionStart but got
            # UserPromptSubmit"). Falls back to SessionStart if the field is absent
            # (older harnesses). Credit: @danikdanik (PR #142).
            _measure_facade._run_hook_emitting_json(
                _measure_facade._run_compact_restore,
                event=str(_measure_facade.hook_input.get("hook_event_name") or "SessionStart"))
        except _measure_facade._HookTimeout:
            pass
        finally:
            _measure_facade._clear_hook_budget(_measure_facade._tok_hook_deadline)
    elif _measure_facade.args[0] in ("continue-last", "codex-continue-last"):
        _measure_facade.topic = ""
        for _measure_facade.i, _measure_facade.a in enumerate(_measure_facade.args):
            if _measure_facade.a == "--topic" and _measure_facade.i + 1 < len(_measure_facade.args):
                _measure_facade.topic = " ".join(_measure_facade.args[_measure_facade.i + 1:])
                break
        if not _measure_facade.topic:
            _measure_facade.topic = "continue where we left off"
        _measure_facade.hint = _measure_facade.codex_prompt_hints(prompt_text=_measure_facade.topic, cwd=str(_measure_facade.Path.cwd()))
        if _measure_facade.hint:
            print(_measure_facade.hint)
        else:
            _measure_facade.compact_restore(new_session_only=True)
    elif _measure_facade.args[0] == "prompt-continuity":
        # Claude Code UserPromptSubmit: inject a topic-matched prior-session hint.
        # Mirrors the Codex bridge but runs the runtime-agnostic matcher directly.
        # Wall-clock guarded so a slow scan never delays the user's prompt; emits
        # the hint (fenced as DATA) via hookSpecificOutput.additionalContext.
        _measure_facade._tok_hook_old_sig = _measure_facade._install_hook_budget(8)
        try:
            _measure_facade.hook_input = _measure_facade._read_stdin_hook_input()
            _measure_facade.prompt_text = (
                _measure_facade.hook_input.get("prompt")
                or _measure_facade.hook_input.get("current_prompt")
                or _measure_facade.hook_input.get("user_prompt")
                or ""
            )
            _measure_facade.sid = _measure_facade.hook_input.get("session_id")
            _measure_facade.cwd = _measure_facade.hook_input.get("cwd")
            _measure_facade.transcript_path = _measure_facade.hook_input.get("transcript_path")
            if not _measure_facade.cwd and _measure_facade.transcript_path:
                try:
                    _measure_facade.cwd = str(_measure_facade.Path(_measure_facade.transcript_path).parent)
                except TypeError:
                    _measure_facade.cwd = None
            _measure_facade.hint = ""
            try:
                _measure_facade.hint = _measure_facade._continuity_prompt_hint(prompt_text=_measure_facade.prompt_text, session_id=_measure_facade.sid, cwd=_measure_facade.cwd)
            except Exception:
                _measure_facade.hint = ""
            _measure_facade.hint = (_measure_facade.hint or "").strip()
            if _measure_facade.hint:
                print(_measure_facade.json.dumps({
                    "continue": True,
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": _measure_facade.hint,
                    },
                }))
        except _measure_facade._HookTimeout:
            pass
        finally:
            _measure_facade._clear_hook_budget(_measure_facade._tok_hook_old_sig)
    elif _measure_facade.args[0] == "verbosity-steer":
        # Claude Code UserPromptSubmit: delegate to run_verbosity_steer.
        # Reads stdin for transcript_path, then calls the shared function.
        _measure_facade._tok_hook_deadline = _measure_facade._install_hook_budget(8)
        try:
            _measure_facade.hook_input = _measure_facade._read_stdin_hook_input()
            _measure_facade.transcript_path = _measure_facade.hook_input.get("transcript_path")
            # Claude Code supplies session_id on every hook payload; passing it
            # lets the identity guard verify rather than infer.
            _measure_facade.payload = _measure_facade.run_verbosity_steer(
                transcript_path=_measure_facade.transcript_path,
                quiet=False,
                session_id=_measure_facade.hook_input.get("session_id"),
            )
            if _measure_facade.payload:
                print(_measure_facade.payload)
        except Exception:
            pass
        finally:
            _measure_facade._clear_hook_budget(_measure_facade._tok_hook_deadline)
    elif _measure_facade.args[0] == "compact-instructions":
        _measure_facade.output_json = "--json" in _measure_facade.args
        _measure_facade.install = "--install" in _measure_facade.args
        _measure_facade.dry = "--dry-run" in _measure_facade.args
        _measure_facade.generate_compact_instructions(as_json=_measure_facade.output_json, install=_measure_facade.install, dry_run=_measure_facade.dry,
                                      user_initiated=True)
    elif _measure_facade.args[0] == "dynamic-compact-instructions":
        _measure_facade.hook_input = _measure_facade._read_stdin_hook_input()
        _measure_facade.sid = _measure_facade.hook_input.get("session_id") or _measure_facade.os.environ.get("CLAUDE_SESSION_ID", "")
        _measure_facade.dynamic_compact_instructions(session_id=_measure_facade.sid)
    elif _measure_facade.args[0] == "setup-smart-compact":
        _measure_facade.dry = "--dry-run" in _measure_facade.args
        _measure_facade.uninstall = "--uninstall" in _measure_facade.args
        _measure_facade.status = "--status" in _measure_facade.args
        _measure_facade.setup_smart_compact(dry_run=_measure_facade.dry, uninstall=_measure_facade.uninstall, status_only=_measure_facade.status,
                            user_initiated=True)
    elif _measure_facade.args[0] == "daemon-revive":
        # Detached child spawned by _daemon_midsession_pulse. Revives a dead
        # dashboard daemon OFF the hot path: force=True bypasses the 24h
        # SessionStart ensure throttle; the parent's 5-min revive throttle bounds
        # how often we are spawned. Silent + fail-open (never disrupts anything).
        _measure_facade._revive_status = "error"
        try:
            _measure_facade._revive_status = _measure_facade._ensure_dashboard_daemon(force=True) or "unknown"
        except Exception as _measure_exception_46:
            _measure_facade._e = _measure_exception_46
            try:
                _measure_facade._revive_status = 'exception:' + type(_compat.read_global(_measure_facade, '_e')).__name__
            finally:
                _measure_facade._e = None
                del _measure_facade._e
        # OBS-1: record the last mid-session revive outcome so a persistently-
        # failing revive is visible (doctor / manual inspection) instead of
        # vanishing into DEVNULL. One overwritten line = naturally bounded.
        try:
            _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            (_measure_facade.SNAPSHOT_DIR / "daemon-revive-last.json").write_text(
                _measure_facade.json.dumps({"ts": int(_measure_facade.time.time()), "status": _measure_facade._revive_status}),
                encoding="utf-8")
        except Exception:
            pass
    elif _measure_facade.args[0] == "quality-cache":
        # Parse argument-only policy before touching the filesystem, daemon,
        # settings, config, or stdin. The throttle-only path pays one marker
        # stat and exits immediately unless a refresh is actually due.
        _measure_facade.quiet = "--quiet" in _measure_facade.args or "-q" in _measure_facade.args
        _measure_facade.warn = "--warn" in _measure_facade.args
        _measure_facade.force = "--force" in _measure_facade.args
        _measure_facade.throttle_only = "--throttle-only" in _measure_facade.args
        _measure_facade.throttle = 120
        _measure_facade.warn_threshold = 70
        for _measure_facade.i, _measure_facade.a in enumerate(_measure_facade.args):
            if _measure_facade.a == "--throttle" and _measure_facade.i + 1 < len(_measure_facade.args):
                try:
                    _measure_facade.throttle = int(_measure_facade.args[_measure_facade.i + 1])
                except ValueError:
                    pass
            if _measure_facade.a == "--warn-threshold" and _measure_facade.i + 1 < len(_measure_facade.args):
                try:
                    _measure_facade.warn_threshold = int(_measure_facade.args[_measure_facade.i + 1])
                except ValueError:
                    pass
        _measure_facade._tok_hook_deadline = _measure_facade._install_hook_budget(8)
        _measure_facade.payload = {}
        if _measure_facade.throttle_only:
            _measure_facade.payload, _measure_facade.due = _measure_facade._quality_cache_throttle_only_state(_measure_facade.throttle, _measure_facade.force)
            if not _measure_facade.due:
                _measure_facade._clear_hook_budget(_measure_facade._tok_hook_deadline)
                _measure_facade.sys.exit(0)
        # Mid-session dashboard-daemon liveness pulse. quality-cache is the
        # per-turn UserPromptSubmit handler, so piggybacking here adds NO new hook
        # process (zero extra per-turn interpreter spawn). Cheap + throttled +
        # detached revive, wrapped so it can never affect quality-cache output or
        # timing. See _daemon_midsession_pulse.
        try:
            _measure_facade._daemon_midsession_pulse()
        except Exception:
            pass
        try:
            # Self-healing: if quality-cache hook is missing from settings.json, reinstall it.
            # Respects "quality_bar_disabled" in config.json for permanent opt-out.
            #
            # Skip entirely for plugin installs: the hook is provided by the
            # plugin's hooks.json and is intentionally kept OUT of settings.json
            # (plugin hooks.json), so it is permanently "missing" here. Calling
            # setup_quality_bar() every turn was futile (it can never add the
            # hook to settings.json for a plugin) and re-emitted the
            # foreign-status-line advisory on every prompt.
            _measure_facade._is_plugin = _measure_facade._is_running_from_plugin_cache() or _measure_facade._is_plugin_installed()
            try:
                _measure_facade._qb_disabled = _measure_facade._read_config_flag("quality_bar_disabled", False)
                if not _measure_facade._is_plugin and not _measure_facade._qb_disabled and _measure_facade._is_regular_file(_measure_facade.SETTINGS_PATH):
                    _measure_facade._sh_settings = _measure_facade.json.loads(_measure_facade.SETTINGS_PATH.read_text(encoding="utf-8"))
                    _measure_facade._sh_hooks = _measure_facade._sh_settings.get("hooks", {}).get("UserPromptSubmit", [])
                    # Recognize the consolidated dispatcher too, so a script
                    # install whose canonical hook is already present is not
                    # "healed" by appending a duplicate legacy hook.
                    if not _measure_facade._quality_cache_hook_present(_measure_facade._sh_hooks):
                        _measure_facade.setup_quality_bar(quiet=True)
            except Exception:
                pass
            # Read hook payload from stdin if available (provides exact transcript_path)
            _measure_facade.session_jsonl = None
            _measure_facade.session_id_from_hook = None
            if _measure_facade.payload:
                _measure_facade.session_jsonl = _measure_facade.payload.get("transcript_path")
                _measure_facade.session_id_from_hook = _measure_facade.payload.get("session_id")
            elif not _measure_facade.throttle_only and not _measure_facade.sys.stdin.isatty():
                try:
                    _measure_facade.payload = _measure_facade.json.loads(_measure_facade.sys.stdin.read(1_000_000))
                    _measure_facade.session_jsonl = _measure_facade.payload.get("transcript_path")
                    _measure_facade.session_id_from_hook = _measure_facade.payload.get("session_id")
                except (_measure_facade.json.JSONDecodeError, OSError):
                    pass
            # Cowork parity: the SessionStart cache-warm (quality-cache --force)
            # is wired onto UserPromptSubmit too, where it fires every prompt.
            # --force bypasses the throttle, so unguarded it would recompute the
            # full quality snapshot on every prompt. SessionStart (--once-mark)
            # always re-warms and refreshes the marker so post-compaction /
            # resume re-warms are NOT latched out; only the
            # UserPromptSubmit copy (--once-per-session) checks-then-skips, so the
            # throttled --warn / --throttle-only ticks keep maintaining the cache
            # thereafter.
            if "--once-mark" in _measure_facade.args:
                _measure_facade._mark_ran_this_session("quality-cache-force", _measure_facade.session_id_from_hook)
            elif "--once-per-session" in _measure_facade.args and _measure_facade._ran_once_this_session(
                "quality-cache-force", _measure_facade.session_id_from_hook
            ):
                _measure_facade._clear_hook_budget(_measure_facade._tok_hook_deadline)
                _measure_facade.sys.exit(0)
            # The --warn print itself now happens inside quality_cache() (gated by
            # _maybe_quality_warn: session cap + cooldown + band-drop), so it can
            # reuse the already-resolved filepath/cache_path/result instead of
            # re-deriving them here. See quality_cache()'s `warn` handling for the
            # actual message text and gating.
            _measure_facade._qc_score_box = {}

            def _qc_run():
                _measure_facade = _compat.facade()
                _measure_facade._qc_score_box["score"] = _measure_facade.quality_cache(throttle_seconds=_measure_facade.throttle, warn_threshold=_measure_facade.warn_threshold, quiet=_measure_facade.quiet, session_jsonl=_measure_facade.session_jsonl, force=_measure_facade.force, pure_time_throttle=_measure_facade.throttle_only, session_id=_measure_facade.session_id_from_hook, warn=_measure_facade.warn, post_compact=isinstance(_measure_facade.payload, dict) and _measure_facade.payload.get("hook_event_name") == "PostCompact")
            _measure_facade._qc_run = _compat.bind(_qc_run, _measure_facade.__name__)
            _measure_facade._qc_run.__qualname__ = '_qc_run'
            del _qc_run

            # SessionStart is the one wiring where Codex parses this hook's stdout
            # against the strict session-start schema, so collapse whatever was
            # printed (systemMessage JSON, plus any stray "[Token Optimizer]" line
            # from the self-heal / advisory paths) into ONE valid object. Other
            # events (UserPromptSubmit, PostToolUse, PostCompact) keep their
            # existing stream untouched -- additionalContext is not accepted there.
            _measure_facade._qc_event = str((_measure_facade.payload or {}).get("hook_event_name") or "").strip()
            if _measure_facade._qc_event == "SessionStart" or (not _measure_facade._qc_event and "--once-mark" in _measure_facade.args):
                _measure_facade._run_hook_emitting_json(_measure_facade._qc_run, event="SessionStart")
            else:
                _measure_facade._qc_run()
            _measure_facade.score = _measure_facade._qc_score_box.get("score")
            # Tripwire piggyback: the --throttle-only invocation fires on the
            # PostToolUse Edit/Write path (where active first-read follow-ups are
            # resolved), so this is the natural place to refresh the per-cohort
            # live edit-rate verdict. It is mtime-gated to 5 min, so the common
            # case is a single sidecar stat() — near-zero added hot-path cost.
            if _measure_facade.throttle_only:
                try:
                    _measure_facade.evaluate_cohort_tripwire()
                except Exception:
                    pass
        except _measure_facade._HookTimeout:
            print(
                "[Token Optimizer] hook budget exceeded; skipping quality-cache tick to keep session responsive",
                file=_measure_facade.sys.stderr,
            )
            _measure_facade.sys.exit(0)
        finally:
            _measure_facade._clear_hook_budget(_measure_facade._tok_hook_deadline)
    elif _measure_facade.args[0] == "v5":
        # v5 feature management: measure.py v5 [status|enable|disable|welcome] [feature]
        _measure_facade.output_json = "--json" in _measure_facade.args
        _measure_facade.sub = _measure_facade.args[1] if len(_measure_facade.args) > 1 else "status"

        if _measure_facade.sub == "status":
            _measure_facade.status = _measure_facade._get_v5_feature_status()
            if _measure_facade.output_json:
                print(_measure_facade.json.dumps(_measure_facade.status, indent=2))
            else:
                print("\n  Token Optimizer v5: Active Compression Features")
                print("  " + "=" * 60)
                for _measure_facade.name, _measure_facade.info in _measure_facade.status.items():
                    _measure_facade.mark = "[ON] " if _measure_facade.info["enabled"] else "[off]"
                    _measure_facade.src = f"({_measure_facade.info['source']})" if _measure_facade.info["source"] != "default" else ""
                    _measure_facade.rec = "*" if _measure_facade.info["recommended"] else " "
                    print(f"  {_measure_facade.mark} {_measure_facade.rec} {_measure_facade.info['label']:35s}  {_measure_facade.src}")
                    print(f"         {_measure_facade.info['what']}")
                    if _measure_facade.info["risk"] != "None.":
                        print(f"         Risk: {_measure_facade.info['risk'][:90]}...")
                    print()
                print("  * = recommended to keep enabled")
                print("\n  Toggle: measure.py v5 enable|disable <feature_name>")
                print(f"  Features: {', '.join(_measure_facade.V5_FEATURES.keys())}")
                print()
        elif _measure_facade.sub in ("enable", "disable"):
            if len(_measure_facade.args) < 3:
                print(f"[Error] Usage: measure.py v5 {_measure_facade.sub} <feature_name>")
                print(f"  Available: {', '.join(_measure_facade.V5_FEATURES.keys())}")
                _measure_facade.sys.exit(1)
            _measure_facade.feature_name = _measure_facade.args[2]
            if _measure_facade.feature_name not in _measure_facade.V5_FEATURES:
                print(f"[Error] Unknown feature '{_measure_facade.feature_name}'")
                print(f"  Available: {', '.join(_measure_facade.V5_FEATURES.keys())}")
                _measure_facade.sys.exit(1)
            _measure_facade.enabled = (_measure_facade.sub == "enable")
            if _measure_facade._set_v5_feature(_measure_facade.feature_name, _measure_facade.enabled):
                if _measure_facade.output_json:
                    _measure_facade.status = _measure_facade._get_v5_feature_status()
                    _measure_facade.v5_map = {}
                    for _measure_facade.n, _measure_facade.info in _measure_facade.status.items():
                        _measure_facade.v5_map[_measure_facade.n] = {"enabled": _measure_facade.info["enabled"]}
                    print(_measure_facade.json.dumps({"ok": True, "v5_features": _measure_facade.v5_map}))
                else:
                    _measure_facade.feat = _measure_facade.V5_FEATURES[_measure_facade.feature_name]
                    _measure_facade.state = "ENABLED" if _measure_facade.enabled else "DISABLED"
                    print(f"  {_measure_facade.state}: {_measure_facade.feat['label']}")
                    print(f"  Stored in: {_measure_facade.CONFIG_PATH}")
                    if _measure_facade.enabled and _measure_facade.feat.get("risk") and _measure_facade.feat["risk"] != "None.":
                        print(f"\n  Note: {_measure_facade.feat['risk']}")
                    print()
            else:
                if _measure_facade.output_json:
                    print(_measure_facade.json.dumps({"ok": False}))
                else:
                    print(f"[Error] Failed to update {_measure_facade.feature_name}")
                _measure_facade.sys.exit(1)
        elif _measure_facade.sub == "welcome":
            # First-run welcome: show all features with full details
            _measure_facade._show_v5_welcome()
        elif _measure_facade.sub == "info":
            if len(_measure_facade.args) < 3:
                print("[Error] Usage: measure.py v5 info <feature_name>")
                _measure_facade.sys.exit(1)
            _measure_facade.feature_name = _measure_facade.args[2]
            if _measure_facade.feature_name not in _measure_facade.V5_FEATURES:
                print(f"[Error] Unknown feature '{_measure_facade.feature_name}'")
                _measure_facade.sys.exit(1)
            _measure_facade.feat = _measure_facade.V5_FEATURES[_measure_facade.feature_name]
            _measure_facade.enabled = _measure_facade._is_v5_feature_enabled(_measure_facade.feature_name)
            print(f"\n  {_measure_facade.feat['label']}  [{'ON' if _measure_facade.enabled else 'off'}]")
            print("  " + "=" * 60)
            print("  What it does:")
            print(f"    {_measure_facade.feat['what']}")
            print("\n  How it works:")
            print(f"    {_measure_facade.feat['how']}")
            print("\n  Risk level:")
            print(f"    {_measure_facade.feat['risk']}")
            print(f"\n  Toggle: measure.py v5 {'disable' if _measure_facade.enabled else 'enable'} {_measure_facade.feature_name}")
            print()
        else:
            print(f"[Error] Unknown v5 subcommand '{_measure_facade.sub}'")
            print("  Usage: measure.py v5 [status|enable|disable|welcome|info] [feature]")
            _measure_facade.sys.exit(1)
    elif _measure_facade.args[0] == "upgrade-drain":
        # Detached child started once per version by a collect pass that left
        # a re-scoring backlog; also safe to run by hand.
        _measure_facade._run_upgrade_drain(quiet="--quiet" in _measure_facade.args)
    elif _measure_facade.args[0] == "counted-backfill":
        # Interactively (re)build the full counted-cumulative ledger
        # with no per-pass caps (the collect pass converges incrementally; this
        # drains the whole backlog in one run). Prints the resulting summary.
        _measure_facade._quiet = "--quiet" in _measure_facade.args
        _measure_facade.conn = _measure_facade._init_trends_db()
        try:
            _measure_facade._counted_backfill_all(_measure_facade.conn, quiet=_measure_facade._quiet)
        finally:
            _measure_facade.conn.close()
        print(_measure_facade.json.dumps(_measure_facade._counted_cumulative_summary(), indent=2))
    elif _measure_facade.args[0] == "cohorts":
        _measure_facade.sub = _measure_facade.args[1] if len(_measure_facade.args) > 1 else "status"
        if _measure_facade.sub == "promote":
            _measure_facade.cleared = _measure_facade.cohorts_promote(_measure_facade.args[2:])
            print(f"  Re-promoted {_measure_facade.cleared} cohort(s); eligible first reads may use target outlines." if _measure_facade.cleared
                  else "  No matching demoted cohorts to promote.")
        elif _measure_facade.sub == "evaluate":
            _measure_facade.evaluate_cohort_tripwire(force="--force" in _measure_facade.args)
            _measure_facade.state = _measure_facade._cohort_tripwire_state()
            print(_measure_facade.json.dumps(_measure_facade.state, indent=2))
        else:
            _measure_facade.state = _measure_facade._cohort_tripwire_state()
            if "--json" in _measure_facade.args:
                print(_measure_facade.json.dumps(_measure_facade.state, indent=2))
            else:
                print("\n  First-read cohort tripwire")
                print(f"  {'=' * 50}")
                if not _measure_facade.state["cohorts"]:
                    print("  No active first-read cohort data yet.")
                for _measure_facade.c in _measure_facade.state["cohorts"]:
                    _measure_facade.flag = "DEMOTED" if _measure_facade.c["demoted"] else "active"
                    print(f"    {_measure_facade.c['language']:10s} {_measure_facade.c['band']:10s} "
                          f"reads: {_measure_facade.c['skeletons']:>4d}  edits: {_measure_facade.c['followups']:>3d}  "
                          f"rate: {_measure_facade.c['edit_rate_pct']:>5.1f}%  [{_measure_facade.flag}]")
                if _measure_facade.state["demoted"]:
                    print("\n  Demoted -> measure-only: "
                          + ", ".join(f"{c[0]}:{c[1]}" for c in _measure_facade.state["demoted"]))
                    print("  Re-promote: measure.py cohorts promote <lang:band>")
                print()
    elif _measure_facade.args[0] == "compression-stats":
        _measure_facade.output_json = "--json" in _measure_facade.args
        _measure_facade.days = 30
        for _measure_facade.i, _measure_facade.a in enumerate(_measure_facade.args):
            if _measure_facade.a == "--days" and _measure_facade.i + 1 < len(_measure_facade.args):
                try:
                    _measure_facade.days = int(_measure_facade.args[_measure_facade.i + 1])
                except ValueError:
                    pass
        _measure_facade.summary = _measure_facade._get_compression_summary(days=_measure_facade.days)
        _measure_facade.coverage = _measure_facade._get_compression_coverage(days=_measure_facade.days)
        _measure_facade.tripwire = _measure_facade._cohort_tripwire_state()
        if _measure_facade.output_json:
            print(_measure_facade.json.dumps(
                {"summary": _measure_facade.summary, "coverage": _measure_facade.coverage, "cohort_tripwire": _measure_facade.tripwire},
                indent=2))
        else:
            print(f"\n  Compression Stats ({_measure_facade.days}d)")
            print(f"  {'=' * 50}")
            print(f"  Total events:       {_measure_facade.summary['total_events']}")
            print(f"  Tokens saved:       {_measure_facade.summary['total_tokens_saved']:,}")
            print(f"  Overall ratio:      {_measure_facade.summary['overall_ratio']:.1%}")
            if _measure_facade.summary["by_feature"]:
                print("\n  By feature:")
                for _measure_facade.feat, _measure_facade.data in _measure_facade.summary["by_feature"].items():
                    print(f"    {_measure_facade.feat:25s}  {_measure_facade.data['events']:5d} events  "
                          f"{_measure_facade.data['tokens_saved']:>8,} tokens saved  "
                          f"ratio: {_measure_facade.data['avg_ratio']:.1%}  "
                          f"quality: {_measure_facade.data['quality_preserved_pct']:.0f}%")
            else:
                print("  No compression events yet. Enable v5 features to start tracking.")
            # U6: tiered coverage — measured / shadow kept strictly separate.
            for _measure_facade.tkey in _measure_facade._COMPRESSION_TIERS:
                _measure_facade.bucket = _measure_facade.coverage.get("tiers", {}).get(_measure_facade.tkey, [])
                if not _measure_facade.bucket:
                    continue
                print(f"\n  {_measure_facade._TIER_DISPLAY_NAMES[_measure_facade.tkey]}:")
                for _measure_facade.e in _measure_facade.bucket:
                    _measure_facade.kind = "realized" if _compat.read_global(_measure_facade, 'e')["ratio_kind"] == "realized" else "would-be"
                    print(f"    {_compat.read_global(_measure_facade, 'e')['label']:28s}  {_compat.read_global(_measure_facade, 'e')['events']:5d} events  "
                          f"{_measure_facade.kind} ratio: {_compat.read_global(_measure_facade, 'e')['ratio']:.1%}")
            _measure_facade.proxy = _measure_facade.coverage.get("first_read_proxy")
            if _measure_facade.proxy:
                _measure_facade.ready = "READY" if _measure_facade.proxy["promotion_ready"] else "not yet"
                print("\n  First-read shadow quality proxy (promotion gate):")
                print(f"    shadow reads: {_measure_facade.proxy['shadow_reads']}  "
                      f"edits after read: {_measure_facade.proxy['edits_after_read']}  "
                      f"edit rate: {_measure_facade.proxy['edit_rate_pct']:.1f}% "
                      f"(gate <{_measure_facade.proxy['promotion_gate_pct']:.0f}% — {_measure_facade.ready})")
            # Tripwire: per-cohort live edit-rate + any auto-demotions.
            if _measure_facade.tripwire.get("cohorts") or _measure_facade.tripwire.get("demoted"):
                print("\n  First-read cohort tripwire (live edit-rate, auto-demote):")
                for _measure_facade.c in _measure_facade.tripwire.get("cohorts", []):
                    _measure_facade.flag = "DEMOTED" if _measure_facade.c["demoted"] else "active"
                    print(f"    {_measure_facade.c['language']:10s} {_measure_facade.c['band']:10s} "
                          f"reads: {_measure_facade.c['skeletons']:>4d}  edits: {_measure_facade.c['followups']:>3d}  "
                          f"rate: {_measure_facade.c['edit_rate_pct']:>5.1f}%  [{_measure_facade.flag}]")
                if _measure_facade.tripwire.get("demoted"):
                    _measure_facade.dem = ", ".join(f"{c[0]}:{c[1]}" for c in _measure_facade.tripwire["demoted"])
                    print(f"    demoted -> measure-only: {_measure_facade.dem}")
                    print("    re-promote via: measure.py cohorts promote <lang:band>")
            print()
    elif _measure_facade.args[0] == "cache-report":
        # Cache-TTL watchdog (opportunity tier — observed waste, potential
        # recovery; never enters the realized savings headline).
        _measure_facade.output_json = "--json" in _measure_facade.args
        _measure_facade.fresh = "--fresh" in _measure_facade.args
        _measure_facade.verbose = "--verbose" in _measure_facade.args
        _measure_facade.days = 30
        for _measure_facade.i, _measure_facade.a in enumerate(_measure_facade.args):
            if _measure_facade.a == "--days" and _measure_facade.i + 1 < len(_measure_facade.args):
                try:
                    _measure_facade.days = int(_measure_facade.args[_measure_facade.i + 1])
                except ValueError:
                    pass
        # W6: clamp the window to a sane range. A 0/negative day count silently
        # scans nothing; an absurd one walks the entire history. Clamp to
        # [1, 365] and note any clamp on stderr so the number stays explainable.
        if _measure_facade.days < 1 or _measure_facade.days > 365:
            _measure_facade.clamped = min(365, max(1, _measure_facade.days))
            print(f"[Token Optimizer] --days {_measure_facade.days} out of range; "
                  f"clamped to {_measure_facade.clamped} (valid 1-365).", file=_measure_facade.sys.stderr)
            _measure_facade.days = _measure_facade.clamped
        if _measure_facade.verbose:
            # W8: --verbose needs per-session detail, which is never cached
            # (it bloats the sidecar), so always compute fresh + collect detail.
            _measure_facade.report = _measure_facade._get_cache_ttl_waste(days=_measure_facade.days, collect_sessions=True)
        else:
            _measure_facade.report = _measure_facade._cache_ttl_waste_cached(days=_measure_facade.days, fresh=_measure_facade.fresh)
        # The sidecar bookkeeping key is internal; drop it from the user surface.
        _measure_facade.report = {k: v for k, v in _measure_facade.report.items() if k != "_cached_ts"}
        if _measure_facade.output_json:
            print(_measure_facade.json.dumps(_measure_facade.report, indent=2))
        else:
            print(f"\n  Cache Health — TTL/cache-expiry watchdog ({_measure_facade.days}d)")
            print(f"  {'=' * 56}")
            print(f"  Platform:            {_measure_facade.report.get('platform', 'unknown')}")
            if not _measure_facade.report.get("available"):
                print("  No measurable sessions found in window.")
                print(f"\n  Recommendation:\n    {_measure_facade.report.get('recommendation', '')}")
            else:
                print(f"  Sessions scanned:    {_measure_facade.report['scanned_sessions']}")
                print(f"  Overall cache hits:  {_measure_facade.report['overall_cache_hit_rate']:.1%}")
                print(f"  Affected sessions:   {_measure_facade.report['affected_sessions']}")
                print(f"  Cache-expiry waste:  {_measure_facade.report['wasted_tokens']:,} tokens "
                      f"(${_measure_facade.report['wasted_cost_usd']:.2f} over {_measure_facade.days}d)")
                _measure_facade.rec_1h = _measure_facade.report.get('would_be_savings_1h_usd', 0.0)
                _measure_facade.avoid_beh = _measure_facade.report.get('avoidable_behavioral_usd', 0.0)
                if _measure_facade.rec_1h:
                    print(f"  Recoverable (1h):    ${_measure_facade.rec_1h:.2f} "
                          "(API/SDK cache_control counterfactual, opportunity)")
                if _measure_facade.avoid_beh:
                    print(f"  Avoidable (behavioral): ${_measure_facade.avoid_beh:.2f} "
                          "(resume within the hour; no setting extends the cache)")
                if not _measure_facade.rec_1h and not _measure_facade.avoid_beh:
                    print("  Recoverable:         $0.00")
                _measure_facade.no_cache = _measure_facade.report.get("no_cache_economics_sessions", 0)
                if _measure_facade.no_cache:
                    print(f"  No cache economics:  {_measure_facade.no_cache} session(s) on models with no prompt cache")
                # Per-provider breakdown — cache economics differ by provider.
                _measure_facade.provs = _measure_facade.report.get("by_provider", [])
                if _measure_facade.provs:
                    print("\n  By provider (cache_kind | sessions | affected | waste $ | confidence):")
                    for _measure_facade.p in _measure_facade.provs:
                        print(f"    {_measure_facade.p['label'][:34]:34s} {_measure_facade.p['cache_kind']:18s} "
                              f"{_measure_facade.p['sessions']:4d} {_measure_facade.p['affected_sessions']:4d}  "
                              f"${_measure_facade.p['wasted_cost_usd']:8.2f}  {_measure_facade.p['confidence']}")
                        print(f"        remedy: {_measure_facade.p['remedy']}")
                # W8: --verbose per-session drill-down for disputed numbers.
                _measure_facade.detail = _measure_facade.report.get("session_detail") or []
                if _measure_facade.verbose:
                    print("\n  Per-session detail (affected sessions, biggest waste first):")
                    if not _measure_facade.detail:
                        print("    (no affected sessions in window)")
                    else:
                        print(f"    {'session':40s} {'gap(s)':>8s} "
                              f"{'rewritten tok':>14s} {'est $':>9s}")
                        for _measure_facade.d in _measure_facade.detail:
                            print(f"    {_measure_facade.d['session'][:40]:40s} {_measure_facade.d['gap_seconds']:>8d} "
                                  f"{_measure_facade.d['rewritten_tokens']:>14,} ${_measure_facade.d['est_cost_usd']:>8.2f}")
                print(f"\n  Headline remedy:\n    {_measure_facade.report['recommendation']}")
            # Coverage gaps — platforms whose cache+timestamp data we cannot read.
            _measure_facade.gaps = _measure_facade.report.get("coverage_gaps", [])
            if _measure_facade.gaps:
                print("\n  Not measurable (documented coverage gaps):")
                for _measure_facade.g in _measure_facade.gaps:
                    print(f"    {_measure_facade.g['platform']:10s} {_measure_facade.g['reason']}")
            print("\n  [opportunity tier — observed waste, potential recovery; "
                  "NOT counted in realized savings]\n")
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] == "benchmark":
        # Run compression benchmark fixtures
        _measure_facade.script_dir = _measure_facade.Path(_measure_facade.__file__).resolve().parent
        _measure_facade.benchmark_path = _measure_facade.script_dir / "benchmark.py"
        if not _measure_facade.benchmark_path.exists():
            print("[Error] benchmark.py not found.")
            _measure_facade.sys.exit(1)
        import importlib.util; _measure_facade.importlib = importlib; del importlib
        _measure_facade.spec = _measure_facade.importlib.util.spec_from_file_location("benchmark", str(_measure_facade.benchmark_path))
        _measure_facade.bench_mod = _measure_facade.importlib.util.module_from_spec(_measure_facade.spec)
        _measure_facade.spec.loader.exec_module(_measure_facade.bench_mod)
        _measure_facade.output_json = "--json" in _measure_facade.args
        # Load actual compressor so benchmarks test real compression, not just fixture structure
        _measure_facade.compressor = None
        try:
            _measure_facade.compress_path = _measure_facade.script_dir / "bash_compress.py"
            if _measure_facade.compress_path.exists():
                _measure_facade.compress_spec = _measure_facade.importlib.util.spec_from_file_location("bash_compress", str(_measure_facade.compress_path))
                _measure_facade.compress_mod = _measure_facade.importlib.util.module_from_spec(_measure_facade.compress_spec)
                _measure_facade.compress_spec.loader.exec_module(_measure_facade.compress_mod)
                _measure_facade.compressor = _measure_facade.compress_mod.compress
        except Exception:
            pass  # Fall back to validation-only mode
        _measure_facade.ok = _measure_facade.bench_mod.run_benchmarks(compressor=_measure_facade.compressor, as_json=_measure_facade.output_json)
        _measure_facade.sys.exit(0 if _measure_facade.ok else 1)
    elif _measure_facade.args[0] == "structure-map":
        # User-facing inspection command: show structure map for a single file
        if len(_measure_facade.args) < 2:
            print("[Error] Usage: measure.py structure-map <file>")
            _measure_facade.sys.exit(1)
        _measure_facade.target_file = _measure_facade.args[1]
        if not _measure_facade.Path(_measure_facade.target_file).exists():
            print(f"[Error] File not found: {_measure_facade.target_file}")
            _measure_facade.sys.exit(1)
        try:
            from structure_map import summarize_code_source as _measure_import_48; _measure_facade.summarize_code_source = _measure_import_48; del _measure_import_48
            _measure_facade.target_path = _measure_facade.Path(_measure_facade.target_file).resolve()
            _measure_facade.content = _measure_facade.target_path.read_text(encoding="utf-8", errors="replace")
            _measure_facade.file_size = len(_measure_facade.content.encode("utf-8", errors="replace"))
            _measure_facade.result = _measure_facade.summarize_code_source(
                _measure_facade.content,
                file_path=str(_measure_facade.target_path),
                file_tokens_est=max(1, _measure_facade.file_size // 4),
                file_size_bytes=_measure_facade.file_size,
            )
            if _measure_facade.result and _measure_facade.result.eligible and _measure_facade.result.replacement_text:
                print(f"  Type: {_measure_facade.result.replacement_type}")
                print(f"  Confidence: {_measure_facade.result.confidence:.2f}")
                print(f"  Tokens: {_measure_facade.result.file_tokens_est} -> {_measure_facade.result.replacement_tokens_est} (saved {_measure_facade.result.file_tokens_est - _measure_facade.result.replacement_tokens_est})")
                print(f"\n{_measure_facade.result.replacement_text}")
            else:
                _measure_facade.reason = _measure_facade.result.reason if _measure_facade.result else "unknown"
                print(f"[Info] No structure map for {_measure_facade.target_file} (reason: {_measure_facade.reason})")
        except ImportError:
            print("[Error] structure_map.py not found.")
            _measure_facade.sys.exit(1)
        except Exception as _measure_exception_49:
            _measure_facade.e = _measure_exception_49
            try:
                print(f"[Error] {_compat.read_global(_measure_facade, 'e')}")
                _measure_facade.sys.exit(1)
            finally:
                _measure_facade.e = None
                del _measure_facade.e
    elif _measure_facade.args[0] == "plugin-cleanup":
        _measure_facade.dry = "--dry-run" in _measure_facade.args
        _measure_facade.plugin_cleanup(dry_run=_measure_facade.dry)
    elif _measure_facade.args[0] == "subagent-cache":
        _measure_facade._subagent_cache_cli(_measure_facade.args)
    elif _measure_facade.args[0] == "recommendations":
        _measure_facade._recs_cli(_measure_facade.args)
    elif _measure_facade.args[0] == "ensure-health":
        # Called by SessionStart hook. Wrapped in a wall-clock guard so a
        # pathologically slow filesystem or lock contention cannot block the
        # new session indefinitely. Handler exits 0 gracefully on timeout;
        # the 24h throttle on internal self-heal paths means the next
        # SessionStart is typically a no-op anyway.
        #
        # Cowork parity: ensure-health is wired onto UserPromptSubmit too (Cowork
        # never fires SessionStart). It is idempotent + 24h-throttled internally,
        # but re-running its filesystem scan on every prompt is wasteful, so the
        # UserPromptSubmit copy carries --once-per-session and no-ops after the
        # first fire of the session. On native Claude Code the SessionStart copy
        # carries --once-mark: it always runs and (re)writes the marker so the
        # UserPromptSubmit copy is a single stat no-op -- zero behaviour change
        # for existing users -- while a resume/compact SessionStart still runs.
        #
        # The stdin hook payload is read ONCE here and reused: stdin is a stream,
        # so a second _read_stdin_hook_input() would come back empty. It also tells
        # us whether we are running as a hook at all (hook_event_name present) --
        # which decides whether stdout has to satisfy the host's JSON contract or
        # is just a human reading `measure.py ensure-health` in a terminal.
        _measure_facade._eh_hook_input = {}
        try:
            _measure_facade._eh_stdin_is_tty = _measure_facade.sys.stdin.isatty()
        except (AttributeError, OSError, ValueError):
            _measure_facade._eh_stdin_is_tty = False
        if not _measure_facade._eh_stdin_is_tty:
            _measure_facade._eh_hook_input = _measure_facade._read_stdin_hook_input()
        _measure_facade._eh_sid = _measure_facade._eh_hook_input.get("session_id")
        if "--once-mark" in _measure_facade.args:
            _measure_facade._mark_ran_this_session("ensure-health", _measure_facade._eh_sid)
        elif "--once-per-session" in _measure_facade.args:
            if _measure_facade._ran_once_this_session("ensure-health", _measure_facade._eh_sid):
                _measure_facade.sys.exit(0)
        # Hook context -> stdout MUST be empty or one valid JSON object. Codex
        # rejects the whole SessionStart hook when stdout starts with "[" and is
        # not valid JSON, and every ensure-health line (the v5 welcome banner, the
        # baseline/daemon/cleanupPeriodDays notices) starts with "[Token
        # Optimizer]". --once-mark / --once-per-session are hook-only flags, so
        # they also imply hook context on a host that omits hook_event_name.
        _measure_facade._eh_event = str(_measure_facade._eh_hook_input.get("hook_event_name") or "").strip()
        if not _measure_facade._eh_event:
            if "--once-per-session" in _measure_facade.args:
                _measure_facade._eh_event = "UserPromptSubmit"
            elif "--once-mark" in _measure_facade.args:
                _measure_facade._eh_event = "SessionStart"

        def _eh_body():
            # The dashboard-daemon ensure/revive runs FIRST, under its own
            # short independent guard, BEFORE the 8s health budget is armed. A slow
            # health scan that later trips the 8s watchdog can therefore never skip
            # reinstalling a missing launchd plist / dead daemon. The daemon ensure
            # is idempotent + internally throttled, so the second call inside
            # run_ensure_health is a cheap no-op. Fail-open: never raises.
            _measure_facade = _compat.facade()
            _measure_facade._ensure_health_daemon_revive_first()
            _tok_hook_old_sig = _measure_facade._install_hook_budget(8)
            try:
                _measure_facade.run_ensure_health()
            except _measure_facade._HookTimeout:
                print(
                    "[Token Optimizer] hook budget exceeded; skipping ensure-health tick to keep session responsive",
                    file=_measure_facade.sys.stderr,
                )
                _measure_facade.sys.exit(0)
            finally:
                _measure_facade._clear_hook_budget(_tok_hook_old_sig)
        _measure_facade._eh_body = _compat.bind(_eh_body, _measure_facade.__name__)
        _measure_facade._eh_body.__qualname__ = '_eh_body'
        del _eh_body

        # In hook context the WHOLE body is captured (revive included) so no future
        # stdout line can leak past the envelope; a terminal run keeps the readable
        # text stream it has always had.
        if _measure_facade._eh_event:
            _measure_facade._run_hook_emitting_json(_measure_facade._eh_body, event=_measure_facade._eh_event)
        else:
            _measure_facade._eh_body()
    elif _measure_facade.args[0] == "setup-quality-bar":
        _measure_facade.dry = "--dry-run" in _measure_facade.args
        _measure_facade.uninstall = "--uninstall" in _measure_facade.args
        _measure_facade.status = "--status" in _measure_facade.args
        _measure_facade.setup_quality_bar(dry_run=_measure_facade.dry, uninstall=_measure_facade.uninstall, status_only=_measure_facade.status,
                          user_initiated=True)
    elif _measure_facade.args[0] == "setup-vscode-extension":
        # Manage the VS Code status-bar extension auto-install.
        #   --uninstall : sticky opt-out (+ remove the extension if a CLI is present)
        #   --install / --enable : clear the opt-out and install now
        #   --status (default) : report opt-out + detected editor + install state
        if "--uninstall" in _measure_facade.args:
            _measure_facade._set_vscode_ext_disabled(True)
            _measure_facade._label, _measure_facade._cli = _measure_facade._detect_vscode_family_cli()
            _measure_facade._removed = False
            if _measure_facade._cli:
                try:
                    _measure_facade._r = _measure_facade.subprocess.run(
                        [_measure_facade._cli, "--uninstall-extension", _measure_facade._VSCODE_EXTENSION_ID],
                        capture_output=True, text=True, timeout=60, creationflags=_measure_facade._NO_WINDOW,
                    )
                    _measure_facade._removed = _measure_facade._r.returncode == 0
                except (OSError, _measure_facade.subprocess.SubprocessError):
                    _measure_facade._removed = False
            print("[Token Optimizer] VS Code extension auto-install disabled (sticky)."
                  + (" Extension removed." if _measure_facade._removed else ""))
        elif "--install" in _measure_facade.args or "--enable" in _measure_facade.args:
            _measure_facade._set_vscode_ext_disabled(False)
            _measure_facade._write_config_flag("vscode_ext_last_check", 0)  # force a fresh attempt
            _measure_facade._label, _measure_facade._cli = _measure_facade._detect_vscode_family_cli()
            if not _measure_facade._cli:
                print("[Token Optimizer] Opted in. No VS Code-family editor CLI detected here; "
                      "the extension will install on your next session inside VS Code/Cursor/Windsurf.")
            else:
                try:
                    _measure_facade._ensure_vscode_extension()
                except Exception as _measure_exception_50:
                    _measure_facade._e = _measure_exception_50
                    try:
                        print(f"[Token Optimizer] VS Code extension install failed: {_compat.read_global(_measure_facade, '_e')}", file=_measure_facade.sys.stderr)
                    finally:
                        _measure_facade._e = None
                        del _measure_facade._e
        else:
            _measure_facade._disabled = _measure_facade._read_config_flag("vscode_ext_disabled", False)
            _measure_facade._label, _measure_facade._cli = _measure_facade._detect_vscode_family_cli()
            _measure_facade._state = "unknown"
            if _measure_facade._cli:
                try:
                    _measure_facade._r = _measure_facade.subprocess.run([_measure_facade._cli, "--list-extensions"], capture_output=True,
                                        text=True, timeout=15, creationflags=_measure_facade._NO_WINDOW)
                    _measure_facade._state = ("installed" if _measure_facade._r.returncode == 0
                              and _measure_facade._VSCODE_EXTENSION_ID.lower() in (_measure_facade._r.stdout or "").lower()
                              else "not installed")
                except (OSError, _measure_facade.subprocess.SubprocessError):
                    _measure_facade._state = "unknown"
            print(f"[Token Optimizer] VS Code extension: auto-install "
                  f"{'DISABLED' if _measure_facade._disabled else 'enabled'}; editor="
                  f"{_measure_facade._label or 'none detected'}; state={_measure_facade._state}. "
                  f"Marketplace/OpenVSX id: {_measure_facade._VSCODE_EXTENSION_ID}")
    elif _measure_facade.args[0] == "list-checkpoints":
        _measure_facade.cps = _measure_facade.list_checkpoints()
        if not _measure_facade.cps:
            print("[Token Optimizer] No checkpoints found.")
        else:
            print(f"\n  Session Checkpoints ({len(_measure_facade.cps)} found)")
            print(f"  {'=' * 40}")
            for _measure_facade.cp in _measure_facade.cps[:20]:
                _measure_facade.age = _measure_facade.datetime.now() - _measure_facade.cp["created"]
                _measure_facade.age_str = f"{int(_measure_facade.age.total_seconds() / 60)}m ago" if _measure_facade.age.total_seconds() < 3600 else f"{int(_measure_facade.age.total_seconds() / 3600)}h ago"
                print(f"    {_measure_facade.cp['filename']:50s} {_measure_facade.age_str}")
            print()
    elif _measure_facade.args[0] == "checkpoint-stats":
        _measure_facade.days = 7
        _measure_facade.output_json = "--json" in _measure_facade.args
        _measure_facade.i = 1
        while _measure_facade.i < len(_measure_facade.args):
            if _measure_facade.args[_measure_facade.i] == "--days" and _measure_facade.i + 1 < len(_measure_facade.args):
                try:
                    _measure_facade.days = max(1, min(365, int(_measure_facade.args[_measure_facade.i + 1])))
                except ValueError:
                    pass
                _measure_facade.i += 2
                continue
            _measure_facade.i += 1
        _measure_facade.checkpoint_stats(days=_measure_facade.days, as_json=_measure_facade.output_json)
    elif _measure_facade.args[0] in ("trends", "savings"):
        # Shared --days/--json parsing for trends and savings
        _measure_facade.days = 30
        _measure_facade.output_json = False
        _measure_facade.i = 1
        while _measure_facade.i < len(_measure_facade.args):
            if _measure_facade.args[_measure_facade.i] == "--days" and _measure_facade.i + 1 < len(_measure_facade.args):
                try:
                    _measure_facade.days = int(_measure_facade.args[_measure_facade.i + 1])
                    if _measure_facade.days < 1:
                        print("[Error] --days must be a positive integer.")
                        _measure_facade.sys.exit(1)
                except ValueError:
                    print(f"[Error] Invalid --days value: {_measure_facade.args[_measure_facade.i + 1]}")
                    _measure_facade.sys.exit(1)
                _measure_facade.i += 2
            elif _measure_facade.args[_measure_facade.i] == "--json":
                _measure_facade.output_json = True
                _measure_facade.i += 1
            else:
                print(f"[Error] Unknown flag: {_measure_facade.args[_measure_facade.i]}")
                _measure_facade.sys.exit(1)
        if _measure_facade.args[0] == "trends":
            _measure_facade.usage_trends(days=_measure_facade.days, as_json=_measure_facade.output_json)
        else:
            _measure_facade.savings_report(days=_measure_facade.days, as_json=_measure_facade.output_json)
    elif _measure_facade.args[0] == "pin-baseline":
        _measure_facade.status = "building"
        while _measure_facade.status == "building":
            _measure_facade.status = _measure_facade._pretool_anchor_step(budget_seconds=30.0)
        print(f"Pre-install baseline: {_measure_facade.status}")
    elif _measure_facade.args[0] == "skill" and len(_measure_facade.args) >= 3:
        _measure_facade.action = _measure_facade.args[1]  # archive or restore
        _measure_facade.name = _measure_facade.args[2]
        if _measure_facade.action in ("archive", "restore"):
            _measure_facade.ok = _measure_facade._manage_skill(_measure_facade.action, _measure_facade.name)
            _measure_facade.sys.exit(0 if _measure_facade.ok else 1)
        else:
            print(f"  Unknown skill action: {_measure_facade.action}. Use 'archive' or 'restore'.")
            _measure_facade.sys.exit(1)
    elif _measure_facade.args[0] == "mcp" and len(_measure_facade.args) >= 3:
        _measure_facade.action = _measure_facade.args[1]  # disable or enable
        _measure_facade.name = _measure_facade.args[2]
        if _measure_facade.action in ("disable", "enable"):
            _measure_facade.ok = _measure_facade._manage_mcp(_measure_facade.action, _measure_facade.name)
            _measure_facade.sys.exit(0 if _measure_facade.ok else 1)
        else:
            print(f"  Unknown mcp action: {_measure_facade.action}. Use 'disable' or 'enable'.")
            _measure_facade.sys.exit(1)
    elif _measure_facade.args[0] == "codex-skill" and len(_measure_facade.args) >= 3:
        _measure_facade.action = _measure_facade.args[1]  # disable or enable
        _measure_facade.raw_path = None
        _measure_facade.name = None
        if "--path" in _measure_facade.args:
            _measure_facade.idx = _measure_facade.args.index("--path")
            if _measure_facade.idx + 1 < len(_measure_facade.args):
                _measure_facade.raw_path = _measure_facade.args[_measure_facade.idx + 1]
        else:
            _measure_facade.name = _measure_facade.args[2]
        if _measure_facade.action in ("disable", "enable"):
            _measure_facade.ok = _measure_facade._manage_codex_skill(_measure_facade.action, raw_path=_measure_facade.raw_path, name=_measure_facade.name)
            _measure_facade.sys.exit(0 if _measure_facade.ok else 1)
        print(f"  Unknown codex-skill action: {_measure_facade.action}. Use 'disable' or 'enable'.")
        _measure_facade.sys.exit(1)
    elif _measure_facade.args[0] == "codex-mcp" and len(_measure_facade.args) >= 3:
        _measure_facade.action = _measure_facade.args[1]  # disable or enable
        _measure_facade.name = _measure_facade.args[2]
        if _measure_facade.action in ("disable", "enable"):
            _measure_facade.ok = _measure_facade._manage_codex_mcp(_measure_facade.action, _measure_facade.name)
            _measure_facade.sys.exit(0 if _measure_facade.ok else 1)
        print(f"  Unknown codex-mcp action: {_measure_facade.action}. Use 'disable' or 'enable'.")
        _measure_facade.sys.exit(1)
    elif _measure_facade.args[0] == "api-manage" and len(_measure_facade.args) >= 3:
        # Headless skill/MCP management for the dashboard DAEMON to proxy to.
        # The daemon is a standalone generated script and cannot call the
        # in-process managers that `dashboard --serve` uses, so it shells out
        # here. Mirrors that handler's runtime-aware logic and prints exactly
        # the JSON the dashboard expects ({ok, message, manage}). Always emits
        # one JSON object and exits 0; the daemon keys off JSON `ok`, not the
        # exit code. Fixes the daemon-only bug where skill/MCP toggles 403'd
        # because those endpoints were never ported from --serve to the daemon.
        import json as _measure_import_51; _measure_facade._json = _measure_import_51; del _measure_import_51; import io as _measure_import_52; _measure_facade._io = _measure_import_52; del _measure_import_52; import contextlib as _measure_import_53; _measure_facade._ctx = _measure_import_53; del _measure_import_53
        _measure_facade.endpoint = _measure_facade.args[1]   # skill/archive | skill/restore | mcp/disable | mcp/enable
        _measure_facade.op_name = _measure_facade.args[2]
        _measure_facade.is_codex = _measure_facade.detect_runtime() == "codex"
        # Path-traversal guard. Codex skill ops resolve op_name as a filesystem
        # PATH (raw_path -> Path(...).resolve()), and the daemon's name regex
        # permits '/' and '.', so reject any '..' segment or NUL before dispatch.
        # Claude managers have their own guard; this closes the codex vector that
        # the daemon would otherwise widen. Legitimate skill names/paths never
        # contain '..'.
        _measure_facade._parts = str(_measure_facade.op_name).replace("\\", "/").split("/")
        if "\0" in str(_measure_facade.op_name) or ".." in _measure_facade._parts:
            _measure_facade.sys.stdout.write(_measure_facade._json.dumps({"ok": False, "message": f"Rejected unsafe name: {_measure_facade.op_name}", "manage": None}))
            _measure_facade.sys.exit(0)
        _measure_facade.ok = False
        _measure_facade.msg = ""
        _measure_facade.manage = None
        _measure_facade.known = True
        # The _manage_* helpers and _collect_management_data print human-readable
        # progress to stdout; capture it so this command emits ONLY the JSON the
        # daemon parses (a stray banner line was making json.loads(stdout) fail).
        _measure_facade._buf = _measure_facade._io.StringIO()
        try:
            with _measure_facade._ctx.redirect_stdout(_measure_facade._buf):
                if _measure_facade.endpoint == "skill/archive":
                    _measure_facade.ok = _measure_facade._manage_codex_skill("disable", raw_path=_measure_facade.op_name) if _measure_facade.is_codex else _measure_facade._manage_skill("archive", _measure_facade.op_name)
                    _measure_facade.msg = ((f"Disabled Codex skill: {_measure_facade.op_name}" if _measure_facade.is_codex else f"Archived skill: {_measure_facade.op_name}")
                           if _measure_facade.ok else f"Failed to archive: {_measure_facade.op_name}")
                elif _measure_facade.endpoint == "skill/restore":
                    _measure_facade.ok = _measure_facade._manage_codex_skill("enable", raw_path=_measure_facade.op_name) if _measure_facade.is_codex else _measure_facade._manage_skill("restore", _measure_facade.op_name)
                    _measure_facade.msg = ((f"Enabled Codex skill: {_measure_facade.op_name}" if _measure_facade.is_codex else f"Restored skill: {_measure_facade.op_name}")
                           if _measure_facade.ok else f"Failed to restore: {_measure_facade.op_name}")
                elif _measure_facade.endpoint == "mcp/disable":
                    _measure_facade.ok = _measure_facade._manage_codex_mcp("disable", _measure_facade.op_name) if _measure_facade.is_codex else _measure_facade._manage_mcp("disable", _measure_facade.op_name)
                    _measure_facade.msg = f"Disabled MCP server: {_measure_facade.op_name}" if _measure_facade.ok else f"Failed to disable: {_measure_facade.op_name}"
                elif _measure_facade.endpoint == "mcp/enable":
                    _measure_facade.ok = _measure_facade._manage_codex_mcp("enable", _measure_facade.op_name) if _measure_facade.is_codex else _measure_facade._manage_mcp("enable", _measure_facade.op_name)
                    _measure_facade.msg = f"Enabled MCP server: {_measure_facade.op_name}" if _measure_facade.ok else f"Failed to enable: {_measure_facade.op_name}"
                else:
                    _measure_facade.known = False
                if _measure_facade.known and _measure_facade.ok:
                    try:
                        _measure_facade.manage = _measure_facade._collect_management_data()
                    except Exception:
                        _measure_facade.manage = None
        except Exception as _measure_exception_54:
            _measure_facade.e = _measure_exception_54
            try:
                _measure_facade.sys.stdout.write(_measure_facade._json.dumps({'ok': False, 'message': f"error: {_compat.read_global(_measure_facade, 'e')}", 'manage': None}))
                _measure_facade.sys.exit(0)
            finally:
                _measure_facade.e = None
                del _measure_facade.e
        if not _measure_facade.known:
            _measure_facade.sys.stdout.write(_measure_facade._json.dumps({"ok": False, "message": f"Unknown endpoint: {_measure_facade.endpoint}", "manage": None}))
            _measure_facade.sys.exit(0)
        _measure_facade.sys.stdout.write(_measure_facade._json.dumps({"ok": _measure_facade.ok, "message": _measure_facade.msg, "manage": _measure_facade.manage}, default=str))
        _measure_facade.sys.exit(0)
    elif _measure_facade.args[0] == "jsonl-inspect":
        _measure_facade.output_json = "--json" in _measure_facade.args
        _measure_facade.target = None
        for _measure_facade.a in _measure_facade.args[1:]:
            if _measure_facade.a.startswith("--"):
                continue
            _measure_facade.target = _measure_facade.a
            break
        _measure_facade.jsonl_inspect(arg=_measure_facade.target, as_json=_measure_facade.output_json)
    elif _measure_facade.args[0] == "jsonl-trim":
        _measure_facade.do_apply = "--apply" in _measure_facade.args
        _measure_facade.threshold = 4000
        _measure_facade.target = None
        for _measure_facade.i, _measure_facade.a in enumerate(_measure_facade.args[1:], start=1):
            if _measure_facade.a == "--threshold" and _measure_facade.i + 1 < len(_measure_facade.args):
                try:
                    _measure_facade.threshold = int(_measure_facade.args[_measure_facade.i + 1])
                except ValueError:
                    print(f"[Error] Invalid --threshold value: {_measure_facade.args[_measure_facade.i + 1]}")
                    _measure_facade.sys.exit(1)
            elif _measure_facade.a.startswith("--"):
                continue
            elif _measure_facade.target is None:
                _measure_facade.target = _measure_facade.a
        _measure_facade.jsonl_trim(arg=_measure_facade.target, apply=_measure_facade.do_apply, threshold=_measure_facade.threshold)
    elif _measure_facade.args[0] == "jsonl-dedup":
        _measure_facade.do_apply = "--apply" in _measure_facade.args
        _measure_facade.target = None
        for _measure_facade.a in _measure_facade.args[1:]:
            if _measure_facade.a.startswith("--"):
                continue
            _measure_facade.target = _measure_facade.a
            break
        _measure_facade.jsonl_dedup(arg=_measure_facade.target, apply=_measure_facade.do_apply)
    elif _measure_facade.args[0] == "attention-score":
        _measure_facade.output_json = "--json" in _measure_facade.args
        _measure_facade.target = None
        for _measure_facade.a in _measure_facade.args[1:]:
            if _measure_facade.a.startswith("--"):
                continue
            _measure_facade.target = _measure_facade.a
            break
        _measure_facade.attention_score(filepath=_measure_facade.target, as_json=_measure_facade.output_json)
    elif _measure_facade.args[0] == "attention-optimize":
        _measure_facade.do_apply = "--apply" in _measure_facade.args
        _measure_facade.dry = "--dry-run" in _measure_facade.args or not _measure_facade.do_apply
        _measure_facade.target = None
        for _measure_facade.a in _measure_facade.args[1:]:
            if _measure_facade.a.startswith("--"):
                continue
            _measure_facade.target = _measure_facade.a
            break
        _measure_facade.attention_optimize(filepath=_measure_facade.target, dry_run=_measure_facade.dry, apply=_measure_facade.do_apply)
    elif _measure_facade.args[0] == "memory-review":
        _measure_facade.output_json = "--json" in _measure_facade.args
        _measure_facade.do_apply = "--apply" in _measure_facade.args
        _measure_facade.stale_d = 180
        _measure_facade.proj_dir = None
        for _measure_facade.i, _measure_facade.a in enumerate(_measure_facade.args):
            if _measure_facade.a == "--stale-days" and _measure_facade.i + 1 < len(_measure_facade.args):
                try:
                    _measure_facade.stale_d = int(_measure_facade.args[_measure_facade.i + 1])
                except ValueError:
                    pass
            elif _measure_facade.a == "--project-dir" and _measure_facade.i + 1 < len(_measure_facade.args):
                _measure_facade.proj_dir = _measure_facade.args[_measure_facade.i + 1]
        _measure_facade.result = _measure_facade.memory_review(as_json=_measure_facade.output_json, apply=_measure_facade.do_apply,
                               stale_days=_measure_facade.stale_d, project_dir=_measure_facade.proj_dir)
        if _measure_facade.output_json and _measure_facade.result:
            print(_measure_facade.json.dumps(_measure_facade.result, indent=2, default=str))
    elif _measure_facade.args[0] == "cursor-audit":
        # Read-only audit of Cursor rules + hooks; deliberately NOT gated on
        # detect_runtime() == "cursor": beyond the Cursor runtime itself it
        # reads only 4 Claude settings files for hook-import coexistence
        # detection (never other ~/.claude state), so a Claude-runtime user
        # auditing their Cursor setup is legitimate.
        _measure_facade.output_json = "--json" in _measure_facade.args
        _measure_facade.proj_dir = None
        for _measure_facade.i, _measure_facade.a in enumerate(_measure_facade.args):
            if _measure_facade.a == "--project-dir" and _measure_facade.i + 1 < len(_measure_facade.args):
                _measure_facade.proj_dir = _measure_facade.args[_measure_facade.i + 1]
        import cursor_audit as _measure_import_ca
        _measure_facade.cursor_audit_mod = _measure_import_ca
        del _measure_import_ca
        _measure_facade.result = _measure_facade.cursor_audit_mod.cursor_audit(project_dir=_measure_facade.proj_dir, as_json=_measure_facade.output_json)
        if _measure_facade.output_json and _measure_facade.result:
            print(_measure_facade.json.dumps(_measure_facade.result, indent=2, default=str))
    elif _measure_facade.args[0] == "archive-result":
        # PostToolUse hook handler: archive large tool results
        _measure_facade.quiet = "--quiet" in _measure_facade.args or "-q" in _measure_facade.args
        _measure_facade.archive_result(quiet=_measure_facade.quiet)
    elif _measure_facade.args[0] == "session-cost":
        # "What did the Garden session yesterday cost?" Read-only; see session_cost_main.
        _measure_facade.sys.exit(_measure_facade.session_cost_main(_measure_facade.args[1:]))
    elif _measure_facade.args[0] == "expand":
        # Retrieve archived tool result
        _measure_facade.list_all = "--list" in _measure_facade.args
        _measure_facade.search_query = None
        _measure_facade.sid = None
        _measure_facade.tool_id = None
        _measure_facade.window_flags = {}
        _measure_facade.expand_value_flags = {"--session", "--search", "--grep",
                              "--lines", "--start", "--chars",
                              "--match", "--context", "--skip"}
        _measure_facade.i = 1
        while _measure_facade.i < len(_measure_facade.args):
            _measure_facade.a = _measure_facade.args[_measure_facade.i]
            if _measure_facade.a in _measure_facade.expand_value_flags:
                if _measure_facade.i + 1 >= len(_measure_facade.args):
                    print(f"[Error] {_measure_facade.a} requires a value")
                    _measure_facade.sys.exit(1)
                _measure_facade.val = _measure_facade.args[_measure_facade.i + 1]
                _measure_facade.i += 2
                if _measure_facade.a == "--session":
                    _measure_facade.sid = _measure_facade.val
                elif _measure_facade.a in ("--search", "--grep"):
                    _measure_facade.search_query = _measure_facade.val
                else:
                    _measure_facade.window_flags[_measure_facade.a[2:]] = _measure_facade.val
            elif _measure_facade.a == "--list":
                _measure_facade.i += 1
            elif _measure_facade.a.startswith("-"):
                # An unrecognized flag must not fall through to a full expand:
                # the caller asked for a bounded read and would get the whole
                # archived output instead.
                print(f"[Error] Unknown expand option: {_measure_facade.a}")
                _measure_facade.sys.exit(1)
            elif _measure_facade.tool_id is None and _measure_facade.search_query is None:
                _measure_facade.tool_id = _measure_facade.a
                _measure_facade.i += 1
            else:
                print(f"[Error] Unexpected expand argument: {_measure_facade.a}")
                _measure_facade.sys.exit(1)
        if _measure_facade.search_query is not None:
            _measure_facade.expand_search(query=_measure_facade.search_query, session_id=_measure_facade.sid)
        else:
            _measure_facade.expand_archived(tool_use_id=_measure_facade.tool_id, session_id=_measure_facade.sid, list_all=_measure_facade.list_all,
                            window_flags=_measure_facade.window_flags or None)
    elif _measure_facade.args[0] == "archive-cleanup":
        # Clean up archived tool results
        _measure_facade.sid = None
        for _measure_facade.a in _measure_facade.args[1:]:
            if _measure_facade.a.startswith("--"):
                continue
            _measure_facade.sid = _measure_facade.a
            break
        _measure_facade.archive_cleanup(session_id=_measure_facade.sid)
    elif _measure_facade.args[0] == "purge":
        # Delete all Token Optimizer data (enterprise compliance / uninstall)
        _measure_facade.confirm = "--confirm" in _measure_facade.args
        _measure_facade.force = "--force" in _measure_facade.args
        _measure_facade._purge_all_data(confirm=_measure_facade.confirm, force=_measure_facade.force)
    elif _measure_facade.args[0] == "read-cache-clear":
        # Clear read cache (called by PreCompact hook or manually)
        _measure_facade.sid = "all"
        for _measure_facade.i, _measure_facade.a in enumerate(_measure_facade.args):
            if _measure_facade.a == "--session" and _measure_facade.i + 1 < len(_measure_facade.args):
                _measure_facade.sid = _measure_facade.args[_measure_facade.i + 1]
        _measure_facade.quiet = "--quiet" in _measure_facade.args or "-q" in _measure_facade.args
        from pathlib import Path as _measure_import_55; _measure_facade._P = _measure_import_55; del _measure_import_55
        _measure_facade.rc_script = _measure_facade._P(_measure_facade.__file__).resolve().parent / "read_cache.py"
        if _measure_facade.rc_script.exists():
            _measure_facade._run_script_child(
                _measure_facade.rc_script, ["--clear", "--session", _measure_facade.sid] + (["--quiet"] if _measure_facade.quiet else []),
                timeout=5,
            )
    elif _measure_facade.args[0] == "read-cache-stats":
        # Show read cache stats
        _measure_facade.sid = "unknown"
        for _measure_facade.i, _measure_facade.a in enumerate(_measure_facade.args):
            if _measure_facade.a == "--session" and _measure_facade.i + 1 < len(_measure_facade.args):
                _measure_facade.sid = _measure_facade.args[_measure_facade.i + 1]
        from pathlib import Path as _measure_import_56; _measure_facade._P = _measure_import_56; del _measure_import_56
        _measure_facade.rc_script = _measure_facade._P(_measure_facade.__file__).resolve().parent / "read_cache.py"
        if _measure_facade.rc_script.exists():
            _measure_facade._run_script_child(_measure_facade.rc_script, ["--stats", "--session", _measure_facade.sid], timeout=5)
    elif _measure_facade.args[0] == "structure-proof":
        from pathlib import Path as _measure_import_57; _measure_facade._P = _measure_import_57; del _measure_import_57
        _measure_facade.proof_script = _measure_facade._P(_measure_facade.__file__).resolve().parent / "structure_replay.py"
        if not _measure_facade.proof_script.exists():
            print(f"[Token Optimizer] structure_replay.py not found at {_measure_facade.proof_script}")
            _measure_facade.sys.exit(1)
        _measure_facade.result = _measure_facade._run_script_child(_measure_facade.proof_script, _measure_facade.args[1:])
        _measure_facade.sys.exit(_measure_facade.result.returncode)
    else:
        print("Usage:")
        print("  python3 measure.py quick               # Quick scan: overhead, degradation risk, top offenders")
        print("  python3 measure.py quick --json         # Machine-readable quick scan")
        print("  python3 measure.py route --task \"...\"    # Model + reasoning-effort advice for one task")
        print("  python3 measure.py route --task \"...\" --json  # Machine-readable recommendation")
        print("  python3 measure.py doctor               # Health check: verify all components installed")
        print("  python3 measure.py doctor --json        # Machine-readable doctor output")
        print("  python3 measure.py codex-doctor         # Codex adapter readiness check")
        print("  python3 measure.py codex-install        # Install Codex hooks into a project")
        print("  python3 measure.py hermes-doctor         # Hermes adapter readiness check")
        print("  python3 measure.py hermes-install        # Install Hermes plugin into ~/.hermes/plugins/")
        print("  python3 measure.py copilot-doctor        # GitHub Copilot readiness + hook capability check")
        print("  python3 measure.py copilot-install       # Install Copilot hooks into ~/.copilot/hooks/")
        print("  python3 measure.py copilot-home          # Print resolved Copilot home (honors COPILOT_HOME)")
        print("  python3 measure.py copilot-summary       # Credits-led Copilot session summary")
        print("  python3 measure.py copilot-rollup        # Ingest Copilot sessions into trends DB")
        print("  python3 measure.py cursor-doctor         # Cursor adapter readiness check")
        print("  python3 measure.py cursor-install        # Install Cursor hooks (merged into ~/.cursor/hooks.json)")
        print("  python3 measure.py cursor-summary        # Token/quality Cursor session summary")
        print("  python3 measure.py cursor-coach          # Cursor coach data (sessions, quality, patterns; --json --days N --focus X)")
        print("  python3 measure.py cursor-rollup         # Ingest Cursor sessions into trends DB")
        print("  python3 measure.py cursor-audit          # Audit Cursor rules (.cursor/rules, AGENTS.md) + hooks.json health")
        print("  python3 measure.py antigravity-doctor    # Google Antigravity adapter readiness check")
        print("  python3 measure.py antigravity-install   # Install Antigravity hooks (consent-gated)")
        print("  python3 measure.py antigravity-uninstall # Remove Antigravity hooks + trend rows")
        print("  python3 measure.py antigravity-home      # Print resolved Antigravity home")
        print("  python3 measure.py antigravity-rollup    # Ingest Antigravity sessions into trends DB")
        print("  python3 measure.py antigravity-summary   # Antigravity session cost/quality summary")
        print("  python3 measure.py grok-doctor           # Grok Build readiness + hook probe")
        print("  python3 measure.py grok-install         # Install Grok hooks into $GROK_HOME/hooks/")
        print("  python3 measure.py grok-home            # Print resolved Grok home (honors GROK_HOME)")
        print("  python3 measure.py grok-summary         # Grok Build session token/cost summary")
        print("  python3 measure.py grok-rollup           # Ingest Grok sessions into trends DB")
        print("  python3 measure.py codex-compact-prompt # Render/install Codex compact prompt")
        print("  python3 measure.py drift                # Drift report: compare against last snapshot")
        print("  python3 measure.py drift --json          # Machine-readable drift output")
        print("  python3 measure.py report              # Full report")
        print("  python3 measure.py snapshot before      # Save pre-optimization snapshot")
        print("  python3 measure.py snapshot after       # Save post-optimization snapshot")
        print("  python3 measure.py compare              # Compare before vs after")
        print("  python3 measure.py dashboard                           # Standalone dashboard (Trends + Health)")
        print("  python3 measure.py dashboard --coord-path PATH         # Full dashboard (after audit)")
        print("  python3 measure.py dashboard --serve [--port 8080]     # Serve over HTTP (headless)")
        print("  python3 measure.py dashboard --serve --host 0.0.0.0   # Serve on all interfaces (remote access)")
        print("  python3 measure.py dashboard --quiet                   # Regenerate silently (for hooks)")
        print("  python3 measure.py health               # Check running session health")
        print("  python3 measure.py trends               # Usage trends (last 30 days)")
        print("  python3 measure.py trends --days 7      # Usage trends (last 7 days)")
        print("  python3 measure.py trends --json        # Machine-readable output")
        print("  python3 measure.py savings              # Savings report (last 30 days)")
        print("  python3 measure.py savings --days 7     # Savings report (last 7 days)")
        print("  python3 measure.py savings --json       # Machine-readable savings output")
        print("  python3 measure.py structure-proof      # Replay local sessions for structure-map proof")
        print("  python3 measure.py structure-proof --json")
        print("  python3 measure.py structure-proof --torture")
        print("  python3 measure.py coach                # Interactive coaching data")
        print("  python3 measure.py coach --json         # Coaching data as JSON")
        print("  python3 measure.py coach --focus skills  # Focus on skill optimization")
        print("  python3 measure.py coach --focus agentic # Focus on multi-agent patterns")
        print("  python3 measure.py quality              # Context quality of most recent session")
        print("  python3 measure.py quality current      # Context quality of current session")
        print("  python3 measure.py quality SESSION_ID   # Context quality of specific session")
        print("  python3 measure.py quality --json       # Machine-readable quality output")
        print("  python3 measure.py collect              # Collect sessions into SQLite DB")
        print("  python3 measure.py collect --quiet      # Silent mode (for hooks)")
        print("  python3 measure.py check-hook           # Check if SessionEnd hook is installed")
        print("  python3 measure.py setup-hook           # Install SessionEnd hook")
        print("  python3 measure.py setup-hook --dry-run # Show what would be installed")
        print("  python3 measure.py setup-hook --uninstall  # Remove Token Optimizer's SessionEnd hook")
        print("  python3 measure.py compact-capture          # Capture session state checkpoint")
        print("  python3 measure.py compact-capture --budget-seconds N  # Allow up to N s (default 14, max 60)")
        print("  python3 measure.py checkpoint-trigger --milestone pre-fanout  # Milestone checkpoint with guards")
        print("  python3 measure.py compact-restore          # Restore context from checkpoint")
        print("  python3 measure.py continue-last --topic TEXT  # Show Codex continuity hint for a topic")
        print("  TOKEN_OPTIMIZER_CHECKPOINT_TELEMETRY=1 python3 measure.py checkpoint-stats --days 7  # Local checkpoint telemetry summary")
        print("  python3 measure.py compact-instructions      # Generate project-specific Compact Instructions")
        print("  python3 measure.py git-context              # Suggest files based on git state")
        print("  python3 measure.py git-context --json       # Machine-readable git context")
        print("  python3 measure.py read-cache-clear         # Clear read cache (all sessions)")
        print("  python3 measure.py read-cache-stats --session ID  # Read cache stats for session")
        print("  python3 measure.py compact-instructions --json")
        print("  python3 measure.py compact-instructions --install     # Write directly to settings.json")
        print("  python3 measure.py compact-instructions --install --dry-run")
        print("  python3 measure.py list-checkpoints          # Show saved session checkpoints")
        print("  python3 measure.py setup-smart-compact              # Install Smart Compaction hooks")
        print("  python3 measure.py setup-smart-compact --dry-run    # Preview what would be installed")
        print("  python3 measure.py setup-smart-compact --status     # Check which hooks are installed")
        print("  python3 measure.py setup-smart-compact --uninstall  # Remove Smart Compaction hooks")
        print("  python3 measure.py quality-cache                    # Update quality cache (for status line)")
        print("  python3 measure.py status-bar --session ID --json   # Desktop status band data (status-bar --help)")
        print("  python3 measure.py quality-cache --warn             # Update cache + warn Claude if low")
        print("  python3 measure.py quality-cache --quiet            # Silent mode (for hooks)")
        print("  python3 measure.py setup-quality-bar                # Install quality bar (status line + hook)")
        print("  python3 measure.py setup-quality-bar --dry-run      # Preview what would be installed")
        print("  python3 measure.py setup-quality-bar --status       # Check installation status")
        print("  python3 measure.py setup-quality-bar --uninstall    # Remove quality bar")
        print("  python3 measure.py skill archive SKILL_NAME        # Archive a skill (move to backups)")
        print("  python3 measure.py skill restore SKILL_NAME        # Restore an archived skill")
        print("  python3 measure.py mcp disable SERVER_NAME         # Disable an MCP server")
        print("  python3 measure.py mcp enable SERVER_NAME          # Re-enable a disabled MCP server")
        print("  python3 measure.py jsonl-inspect [ID|PATH]         # Inspect JSONL session stats")
        print("  python3 measure.py jsonl-inspect --json             # Machine-readable inspect output")
        print("  python3 measure.py jsonl-trim                       # Dry-run: find trimmable tool results")
        print("  python3 measure.py jsonl-trim --apply               # Trim large tool results (backup + sidecar)")
        print("  python3 measure.py jsonl-trim --threshold 8000      # Custom char threshold (default 4000)")
        print("  python3 measure.py jsonl-dedup                      # Dry-run: find duplicate system reminders")
        print("  python3 measure.py jsonl-dedup --apply              # Remove duplicate system reminders")
        print("  python3 measure.py attention-score                   # Score CLAUDE.md against attention curve")
        print("  python3 measure.py attention-score FILE              # Score any markdown file")
        print("  python3 measure.py attention-score --json            # Machine-readable attention score")
        print("  python3 measure.py attention-optimize                # Dry-run: propose section reordering")
        print("  python3 measure.py attention-optimize FILE           # Optimize a specific file")
        print("  python3 measure.py attention-optimize --apply        # Apply reordering (backup + write)")
        print("  python3 measure.py archive-result                        # PostToolUse hook: archive large tool results")
        print("  python3 measure.py archive-result --quiet                 # Silent mode (suppress stderr)")
        print("  python3 measure.py session-cost garden --date yesterday   # What a session cost, in your words (read-only)")
        print("  python3 measure.py session-cost --id UUID --json          # One session by id, machine-readable")
        print("  python3 measure.py expand TOOL_USE_ID                     # Retrieve archived tool result")
        print("  python3 measure.py expand TOOL_USE_ID --session SID       # Retrieve from specific session")
        print("  python3 measure.py expand TOOL_USE_ID --lines A:B         # Bounded window: lines A-B (1-based)")
        print("  python3 measure.py expand TOOL_USE_ID --start N --chars M # Bounded window: M chars at offset N")
        print("  python3 measure.py expand TOOL_USE_ID --match TEXT        # Context windows around matches")
        print("  python3 measure.py expand --search QUERY                  # Search archived results")
        print("  python3 measure.py expand --list                          # List all archived results")
        print("  python3 measure.py expand --list --session SID            # List archived results for session")
        print("  python3 measure.py archive-cleanup                        # Clean archives older than 24h")
        print("  python3 measure.py archive-cleanup SESSION_ID             # Clean specific session archive")
        print("  python3 measure.py purge                       # Dry-run: list all Token Optimizer data")
        print("  python3 measure.py purge --confirm             # Delete all Token Optimizer data (compliance/uninstall)")
        print("  python3 measure.py purge --confirm --force     # Also stop daemon automatically")
        print("  python3 measure.py setup-daemon            # Install persistent dashboard server (macOS)")
        print("  python3 measure.py setup-daemon --dry-run  # Show what would be installed")
        print("  python3 measure.py setup-daemon --uninstall # Remove dashboard daemon")
        print("  python3 measure.py consent                  # Show enterprise consent status")
        print("  python3 measure.py consent --grant          # Grant consent manually")
        print("  python3 measure.py consent --reset          # Reset consent (will re-show data notice)")
        print("  python3 measure.py security-report          # Enterprise security self-assessment")
        print("  python3 measure.py security-report --json   # Machine-readable security report")
        print()
        print("  Global flags:")
        print("    --context-size N   Override context window size (e.g., --context-size 1000000)")
