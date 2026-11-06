"""Definition-only implementations; mutable state belongs to measure."""
from __future__ import annotations
from . import _compat


def _read_codex_config() -> dict:
    _measure_facade = _compat.facade()
    pass
    path = _measure_facade.RUNTIME_DIR / "config.toml"
    if _measure_facade.tomllib is None:
        return {}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if _measure_facade._codex_config_cache is not None and _measure_facade._codex_config_cache[0] == mtime:
        return _measure_facade._codex_config_cache[1]
    try:
        with path.open("rb") as handle:
            data = _measure_facade.tomllib.load(handle)
        result = data if isinstance(data, dict) else {}
    except (OSError, _measure_facade.tomllib.TOMLDecodeError):
        return {}
    _measure_facade._codex_config_cache = (mtime, result)
    return result


def _mcp_read_json(path, skipped=None):
    """Load a JSON config. Missing file -> None (silent).

    An unreadable or malformed file degrades to a SKIP (recorded in `skipped`)
    rather than crashing the whole measurement.
    """
    _measure_facade = _compat.facade()
    try:
        if not path.exists():
            return None
    except OSError:
        return None
    # open() on a FIFO blocks until a writer shows up, and this runs on the
    # SessionStart path. A directory, socket or device is not a config either.
    if not _measure_facade._is_regular_file(path):
        if skipped is not None:
            skipped.append({"path": str(path), "reason": "NotRegularFile"})
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _measure_facade.json.load(f)
    except (_measure_facade.json.JSONDecodeError, UnicodeDecodeError, ValueError,
            PermissionError, OSError) as exc:
        if skipped is not None:
            skipped.append({"path": str(path), "reason": type(exc).__name__})
        return None


def _mcp_servers_from_blob(blob):
    """Server names declared by one config blob.

    Handles the wrapped {"mcpServers": {...}} shape (~/.claude.json,
    settings.json, .mcp.json, most plugins) and the BARE {name: {...}} shape
    some plugins ship -- the official playwright plugin's .mcp.json is
    {"playwright": {"command": "npx", ...}} with no wrapper. The bare shape is
    only accepted when EVERY value looks like a server definition, so an
    unrelated JSON object is never mistaken for a server map.
    """
    _measure_facade = _compat.facade()
    if not isinstance(blob, dict):
        return []
    servers = blob.get("mcpServers")
    if servers is None:
        servers = blob.get("mcp_servers")
    if isinstance(servers, dict):
        return [n for n in servers if isinstance(n, str) and n]
    if servers is not None:
        return []  # present but not a dict -> malformed, declare nothing
    if not blob:
        return []
    for value in blob.values():
        if not isinstance(value, dict):
            return []
        if not any(k in value for k in _measure_facade._MCP_SERVER_DEF_KEYS):
            return []
    return [n for n in blob if isinstance(n, str) and n]


def _mcp_plugin_config_paths(claude_dir, skipped=None, cwd=None):
    """MCP config files shipped by installed (and enabled) plugins.

    Reads ~/.claude/plugins/installed_plugins.json for install paths, then
    returns each install's .mcp.json and .claude-plugin/plugin.json (the two
    places a plugin can declare mcpServers). Mirrors the enabledPlugins filter
    used by _scan_plugin_skills_and_commands: when settings.json declares an
    enabledPlugins map, plugins marked false are skipped; when it declares
    none, no filter is applied.

    enabledPlugins merges across the settings chain (user settings.json, then
    project .claude/settings.json, then project .claude/settings.local.json),
    closest scope winning per plugin key -- the same precedence Claude Code
    applies. Before #301 only the user settings.json map was read, so a plugin
    turned off for one project kept its servers in the report for that project.
    """
    _measure_facade = _compat.facade()
    paths = []
    registry = claude_dir / "plugins" / "installed_plugins.json"
    data = _measure_facade._mcp_read_json(registry, skipped)
    if not isinstance(data, dict):
        return paths
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        return paths

    enabled_plugins = None
    settings_chain = [claude_dir / "settings.json"]
    if cwd is not None:
        project_claude = _measure_facade.Path(cwd) / ".claude"
        settings_chain.append(project_claude / "settings.json")
        settings_chain.append(project_claude / "settings.local.json")
    for settings_path in settings_chain:
        settings = _measure_facade._mcp_read_json(settings_path, skipped)
        if isinstance(settings, dict):
            candidate = settings.get("enabledPlugins")
            if isinstance(candidate, dict):
                if enabled_plugins is None:
                    enabled_plugins = {}
                enabled_plugins.update(candidate)

    seen_install_paths = set()
    for plugin_key, installs in plugins.items():
        if not isinstance(installs, list):
            continue
        if enabled_plugins is not None and not enabled_plugins.get(plugin_key, False):
            continue
        for install in installs:
            if not isinstance(install, dict):
                continue
            raw_path = install.get("installPath") or ""
            if not raw_path:
                continue
            install_path = _measure_facade.Path(raw_path)
            try:
                if not install_path.is_absolute() or not install_path.exists():
                    continue
                resolved = install_path.resolve()
            except OSError:
                continue
            if resolved in seen_install_paths:
                continue
            seen_install_paths.add(resolved)
            for rel in (".mcp.json", _measure_facade.Path(".claude-plugin") / "plugin.json", "plugin.json"):
                candidate_path = install_path / rel
                try:
                    if candidate_path.exists():
                        paths.append(candidate_path)
                except OSError:
                    continue
    return paths


def get_mcp_config_paths(home=None, claude_dir=None, cwd=None):
    """Every LOCAL file that can declare MCP servers for Claude Code, in read order.

    ~/.claude.json comes first because it holds both the top-level mcpServers
    block and the per-project blocks where most servers actually live. Plugin
    configs come last.

    The Claude DESKTOP app config (claude_desktop_config.json) is deliberately
    NOT read: it configures a different product and its servers never enter a
    Claude Code context, so counting it inflated the wrong number.

    Account-level connectors have no local file and so cannot appear here --
    see count_mcp_tools_and_servers()["account_connectors"] (NOT MEASURED).
    """
    _measure_facade = _compat.facade()
    home = _measure_facade.HOME if home is None else _measure_facade.Path(home)
    claude_dir = _measure_facade.CLAUDE_DIR if claude_dir is None else _measure_facade.Path(claude_dir)
    cwd = _measure_facade.Path.cwd() if cwd is None else _measure_facade.Path(cwd)

    paths = [
        home / ".claude.json",             # top-level + projects.<path>.mcpServers
        claude_dir / "settings.json",      # user settings (may carry mcpServers)
        cwd / ".mcp.json",                 # project-scoped servers
        cwd / ".claude" / "settings.json",  # project settings
    ]
    paths.extend(_measure_facade._mcp_plugin_config_paths(claude_dir, cwd=cwd))

    ordered = []
    seen = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(path)
    return ordered


def count_mcp_tools_and_servers(home=None, claude_dir=None, cwd=None):
    """Count MCP servers and estimate tool overhead (deferred vs eager).

    Enumerates every local source (see get_mcp_config_paths) and dedups by
    server NAME, so a server declared under several projects.<path> blocks in
    ~/.claude.json is counted once. Account connectors are reported separately
    as NOT MEASURED -- never estimated.
    """
    _measure_facade = _compat.facade()
    home = _measure_facade.HOME if home is None else _measure_facade.Path(home)
    claude_dir = _measure_facade.CLAUDE_DIR if claude_dir is None else _measure_facade.Path(claude_dir)
    cwd = _measure_facade.Path.cwd() if cwd is None else _measure_facade.Path(cwd)

    seen_names = set()
    server_names = []
    server_scopes = {}   # name -> "user" | "project" | "plugin"
    server_sources = {}  # name -> "<file>:<key>" that first declared it
    mcpjson_names = set()    # names declared by the project's own .mcp.json
    other_declared = set()   # names declared by any non-.mcp.json source
    sources_read = []
    sources_skipped = []
    project_blocks_with_servers = 0

    def _add(name, scope, source):
        _measure_facade = _compat.facade()
        if name in seen_names:
            return
        seen_names.add(name)
        server_names.append(name)
        server_scopes[name] = scope
        server_sources[name] = source

    # --- Source 1: ~/.claude.json (top-level + every per-project block) ---
    claude_json = home / ".claude.json"
    blob = _measure_facade._mcp_read_json(claude_json, sources_skipped)
    claude_json_blob = blob  # reused by the per-project disable filter below
    if blob is not None:
        sources_read.append(str(claude_json))
        top_names = _measure_facade._mcp_servers_from_blob(blob)
        other_declared.update(top_names)
        for name in top_names:
            _add(name, "user", f"{claude_json}:mcpServers")
        projects = blob.get("projects") if isinstance(blob, dict) else None
        if isinstance(projects, dict):
            for project_path, project_cfg in projects.items():
                if not isinstance(project_cfg, dict):
                    continue
                servers = project_cfg.get("mcpServers")
                if not isinstance(servers, dict) or not servers:
                    continue
                project_blocks_with_servers += 1
                proj_names = [n for n in servers if isinstance(n, str) and n]
                for name in proj_names:
                    _add(name, "project",
                         f"{claude_json}:projects[{project_path}].mcpServers")

    # --- Source 2: settings.json + project-scoped config files ---
    file_scopes = [
        (claude_dir / "settings.json", "user"),
        (cwd / ".mcp.json", "project"),
        (cwd / ".claude" / "settings.json", "project"),
    ]
    project_mcp_json = cwd / ".mcp.json"
    for config_path, scope in file_scopes:
        blob = _measure_facade._mcp_read_json(config_path, sources_skipped)
        if blob is None:
            continue
        sources_read.append(str(config_path))
        names = _measure_facade._mcp_servers_from_blob(blob)
        if config_path == project_mcp_json:
            mcpjson_names.update(names)
        else:
            other_declared.update(names)
        for name in names:
            _add(name, scope, f"{config_path}:mcpServers")

    # --- Source 3: plugin-provided servers ---
    for config_path in _measure_facade._mcp_plugin_config_paths(claude_dir, sources_skipped, cwd=cwd):
        blob = _measure_facade._mcp_read_json(config_path, sources_skipped)
        if blob is None:
            continue
        names = _measure_facade._mcp_servers_from_blob(blob)
        if not names:
            continue  # e.g. a plugin.json with no mcpServers key
        sources_read.append(str(config_path))
        other_declared.update(names)
        for name in names:
            _add(name, "plugin", f"{config_path}:mcpServers")

    # --- Per-project disable filter (#301) ---
    # ~/.claude.json projects.<path> blocks can carry a disabledMcpServers
    # list. A server disabled for the project the report runs in is not loaded
    # in its sessions, so it must not be counted here. Matching walks from cwd
    # upward (nearest project block wins), the same ancestor walk
    # find_projects_dir uses; path keys are compared case- and
    # separator-insensitively so Windows spellings (C:\ vs c:/, Git Bash
    # /c/...) still match.
    disabled_for_project = []
    pending_project_approval = []
    best_cfg = None
    if isinstance(claude_json_blob, dict):
        projects = claude_json_blob.get("projects")
        if isinstance(projects, dict):
            def _norm_proj_path(text):
                _measure_facade = _compat.facade()
                t = str(text).replace("\\", "/").rstrip("/")
                # Git Bash spells the same path /c/Users/... -- fold a
                # single-letter first segment into a drive letter.
                m = _measure_facade.re.match(r"^/([A-Za-z])/(.*)$", t)
                if m:
                    t = m.group(1) + ":/" + m.group(2)
                # Case-fold only drive-lettered (Windows) spellings, so POSIX
                # projects that differ only by case stay distinct.
                if _measure_facade.sys.platform == "win32" or _measure_facade.re.match(r"^[A-Za-z]:/", t):
                    t = t.lower()
                return t

            try:
                cwd_parts = _norm_proj_path(cwd)
            except Exception:
                cwd_parts = ""
            best_key = None
            for project_path, project_cfg in projects.items():
                if not isinstance(project_cfg, dict):
                    continue
                norm_key = _norm_proj_path(project_path)
                if not norm_key or not cwd_parts:
                    continue
                if cwd_parts == norm_key or cwd_parts.startswith(norm_key + "/"):
                    if best_key is None or len(norm_key) > len(best_key):
                        best_key = norm_key
                        best_cfg = project_cfg
            if isinstance(best_cfg, dict):
                # Only the matched project's mcpServers count as another load
                # path for the .mcp.json gate: a name declared under an
                # unrelated project block is not loaded here, so a .mcp.json
                # server with that name is still gated.
                matched_servers = best_cfg.get("mcpServers")
                if isinstance(matched_servers, dict):
                    other_declared.update(
                        n for n in matched_servers if isinstance(n, str) and n)
                disabled = best_cfg.get("disabledMcpServers")
                if isinstance(disabled, list):
                    for name in disabled:
                        if isinstance(name, str) and name and name in seen_names:
                            seen_names.discard(name)
                            server_names.remove(name)
                            server_scopes.pop(name, None)
                            server_sources.pop(name, None)
                            disabled_for_project.append(name)

    # --- .mcp.json approval gate (settings-level keys) ---
    # A server declared ONLY in the project's .mcp.json connects only after
    # approval: interactive choices persist in ~/.claude.json
    # projects.<path>.{enabled,disabled}McpjsonServers, and the settings chain
    # (user settings.json, project settings.json, project settings.local.json)
    # carries enabledMcpjsonServers / disabledMcpjsonServers /
    # enableAllProjectMcpServers. A disabledMcpjsonServers entry in ANY
    # settings file rejects the server; enableAllProjectMcpServers approves
    # every .mcp.json server (closest scope wins for the boolean). An approved
    # server counts; an unapproved one is pending approval and contributes no
    # tools, so it is not counted. A name that also appears in another source
    # has a different load path and is not gated.
    mcpjson_only = mcpjson_names - other_declared
    if mcpjson_only:
        disabled_mcpjson = set()
        enabled_mcpjson = set()
        enable_all = None

        def _collect_mcpjson_keys(blob):
            _measure_facade = _compat.facade()
            nonlocal enable_all
            if not isinstance(blob, dict):
                return
            disabled = blob.get("disabledMcpjsonServers")
            if isinstance(disabled, list):
                disabled_mcpjson.update(n for n in disabled if isinstance(n, str) and n)
            enabled = blob.get("enabledMcpjsonServers")
            if isinstance(enabled, list):
                enabled_mcpjson.update(n for n in enabled if isinstance(n, str) and n)
            flag = blob.get("enableAllProjectMcpServers")
            if isinstance(flag, bool):
                enable_all = flag

        for settings_path in (
            claude_dir / "settings.json",
            cwd / ".claude" / "settings.json",
            cwd / ".claude" / "settings.local.json",
        ):
            _collect_mcpjson_keys(_measure_facade._mcp_read_json(settings_path, sources_skipped))
        # The per-project record in ~/.claude.json is the most specific scope.
        _collect_mcpjson_keys(best_cfg)
        # Enterprise managed-settings.json carries the same keys and outranks
        # every user/project scope, so it is read last.
        _collect_mcpjson_keys(_measure_facade._mcp_read_json(
            _measure_facade._subagent_cache_managed_settings_path(), sources_skipped))

        approve_all = enable_all is True
        for name in list(server_names):
            if name not in mcpjson_only:
                continue
            if name in disabled_mcpjson:
                seen_names.discard(name)
                server_names.remove(name)
                server_scopes.pop(name, None)
                server_sources.pop(name, None)
                disabled_for_project.append(name)
            elif not approve_all and name not in enabled_mcpjson:
                seen_names.discard(name)
                server_names.remove(name)
                server_scopes.pop(name, None)
                server_sources.pop(name, None)
                pending_project_approval.append(name)

    server_count = len(server_names)

    # Count tools using known-server table, fall back to average for unknown
    tool_count_estimate = 0
    for name in server_names:
        tool_count_estimate += _measure_facade._KNOWN_SERVER_TOOL_COUNTS.get(name, _measure_facade.AVG_TOOLS_PER_SERVER)

    # Detect deferred (lazy) vs eager loading
    # Modern Claude Code (2.0+) uses deferred loading by default.
    # Deferred: ~15 tokens/tool (just name in ToolSearch menu)
    # Eager: ~150 tokens/tool (full JSON schema in system prompt)
    deferred = True
    if _measure_facade.os.environ.get("CLAUDE_CODE_DISABLE_MCP_DEFERRED") == "1":
        deferred = False

    if deferred:
        tokens_per_tool = _measure_facade.TOKENS_PER_DEFERRED_TOOL
        loading_mode = "deferred"
    else:
        tokens_per_tool = _measure_facade.TOKENS_PER_EAGER_TOOL
        loading_mode = "eager"

    tokens = tool_count_estimate * tokens_per_tool

    return {
        "server_count": server_count,
        "server_names": server_names,
        "server_scopes": server_scopes,
        "server_sources": server_sources,
        "disabled_for_project": disabled_for_project,
        "pending_project_approval": pending_project_approval,
        "project_blocks_with_servers": project_blocks_with_servers,
        "sources_read": sources_read,
        "sources_skipped": sources_skipped,
        "account_connectors": {
            "count": None,
            "status": "NOT MEASURED",
            "note": _measure_facade._MCP_CONNECTORS_NOTE,
        },
        "server_instructions": {
            "status": "NOT MEASURED",
            "note": _measure_facade._MCP_SERVER_INSTRUCTIONS_NOTE,
        },
        "tool_count_estimate": tool_count_estimate,
        "tokens": tokens,
        "loading_mode": loading_mode,
        "note": (
            f"~{tokens_per_tool} tokens/tool ({loading_mode} loading), tool surface only; "
            "server instructions and account connectors NOT MEASURED; local config only"
        ),
    }


def _has_paths_frontmatter(filepath):
    """Check if a rules file has paths: frontmatter (path-scoped rule)."""
    _measure_facade = _compat.facade()
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(2048)  # Only need to check frontmatter
        # Strip UTF-8 BOM that Windows editors may insert
        content = content.lstrip('\ufeff')
        if content.startswith("---"):
            end = content.find("---", 3)
            if end > 0:
                frontmatter = content[3:end]
                return "paths:" in frontmatter
        return False
    except (FileNotFoundError, PermissionError, OSError):
        return False


def _detect_imports(claude_md_path):
    """Detect @import patterns in a CLAUDE.md file and estimate token cost."""
    _measure_facade = _compat.facade()
    imports = []
    try:
        with open(claude_md_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        # Match lines starting with @ followed by a path-like string
        pattern = _measure_facade.re.compile(r'^@(\S+\.(?:md|txt|yaml|yml|json))\s*$', _measure_facade.re.MULTILINE)
        project_root = claude_md_path.parent.resolve()
        for match in pattern.finditer(content):
            import_path = match.group(1)
            resolved = (project_root / import_path).resolve()
            # Security: ensure resolved path stays under project root
            try:
                resolved.relative_to(project_root)
            except ValueError:
                continue  # Skip path traversal attempts
            tokens = _measure_facade.estimate_tokens_from_file(resolved) if resolved.exists() else 0
            imports.append({
                "pattern": f"@{import_path}",
                "resolved_path": str(resolved),
                "exists": resolved.exists(),
                "tokens": tokens,
            })
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return imports


def _check_settings_env(settings_path):
    """Check settings.json for token-relevant environment variables."""
    _measure_facade = _compat.facade()
    result = {"found": {}, "settings_exists": settings_path.exists()}
    if not settings_path.exists():
        return result
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = _measure_facade.json.load(f)
        env = settings.get("env", {})
        for var in _measure_facade.TOKEN_RELEVANT_ENV_VARS:
            if var in env:
                result["found"][var] = env[var]
    except (_measure_facade.json.JSONDecodeError, PermissionError, OSError):
        pass
    return result


def _get_frontmatter_description_length(filepath):
    """Get the combined character length of description + when_to_use in YAML frontmatter.

    Claude Code truncates the combined text at 1,536 characters in the skill listing.
    """
    _measure_facade = _compat.facade()
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(8192)
        if not content.startswith("---"):
            return 0
        end = content.find("---", 3)
        if end <= 0:
            return 0
        frontmatter = content[3:end]
        lines = frontmatter.split("\n")
        total_len = 0
        for target_field in ("description:", "when_to_use:"):
            field_text = ""
            in_field = False
            for line in lines:
                if line.startswith(target_field):
                    value = line[len(target_field):].strip()
                    if value in ("|", ">", "|+", "|-", ">+", ">-"):
                        in_field = True
                        continue
                    if value.startswith('"') and value.endswith('"'):
                        field_text = value[1:-1]
                    elif value.startswith("'") and value.endswith("'"):
                        field_text = value[1:-1]
                    else:
                        field_text = value
                    break
                elif in_field:
                    if line and (line[0] == " " or line[0] == "\t"):
                        field_text += line.strip() + " "
                    else:
                        break
            total_len += len(field_text.strip())
        return total_len
    except (FileNotFoundError, PermissionError, OSError):
        return 0


def _scan_plugin_skills_and_commands():
    """Scan installed plugins for skills and commands not in ~/.claude/skills/ or ~/.claude/commands/."""
    _measure_facade = _compat.facade()
    registry = _measure_facade.CLAUDE_DIR / "plugins" / "installed_plugins.json"
    result = {
        "plugin_skill_count": 0, "plugin_skill_tokens": 0, "plugin_skill_names": [],
        "plugin_cmd_count": 0, "plugin_cmd_tokens": 0, "plugin_cmd_names": [],
        "plugins_found": [], "plugins_skipped_disabled": [],
    }
    if not registry.exists():
        return result
    try:
        with open(registry, "r", encoding="utf-8") as f:
            data = _measure_facade.json.load(f)
    except (_measure_facade.json.JSONDecodeError, PermissionError, OSError):
        return result

    plugins = data.get("plugins") or {}
    if not isinstance(plugins, dict):
        return result

    # Load enabledPlugins from settings.json to filter out disabled plugins
    enabled_plugins = None
    settings_path = _measure_facade.CLAUDE_DIR / "settings.json"
    if _measure_facade._is_regular_file(settings_path):
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                settings = _measure_facade.json.load(f)
            enabled_plugins = settings.get("enabledPlugins")
        except (_measure_facade.json.JSONDecodeError, PermissionError, OSError):
            pass

    seen_paths = set()
    # Track skill sources for duplicate detection
    skill_sources = {}  # "plugin:skill" -> list of install paths
    suspicious_paths = []  # paths inside node_modules or worktrees
    for plugin_key, installs in plugins.items():
        if not isinstance(installs, list):
            continue
        plugin_name = plugin_key.split("@")[0] or plugin_key

        # Skip plugins not enabled in settings.json
        if enabled_plugins is not None and not enabled_plugins.get(plugin_key, False):
            result["plugins_skipped_disabled"].append(plugin_name)
            continue

        for install in installs:
            raw_path = install.get("installPath") or ""
            if not raw_path:
                continue
            install_path = _measure_facade.Path(raw_path)
            if not install_path.is_absolute() or not install_path.exists():
                continue
            resolved = install_path.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            if plugin_name not in result["plugins_found"]:
                result["plugins_found"].append(plugin_name)

            # Flag suspicious install paths
            path_str = str(resolved)
            if "/node_modules/" in path_str:
                suspicious_paths.append({"path": path_str, "reason": "node_modules", "plugin": plugin_name})
            if "/.worktrees/" in path_str or "/worktrees/" in path_str.lower():
                suspicious_paths.append({"path": path_str, "reason": "worktree", "plugin": plugin_name})

            try:
                # Skills
                skills_dir = install_path / "skills"
                if skills_dir.exists():
                    for item in sorted(skills_dir.iterdir()):
                        skill_md = item / "SKILL.md"
                        if item.is_dir() and skill_md.exists():
                            result["plugin_skill_count"] += 1
                            skill_key = f"{plugin_name}:{item.name}"
                            result["plugin_skill_names"].append(skill_key)
                            result["plugin_skill_tokens"] += _measure_facade.estimate_tokens_from_frontmatter(skill_md)
                            skill_sources.setdefault(skill_key, []).append(path_str)

                # Commands
                cmds_dir = install_path / "commands"
                if cmds_dir.exists():
                    for f in sorted(cmds_dir.glob("*.md")):
                        result["plugin_cmd_count"] += 1
                        result["plugin_cmd_names"].append(f"{plugin_name}:{f.stem}")
                        result["plugin_cmd_tokens"] += _measure_facade.estimate_tokens_from_frontmatter(f)
                    for subdir in sorted(cmds_dir.iterdir()):
                        if subdir.is_dir():
                            for f in sorted(subdir.glob("*.md")):
                                result["plugin_cmd_count"] += 1
                                result["plugin_cmd_names"].append(f"{plugin_name}:{subdir.name}/{f.stem}")
                                result["plugin_cmd_tokens"] += _measure_facade.estimate_tokens_from_frontmatter(f)
            except OSError:
                continue

    # Identify duplicates: same skill loaded from multiple install paths
    duplicates = {k: v for k, v in skill_sources.items() if len(v) > 1}
    result["duplicate_skills"] = duplicates
    result["suspicious_paths"] = suspicious_paths
    return result


def measure_components():
    """Measure all controllable token overhead components."""
    _measure_facade = _compat.facade()
    runtime = _measure_facade.detect_runtime()
    if runtime == "codex":
        return _measure_facade._measure_codex_components()
    if runtime == "hermes":
        return _measure_facade._measure_hermes_components()
    if runtime == "cursor":
        return _measure_facade._measure_cursor_components()
    if runtime == "antigravity":
        return _measure_facade._measure_antigravity_components()

    components = {}
    seen_real_paths = set()

    # CLAUDE.md files (with symlink dedup)
    for name, path in [
        ("claude_md_global", _measure_facade.CLAUDE_DIR / "CLAUDE.md"),
        ("claude_md_home", _measure_facade.HOME / "CLAUDE.md"),
    ]:
        real = _measure_facade.resolve_real_path(path)
        if real in seen_real_paths:
            components[name] = {"path": str(path), "exists": False, "tokens": 0, "lines": 0, "note": "duplicate (symlink)"}
            continue
        if path.exists():
            seen_real_paths.add(real)
        raw_tokens = _measure_facade.estimate_tokens_from_file(path)
        components[name] = {
            "path": str(path),
            "exists": path.exists(),
            "tokens": (raw_tokens + _measure_facade.CLAUDE_MD_INJECTION_OVERHEAD) if (path.exists() and raw_tokens > 0) else raw_tokens,
            "lines": _measure_facade.count_lines(path),
        }

    # Find project CLAUDE.md files in cwd and parents
    # Claude Code loads from both <project>/CLAUDE.md and <project>/.claude/CLAUDE.md
    cwd = _measure_facade.Path.cwd()
    for parent in [cwd] + list(cwd.parents)[:3]:
        if parent == _measure_facade.HOME:
            continue  # Already checked ~/CLAUDE.md
        candidates = [
            (f"claude_md_project_{parent.name}", parent / "CLAUDE.md"),
            (f"claude_md_project_{parent.name}_dotclaude", parent / ".claude" / "CLAUDE.md"),
        ]
        for comp_key, claude_md in candidates:
            if claude_md.exists():
                real = _measure_facade.resolve_real_path(claude_md)
                if real not in seen_real_paths:
                    seen_real_paths.add(real)
                    raw_tokens = _measure_facade.estimate_tokens_from_file(claude_md)
                    components[comp_key] = {
                        "path": str(claude_md),
                        "exists": True,
                        "tokens": (raw_tokens + _measure_facade.CLAUDE_MD_INJECTION_OVERHEAD) if raw_tokens > 0 else raw_tokens,
                        "lines": _measure_facade.count_lines(claude_md),
                    }

    # MEMORY.md resolution.
    #
    # Claude Code's auto-memory loads from the HOME project
    # (-Users-<you>/memory/MEMORY.md) on every session regardless of
    # cwd. Before v5.3.10 this helper only checked the cwd-matched
    # project dir, so running /token-optimizer from a subdirectory
    # (e.g. a nested project checkout) reported
    # "Not configured" for users whose memory actually lived in HOME.
    #
    # Resolution order now:
    #   1. HOME project (~/.claude/projects/-Users-<you>/memory/MEMORY.md)
    #      -- this is what Claude Code actually injects.
    #   2. cwd-matched project dir -- for users who scope memory per
    #      project instead of (or in addition to) HOME.
    #   Anything beyond those two is a file Claude Code does not inject in
    #   this session; reporting it charged another project's memory here (#301).
    memory_tokens = 0
    memory_lines = 0
    memory_path_str = ""
    memory_exists = False

    projects_base = _measure_facade.CLAUDE_DIR / "projects"
    # Claude Code names project dirs by replacing EVERY non-alphanumeric
    # character with '-' (_encode_project_dir_name). The old "/"-and-"_"-only
    # replacement left a Windows HOME (C:\Users\<name>) with its drive colon
    # and backslashes intact, so the HOME project never matched and resolution
    # fell through to the scan-all fallback below (#301).
    home_project_name = _measure_facade._encode_project_dir_name(str(_measure_facade.HOME))
    candidate_paths = []
    home_candidate = projects_base / home_project_name / "memory" / "MEMORY.md"
    candidate_paths.append(home_candidate)

    # Only a CONFIDENT cwd match may contribute a second candidate: the
    # mtime guess inside find_projects_dir names some other project, and
    # charging its memory here is the same cross-project leak as the removed
    # scan-all fallback (#301).
    projects_dir = _measure_facade.find_projects_dir(allow_mtime_guess=False)
    if projects_dir and projects_dir.name != home_project_name:
        candidate_paths.append(projects_dir / "memory" / "MEMORY.md")

    for candidate in candidate_paths:
        if candidate.exists():
            memory_path_str = str(candidate)
            memory_exists = True
            memory_tokens = _measure_facade.estimate_tokens_from_file(candidate)
            memory_lines = _measure_facade.count_lines(candidate)
            break

    # No scan-all fallback: when neither the HOME project nor the cwd-matched
    # project has a memory/MEMORY.md, Claude Code injects nothing, so the
    # report must say 0. The pre-#301 fallback charged the newest MEMORY.md
    # found in ANY project -- a file Claude Code never loads in this session --
    # which is how an unrelated project's memory showed up in the report.
    components["memory_md"] = {
        "path": memory_path_str,
        "exists": memory_exists,
        "tokens": memory_tokens,
        "lines": memory_lines,
    }

    # Skills (read actual frontmatter size + check description quality in single pass)
    skills_dir = _measure_facade.CLAUDE_DIR / "skills"
    skill_count = 0
    skill_tokens = 0
    skill_names = []
    verbose_skills = []
    skills_detail = {}
    skill_name_to_dir = {}   # SKILL.md name -> directory name (for usage matching)
    skill_dir_to_name = {}   # directory name -> SKILL.md name
    # Claude Code injects PROJECT-scoped skills (<cwd>/.claude/skills) into
    # the skill listing for every session in that cwd, exactly like the global
    # ones. Scanning only CLAUDE_DIR under-reported any project that uses them --
    # measured at 28 skills / ~1,426 frontmatter tokens on one real project, which
    # made the reported total low by 5-7%. Worse, the miss landed in the
    # "estimated vs real" calibration line, so the number whose whole job is to
    # flag our inaccuracy was quietly absorbing a KNOWN inaccuracy.
    #
    # Global first so a project skill sharing a name cannot displace the global
    # entry in the name maps, and so ordering stays stable for the dashboard.
    for _sdir in _measure_facade._skill_scan_dirs():
        if not _sdir.exists():
            continue
        for item in sorted(_sdir.iterdir()):
            skill_md = item / "SKILL.md"
            if item.is_dir() and skill_md.exists():
                skill_count += 1
                skill_names.append(item.name)
                fm_tokens = _measure_facade.estimate_tokens_from_frontmatter(skill_md)
                skill_tokens += fm_tokens
                desc_len = _measure_facade._get_frontmatter_description_length(skill_md)
                if desc_len > _measure_facade._SKILL_DESC_TRUNCATION_LIMIT:
                    verbose_skills.append({
                        "name": item.name,
                        "description_chars": desc_len,
                        "truncated": True,
                    })
                elif desc_len > 200:
                    verbose_skills.append({
                        "name": item.name,
                        "description_chars": desc_len,
                        "truncated": False,
                    })
                # Collect per-skill detail for dashboard
                detail = {
                    "name": item.name,
                    "frontmatter_tokens": fm_tokens,
                    "description_chars": desc_len,
                }
                # Gather file structure (top-level only)
                try:
                    children = sorted(p.name for p in item.iterdir() if not p.name.startswith("."))
                    detail["files"] = children
                except OSError:
                    detail["files"] = []
                # Read name + description from frontmatter or first paragraph
                try:
                    with open(skill_md, "r", encoding="utf-8") as f:
                        content = f.read(4000)  # first 4K is enough
                    if content.startswith("---"):
                        end = content.find("---", 3)
                        if end > 0:
                            fm_block = content[3:end]
                            for line in fm_block.split("\n"):
                                stripped = line.strip()
                                if stripped.startswith("name:"):
                                    fm_name = stripped[5:].strip().strip('"').strip("'")
                                    if fm_name and fm_name != item.name:
                                        detail["skill_name"] = fm_name
                                        skill_name_to_dir[fm_name] = item.name
                                        skill_dir_to_name[item.name] = fm_name
                                elif stripped.startswith("description:"):
                                    desc_text = stripped[12:].strip().strip("|").strip(">").strip()
                                    if not desc_text:
                                        # Multi-line description
                                        desc_lines = []
                                        for dl in fm_block.split("\n")[fm_block.split("\n").index(line)+1:]:
                                            if dl and dl[0] in (' ', '\t'):
                                                desc_lines.append(dl.strip())
                                            else:
                                                break
                                        desc_text = " ".join(desc_lines)
                                    detail["description"] = desc_text[:200]
                    # Fallback: no YAML frontmatter, grab first non-heading paragraph
                    if "description" not in detail:
                        for line in content.split("\n"):
                            stripped = line.strip()
                            if stripped and not stripped.startswith("#") and not stripped.startswith("---"):
                                detail["description"] = stripped[:200]
                                break
                except (OSError, UnicodeDecodeError):
                    pass
                skills_detail[item.name] = detail
    components["skills"] = {
        "count": skill_count,
        "tokens": skill_tokens,
        "names": skill_names,
        "name_to_dir": skill_name_to_dir,
        "dir_to_name": skill_dir_to_name,
    }
    components["skills_detail"] = skills_detail

    # Agents (wire up _agent_scan_dirs, which existed but
    # had zero production callers. Agents are .md files in ~/.claude/agents/
    # and <cwd>/.claude/agents/, injected into the system prompt like skills.
    # Without this count the "estimated vs real" calibration line silently
    # absorbs the agent frontmatter tokens, understating the reported total
    # by the same 5-7% the skills half of the agent-scan fix measured.)
    agent_count = 0
    agent_tokens = 0
    agent_names = []
    for _adir in _measure_facade._agent_scan_dirs():
        if not _adir.exists():
            continue
        try:
            for item in sorted(_adir.iterdir()):
                if item.is_file() and item.suffix == ".md":
                    agent_count += 1
                    agent_names.append(item.stem)
                    agent_tokens += _measure_facade.estimate_tokens_from_frontmatter(item)
        except OSError:
            pass
    components["agents"] = {
        "count": agent_count,
        "tokens": agent_tokens,
        "names": agent_names,
    }

    # Commands (read actual file sizes for frontmatter estimate)
    commands_dir = _measure_facade.CLAUDE_DIR / "commands"
    cmd_count = 0
    cmd_tokens = 0
    cmd_names = []
    if commands_dir.exists():
        for f in sorted(commands_dir.glob("*.md")):
            cmd_count += 1
            cmd_names.append(f.stem)
            cmd_tokens += _measure_facade.estimate_tokens_from_frontmatter(f)
        for subdir in sorted(commands_dir.iterdir()):
            if subdir.is_dir():
                for f in sorted(subdir.glob("*.md")):
                    cmd_count += 1
                    cmd_names.append(f"{subdir.name}/{f.stem}")
                    cmd_tokens += _measure_facade.estimate_tokens_from_frontmatter(f)
    components["commands"] = {
        "count": cmd_count,
        "tokens": cmd_tokens,
        "names": cmd_names,
    }

    # Plugin-bundled skills and commands
    plugin_data = _measure_facade._scan_plugin_skills_and_commands()
    components["plugin_skills"] = {
        "count": plugin_data["plugin_skill_count"],
        "tokens": plugin_data["plugin_skill_tokens"],
        "names": plugin_data["plugin_skill_names"],
        "plugins": plugin_data["plugins_found"],
        "disabled_plugins": plugin_data["plugins_skipped_disabled"],
        "duplicate_skills": plugin_data.get("duplicate_skills", {}),
        "suspicious_paths": plugin_data.get("suspicious_paths", []),
    }
    components["plugin_commands"] = {
        "count": plugin_data["plugin_cmd_count"],
        "tokens": plugin_data["plugin_cmd_tokens"],
        "names": plugin_data["plugin_cmd_names"],
    }

    # MCP servers and deferred tools
    mcp = _measure_facade.count_mcp_tools_and_servers()
    components["mcp_tools"] = {
        "server_count": mcp["server_count"],
        "server_names": mcp["server_names"],
        "server_scopes": mcp["server_scopes"],
        "server_sources": mcp["server_sources"],
        "disabled_for_project": mcp["disabled_for_project"],
        "pending_project_approval": mcp["pending_project_approval"],
        "sources_read": mcp["sources_read"],
        "sources_skipped": mcp["sources_skipped"],
        "account_connectors": mcp["account_connectors"],
        "server_instructions": mcp["server_instructions"],
        "tool_count_estimate": mcp["tool_count_estimate"],
        "tokens": mcp["tokens"],
        "loading_mode": mcp["loading_mode"],
        "note": mcp["note"],
    }

    # File exclusion rules (permissions.deny with Read() patterns)
    def _extract_deny_read_rules(settings_obj):
        """Extract Read() deny patterns from a settings object."""
        _measure_facade = _compat.facade()
        if not settings_obj or not isinstance(settings_obj, dict):
            return []
        perms = settings_obj.get("permissions", {})
        if not isinstance(perms, dict):
            return []
        deny = perms.get("deny", [])
        if not isinstance(deny, list):
            return []
        return [r for r in deny if isinstance(r, str) and r.startswith("Read(")]

    # Read settings.json once (used for hooks, env vars, MCP, file exclusion)
    settings_path = _measure_facade.CLAUDE_DIR / "settings.json"
    _cached_settings = None
    if _measure_facade._is_regular_file(settings_path):
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                _cached_settings = _measure_facade.json.load(f)
        except (_measure_facade.json.JSONDecodeError, PermissionError, OSError):
            pass

    # Check permissions.deny in global and project-level settings
    global_deny_rules = _extract_deny_read_rules(_cached_settings)
    project_settings_path = cwd / ".claude" / "settings.json"
    _project_settings = None
    if _measure_facade._is_regular_file(project_settings_path):
        try:
            with open(project_settings_path, "r", encoding="utf-8") as f:
                _project_settings = _measure_facade.json.load(f)
        except (_measure_facade.json.JSONDecodeError, PermissionError, OSError):
            pass
    project_deny_rules = _extract_deny_read_rules(_project_settings)
    components["file_exclusion"] = {
        "global_deny_rules": global_deny_rules,
        "project_deny_rules": project_deny_rules,
        "has_rules": bool(global_deny_rules or project_deny_rules),
    }

    # Hooks — analyze both structure and content for per-turn cost patterns
    hooks_configured = False
    hook_names_set = set()
    hook_warnings = []
    hook_est_per_turn_tokens = 0
    if _cached_settings:
        hooks = _cached_settings.get("hooks", {})
        if hooks:
            hooks_configured = True
            hook_names_set.update(hooks.keys())
            for event_name, hook_list in hooks.items():
                if not isinstance(hook_list, list):
                    continue
                for entry in hook_list:
                    inner_hooks = entry.get("hooks", []) if isinstance(entry, dict) else []
                    for h in inner_hooks:
                        if not isinstance(h, dict):
                            continue
                        cmd = h.get("command", "")
                        if not cmd:
                            continue
                        if '"decision"' in cmd and '"block"' in cmd:
                            hook_est_per_turn_tokens += 80
                            hook_warnings.append(
                                f"{event_name} hook re-invokes model via decision:block (~80+ tok/turn)"
                            )
                        if any(kw in cmd for kw in ("curl ", "anthropic", "openai", "gemini")):
                            hook_warnings.append(
                                f"{event_name} hook calls external API ({cmd[:60]})"
                            )
    # Also detect plugin-installed hooks (hooks/hooks.json in plugin cache)
    if _measure_facade._is_plugin_installed():
        hooks_configured = True
        plugin_cache = _measure_facade.CLAUDE_DIR / "plugins" / "cache"
        if plugin_cache.exists():
            import glob as _glob_mod
            for hf in _glob_mod.glob(str(plugin_cache / "*" / "token-optimizer" / "*" / "hooks" / "hooks.json")):
                try:
                    with open(hf, "r", encoding="utf-8") as f:
                        ph = _measure_facade.json.load(f)
                    for event_name in ph.get("hooks", {}):
                        hook_names_set.add(event_name)
                except (_measure_facade.json.JSONDecodeError, PermissionError, OSError):
                    continue
    components["hooks"] = {
        "configured": hooks_configured,
        "names": sorted(hook_names_set),
        "warnings": hook_warnings,
        "est_per_turn_tokens": hook_est_per_turn_tokens,
    }

    # .claude/rules/ directory
    rules_dirs = [
        ("global", _measure_facade.CLAUDE_DIR / "rules"),
        ("project", cwd / ".claude" / "rules"),
    ]
    rules_count = 0
    rules_tokens = 0
    rules_always_loaded_tokens = 0
    rules_path_scoped_tokens = 0
    rules_files = []
    rules_always_loaded = 0
    for scope, rules_dir in rules_dirs:
        if rules_dir.exists() and rules_dir.is_dir():
            # rglob (not iterdir) so nested rule dirs like .claude/rules/staging/
            # are counted — Claude Code loads them into context, so iterdir
            # undercounted the rules component.
            for f in sorted(rules_dir.rglob("*")):
                if f.is_file() and f.suffix == ".md":
                    rules_count += 1
                    tokens = _measure_facade.estimate_tokens_from_file(f)
                    rules_tokens += tokens
                    has_paths = _measure_facade._has_paths_frontmatter(f)
                    if has_paths:
                        rules_path_scoped_tokens += tokens
                    else:
                        rules_always_loaded_tokens += tokens
                        rules_always_loaded += 1
                    rules_files.append({
                        "name": str(f.relative_to(rules_dir)),
                        "tokens": tokens,
                        "path_scoped": has_paths,
                        "scope": scope,
                    })
    components["rules"] = {
        "count": rules_count,
        "tokens": rules_tokens,
        "always_loaded_tokens": rules_always_loaded_tokens,
        "path_scoped_tokens": rules_path_scoped_tokens,
        "files": rules_files,
        "always_loaded": rules_always_loaded,
    }

    # @imports in CLAUDE.md
    imports_tokens = 0
    imports_found = []
    for key in components:
        if key.startswith("claude_md") and components[key].get("exists"):
            found = _measure_facade._detect_imports(_measure_facade.Path(components[key]["path"]))
            for imp in found:
                imports_tokens += imp["tokens"]
            imports_found.extend(found)
    components["imports"] = {
        "count": len(imports_found),
        "tokens": imports_tokens,
        "files": imports_found,
    }

    # CLAUDE.local.md
    claude_local = cwd / "CLAUDE.local.md"
    components["claude_local_md"] = {
        "path": str(claude_local),
        "exists": claude_local.exists(),
        "tokens": _measure_facade.estimate_tokens_from_file(claude_local) if claude_local.exists() else 0,
        "lines": _measure_facade.count_lines(claude_local) if claude_local.exists() else 0,
    }

    # settings.json env vars (token-relevant). Resolve across project local/shared
    # and global local/shared settings, not just global ~/.claude/settings.json, so
    # a flag a user marked at the project level is not missed.
    found_vars = {}
    for var in _measure_facade.TOKEN_RELEVANT_ENV_VARS:
        val = _measure_facade._settings_env_value(var)
        if val is not None:
            found_vars[var] = val
    components["settings_env"] = {
        "found": found_vars,
        "settings_exists": bool(_cached_settings) or settings_path.exists(),
    }

    # settings.local.json existence
    settings_local = _measure_facade.CLAUDE_DIR / "settings.local.json"
    project_settings_local = cwd / ".claude" / "settings.local.json"
    components["settings_local"] = {
        "global_exists": settings_local.exists(),
        "project_exists": project_settings_local.exists(),
        "exists": settings_local.exists() or project_settings_local.exists(),
        # Resolve across all settings files (project overrides global) so a
        # project-level includeGitInstructions / model is honoured.
        "includeGitInstructions": _measure_facade._resolve_settings_value("includeGitInstructions", True),
        "effortLevel": _measure_facade._resolve_settings_value("effortLevel", None),
        "defaultModel": _measure_facade._resolve_settings_value("model", None),
    }

    # compactInstructions from settings.json
    compact_instructions = ""
    if _cached_settings:
        raw_ci = _cached_settings.get("compactInstructions")
        compact_instructions = raw_ci if isinstance(raw_ci, str) else ""
    components["compact_instructions"] = {
        "exists": bool(compact_instructions),
        "tokens": int(len(compact_instructions) / _measure_facade.CHARS_PER_TOKEN) if compact_instructions else 0,
        "note": "Injected at compaction time, not startup. Included for completeness.",
    }

    # Skill frontmatter quality (collected during skills scan above)
    components["skill_frontmatter_quality"] = {
        "verbose_skills": verbose_skills,
    }

    # Fixed overhead
    components["core_system"] = {
        "tokens": 12900,
        "note": "System prompt (~7,400) + built-in tools (~5,500). Fixed. Measured against v2.1.94+.",
    }

    return components


def _measure_codex_components():
    """Measure Codex-relevant startup/config components without reading Claude config."""
    _measure_facade = _compat.facade()
    components = {}
    seen_real_paths = set()
    cwd = _measure_facade.Path.cwd()
    cfg = _measure_facade._read_codex_config()

    project_doc_max_bytes = cfg.get("project_doc_max_bytes", 32 * 1024)
    try:
        project_doc_max_bytes = int(project_doc_max_bytes)
    except (TypeError, ValueError):
        project_doc_max_bytes = 32 * 1024
    if project_doc_max_bytes <= 0:
        project_doc_max_bytes = 32 * 1024
    project_doc_bytes = 0

    fallback_names = cfg.get("project_doc_fallback_filenames", [])
    if not isinstance(fallback_names, list):
        fallback_names = []
    fallback_names = [name for name in fallback_names if isinstance(name, str) and name.strip()]

    def _add_agents_file(key: str, path: Path, *, project_scoped: bool) -> None:
        _measure_facade = _compat.facade()
        nonlocal project_doc_bytes
        real = _measure_facade.resolve_real_path(path)
        if real in seen_real_paths:
            return
        seen_real_paths.add(real)
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        included_bytes = size
        truncated = False
        if project_scoped:
            remaining = max(0, project_doc_max_bytes - project_doc_bytes)
            included_bytes = min(size, remaining)
            truncated = size > remaining
            project_doc_bytes += included_bytes
        components[key] = {
            "path": str(path),
            "exists": True,
            "tokens": int(included_bytes / _measure_facade.CHARS_PER_TOKEN),
            "raw_tokens": _measure_facade.estimate_tokens_from_file(path),
            "lines": _measure_facade.count_lines(path),
            "bytes": size,
            "included_bytes": included_bytes,
            "truncated": truncated,
            "project_doc_max_bytes": project_doc_max_bytes if project_scoped else None,
        }

    for global_name in ("AGENTS.override.md", "AGENTS.md"):
        agents_md = _measure_facade.runtime_home() / global_name
        if agents_md.exists() and agents_md.read_text(encoding="utf-8", errors="replace").strip():
            _add_agents_file(f"agents_md_global_{global_name.replace('.', '_')}", agents_md, project_scoped=False)
            break

    for parent in [cwd] + list(cwd.parents)[:3]:
        for candidate_name in ("AGENTS.override.md", "AGENTS.md", *fallback_names):
            agents_md = parent / candidate_name
            if agents_md.exists() and agents_md.read_text(encoding="utf-8", errors="replace").strip():
                safe_parent = parent.name or "root"
                safe_name = candidate_name.replace(".", "_").replace("-", "_")
                _add_agents_file(f"agents_md_project_{safe_parent}_{safe_name}", agents_md, project_scoped=True)
                break

    codex_config_tokens = 0
    codex_config_files = []
    for config_path in (_measure_facade.runtime_home() / "config.toml", cwd / ".codex" / "config.toml"):
        if config_path.exists():
            tokens = _measure_facade.estimate_tokens_from_file(config_path)
            codex_config_tokens += tokens
            codex_config_files.append({"path": str(config_path), "tokens": tokens})

    hook_paths = (_measure_facade.runtime_home() / "hooks.json", cwd / ".codex" / "hooks.json")
    hook_names_set = set()
    hook_path_labels = []
    hooks_configured = False
    for hook_path in hook_paths:
        if not hook_path.exists():
            continue
        try:
            data = _measure_facade.json.loads(hook_path.read_text(encoding="utf-8"))
            hooks = data.get("hooks", {}) if isinstance(data, dict) else {}
            if isinstance(hooks, dict):
                hooks_configured = hooks_configured or bool(hooks)
                hook_names_set.update(hooks)
                hook_path_labels.append(str(hook_path))
        except (_measure_facade.json.JSONDecodeError, OSError):
            hooks_configured = True
            hook_path_labels.append(str(hook_path))

    components["claude_md_global"] = {"path": str(_measure_facade.CLAUDE_DIR / "CLAUDE.md"), "exists": False, "tokens": 0, "lines": 0}
    components["claude_md_home"] = {"path": str(_measure_facade.HOME / "CLAUDE.md"), "exists": False, "tokens": 0, "lines": 0}
    # Machine-wide audit view by design (not a current-thread claim): the
    # Codex install's total memory overhead across all threads.
    state_memory = _measure_facade.codex_state.memory_overhead()
    components["memory_md"] = {
        "path": str(_measure_facade.runtime_home() / "state_*.sqlite"),
        "exists": bool(state_memory.get("available")),
        "tokens": int(state_memory.get("total_memory_tokens", 0) or 0),
        "lines": 0,
        "files": state_memory.get("by_thread", []),
        "scope": "all_threads",
        "thread_count": int(state_memory.get("thread_count", 0) or 0),
        "truncated": bool(state_memory.get("truncated")),
        "note": (
            "Codex memory overhead is measured from state_*.sqlite stage1_outputs; "
            "legacy ~/.codex/memories files are not counted as automatic prompt overhead."
        ),
    }

    skill_inventory = _measure_facade._collect_codex_skill_inventory(cfg, project=cwd.resolve(strict=False))
    user_skills = [item for item in skill_inventory["active"] if item.get("source") != "plugin"]
    plugin_skills = [item for item in skill_inventory["active"] if item.get("source") == "plugin"]
    verbose_skills = []
    for item in skill_inventory["active"]:
        desc_len = len(item.get("description", ""))
        if desc_len > _measure_facade._SKILL_DESC_TRUNCATION_LIMIT:
            verbose_skills.append({
                "name": item["name"],
                "description_chars": desc_len,
                "tokens": item.get("tokens", 0),
                "path": item.get("path", ""),
                "truncated": True,
            })
        elif desc_len > 120:
            verbose_skills.append({
                "name": item["name"],
                "description_chars": desc_len,
                "tokens": item.get("tokens", 0),
                "path": item.get("path", ""),
                "truncated": False,
            })
    components["skills"] = {
        "count": len(user_skills),
        "tokens": sum(int(item.get("tokens", 0)) for item in user_skills),
        "names": [item["name"] for item in user_skills],
        "name_to_dir": {item["name"]: str(_measure_facade.Path(item["path"]).parent) for item in user_skills},
        "dir_to_name": {str(_measure_facade.Path(item["path"]).parent): item["name"] for item in user_skills},
    }
    components["skills_detail"] = {
        item["name"]: {
            "name": item["name"],
            "skill_name": item["name"],
            "description": item.get("description", ""),
            "frontmatter_tokens": item.get("tokens", 0),
            "description_chars": len(item.get("description", "")),
            "source": item.get("source", ""),
            "path": item.get("path", ""),
        }
        for item in skill_inventory["active"]
    }
    components["commands"] = {"count": 0, "tokens": 0, "names": []}
    components["plugin_skills"] = {
        "count": len(plugin_skills),
        "tokens": sum(int(item.get("tokens", 0)) for item in plugin_skills),
        "names": [item["name"] for item in plugin_skills],
        "per_plugin": dict(_measure_facade.Counter(item.get("plugin") or "other" for item in plugin_skills)),
        "plugins": _measure_facade._collect_codex_plugin_inventory(cfg),
        "disabled_plugins": [p["name"] for p in _measure_facade._collect_codex_plugin_inventory(cfg) if not p.get("enabled", True)],
        "duplicate_skills": {},
        "suspicious_paths": [],
    }
    components["plugin_commands"] = {"count": 0, "tokens": 0, "names": []}
    codex_mcp = _measure_facade._collect_codex_mcp_inventory(cfg)
    components["mcp_tools"] = {
        "server_count": len(codex_mcp),
        "server_names": [item["name"] for item in codex_mcp],
        "tool_count_estimate": len(codex_mcp),
        "tokens": sum(int(item.get("tokens", 0)) for item in codex_mcp),
        "note": "Codex MCP servers expand into tools at runtime; exact tool schemas are not exposed in config.toml.",
    }
    deny_read = (((cfg.get("permissions") or {}).get("filesystem") or {}).get("deny_read") or [])
    if not isinstance(deny_read, list):
        deny_read = []
    components["file_exclusion"] = {"global_deny_rules": deny_read, "project_deny_rules": [], "has_rules": bool(deny_read)}
    components["hooks"] = {
        "configured": hooks_configured,
        "names": sorted(hook_names_set),
        "warnings": [],
        "est_per_turn_tokens": 0,
        "path": ", ".join(hook_path_labels),
    }
    components["rules"] = {"count": 0, "tokens": 0, "always_loaded_tokens": 0, "path_scoped_tokens": 0, "files": [], "always_loaded": 0}
    components["imports"] = {"count": 0, "tokens": 0, "files": []}
    components["claude_local_md"] = {"path": "", "exists": False, "tokens": 0, "lines": 0}
    components["settings_env"] = {"found": {}, "settings_exists": (_measure_facade.runtime_home() / "config.toml").exists()}
    components["settings_local"] = {"global_exists": False, "project_exists": False, "exists": False, "includeGitInstructions": True, "effortLevel": None, "defaultModel": None}
    components["compact_instructions"] = {
        "exists": bool(codex_config_files),
        "tokens": codex_config_tokens,
        "note": "Codex compact prompt/config guidance. Counted as configurable local setup.",
        "files": codex_config_files,
    }
    components["codex_config"] = {
        "count": len(codex_config_files),
        "tokens": codex_config_tokens,
        "files": codex_config_files,
    }
    components["skill_frontmatter_quality"] = {"verbose_skills": verbose_skills}
    components["core_system"] = {
        "tokens": 12900,
        "note": "Codex base instructions and built-in tools. Fixed overhead, not user-configurable.",
    }
    return components


def _measure_cursor_components():
    """Minimal component dict for Cursor, without reading ~/.claude.

    Cursor has no Claude-style CLAUDE.md / skills / MCP / settings.json startup
    overhead: its only wiring is the shared ~/.cursor/hooks.json, which is not a
    token-measurable surface (it is the host's own file, merged, never owned by
    TO). Returns a minimal dict so ``calculate_totals`` /
    ``detect_calibration_gap`` / the dashboard template render without
    CLAUDE_DIR access (the dashboard is the one _CLAUDE_TARGET_CMDS subcommand
    Cursor exempts — see _FOREIGN_RUNTIME_EXEMPTIONS).
    """
    _measure_facade = _compat.facade()
    hooks_path = _measure_facade.runtime_home() / "hooks.json"
    return {
        "cursor_hooks": {
            "path": str(hooks_path),
            "exists": hooks_path.is_file(),
            "tokens": 0,
            "lines": 0,
            "note": "shared host hooks.json — not a Token Optimizer token surface",
        }
    }


def _hermes_config(home: Path) -> dict:
    """Parse <hermes home>/config.yaml (or config.yml). Returns {} when absent
    or unreadable. Uses PyYAML when installed (Hermes itself depends on it);
    otherwise reads only the two blocks the dashboard needs (mcp_servers and
    skills.disabled) with a small indentation parser."""
    _measure_facade = _compat.facade()
    path = home / "config.yaml"
    if not path.is_file():
        path = home / "config.yml"
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
            return {}
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    try:
        import yaml  # type: ignore[import]  # noqa: PLC0415

        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    except ImportError:
        pass
    except Exception:
        return {}
    return _measure_facade._hermes_config_fallback(text)


def _hermes_config_fallback(text: str) -> dict:
    """Minimal reader for `mcp_servers:` (server names plus scalar keys) and
    `skills: disabled:` (a block or flow list). Not a YAML parser."""
    _measure_facade = _compat.facade()
    def scalar(raw: str):
        _measure_facade = _compat.facade()
        raw = raw.split(" #", 1)[0].strip().strip('"').strip("'")
        low = raw.lower()
        if low in ("true", "yes", "on"):
            return True
        if low in ("false", "no", "off"):
            return False
        return raw

    out: dict = {}
    section = None
    server = None
    server_indent = None
    in_disabled = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        body = line.strip()
        if indent == 0:
            section = body.split(":", 1)[0].strip() if body.endswith(":") or ":" in body else None
            server, server_indent, in_disabled = None, None, False
            if section == "mcp_servers":
                out.setdefault("mcp_servers", {})
            continue
        if section == "mcp_servers":
            if server_indent is None or indent <= server_indent:
                if body.endswith(":") and not body.startswith("-"):
                    server_indent = indent
                    server = body[:-1].strip().strip('"').strip("'")
                    out["mcp_servers"][server] = {}
                continue
            if server is not None and ":" in body and not body.startswith("-"):
                key, value = body.split(":", 1)
                if value.strip():
                    out["mcp_servers"][server][key.strip()] = scalar(value)
        elif section == "skills":
            if body.startswith("disabled:"):
                rest = body[len("disabled:"):].strip()
                skills = out.setdefault("skills", {}).setdefault("disabled", [])
                if rest.startswith("[") and rest.endswith("]"):
                    skills.extend(s for s in (scalar(x) for x in rest[1:-1].split(",")) if s)
                    in_disabled = False
                else:
                    in_disabled = True
                continue
            if in_disabled and body.startswith("- "):
                out["skills"]["disabled"].append(scalar(body[2:]))
            elif not body.startswith("- "):
                in_disabled = False
    return out


def _hermes_skill_inventory(home: Path, cfg: dict | None = None) -> dict[str, list[dict]]:
    """Skills under <hermes home>/skills/<category>/<name>/SKILL.md.

    Hermes puts each available skill's name and description in the system
    prompt and loads the body only on use, so `tokens` is that index entry.
    Skills moved to skills/.archive/ are archived; names in config
    `skills.disabled` are disabled. Other dot-folders (.hub) are skipped.
    """
    _measure_facade = _compat.facade()
    cfg = _measure_facade._hermes_config(home) if cfg is None else cfg
    disabled_names = set()
    skills_cfg = cfg.get("skills") if isinstance(cfg, dict) else None
    if isinstance(skills_cfg, dict) and isinstance(skills_cfg.get("disabled"), list):
        disabled_names = {str(n) for n in skills_cfg["disabled"]}
    root = home / "skills"
    out: dict[str, list[dict]] = {"active": [], "archived": [], "disabled": []}
    if not root.is_dir() or root.is_symlink():
        return out
    seen = 0
    for path in sorted(root.rglob("SKILL.md")):
        seen += 1
        if seen > _measure_facade._HERMES_SKILL_SCAN_CAP:
            break
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if path.is_symlink():
            continue
        hidden = [part for part in rel.parts[:-1] if part.startswith(".")]
        if hidden and hidden[0] != ".archive":
            continue
        meta = _measure_facade._parse_skill_frontmatter(path)
        name = meta.get("name") or path.parent.name
        category = rel.parts[-3] if len(rel.parts) >= 3 and not hidden else ""
        item = {
            "name": name,
            "skill_name": name,
            "description": meta.get("description", ""),
            "category": category,
            "tokens": meta.get("tokens", 0),
            "source": "hermes",
            "path": _measure_facade._display_path(str(path)),
        }
        if hidden:
            out["archived"].append(item)
        elif name in disabled_names or path.parent.name in disabled_names:
            out["disabled"].append(item)
        else:
            out["active"].append(item)
    for items in out.values():
        items.sort(key=lambda item: ((_measure_facade := _compat.facade()), (item["category"], item["name"]))[1])
    return out


def _hermes_mcp_inventory(cfg: dict) -> list[dict]:
    """MCP servers from the `mcp_servers:` block of Hermes config.yaml.
    `enabled: false` means Hermes skips the server entirely."""
    _measure_facade = _compat.facade()
    servers = cfg.get("mcp_servers") if isinstance(cfg, dict) else None
    if not isinstance(servers, dict):
        return []
    items = []
    for name, server in servers.items():
        if not isinstance(server, dict):
            server = {}
        enabled = server.get("enabled", True) is not False
        items.append({
            "name": str(name),
            "command": str(server.get("url") or server.get("command") or ""),
            "transport": "http" if server.get("url") else "stdio",
            "tokens": _measure_facade.TOKENS_PER_DEFERRED_TOOL if enabled else 0,
            "enabled": enabled,
        })
    return sorted(items, key=lambda item: ((_measure_facade := _compat.facade()), item["name"])[1])


def _measure_hermes_components():
    """Measure Hermes-relevant startup/config components without reading Claude config.

    Mirrors ``_measure_codex_components()`` for the Hermes runtime. Hermes has
    no Claude-style CLAUDE.md / skills / MCP / settings.json startup overhead:
    its session data lives in ``~/.hermes/state.db`` (read by the
    hermes_session adapter) and Token Optimizer ships as a plugin under
    ``~/.hermes/plugins/token-optimizer/``. The dashboard (the one
    _CLAUDE_TARGET_CMDS subcommand Hermes exempts — see
    _FOREIGN_RUNTIME_EXEMPTIONS) calls this so it never scans ``~/.claude``
    (cross-platform universality). Returns a minimal but valid
    component dict so ``calculate_totals`` / ``detect_calibration_gap`` /
    the dashboard template render without CLAUDE_DIR access.
    """
    _measure_facade = _compat.facade()
    components: dict = {}
    home = _measure_facade.runtime_home()  # ~/.hermes (or HERMES_HOME when safe)

    # Token Optimizer plugin payload under ~/.hermes/plugins/token-optimizer/.
    # This is the only Hermes-side controllable overhead the audit can see.
    plugin_dir = home / "plugins" / "token-optimizer"
    plugin_tokens = 0
    plugin_files: list[dict] = []
    if plugin_dir.is_dir():
        for f in sorted(plugin_dir.rglob("*.py")):
            try:
                # Defense-in-depth: skip symlinked entries so a
                # planted symlink under ~/.hermes/plugins/token-optimizer/
                # can't leak a path outside the plugin dir into the dashboard.
                if f.is_symlink():
                    continue
                if not f.is_file():
                    continue
                tok = _measure_facade.estimate_tokens_from_file(f)
                plugin_tokens += tok
                plugin_files.append({"path": str(f), "tokens": tok})
            except OSError:
                continue

    # Plugin code runs inside the Hermes process; it is never sent to the
    # model, so it adds no context tokens. Its size is kept for reference.
    components["hermes_plugin"] = {
        "path": str(plugin_dir),
        "exists": plugin_dir.is_dir(),
        "tokens": 0,
        "code_tokens": plugin_tokens,
        "files": plugin_files,
        "note": "Token Optimizer plugin code; runs in the Hermes process, not in the model context.",
    }

    # Files Hermes injects into the system prompt at session start: SOUL.md
    # (agent identity) and the memories snapshot (MEMORY.md + USER.md).
    def _file_component(paths, note):
        _measure_facade = _compat.facade()
        tokens = lines = 0
        files = []
        for f in paths:
            try:
                if f.is_symlink() or not f.is_file():
                    continue
                tok = _measure_facade.estimate_tokens_from_file(f)
                tokens += tok
                lines += len(f.read_text(encoding="utf-8", errors="replace").splitlines())
                files.append({"path": str(f), "tokens": tok})
            except OSError:
                continue
        return {"exists": bool(files), "tokens": tokens, "lines": lines, "files": files, "note": note}

    components["hermes_soul"] = _file_component(
        [home / "SOUL.md"], "Agent identity, loaded into every session.")
    components["hermes_memory"] = _file_component(
        [home / "memories" / "MEMORY.md", home / "memories" / "USER.md"],
        "Memory snapshot (MEMORY.md + USER.md), loaded into every session.")

    cfg = _measure_facade._hermes_config(home)
    inventory = _measure_facade._hermes_skill_inventory(home, cfg)
    components["skills"] = {
        "count": len(inventory["active"]),
        "tokens": sum(item["tokens"] for item in inventory["active"]),
        "archived_count": len(inventory["archived"]),
        "disabled_count": len(inventory["disabled"]),
        "note": "Name and description of each skill, listed in the system prompt; bodies load on use.",
    }
    mcp = _measure_facade._hermes_mcp_inventory(cfg)
    enabled_mcp = [item for item in mcp if item["enabled"]]
    components["mcp_tools"] = {
        "server_count": len(enabled_mcp),
        "disabled_count": len(mcp) - len(enabled_mcp),
        "tool_count_estimate": "?",
        "tokens": sum(item["tokens"] for item in enabled_mcp),
        "note": "Configured servers; tool schemas are listed on demand by Hermes tool search.",
    }

    # core_system is 0 because Hermes base instructions are not exposed for
    # measurement; the dashboard's "fixed overhead" row renders without
    # scanning ~/.claude.
    components["core_system"] = {
        "tokens": 0,
        "note": "Hermes base instructions are not exposed for measurement; no ~/.claude scan.",
    }
    return components


def _measure_antigravity_components():
    """Measure Antigravity-relevant startup/config components without reading Claude config.

    Mirrors ``_measure_hermes_components()`` for the Antigravity runtime.
    Antigravity has no Claude-style CLAUDE.md / skills / MCP / settings.json
    startup overhead: its session data lives in the read-only conversation
    store under ``~/.gemini`` (read by the antigravity_state adapter) and Token
    Optimizer ships as a plugin under ``~/.gemini/config/plugins/token-optimizer/``.
    The dashboard (the _CLAUDE_TARGET_CMDS subcommand Antigravity exempts — see
    _FOREIGN_RUNTIME_EXEMPTIONS) calls this so it never scans ``~/.claude``
    (cross-platform universality). Returns a minimal but valid
    component dict so the dashboard template renders without CLAUDE_DIR access.
    """
    _measure_facade = _compat.facade()
    components: dict = {}
    home = _measure_facade.runtime_home()  # ~/.gemini (or TOKEN_OPTIMIZER_ANTIGRAVITY_HOME when safe)

    # Token Optimizer plugin payload under ~/.gemini/config/plugins/token-optimizer/.
    plugin_dir = home / "config" / "plugins" / "token-optimizer"
    plugin_tokens = 0
    plugin_files: list[dict] = []
    if plugin_dir.is_dir():
        for f in sorted(plugin_dir.rglob("*.py")):
            try:
                # Defense-in-depth: skip symlinked entries so a
                # planted symlink can't leak a path outside the plugin dir into
                # the dashboard.
                if f.is_symlink():
                    continue
                if not f.is_file():
                    continue
                tok = _measure_facade.estimate_tokens_from_file(f)
                plugin_tokens += tok
                plugin_files.append({"path": str(f), "tokens": tok})
            except OSError:
                continue

    components["antigravity_plugin"] = {
        "path": str(plugin_dir),
        "exists": plugin_dir.is_dir(),
        "tokens": plugin_tokens,
        "files": plugin_files,
        "note": "Token Optimizer plugin under ~/.gemini/config/plugins/token-optimizer/.",
    }

    components["core_system"] = {
        "tokens": 0,
        "note": "Antigravity base instructions are not exposed for measurement; no ~/.claude scan.",
    }
    return components


def calculate_totals(components):
    """Calculate total controllable and estimated overhead."""
    _measure_facade = _compat.facade()
    controllable = 0
    fixed = 0
    # Keys that don't contribute direct token overhead (metadata only)
    non_token_keys = {
        "file_exclusion", "hooks", "settings_env", "settings_local",
        "skill_frontmatter_quality", "skills_detail", "compact_instructions",
    }

    for name, info in components.items():
        if name in non_token_keys:
            continue
        tokens = info.get("tokens", 0)
        if name == "core_system":
            fixed += tokens
        else:
            controllable += tokens

    return {
        "controllable_tokens": controllable,
        "fixed_tokens": fixed,
        "estimated_total": controllable + fixed,
    }


def _is_1m_model(model_str):
    """Check if a model string indicates a 1M-context model.

    Verified 2026-10-10 from code.claude.com/docs/en/model-config#extended-context:
      - Fable 5.1/5, Sonnet 5 and later, Haiku 5.5, Opus 4.7 and later run
        with the 1M window by default (no [1m] suffix).
      - Sonnet 4.6 and Opus 4.6 reach 1M ONLY through their [1m] variant.
      - Older models (Haiku <= 4.5, Sonnet <= 4.5, Opus <= 4.5) are 200K.
    A bare family alias (`sonnet`, `opus`, `fable`) resolves to a 1M-native
    model on the Anthropic API; bare `haiku` is left at 200K because the
    alias resolves to Haiku 5.5 on the Anthropic API but Haiku 4.5 on other
    providers (conservative, overridable with TOKEN_OPTIMIZER_CONTEXT_SIZE).
    """
    _measure_facade = _compat.facade()
    return _measure_facade._claude_model_window(model_str) >= 1_000_000


def _claude_model_window(model_str):
    """Context window for a Claude model string, per the verified doc table.

    1M-native models (Fable, Sonnet 5+, Haiku 5.5, Opus 4.7+) get 1M with no
    suffix; Sonnet 4.6 / Opus 4.6 get 1M only with the ``[1m]`` variant;
    everything else is 200K. Unrecognized strings fall back to the 1M default
    that detect_context_window() documents (most users are on 1M-native
    models); override with TOKEN_OPTIMIZER_CONTEXT_SIZE.
    """
    _measure_facade = _compat.facade()
    m = _measure_facade._strip_provider_prefixes(str(model_str or "").split("/")[-1])
    if not m:
        return 200_000
    one_m_suffix = "[1m]" in m or "1000k" in m
    m = m.replace("[1m]", "").strip()
    # Strip date suffixes (-20250929 / @20250929): same model, same window.
    m = _measure_facade.re.sub(r"[-@]\d{8}$", "", m).strip()
    match = _measure_facade._CLAUDE_MODEL_ID_RE.match(m)
    if not match:
        # Claude 3-era and older ids ("claude-3-5-sonnet-20241022",
        # "claude-3-haiku", "claude-2", "instant") are all 200K windows.
        if (_measure_facade._CLAUDE_LEGACY_ID_RE.search(m)
                or _measure_facade.re.search(r"claude[-_]?[0-3]\b", m)
                or "instant" in m):
            return 200_000
        # GPT-5.6 (Sol/Terra/Luna) publishes 1.05M, same as the other runtimes' tables.
        if _measure_facade.re.match(r"gpt-5\.6(?:$|-)", m):
            return 1_050_000
        # Unrecognized (e.g. gateway alias): keep the historical 1M default.
        return 1_000_000
    family, major_raw, minor_raw = match.groups()
    if family in ("fable", "mythos"):
        return 1_000_000  # every Fable/Mythos release is 1M-native
    try:
        major = int(major_raw) if major_raw else None
        minor = int(minor_raw) if minor_raw else 0
    except ValueError:
        return 1_000_000
    if family == "haiku":
        if major is None:
            return 200_000  # bare alias: provider-dependent version, conservative
        return 1_000_000 if (major, minor) >= (5, 5) else 200_000
    if family == "sonnet":
        if major is None:
            return 1_000_000  # alias resolves to Sonnet 5.5 on the Anthropic API
        if major >= 5:
            return 1_000_000
        if major == 4 and minor == 6:
            return 1_000_000 if one_m_suffix else 200_000
        return 200_000
    if family == "opus":
        if major is None:
            return 1_000_000  # alias resolves to Opus 5.5 on the Anthropic API
        if major >= 5:
            return 1_000_000
        if major == 4 and minor >= 7:
            return 1_000_000
        if major == 4 and minor == 6:
            return 1_000_000 if one_m_suffix else 200_000
        return 200_000
    return 1_000_000


def _configured_model_string():
    """The model string the user configured (env first, then settings), the
    same lookup order detect_context_window() uses. Lowercased, may be ''."""
    _measure_facade = _compat.facade()
    for var in ("CLAUDE_MODEL", "ANTHROPIC_MODEL"):
        v = (_measure_facade.os.environ.get(var) or "").strip().lower()
        if v:
            return v
    for cfg_name in ("config.json", "settings.json"):
        try:
            if not _measure_facade._is_regular_file(_measure_facade.CLAUDE_DIR / cfg_name):
                continue
            with open(_measure_facade.CLAUDE_DIR / cfg_name, "r", encoding="utf-8") as f:
                cfg = _measure_facade.json.load(f)
            m = cfg.get("model") or cfg.get("primaryModel") or ""
            if isinstance(m, str) and m.strip():
                return m.strip().lower()
        except (OSError, ValueError, AttributeError):
            continue
    return ""


def _plain_46_family(model_str):
    """'opus' / 'sonnet' when model_str is a 4.6 id WITHOUT the [1m] suffix."""
    _measure_facade = _compat.facade()
    m = _measure_facade._strip_provider_prefixes(str(model_str or "").split("/")[-1])
    if not m or "[1m]" in m or "1000k" in m:
        return None
    m = _measure_facade.re.sub(r"[-@]\d{8}$", "", m).strip()
    match = _measure_facade._CLAUDE_MODEL_ID_RE.match(m)
    if not match:
        return None
    family, major, minor = match.groups()
    if family in ("opus", "sonnet") and major == "4" and minor == "6":
        return family
    return None


def _promote_plain_46_window(model_str, window, context_tokens):
    """Opus/Sonnet 4.6 reach 1M only as the [1m] variant, but Claude Code
    writes the PLAIN id into the transcript, so a 1M session looks 200K.

    Two pieces of evidence say the window is bigger, in this order:
      1. the configured (env/settings) model carries [1m] for the same family;
      2. observed tokens exceed the window (arithmetic, needs no cooperation).
    Returns (window, source_or_None, inferred_from_tokens). Explicit user
    overrides (TOKEN_OPTIMIZER_CONTEXT_SIZE, --context-size, DISABLE_1M) are
    never promoted: _context_window_for_model_str() returns them unchanged for
    the [1m] spelling too, which is how that case is detected.
    """
    _measure_facade = _compat.facade()
    family = _measure_facade._plain_46_family(model_str)
    if not family or window >= 1_000_000:
        return window, None, False
    promoted = _measure_facade._context_window_for_model_str(f"{model_str.strip()}[1m]")
    if promoted <= window:
        return window, None, False
    configured = _measure_facade._configured_model_string()
    if "[1m]" in configured:
        c = _measure_facade._CLAUDE_MODEL_ID_RE.match(
            _measure_facade.re.sub(r"[-@]\d{8}$", "", configured.replace("[1m]", "").strip()))
        if c and c.group(1) == family and (c.group(2) is None
                                          or (c.group(2), c.group(3)) == ("4", "6")):
            return promoted, f"settings/env model [1m]: {configured}", False
    try:
        if context_tokens is not None and float(context_tokens) > window:
            return promoted, "observed tokens above the 200K window (plain 4.6 id, 1M variant)", True
    except (TypeError, ValueError):
        pass
    return window, None, False


def _context_window_for_model_str(model_str):
    """Resolve a context-window size for a SPECIFIC model string (e.g. one
    parsed straight out of a transcript message).

    Precedence (highest first):
      1. CLAUDE_CODE_DISABLE_1M_CONTEXT=1 -> 200k (kills the 1M tier globally).
      2. TOKEN_OPTIMIZER_CONTEXT_SIZE=<int> -> that exact size.
      3. _cli_context_size (parsed from the CLI --context-size flag).
      4. The model string itself: _claude_model_window() (1M-native models,
         [1m] variants, 200K otherwise).

    Note: the env overrides in steps 1-3 are GLOBAL -- they are not keyed
    to the model string. A user who exports TOKEN_OPTIMIZER_CONTEXT_SIZE=200000
    for their default sonnet and then runs a 1M-context opus variant will get
    200000 returned for that opus session, inflating fill_pct ~5x. This is
    by design: the overrides are explicit user intent ("treat every session as
    N tokens"), not a per-model hint. The model-string tier (step 4) only runs
    when no override is set. The earlier docstring claimed the function keyed
    to "the model that actually produced the tokens, not an env/config global
    that may name a different model entirely" -- that was an overclaim; the
    globals still win when set.
    """
    _measure_facade = _compat.facade()
    if _measure_facade.os.environ.get("CLAUDE_CODE_DISABLE_1M_CONTEXT") == "1":
        return 200_000
    raw = _measure_facade.os.environ.get("TOKEN_OPTIMIZER_CONTEXT_SIZE", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    if _measure_facade._cli_context_size:
        return _measure_facade._cli_context_size
    m = (model_str or "").lower().strip()
    if not m:
        return None
    return _measure_facade._claude_model_window(m)


def _host_parse_int(raw):
    """Claude Code's Dd(): int, or None where the host yields NaN."""
    _measure_facade = _compat.facade()
    text = str(raw).strip()
    if len(text) <= 32:
        if _measure_facade._HOST_SCI_RE.match(text):
            try:
                number = float(text)
            except ValueError:
                return None
            return int(number) if _measure_facade.math.isfinite(number) and number == int(number) else None
        if _measure_facade._HOST_GROUPED_RE.match(text):
            return int(_measure_facade._HOST_SEPARATOR_RE.sub("", text))
    m = _measure_facade._HOST_INT_PREFIX_RE.match(text)
    return int(m.group(0)) if m else None


def _parse_compact_window_value(value):
    """Parse a compact-window token count -> int, or None when it is ignored.

    Numbers pass through when positive. Strings parse the way the host does
    (see _host_parse_int); "auto", "", None, garbage, zero and negatives -> None
    so the caller falls through to the next precedence level.
    """
    _measure_facade = _compat.facade()
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not _measure_facade.math.isfinite(value) or value <= 0:
            return None
        return int(value)
    parsed = _measure_facade._host_parse_int(value)
    return parsed if parsed is not None and parsed > 0 else None


def _clamp_compact_window(tokens):
    """Explicit windows accept 100000..1000000 per the settings reference."""
    _measure_facade = _compat.facade()
    return max(_measure_facade._COMPACT_WINDOW_MIN, min(_measure_facade._COMPACT_WINDOW_MAX, int(tokens)))


def _canonical_compact_model_id(model):
    """Canonical id Claude Code matches modelSettings keys against.

    The host canonicalizes aliases, [1m] variants, date-suffixed ids and
    recognized provider ids, so "claude-opus-5-5[1m]" and
    "claude-opus-5-5-20261001" both read the "claude-opus-5-5" entry.
    """
    _measure_facade = _compat.facade()
    m = str(model or "").strip().lower()
    if not m:
        return ""
    # provider-prefixed ids ("anthropic/x", "us.anthropic.x", "bedrock:x")
    m = _measure_facade._strip_provider_prefixes(m.split("/")[-1])
    m = m.replace("[1m]", "").strip()
    m = _measure_facade.re.sub(r"[-@]\d{8}$", "", m)        # -20250929 / @20250929 date suffix
    if m and not m.startswith("claude-"):
        m = "claude-" + m                   # bare "opus-5-5" -> canonical form
    return m


def _model_settings_window(model, model_settings):
    """(value, key) of the modelSettings entry's autoCompactWindow for `model`,
    or (None, None). Matches by exact id, canonicalized id, or family alias."""
    _measure_facade = _compat.facade()
    if not isinstance(model_settings, dict) or not model:
        return None, None
    raw = str(model).strip().lower()
    canon = _measure_facade._canonical_compact_model_id(model)
    family_match = _measure_facade._CLAUDE_MODEL_ID_RE.match(canon)
    family = family_match.group(1) if family_match else ""
    # Two passes: an exact/canonical id outranks a family alias regardless of
    # key order; the first matching key decides.
    for match_family in (False, True):
        for key, entry in model_settings.items():
            if not isinstance(entry, dict) or "autoCompactWindow" not in entry:
                continue
            key_l = str(key).strip().lower()
            if match_family:
                hit = bool(family) and key_l == family
            else:
                hit = key_l == raw or _measure_facade._canonical_compact_model_id(key_l) == canon
            if hit:
                return entry.get("autoCompactWindow"), key
    return None, None


def _compact_window_env():
    """Live env values for the compact-window resolver: process env first, then
    the settings.json env block (the host injects settings env into the
    session, and hooks don't always inherit it)."""
    _measure_facade = _compat.facade()
    out = {}
    try:
        for var in ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"):
            val = _measure_facade._resolve_feature_env(var)
            if val is not None:
                out[var] = val
    except Exception:
        pass
    return out


def _resolve_compact_window(model, env=None, settings=None):
    """Full resolution of the effective compact window for a session model.

    Returns {"tokens", "source", "user_override", "model_window",
    "default_tokens"}. ``user_override`` is False when only the tuned default
    produced the answer -- callers that mirror the host's own percentage must
    only recompute fill when a real override shrank the window (PR #210).
    """
    _measure_facade = _compat.facade()
    if env is None:
        env = _measure_facade._compact_window_env()
    if settings is None:
        try:
            settings, _settings_path = _measure_facade._read_settings_json()
        except Exception:
            settings = {}
    if not isinstance(env, dict):
        env = {}
    if not isinstance(settings, dict):
        settings = {}

    if _measure_facade.detect_runtime() == "codex":
        # Codex: the Claude table and Claude's autocompact settings do not
        # apply. A GPT id fell through to the Claude 1M default, so a 475K
        # model reported a 967K compaction point (issue #276). Use Codex's
        # own catalog window, then the runtime's detected window.
        window = None
        try:
            import codex_models
            window = codex_models.effective_window(model) if model else None
        except Exception:
            window = None
        if not window:
            try:
                window = _measure_facade.detect_context_window()[0]
            except Exception:
                window = None
        if window:
            return {
                "tokens": int(window),
                "source": f"Codex model window ({model or 'configured model'})",
                "user_override": False,
                "model_window": int(window),
                "default_tokens": int(window),
            }

    model_window = _measure_facade._context_window_for_model_str(model)
    if not model_window:
        try:
            model_window = _measure_facade.detect_context_window()[0]
        except Exception:
            model_window = 1_000_000

    default_tokens = (_measure_facade._COMPACT_WINDOW_1M_DEFAULT if model_window >= 1_000_000
                      else model_window)
    default_source = (
        "default (~967K for 1M-native models)" if model_window >= 1_000_000
        else f"default (model window {model_window})"
    )

    tokens = None
    source = None
    user_override = False

    # 1. env var wins over every settings source.
    raw_env = env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW")
    parsed = _measure_facade._parse_compact_window_value(raw_env)
    if parsed is not None:
        tokens = _measure_facade._clamp_compact_window(parsed)
        source = f"env CLAUDE_CODE_AUTO_COMPACT_WINDOW={raw_env}"
        if str(raw_env).strip() != str(tokens):
            # The host's own reading ("500k" is 500, floored) is not the number
            # the user typed: say so instead of presenting it as their override.
            if tokens == parsed:
                source += f" (read as {parsed})"
            else:
                bound = "floored to" if tokens > parsed else "capped at"
                source += f" (read as {parsed}, {bound} {tokens})"
        user_override = True
    else:
        prefix = ""
        # 2. /autocompact's per-model entry beats the top-level setting.
        ms_val, ms_key = _measure_facade._model_settings_window(model, settings.get("modelSettings"))
        parsed_ms = _measure_facade._parse_compact_window_value(ms_val)
        if parsed_ms is not None:
            tokens = _measure_facade._clamp_compact_window(parsed_ms)
            source = f"modelSettings[{ms_key}].autoCompactWindow={ms_val}"
            user_override = True
        elif ms_val == "auto":
            # `/autocompact auto` is stored per model and means "the window
            # tuned for this model": it replaces the top-level value for this
            # model, it does not fall through to it (host: byModel[key] ?? top
            # level, then "auto" -> tuned default).
            tokens = default_tokens
            source = (f"modelSettings[{ms_key}].autoCompactWindow='auto' "
                      f"(tuned default, top-level autoCompactWindow not used); {default_source}")
        else:
            if ms_val is not None:
                prefix = (f"modelSettings[{ms_key}].autoCompactWindow={ms_val!r} "
                          "(not a window, ignored); ")
            # 3. Top-level autoCompactWindow.
            top_val = settings.get("autoCompactWindow")
            parsed_top = _measure_facade._parse_compact_window_value(top_val)
            if parsed_top is not None:
                tokens = _measure_facade._clamp_compact_window(parsed_top)
                source = f"{prefix}autoCompactWindow={top_val}"
                user_override = True
            else:
                # 4. Default.
                tokens = default_tokens
                source = f"{prefix}{default_source}"

    # A window can never exceed the model's own context window.
    if tokens > model_window:
        tokens = model_window
        source += f"; capped at model window {model_window}"

    # CLAUDE_AUTOCOMPACT_PCT_OVERRIDE applies to whatever the window resolved
    # to: it is the USED percentage at which compaction runs, so a lower value
    # compacts earlier. Values outside 1..100 cannot raise the threshold and
    # are ignored.
    pct = None
    raw_pct = env.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE")
    if raw_pct is not None:
        # The host reads it with parseFloat: a float prefix, "50.5" and "50%" count.
        m_pct = _measure_facade._HOST_FLOAT_PREFIX_RE.match(str(raw_pct).strip())
        try:
            pct = float(m_pct.group(0)) if m_pct else None
        except ValueError:
            pct = None
    if pct is not None and _measure_facade.math.isfinite(pct) and 1 <= pct < 100:
        tokens = int(tokens * pct / 100)
        source += f" x CLAUDE_AUTOCOMPACT_PCT_OVERRIDE={pct:g}%"
        user_override = True

    return {
        "tokens": int(tokens),
        "source": source,
        "user_override": user_override,
        "model_window": model_window,
        "default_tokens": default_tokens,
    }


def effective_compact_window(model, env=None, settings=None):
    """(tokens, provenance): where this session's model will auto-compact.

    ``env``/``settings`` default to the live process env + settings.json when
    omitted; callers pass dicts in tests. See _resolve_compact_window for the
    precedence and clamp rules.
    """
    _measure_facade = _compat.facade()
    resolved = _measure_facade._resolve_compact_window(model, env=env, settings=settings)
    return resolved["tokens"], resolved["source"]


def _codex_config_int(name: str) -> int | None:
    _measure_facade = _compat.facade()
    value = _measure_facade._read_codex_config().get(name)
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        # TOML allows `inf`/`nan` literals: int(inf) raises OverflowError,
        # int(nan) ValueError — both mean "no usable value", skip the key.
        return None
    return parsed if parsed > 0 else None


def _codex_config_model() -> str | None:
    _measure_facade = _compat.facade()
    value = _measure_facade._read_codex_config().get("model")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _latest_codex_logged_context_window() -> tuple[int | None, str | None]:
    _measure_facade = _compat.facade()
    try:
        files = _measure_facade.codex_session.find_all_jsonl_files(days=30)
    except Exception:
        return None, None
    for path, _mtime, _project in files[:25]:
        try:
            p = _measure_facade.Path(path)
            with p.open("rb") as handle:
                try:
                    size = handle.seek(0, _measure_facade.os.SEEK_END)
                    handle.seek(max(0, size - 1_000_000))
                except OSError:
                    handle.seek(0)
                raw = handle.read()
            for line in reversed(raw.decode("utf-8", errors="replace").splitlines()[-2000:]):
                try:
                    record = _measure_facade.json.loads(line)
                except _measure_facade.json.JSONDecodeError:
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict) or payload.get("type") != "token_count":
                    continue
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                value = info.get("model_context_window")
                try:
                    parsed = int(value)
                except (TypeError, ValueError):
                    continue
                if parsed > 0:
                    return parsed, p.name
        except OSError:
            continue
    return None, None


def detect_context_window():
    """Detect context window size without assuming API limits for Codex.

    Detection order:
      1. CLAUDE_CODE_DISABLE_1M_CONTEXT=1 -> 200K (explicit opt-out)
      2. TOKEN_OPTIMIZER_CONTEXT_SIZE env var -> explicit override
      3. --context-size CLI flag (set via _cli_context_size) -> override
      4. Codex: logged model_context_window, then explicit Codex config,
         then conservative Codex effective default
      5. Claude: CLAUDE_MODEL / ANTHROPIC_MODEL env var -> check model family
      6. Claude config.json or settings.json model field -> check model family
      7. Claude fallback: 1M (Sonnet 5+, Opus 4.7+, Fable and Haiku 5.5 are 1M with no
         suffix; Opus/Sonnet 4.6 are 1M only as the [1m] variant)
    """
    _measure_facade = _compat.facade()
    pass
    # Resolve context flags from process env AND settings.json:
    # a user who set these in settings.json env would otherwise get a mis-detected
    # window, corrupting fill-% and every downstream recommendation.
    _disable_1m = _measure_facade._resolve_feature_env("CLAUDE_CODE_DISABLE_1M_CONTEXT") or ""
    _ctx_size_override = (_measure_facade._resolve_feature_env("TOKEN_OPTIMIZER_CONTEXT_SIZE") or "").strip()
    cache_key = (
        _measure_facade.detect_runtime(),
        _disable_1m,
        _ctx_size_override,
        _measure_facade.os.environ.get("CODEX_MODEL", ""),
        _measure_facade.os.environ.get("OPENAI_MODEL", ""),
        _measure_facade.os.environ.get("CLAUDE_MODEL", ""),
        _measure_facade.os.environ.get("ANTHROPIC_MODEL", ""),
        _measure_facade._cli_context_size,
    )
    if _measure_facade._context_window_cache and _measure_facade._context_window_cache[0] == cache_key:
        return _measure_facade._context_window_cache[1]

    def remember(value):
        _measure_facade = _compat.facade()
        pass
        _measure_facade._context_window_cache = (cache_key, value)
        return value

    if str(_disable_1m).strip().lower() in _measure_facade._TRUTHY_ENV_VALUES:
        return remember((200_000, "env: CLAUDE_CODE_DISABLE_1M_CONTEXT"))
    raw = _ctx_size_override
    if raw:
        try:
            _parsed_ctx = int(raw)
        except ValueError:
            _parsed_ctx = 0
        # Mirror _codex_config_int's >0 guard: "0"/negative/garbage would
        # otherwise yield ctx_window<=0 and crash every `overhead / ctx_window`
        # division downstream.
        if _parsed_ctx > 0:
            return remember((_parsed_ctx, "env: TOKEN_OPTIMIZER_CONTEXT_SIZE"))
    # CLI override (set by --context-size flag)
    if _measure_facade._cli_context_size and _measure_facade._cli_context_size > 0:
        return remember((_measure_facade._cli_context_size, "cli: --context-size"))
    if _measure_facade.detect_runtime() == "codex":
        logged_window, session_name = _measure_facade._latest_codex_logged_context_window()
        if logged_window:
            return remember((logged_window, f"codex session log: {session_name}"))
        configured_window = _measure_facade._codex_config_int("model_context_window")
        if configured_window:
            return remember((configured_window, "codex config: model_context_window"))
        model = ((_measure_facade.os.environ.get("CODEX_MODEL") or "").strip()
                 or (_measure_facade.os.environ.get("OPENAI_MODEL") or "").strip()
                 or _measure_facade._codex_config_model())
        import codex_models
        window = codex_models.effective_window(model)
        if window:
            return remember((window, f'Codex model catalog: {model}'))
        if _measure_facade._normalize_openai_model_name(model) == 'gpt-6-astra':
            return remember((1_050_000, 'OpenAI published GPT-6 Astra context window'))
        model_note = f" for {model}" if model else ""
        return remember((_measure_facade.CODEX_DEFAULT_EFFECTIVE_CONTEXT_WINDOW, f"Codex conservative effective window{model_note} (override: TOKEN_OPTIMIZER_CONTEXT_SIZE)"))
    # Hermes: Hermes does not expose a model field in
    # ~/.claude/config.json or settings.json. Use env override or a
    # conservative default WITHOUT reading CLAUDE_DIR files.
    if _measure_facade.detect_runtime() == "hermes":
        model = _measure_facade.os.environ.get("HERMES_MODEL", "").lower()
        if model:
            w = _measure_facade._claude_model_window(model)
            return remember((w, f"hermes env: {model} ({'1M' if w >= 1_000_000 else '200K'})"))
        return remember((200_000, "hermes default (200K. Override: TOKEN_OPTIMIZER_CONTEXT_SIZE)"))
    # Foreign runtimes (cursor, antigravity, grok, opencode, copilot) must not
    # inherit Claude's model env vars, ~/.claude config, or the 1M Claude
    # default — a conservative labeled default, always overridable via
    # TOKEN_OPTIMIZER_CONTEXT_SIZE.
    _rt = _measure_facade.detect_runtime()
    if _rt != "claude":
        return remember((200_000, f"{_rt} default (200K conservative. Override: TOKEN_OPTIMIZER_CONTEXT_SIZE)"))
    # Detect from model string in environment
    model = _measure_facade.os.environ.get("CLAUDE_MODEL", "").lower()
    if not model:
        model = _measure_facade.os.environ.get("ANTHROPIC_MODEL", "").lower()
    if model:
        reason = f"model: {model}"
        if "claude-3-haiku" in model or "3-haiku" in model:
            reason += " [WARNING: Claude 3 Haiku retired April 2026. Migrate to claude-haiku-5-5]"
            print(f"[Token Optimizer] WARNING: {model} was retired April 2026. Migrate to claude-haiku-5-5.", file=_measure_facade.sys.stderr)
        w = _measure_facade._claude_model_window(model)
        return remember((w, f"{reason} ({'1M' if w >= 1_000_000 else '200K'})"))
    # Check config files for model preference
    for cfg_name in ("config.json", "settings.json"):
        cfg_path = _measure_facade.CLAUDE_DIR / cfg_name
        if _measure_facade._is_regular_file(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = _measure_facade.json.load(f)
                m = (cfg.get("model") or cfg.get("primaryModel") or "").lower()
                if m:
                    reason = f"{cfg_name.split('.')[0]}: {m}"
                    if "claude-3-haiku" in m or "3-haiku" in m:
                        reason += " [WARNING: Claude 3 Haiku retired April 2026. Migrate to claude-haiku-5-5]"
                        print(f"[Token Optimizer] WARNING: {m} was retired April 2026. Migrate to claude-haiku-5-5.", file=_measure_facade.sys.stderr)
                    w = _measure_facade._claude_model_window(m)
                    return remember((w, f"{reason} ({'1M' if w >= 1_000_000 else '200K'})"))
            except (_measure_facade.json.JSONDecodeError, PermissionError, OSError):
                pass
    # Most Claude Code models are 1M-native now (Sonnet 5+, Opus 4.7+, Fable,
    # Haiku 5.5). Default to 1M; users on 200K models can override with
    # TOKEN_OPTIMIZER_CONTEXT_SIZE=200000.
    return remember((1_000_000, "default (1M, Sonnet 5+/Opus 4.7+/Haiku 5.5 native. Override: TOKEN_OPTIMIZER_CONTEXT_SIZE)"))


def _interpolate_curve(value, curve):
    _measure_facade = _compat.facade()
    value = max(curve[0][0], min(curve[-1][0], value))
    for i in range(len(curve) - 1):
        x0, y0 = curve[i]
        x1, y1 = curve[i + 1]
        if x0 <= value <= x1:
            t = (value - x0) / (x1 - x0) if x1 > x0 else 0
            return round(y0 + t * (y1 - y0))
    return curve[-1][1]


def _quality_curve_for_model(model):
    _measure_facade = _compat.facade()
    m = str(model or "").lower()
    if 'gpt-5.6' in m or 'daybreak' in m or m == 'gpt-reserve':
        return 'openai-gpt-5.5-proxy (uncalibrated)', _measure_facade._OPENAI_GPT55_MRCR_TOKENS, 'absolute_tokens'
    if 'gpt-6-astra' in m:
        # No calibrated Astra retrieval curve is bundled. Label the proxy
        # explicitly instead of silently treating Astra as an Anthropic model.
        return 'openai-gpt-5.5-proxy-for-astra (uncalibrated)', _measure_facade._OPENAI_GPT55_MRCR_TOKENS, 'absolute_tokens'
    if "gemini" in m:
        return "google-gemini", _measure_facade._GEMINI_MRCR_TOKENS, "absolute_tokens"
    if "gpt-5.5" in m:
        return "openai-gpt-5.5", _measure_facade._OPENAI_GPT55_MRCR_TOKENS, "absolute_tokens"
    if "gpt-5" in m or m.startswith("codex"):
        return "openai-gpt-5", _measure_facade._OPENAI_GPT5_MRCR_TOKENS, "absolute_tokens"
    return "anthropic-default", _measure_facade._MRCR_CURVE, "fill_fraction"


def _estimate_quality_from_fill(fill_pct, model=None, context_window=None):
    """Estimate long-context retrieval quality from fill and model family."""
    _measure_facade = _compat.facade()
    return _measure_facade._estimate_quality_with_curve(fill_pct, model=model, context_window=context_window)[0]


def _estimate_quality_with_curve(fill_pct, model=None, context_window=None):
    """Estimate quality and return the curve label for reporting."""
    _measure_facade = _compat.facade()
    fill = max(0.0, min(1.0, fill_pct))
    curve_name, curve, curve_type = _measure_facade._quality_curve_for_model(model)
    if curve_type == "absolute_tokens":
        try:
            ctx = float(context_window or _measure_facade.detect_context_window()[0])
        except (TypeError, ValueError):
            ctx = float(_measure_facade.detect_context_window()[0])
        return _measure_facade._interpolate_curve(fill * max(ctx, 1), curve), curve_name
    return _measure_facade._interpolate_curve(fill, curve), curve_name


def _degradation_band(fill_pct):
    """Return degradation band name and color code from fill percentage."""
    _measure_facade = _compat.facade()
    if fill_pct < 0.50:
        return "PEAK ZONE", "green"
    elif fill_pct < 0.70:
        return "DEGRADATION STARTING", "yellow"
    elif fill_pct < 0.80:
        return "QUALITY DROPPING", "orange"
    else:
        return "SEVERE", "red"


def score_to_grade(score):
    """Convert a 0-100 quality score to a letter grade.

    S: 90-100 | A: 80-89 | B: 70-79 | C: 55-69 | D: 40-54 | F: 0-39
    """
    _measure_facade = _compat.facade()
    if score >= 90:
        return "S"
    if score >= 80:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    if score >= 40:
        return "D"
    return "F"


def score_to_band(score):
    """Convert a 0-100 quality score to a band label."""
    _measure_facade = _compat.facade()
    if score >= 80:
        return "Good"
    if score >= 60:
        return "Fair"
    if score >= 40:
        return "Needs Work"
    return "Poor"


def _estimate_messages_until_compact(ctx_window, overhead, avg_msg_tokens=None, model=None):
    """Estimate how many messages fit before auto-compact fires.

    Uses the resolved effective compact window (env > /autocompact >
    autoCompactWindow > ~967K-on-1M / model-limit default), not a fixed
    fraction of the model window.
    """
    _measure_facade = _compat.facade()
    try:
        compact_threshold = _measure_facade._resolve_compact_window(model)["tokens"]
    except Exception:
        compact_threshold = ctx_window
    usable = max(0, compact_threshold - overhead)
    return max(0, usable // avg_msg_tokens)


def _auto_snapshot(components, totals, ctx_window):
    """Save an auto-snapshot for drift detection. Silent, never fails."""
    _measure_facade = _compat.facade()
    try:
        snap_dir = _measure_facade.SNAPSHOT_DIR / "auto-snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        snap = {
            "timestamp": _measure_facade.datetime.now().isoformat(),
            "context_window": ctx_window,
            "total_overhead": totals["estimated_total"],
            "controllable_tokens": totals["controllable_tokens"],
            "fixed_tokens": totals["fixed_tokens"],
            "skill_count": components.get("skills", {}).get("count", 0),
            "skill_tokens": components.get("skills", {}).get("tokens", 0),
            "mcp_server_count": components.get("mcp_tools", {}).get("count", 0),
            "mcp_tokens": components.get("mcp_tools", {}).get("tokens", 0),
            "claude_md_tokens": sum(
                components[k].get("tokens", 0)
                for k in components if k.startswith("claude_md") and components[k].get("exists")
            ),
            "memory_md_tokens": components.get("memory_md", {}).get("tokens", 0),
            "memory_md_lines": components.get("memory_md", {}).get("lines", 0),
            "memory_md_scope": components.get("memory_md", {}).get("scope", "file"),
            "memory_md_truncated": bool(components.get("memory_md", {}).get("truncated")),
        }
        # Keep last 30 snapshots
        existing = sorted(snap_dir.glob("snap_*.json"))
        if len(existing) >= 30:
            for old in existing[:-29]:
                old.unlink()
        fname = f"snap_{_measure_facade.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        fd = _measure_facade.os.open(str(snap_dir / fname), _measure_facade.os.O_WRONLY | _measure_facade.os.O_CREAT | _measure_facade.os.O_TRUNC, 0o600)
        with _measure_facade.os.fdopen(fd, "w", encoding="utf-8") as f:
            _measure_facade.json.dump(snap, f, indent=2)
    except OSError:
        pass


def quick_scan(as_json=None):
    """Fast overview: overhead, degradation risk, top offenders, coaching insight."""
    _measure_facade = _compat.facade()
    components = _measure_facade.measure_components()
    totals = _measure_facade.calculate_totals(components)
    ctx_window, ctx_source = _measure_facade.detect_context_window()
    ctx_window = max(int(ctx_window or 0), 1)  # every division below needs >0
    ctx_label = _measure_facade._fmt_context_window(ctx_window)

    overhead = totals["estimated_total"]
    overhead_pct = overhead / ctx_window * 100

    # Degradation calculations
    peak_limit = int(ctx_window * 0.50)  # 50% = peak quality zone boundary
    usable_before_degradation = max(0, peak_limit - overhead)
    msgs_before_compact = _measure_facade._estimate_messages_until_compact(ctx_window, overhead)

    # Current session fill estimate (overhead only, no session data)
    fill_pct = overhead / ctx_window
    # Resolve the model so Codex never gets scored on the Anthropic curve.
    # Codex sessions run gpt-5.x-codex variants; env/config resolution covers
    # the common cases, and "codex" as fallback maps to the published
    # openai-gpt-5 MRCR curve rather than anthropic-default.
    _rt = _measure_facade.detect_runtime()
    if _rt == "codex":
        _qmodel = ((_measure_facade.os.environ.get("CODEX_MODEL") or "").strip()
                   or (_measure_facade.os.environ.get("OPENAI_MODEL") or "").strip()
                   or _measure_facade._codex_config_model() or "codex")
    elif _rt in ("claude", "hermes"):
        _qmodel = _measure_facade.os.environ.get("CLAUDE_MODEL") or _measure_facade.os.environ.get("ANTHROPIC_MODEL")
    else:
        # Foreign runtimes have no Claude-model settings to read — leave the
        # model unresolved rather than inherit another runtime's env vars.
        _qmodel = None
    quality_est, _qcurve = _measure_facade._estimate_quality_with_curve(fill_pct, model=_qmodel, context_window=ctx_window)
    if _rt == "codex" and _qcurve == "anthropic-default":
        # Custom/unknown Codex model string (o3, gpt-4.1, custom-provider
        # names) matched no known family — a Codex session must never be
        # scored or labeled on the Anthropic curve. Fall back to the
        # documented Codex default.
        quality_est, _qcurve = _measure_facade._estimate_quality_with_curve(
            fill_pct, model="codex", context_window=ctx_window)
    elif _rt not in ("claude", "hermes", "codex") and _qcurve == "anthropic-default":
        # No model-family knowledge for this runtime — the estimate came from
        # the generic fill-fraction curve, so label it honestly instead of
        # implying an Anthropic curve scored a foreign runtime.
        _qcurve = "generic-fill"
    band_name, band_color = _measure_facade._degradation_band(fill_pct)

    # Top offenders
    offenders = []
    skills = components.get("skills", {})
    if skills.get("count", 0) > 0:
        offenders.append(("skills", skills.get("count", 0), skills.get("tokens", 0),
                         f"{skills.get('count', 0)} skill metadata entries"))
    mcp = components.get("mcp_tools", {})
    mcp_count = mcp.get("count", 0)
    if _measure_facade.detect_runtime() == "codex":
        mcp_count = mcp.get("server_count", 0)
    if mcp_count > 0:
        eager = mcp.get("eager_tool_count", 0)
        detail = f"{mcp_count} MCP servers"
        if eager > 0:
            detail += f" ({eager} with eager-loaded tools)"
        offenders.append(("mcp", mcp_count, mcp.get("tokens", 0), detail))
    # Per-file CLAUDE.md offenders (PR #100, danikdanik). measure_components()
    # keys each ancestor CLAUDE.md separately (claude_md_global,
    # claude_md_home, claude_md_project_<dir>), mirroring how Claude Code loads
    # them up the directory tree. Blending them into one "CLAUDE.md" entry
    # produced a token/line count that was no single file's size and a
    # "slim to N lines" action that was not actionable on any one file.
    _claude_md_files = [
        (k, components[k]) for k in sorted(components)
        if k.startswith("claude_md") and components[k].get("exists")
    ]
    for _k, _c in _claude_md_files:
        _f_tokens = _c.get("tokens", 0)
        _f_lines = _c.get("lines", 0)
        _f_path = _c.get("path", "") or _k
        _f_label = _f_path.replace(str(_measure_facade.HOME), "~") if _f_path else _k
        if _f_tokens > 0:
            offenders.append(("claude_md", _f_lines, _f_tokens,
                             f"{_f_label} ({_f_lines} lines)"))
    if _measure_facade.detect_runtime() == "codex":
        agents_md_tokens = sum(
            components[k].get("tokens", 0)
            for k in components if k.startswith("agents_md") and components[k].get("exists")
        )
        agents_md_lines = sum(
            components[k].get("lines", 0)
            for k in components if k.startswith("agents_md") and components[k].get("exists")
        )
        if agents_md_tokens > 0:
            offenders.append(("agents_md", agents_md_lines, agents_md_tokens,
                             f"AGENTS.md chain ({agents_md_lines} lines)"))
    mem = components.get("memory_md", {})
    if mem.get("tokens", 0) > 0:
        # Under Codex this component holds state_*.sqlite memory, not a
        # MEMORY.md file — label it for the runtime the user is on.
        _mem_label = "Codex memories (all threads)" if _rt == "codex" else "MEMORY.md"
        if _rt == "codex" and mem.get("truncated"):
            _mem_label = "Codex memories (all threads, partial: scan cap reached)"
        offenders.append(("memory_md", mem.get("lines", 0), mem.get("tokens", 0),
                         f"{_mem_label} ({mem.get('thread_count', 0)} threads)" if _rt == "codex"
                         else f"{_mem_label} ({mem.get('lines', 0)} lines)"))

    # Sort by tokens descending, top 3
    offenders.sort(key=lambda x: ((_measure_facade := _compat.facade()), -x[2])[1])
    top_offenders = offenders[:3]

    # Quick win: check for unused skills via trends
    quick_win = None
    try:
        trends = _measure_facade._collect_trends_data(days=30)
        if trends and _measure_facade.detect_runtime() != "codex":
            never_used = trends.get("skills", {}).get("never_used", [])
            active_names = set(skills.get("names", []))
            never_used = sorted({n for n in never_used
                                 if n in active_names and not _measure_facade._is_own_tool_skill(n)})
            if len(never_used) >= 3:
                # Same rule as the coach's unused-skill savings: measured
                # per-skill frontmatter only — never count x inventory average
                # (which reports the inventory bound when most skills are
                # unused) and never count x a flat constant.
                detail_map = components.get("skills_detail", {})
                measured = 0
                unmeasured = 0
                for _n in never_used:
                    try:
                        _t = int((detail_map.get(_n) or {}).get("frontmatter_tokens") or 0)
                    except (TypeError, ValueError, OverflowError):
                        _t = 0
                    if _t > 0:
                        measured += _t
                    else:
                        unmeasured += 1
                if measured > 0:
                    _unm = f" ({unmeasured} of {len(never_used)} lack per-skill measurement)" if unmeasured else ""
                    quick_win = {
                        "action": f"Review {len(never_used)} skills not invoked in the window",
                        "savings": measured,
                        "detail": f"save ~{measured:,} measured tokens/session{_unm}",
                        "extend": f"Extends peak quality zone by ~{measured:,} tokens",
                    }
                else:
                    quick_win = {
                        "action": f"Review {len(never_used)} skills not invoked in the window",
                        "savings": 0,
                        "detail": "savings unknown: per-skill measurements unavailable",
                        "extend": "Removing unused skills extends the peak quality zone",
                    }
    except Exception:
        pass

    # If no trends-based win, suggest instruction-file trimming
    if not quick_win and _measure_facade.detect_runtime() == "codex":
        agents_md_tokens = sum(
            components[k].get("tokens", 0)
            for k in components if k.startswith("agents_md") and components[k].get("exists")
        )
        agents_md_lines = sum(
            components[k].get("lines", 0)
            for k in components if k.startswith("agents_md") and components[k].get("exists")
        )
        if agents_md_tokens > 3500:
            savings = agents_md_tokens - 3000
            quick_win = {
                "action": f"Slim AGENTS.md chain from {agents_md_lines} lines",
                "savings": savings,
                "detail": f"save ~{savings:,} tokens/session",
                "extend": f"Improves stable prompt-cache prefix and extends peak quality by ~{savings:,} tokens",
            }
    if not quick_win and _claude_md_files:
        # PR #100 (danikdanik): pick the LARGEST single CLAUDE.md rather than
        # blending every ancestor file, so the action names a file the user can
        # actually open. Savings math stays ours: Anthropic's documented
        # 200-line guidance, with tokens-per-line taken from that one file, and
        # the >200-line guard so a token-dense but short file never gets a
        # self-contradicting "slim to 200 lines, save ~0 tokens".
        #
        # G3 C-P2-1: filter to the files that ACTUALLY qualify (>5000 tokens AND
        # >200 lines) BEFORE picking the largest. The old code did max()-by-tokens
        # first and then applied the gate to that one file, so a token-dense but
        # short (<=200-line) file could win max() and then fail the >200-line
        # gate -- masking a genuine slim win in a different, over-200-line file
        # (exactly the multi-file monorepo/ancestor layout PR#100 serves).
        _qualifying = [
            (k, c) for k, c in _claude_md_files
            if c.get("tokens", 0) > 5000 and c.get("lines", 0) > 200
        ]
        if _qualifying:
            _lk, _lc = max(_qualifying, key=lambda kv: ((_measure_facade := _compat.facade()), kv[1].get("tokens", 0))[1])
            claude_md_tokens = _lc.get("tokens", 0)
            claude_md_lines = _lc.get("lines", 0)
            _lc_path = _lc.get("path", "") or _lk
            _lc_label = _lc_path.replace(str(_measure_facade.HOME), "~") if _lc_path else _lk
            tokens_per_line = claude_md_tokens / max(claude_md_lines, 1)
            savings = max(0, claude_md_tokens - int(200 * tokens_per_line))
            if savings > 0:
                quick_win = {
                    "action": f"Slim {_lc_label} from {claude_md_lines} lines to under 200 (per Anthropic guidance)",
                    "savings": savings,
                    "detail": f"save ~{savings:,} tokens/session",
                    "extend": f"Extends peak quality zone by ~{savings:,} tokens",
                }

    # Coaching insight
    coaching = None
    if _measure_facade.detect_runtime() == "codex":
        coaching = (
            "Codex-native optimization starts with AGENTS.md, active plugins/skills, MCP servers,\n"
            "  status-line/context telemetry, and logged cached_input_tokens. Trust the logged context window."
        )
    elif ctx_window >= 500_000:
        coaching = (
            "At 1M, Sonnet 4.6 has held the lead on multi-hop reasoning vs earlier Opus\n"
            "  (GraphWalks: 73.8 vs 38.7 on 4.6). Opus 4.7 narrows this — benchmark your own workload."
        )

    # Auto-save snapshot for drift detection
    _measure_facade._auto_snapshot(components, totals, ctx_window)

    grade = _measure_facade.score_to_grade(quality_est)

    if as_json:
        result = {
            "context_window": ctx_window,
            "context_source": ctx_source,
            "overhead_tokens": overhead,
            "overhead_pct": round(overhead_pct, 1),
            "usable_before_degradation": usable_before_degradation,
            "messages_before_compact": msgs_before_compact,
            "fill_pct": round(fill_pct * 100, 1),
            "quality_estimate": quality_est,
            "quality_curve": _qcurve,
            "quality_basis": (
                f"heuristic: {_qcurve} MRCR curve at current startup fill; not a measured per-session score"
            ),
            "grade": grade,
            "degradation_band": band_name,
            "top_offenders": [
                {"name": o[0], "count": o[1], "tokens": o[2], "detail": o[3]}
                for o in top_offenders
            ],
            "quick_win": quick_win,
            "coaching": coaching,
            "subagent_cache": _measure_facade.subagent_cache_block(),
        }
        try:
            _rb = _measure_facade._recs_block()
            if _rb is not None:
                result["usage_recommendations"] = _rb
        except Exception:
            pass
        print(_measure_facade.json.dumps(result, indent=2))
        return result

    # Pretty print
    print("\nTOKEN OPTIMIZER: QUICK SCAN")
    print(f"{'=' * 40}")
    print(f"  Context window:      {ctx_window:,} tokens ({ctx_label}, {ctx_source})")
    print(f"  Startup overhead:    {overhead:,} tokens ({overhead_pct:.1f}%)")
    print(f"  Usable before degradation: ~{usable_before_degradation:,} (50% fill = peak quality zone)")
    print(f"  Messages before auto-compact: ~{msgs_before_compact} at typical message size")

    print("\n  DEGRADATION RISK")
    print(f"    Current startup fill:  {fill_pct * 100:.0f}% ({overhead:,}) -- {band_name}")
    print(f"    Quality estimate:      {grade} ({quality_est}/100) ({_qcurve} MRCR curve at this fill level; heuristic, not measured)")
    next_danger = int(ctx_window * 0.50)
    print(f"    Next danger zone:      {next_danger:,} (50%, \"lost in the middle\" begins)")
    try:
        _cw_res = _measure_facade._resolve_compact_window(None)
        compact_at = _cw_res["tokens"]
        compact_at_note = f" ({_cw_res['source']})" if _cw_res.get("user_override") else ""
    except Exception:
        compact_at, compact_at_note = ctx_window, ""
    print(f"    Auto-compact fires at: ~{compact_at:,}{compact_at_note} (context is rewritten to a summary; Token Optimizer restores what the summary drops)")

    if top_offenders:
        print("\n  TOP OFFENDERS")
        for i, (_, count, tokens, detail) in enumerate(top_offenders, 1):
            print(f"    {i}. {detail}: {tokens:,} tokens")

    if quick_win:
        print("\n  #1 QUICK WIN")
        print(f"    {quick_win['action']} -> {quick_win['detail']}")
        print(f"    {quick_win['extend']}")

    # Memory review nudge
    mem_lines = mem.get("lines", 0)
    if mem_lines > 200:
        print(f"\n  MEMORY.md: {mem_lines} lines ({mem_lines - 200} over 200-line visible limit)")
        print("    Run: python3 $MEASURE_PY memory-review  (structural breakdown + fix suggestions)")

    if coaching:
        print("\n  COACHING INSIGHT")
        print(f"    {coaching}")

    # Usage recommendations: the daily-measured record is the advice channel.
    # One line per `recommend` item, nothing otherwise; the record is read,
    # never scanned for (a detached refresh writes it).
    try:
        _rblk = _measure_facade._recs_block() or {}
        _ritems = _rblk.get("items") or []
        _rcovered = {i.get("id") for i in _ritems}
        for _it in _ritems:
            if _it.get("state") == "recommend":
                _ln = _measure_facade._recs_item_line(_it)
                if _ln:
                    print(f"\n  {_ln}")
    except Exception:
        _rcovered = set()

    # Advise-only subagent cache verdict: only an actionable recommendation
    # earns a line here (a pending/thin verdict stays quiet -- see doctor).
    # Falls silent once the daily record covers the item so no line repeats.
    try:
        if "subagent_cache" not in _rcovered:
            _scr = (_measure_facade.subagent_cache_block().get("recommendation") or {})
            if _scr.get("action") in ("enable", "disable"):
                print(f"\n  SUBAGENT CACHE: {_scr['line']}")
    except Exception:
        pass

    print("\n  Full audit + fixes: /token-optimizer")
    print("  Health check: python3 $MEASURE_PY doctor")
    print()


def doctor(as_json=None):
    """Health check: verify all Token Optimizer components are installed and working."""
    _measure_facade = _compat.facade()
    checks = []
    score = 0
    total = 0

    # 1. Install mode
    total += 1
    plugin_cache = _measure_facade.CLAUDE_DIR / "plugins" / "cache"
    is_plugin = False
    if plugin_cache.exists():
        import glob as globmod
        for _ in globmod.glob(str(plugin_cache / "*" / "token-optimizer" / "*")):
            is_plugin = True
            break
    skill_link = _measure_facade.CLAUDE_DIR / "skills" / "token-optimizer"
    is_skill = skill_link.exists()
    if is_plugin:
        checks.append(("OK", "Install", "plugin mode"))
        score += 1
    elif is_skill:
        checks.append(("OK", "Install", "skill mode (symlink)"))
        score += 1
    else:
        checks.append(("!!", "Install", "not detected (run install.sh)"))

    # 2. Python version
    total += 1
    py_ver = f"{_measure_facade.sys.version_info.major}.{_measure_facade.sys.version_info.minor}.{_measure_facade.sys.version_info.micro}"
    if _measure_facade.sys.version_info >= (3, 9):
        checks.append(("OK", f"Python {py_ver}", ">= 3.9"))
        score += 1
    else:
        checks.append(("!!", f"Python {py_ver}", "requires >= 3.9"))

    # 3. Context window detection
    total += 1
    ctx_window, ctx_source = _measure_facade.detect_context_window()
    ctx_label = _measure_facade._fmt_context_window(ctx_window)
    checks.append(("OK", "Context window", f"{ctx_label} detected ({ctx_source})"))
    score += 1

    # 4. SessionEnd hook (plugin hooks.json auto-installs these, so check for plugin too)
    total += 1
    settings, _ = _measure_facade._read_settings_json()
    # The label names where the hook really comes from: _is_hook_installed alone
    # also counts the plugin's hooks.json, which is not settings.json.
    if _measure_facade._is_hook_installed(settings, settings_only=True):
        checks.append(("OK", "SessionEnd hook", "active (settings.json)"))
        score += 1
    elif _measure_facade._is_plugin_installed() or _measure_facade._is_hook_installed(settings):
        checks.append(("OK", "SessionEnd hook", "active (plugin hooks.json)"))
        score += 1
    else:
        checks.append(("!!", "SessionEnd hook", _measure_facade._hint("missing (fix: python3 measure.py setup-hook)")))

    # Advisory only: do not change proxy/tool-search settings or infer a bill.
    from tool_search_diagnostic import diagnose_tool_search
    _search_check = diagnose_tool_search(_measure_facade.os.environ, settings, runtime=_measure_facade.detect_runtime())
    if _search_check is not None:
        checks.append(_search_check)
        total += 1
        if _search_check[0] == "OK":
            score += 1

    # 5. Smart Compaction
    total += 1
    sc_status = _measure_facade._is_smart_compact_installed(settings)
    sc_count = sum(1 for v in sc_status.values() if v)
    if sc_count == 4:
        checks.append(("OK", "Smart Compaction", "4/4 hooks active"))
        score += 1
    elif sc_count > 0:
        missing = [e for e, v in sc_status.items() if not v]
        checks.append(("!!", "Smart Compaction", f"{sc_count}/4 hooks (missing: {', '.join(missing)})"))
    else:
        checks.append(("!!", "Smart Compaction", _measure_facade._hint("not installed (fix: python3 measure.py setup-smart-compact)")))

    # 6. Quality bar
    total += 1
    qb = _measure_facade._is_quality_bar_installed(settings)
    # Which statusline.js the status line really runs: a missing file shows a
    # blank status line, and another checkout runs other code than this one.
    _qb_sl_path = None
    try:
        _found = _measure_facade._statusline_js_in_command(((settings or {}).get("statusLine") or {}).get("command") or "")
        _qb_sl_path = _found[2] if _found else None
    except Exception:
        _qb_sl_path = None
    _qb_ours = _measure_facade._get_statusline_path()
    if qb["statusline"] and _qb_sl_path and not _measure_facade.Path(_qb_sl_path).expanduser().is_file():
        checks.append(("!!", "Quality bar", _measure_facade._hint(
            f"status line runs a missing file: {_qb_sl_path} "
            "(fix: start a new session, it re-points it; or python3 measure.py setup-quality-bar --force)")))
    elif qb["statusline"] and qb["hook"]:
        _qb_note = ""
        if _qb_sl_path and _measure_facade.os.path.normcase(_measure_facade.os.path.abspath(_qb_sl_path)) != _measure_facade.os.path.normcase(_measure_facade.os.path.abspath(_qb_ours)):
            _qb_note = f" (runs another Token Optimizer copy: {_qb_sl_path})"
        checks.append(("OK", "Quality bar", "status line + hook active" + _qb_note))
        score += 1
    else:
        missing = []
        if not qb["statusline"]:
            missing.append("status line")
        if not qb["hook"]:
            missing.append("cache hook")
        checks.append(("!!", "Quality bar", _measure_facade._hint(f"missing: {', '.join(missing)} (fix: python3 measure.py setup-quality-bar)")))

    # 7. Trends DB
    total += 1
    if _measure_facade.TRENDS_DB.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(str(_measure_facade.TRENDS_DB))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            count = conn.execute("SELECT COUNT(*) FROM session_log").fetchone()[0]
            conn.close()
            mtime = _measure_facade.TRENDS_DB.stat().st_mtime
            age_hours = (_measure_facade.time.time() - mtime) / 3600
            if age_hours < 1:
                age_str = f"{int(age_hours * 60)}m ago"
            else:
                age_str = f"{int(age_hours)}h ago"
            checks.append(("OK", "Trends DB", f"{count} sessions, last collected {age_str}"))
            score += 1
        except Exception:
            checks.append(("!!", "Trends DB", "exists but unreadable"))
    else:
        checks.append(("!!", "Trends DB", _measure_facade._hint("not found (fix: python3 measure.py collect)")))

    # 8. Dashboard freshness
    total += 1
    if _measure_facade.DASHBOARD_PATH.exists():
        mtime = _measure_facade.DASHBOARD_PATH.stat().st_mtime
        age_hours = (_measure_facade.time.time() - mtime) / 3600
        if age_hours < 1:
            age_str = f"{int(age_hours * 60)}m ago"
        else:
            age_str = f"{int(age_hours)}h ago"
        checks.append(("OK", "Dashboard", f"fresh ({age_str})"))
        score += 1
    else:
        checks.append(("!!", "Dashboard", _measure_facade._hint("not generated (fix: python3 measure.py dashboard)")))

    # 9. Explain the compaction-percentage override (READ-ONLY).
    # CLAUDE_AUTOCOMPACT_PCT_OVERRIDE is documented; doctor explains what it
    # does and flags very-low values. It must NEVER be edited or removed here
    # (the old auto-delete destroyed a documented user setting).
    total += 1
    try:
        _env_checks = _measure_facade._autocompact_pct_override_explanation()
    except Exception:
        _env_checks = [("OK", "Env override", "status unknown (read failed)")]
    checks.extend(_env_checks)
    score += 1

    # 10. Broken symlinks
    total += 1
    broken = []
    skills_dir = _measure_facade.CLAUDE_DIR / "skills"
    if skills_dir.exists():
        for item in skills_dir.iterdir():
            if item.is_symlink() and not item.resolve().exists():
                broken.append(item.name)
    if not broken:
        checks.append(("OK", "Symlinks", "no broken symlinks"))
        score += 1
    else:
        checks.append(("!!", "Symlinks", f"{len(broken)} broken: {', '.join(broken[:5])}"))

    # 11. Duplicate installs
    total += 1
    if is_plugin and is_skill:
        checks.append(("!!", "Duplicate installs", "both plugin and skill detected (pick one)"))
    else:
        checks.append(("OK", "No duplicate installs", ""))
        score += 1

    # 12. Duplicate plugin skills (worktrees / stale install paths)
    total += 1
    _plugin_scan = _measure_facade._scan_plugin_skills_and_commands()
    plugin_dupes = _plugin_scan.get("duplicate_skills", {})
    plugin_suspicious = _plugin_scan.get("suspicious_paths", [])
    if plugin_dupes:
        dupe_count = sum(len(v) - 1 for v in plugin_dupes.values())
        dupe_names = ", ".join(list(plugin_dupes.keys())[:3])
        checks.append(("!!", "Duplicate plugin skills",
                       f"{dupe_count} extra copies ({dupe_names}). Likely from worktrees. "
                       f"Clean stale entries from ~/.claude/plugins/installed_plugins.json"))
    elif plugin_suspicious:
        reasons = set(s["reason"] for s in plugin_suspicious)
        checks.append(("!!", "Suspicious plugin paths",
                       f"plugins loaded from {', '.join(reasons)} directories"))
    else:
        checks.append(("OK", "Plugin paths clean", "no duplicates or suspicious sources"))
        score += 1

    # 13. Dashboard daemon (doctor had ZERO daemon checks,
    # so a wedged sticky marker was invisible to the one command named for the
    # job). Fail-open: a probe error must not break doctor.
    total += 1
    try:
        if _measure_facade._daemon_install_failed_marker_present():
            _mk = _measure_facade._daemon_install_failed_reason()
            checks.append(("!!", "Dashboard daemon",
                           f"self-heal disabled by a permanent install failure "
                           f"({_mk or 'reason unknown'}) "
                           f"(fix: {_measure_facade._measure_cli()} setup-daemon)"))
        elif _measure_facade._daemon_snapshot_sandboxed():
            checks.append(("OK", "Dashboard daemon",
                           "not managed from this process (HOME or data dir redirected: sandbox)"))
            score += 1
        elif _measure_facade._daemon_resurrection_blocked() is not None:
            checks.append(("OK", "Dashboard daemon", "disabled by user (opt-out honored)"))
            score += 1
        elif (_foreign_job := _measure_facade._scheduler_job_points_elsewhere()):
            checks.append(("!!", "Dashboard daemon",
                           f"owned by another install: {_foreign_job} "
                           f"(this install will not re-point it; run `setup-daemon --uninstall` "
                           f"from that install, or set TOKEN_OPTIMIZER_ALLOW_FOREIGN_HOME_SCHEDULER=1)"))
        elif _measure_facade._verify_daemon_port(timeout_seconds=1, retries=1, retry_sleep=0):
            checks.append(("OK", "Dashboard daemon",
                           f"running at http://localhost:{_measure_facade.DAEMON_PORT}/token-optimizer"))
            score += 1
        elif _measure_facade._daemon_service_installed(_measure_facade._normalized_platform()):
            checks.append(("!!", "Dashboard daemon",
                           "installed but not serving "
                           f"(fix: {_measure_facade._measure_cli()} setup-daemon)"))
        else:
            checks.append(("OK", "Dashboard daemon",
                           "not installed (self-installs at SessionStart)"))
            score += 1
    except Exception:
        checks.append(("OK", "Dashboard daemon", "status unknown (probe failed)"))
        score += 1

    # Settings self-heal history (visible to anyone who runs doctor).
    try:
        _heal_log = _measure_facade._settings_heal_dir() / "heal_log.jsonl"
        if _heal_log.is_file():
            _heal_entries = [
                _measure_facade.json.loads(l) for l in _heal_log.read_text(encoding="utf-8").strip().split("\n")
                if l.strip()
            ]
            _restores = [e for e in _heal_entries if e.get("action") == "restore"]
            if _restores:
                _last = _restores[-1]
                _keys = ", ".join(_last.get("restored_keys", [])[:5])
                if len(_last.get("restored_keys", [])) > 5:
                    _keys += f" (+{len(_last['restored_keys']) - 5} more)"
                checks.append((
                    "OK", "Settings self-heal",
                    f"{len(_restores)} restore(s), last: {_keys} at {_last.get('timestamp', '?')[:19]}",
                ))
            else:
                checks.append(("OK", "Settings self-heal", "no restores needed"))
        else:
            checks.append(("OK", "Settings self-heal", "no heal history yet"))
    except Exception:
        pass  # doctor must never fail on heal history

    if as_json:
        result = {
            "score": score,
            "total": total,
            "checks": [{"status": s, "name": n, "detail": d} for s, n, d in checks],
            "subagent_cache": _measure_facade.subagent_cache_block(),
        }
        try:
            _rb = _measure_facade._recs_block()
            if _rb is not None:
                result["usage_recommendations"] = _rb
        except Exception:
            pass
        print(_measure_facade.json.dumps(result, indent=2))
        return result

    # Pretty print
    print("\nTOKEN OPTIMIZER DOCTOR")
    print(f"{'=' * 40}")
    for status, name, detail in checks:
        icon = "[OK]" if status == "OK" else "[!!]"
        detail_str = f"  {detail}" if detail else ""
        print(f"  {icon:5s} {name}: {detail_str}")

    # Subagent cache row: state + the one-line payoff estimate.
    try:
        _scb = _measure_facade.subagent_cache_block()
        _sc_state = _scb.get("state") or "unknown"
        _sc_payoff = _scb.get("payoff") or {}
        _sc_detail = (
            f"state: {_sc_state}; last {_sc_payoff.get('window_days', 30)}d "
            f"estimated net {_measure_facade._recs_usd(_sc_payoff.get('net_usd_est') or 0.0)}"
            + (" API-equivalent" if _scb.get("billing_mode") == "subscription" else "")
            + " (estimate)")
        _sc_auto = _scb.get("auto_decision") or {}
        if _sc_auto.get("reason"):
            _sc_detail += f"; verdict: {_sc_auto['reason']}"
        print(f"  {'':5s} Subagent cache: {_sc_detail}")
        _sc_rec = _scb.get("recommendation") or {}
        if _sc_rec.get("line") \
                and _sc_rec["line"] != _sc_auto.get("reason") \
                and not _measure_facade._recs_covers("subagent_cache"):
            # Never a line that repeats the verdict, and silent once the
            # daily record carries this item itself.
            print(f"  {'':5s}   advice: {_sc_rec['line']}")
        if _scb.get("hint"):
            print(f"  {'':5s}   note: {_scb['hint']}")
        if _sc_state == "set":
            print(f"  {'':5s} Undo: {_measure_facade._subagent_cache_cmd('disable')}")
    except Exception:
        pass  # doctor must never fail on this optional row

    # Usage recommendations: one line per applicable item, any state. The
    # daily-measured record is read, never scanned for.
    try:
        for _it in ((_measure_facade._recs_block() or {}).get("items") or []):
            _ln = _measure_facade._recs_item_line(_it)
            if _ln:
                print(f"  {'':5s} {_ln}")
    except Exception:
        pass  # doctor must never fail on this optional row

    print(f"\n  Score: {score}/{total}")
    # Show fix command for first failing check
    for status, name, detail in checks:
        if status == "!!" and "fix:" in detail:
            fix_cmd = detail.split("fix: ")[1].rstrip(")")
            print(f"  Fix: {fix_cmd}")
            break
    print()


def git_context(as_json=None):
    """Suggest context-relevant files based on git state.

    Analyzes git diff, test companions, co-change history, and import chains
    to suggest which files should be in context for the current work.
    """
    _measure_facade = _compat.facade()
    import subprocess as _sp

    def _run_git(*cmd):
        _measure_facade = _compat.facade()
        try:
            r = _sp.run(["git"] + list(cmd), capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=10,
                        creationflags=_measure_facade._NO_WINDOW)
            return r.stdout.strip() if r.returncode == 0 else ""
        except (FileNotFoundError, _sp.TimeoutExpired):
            return ""

    # 1. Modified files (staged + unstaged + untracked)
    diff_output = _run_git("diff", "--name-only")
    staged_output = _run_git("diff", "--name-only", "--cached")
    status_output = _run_git("status", "--porcelain")

    modified = set()
    if diff_output:
        modified.update(diff_output.splitlines())
    if staged_output:
        modified.update(staged_output.splitlines())
    # Untracked new files from status
    for line in (status_output or "").splitlines():
        if line.startswith("??"):
            modified.add(line[3:].strip())

    if not modified:
        result = {"modified": [], "test_companions": [], "co_changed": [], "import_chain": []}
        if as_json:
            print(_measure_facade.json.dumps(result, indent=2))
        else:
            print("\n  GIT CONTEXT: No modified files detected.")
            print("  Run this after making changes to get context suggestions.\n")
        return result

    # 2. Test companion mapping
    test_companions = []
    for f in sorted(modified):
        base = _measure_facade.Path(f)
        stem = base.stem
        parent = str(base.parent)
        ext = base.suffix
        if "test" in stem.lower() or "spec" in stem.lower():
            continue  # Skip test/spec files themselves
        candidates = [
            f"test_{stem}{ext}",
            f"{stem}_test{ext}",
            f"tests/test_{stem}{ext}",
            f"tests/{stem}_test{ext}",
            f"{parent}/test_{stem}{ext}",
            f"{parent}/{stem}_test{ext}",
            f"{parent}/tests/test_{stem}{ext}",
            # JS/TS patterns
            f"{stem}.test{ext}",
            f"{stem}.spec{ext}",
            f"__tests__/{stem}{ext}",
            f"{parent}/__tests__/{stem}{ext}",
            f"{parent}/{stem}.test{ext}",
            f"{parent}/{stem}.spec{ext}",
        ]
        for c in candidates:
            if _measure_facade.Path(c).exists() and c not in modified:
                test_companions.append({"source": f, "test": c})
                break

    # 3. Co-change analysis from last 50 commits
    co_changed = {}
    log_output = _run_git("log", "--oneline", "--name-only", "-50", "--pretty=format:")
    if log_output:
        commits = log_output.split("\n\n")
        for commit_files_str in commits:
            commit_files = [cf.strip() for cf in commit_files_str.splitlines() if cf.strip()]
            for mf in modified:
                if mf in commit_files:
                    for cf in commit_files:
                        if cf != mf and cf not in modified:
                            co_changed[cf] = co_changed.get(cf, 0) + 1
    # Top 10 co-changed files, sorted by frequency
    top_co = sorted(co_changed.items(), key=lambda x: ((_measure_facade := _compat.facade()), -x[1])[1])[:10]

    # 4. Import chain for Python/JS modified files
    import_chain = []
    for f in sorted(modified):
        if not _measure_facade.Path(f).exists():
            continue
        ext = _measure_facade.Path(f).suffix
        if ext not in (".py", ".js", ".ts", ".jsx", ".tsx"):
            continue
        try:
            content = _measure_facade.Path(f).read_text(encoding="utf-8", errors="ignore")[:5000]
        except OSError:
            continue
        imports = []
        for line in content.splitlines():
            line = line.strip()
            if ext == ".py":
                if line.startswith("from ") and " import " in line:
                    mod = line.split("from ")[1].split(" import")[0].strip()
                    if mod.startswith("."):
                        # Relative import, resolve to file
                        rel = mod.lstrip(".")
                        candidate = str(_measure_facade.Path(f).parent / rel.replace(".", "/")) + ".py"
                        if _measure_facade.Path(candidate).exists() and candidate not in modified:
                            imports.append(candidate)
                elif line.startswith("import "):
                    mod = line.split("import ")[1].split(" as")[0].split(",")[0].strip()
                    if "." in mod:
                        candidate = mod.replace(".", "/") + ".py"
                        if _measure_facade.Path(candidate).exists() and candidate not in modified:
                            imports.append(candidate)
            else:
                # JS/TS imports
                if "from " in line and ("import " in line or "require(" in line):
                    # Extract path from quotes
                    for q in ('"', "'"):
                        if q in line:
                            parts = line.split(q)
                            if len(parts) >= 2:
                                imp_path = parts[1]
                                if imp_path.startswith("."):
                                    base_dir = str(_measure_facade.Path(f).parent)
                                    for try_ext in ("", ".ts", ".tsx", ".js", ".jsx"):
                                        candidate = str(_measure_facade.Path(base_dir) / imp_path) + try_ext
                                        if _measure_facade.Path(candidate).exists() and candidate not in modified:
                                            imports.append(candidate)
                                            break
                                break
        if imports:
            import_chain.append({"source": f, "imports": imports[:5]})

    result = {
        "modified": sorted(modified),
        "test_companions": test_companions,
        "co_changed": [{"file": f, "times": n} for f, n in top_co],
        "import_chain": import_chain,
    }

    if as_json:
        print(_measure_facade.json.dumps(result, indent=2))
        return result

    # Pretty print
    print("\n  GIT CONTEXT SUGGESTIONS")
    print(f"  {'=' * 40}")
    print(f"  Modified files ({len(modified)}):")
    for f in sorted(modified):
        print(f"    {f}")

    if test_companions:
        print("\n  Test companions (add to context):")
        for tc in test_companions:
            print(f"    {tc['test']}  (tests {tc['source']})")

    if top_co:
        print("\n  Frequently co-changed (consider adding):")
        for f, n in top_co:
            print(f"    {f}  ({n}x in last 50 commits)")

    if import_chain:
        print("\n  Import chain (dependencies):")
        for ic in import_chain:
            print(f"    {ic['source']} imports:")
            for imp in ic["imports"]:
                print(f"      {imp}")

    total_suggestions = len(test_companions) + len(top_co) + sum(len(ic["imports"]) for ic in import_chain)
    if total_suggestions > 0:
        print(f"\n  Total: {total_suggestions} suggested files to add to context")
    else:
        print("\n  No additional context suggestions. Modified files are self-contained.")
    print()
    return result


def drift_check(as_json=None):
    """Compare current state against most recent auto-snapshot for drift detection."""
    _measure_facade = _compat.facade()
    snap_dir = _measure_facade.SNAPSHOT_DIR / "auto-snapshots"
    if not snap_dir.exists():
        if as_json:
            print(_measure_facade.json.dumps({"error": "No snapshots found. Run 'quick' first to create a baseline."}))
        else:
            print(_measure_facade._hint("\n  No snapshots found. Run 'python3 measure.py quick' first to create a baseline."))
        return

    snaps = sorted(snap_dir.glob("snap_*.json"), key=lambda f: ((_measure_facade := _compat.facade()), f.stat().st_mtime)[1])
    if not snaps:
        if as_json:
            print(_measure_facade.json.dumps({"error": "No snapshots found. Run 'quick' first to create a baseline."}))
        else:
            print(_measure_facade._hint("\n  No snapshots found. Run 'python3 measure.py quick' first to create a baseline."))
        return

    # Load most recent snapshot (baseline)
    baseline_path = snaps[-1]
    try:
        with open(baseline_path, "r", encoding="utf-8") as f:
            baseline = _measure_facade.json.load(f)
    except (_measure_facade.json.JSONDecodeError, OSError):
        print("[Error] Could not read baseline snapshot.")
        return

    # Measure current
    components = _measure_facade.measure_components()
    totals = _measure_facade.calculate_totals(components)
    ctx_window = _measure_facade.detect_context_window()[0]

    current = {
        "total_overhead": totals["estimated_total"],
        "skill_count": components.get("skills", {}).get("count", 0),
        "skill_tokens": components.get("skills", {}).get("tokens", 0),
        "mcp_server_count": components.get("mcp_tools", {}).get("count", 0),
        "mcp_tokens": components.get("mcp_tools", {}).get("tokens", 0),
        "claude_md_tokens": sum(
            components[k].get("tokens", 0)
            for k in components if k.startswith("claude_md") and components[k].get("exists")
        ),
        "memory_md_tokens": components.get("memory_md", {}).get("tokens", 0),
    }

    # Calculate deltas
    b_overhead = baseline.get("total_overhead", 0)
    c_overhead = current["total_overhead"]
    delta_overhead = c_overhead - b_overhead
    delta_pct = (delta_overhead / b_overhead * 100) if b_overhead > 0 else 0

    b_skills = baseline.get("skill_count", 0)
    c_skills = current["skill_count"]
    b_skill_tok = baseline.get("skill_tokens", 0)
    c_skill_tok = current["skill_tokens"]

    b_claude = baseline.get("claude_md_tokens", 0)
    c_claude = current["claude_md_tokens"]

    b_mcp = baseline.get("mcp_server_count", 0)
    c_mcp = current["mcp_server_count"]
    b_mcp_tok = baseline.get("mcp_tokens", 0)
    c_mcp_tok = current["mcp_tokens"]

    # Baseline date
    base_ts = baseline.get("timestamp", "")
    try:
        base_dt = _measure_facade.datetime.fromisoformat(base_ts)
        days_ago = (_measure_facade.datetime.now() - base_dt).days
        date_str = f"{base_ts[:10]}, {days_ago} day{'s' if days_ago != 1 else ''} ago"
    except (ValueError, TypeError):
        date_str = base_ts[:10] if base_ts else "unknown"

    # Impact on degradation
    peak_zone = int(ctx_window * 0.50)
    b_peak_usable = max(0, peak_zone - b_overhead)
    c_peak_usable = max(0, peak_zone - c_overhead)
    peak_delta = c_peak_usable - b_peak_usable

    if as_json:
        result = {
            "baseline_date": base_ts,
            "baseline_overhead": b_overhead,
            "current_overhead": c_overhead,
            "delta_tokens": delta_overhead,
            "delta_pct": round(delta_pct, 1),
            "skills": {"before": b_skills, "after": c_skills, "delta_tokens": c_skill_tok - b_skill_tok},
            "claude_md": {"before": b_claude, "after": c_claude, "delta_tokens": c_claude - b_claude},
            "mcp": {"before": b_mcp, "after": c_mcp, "delta_tokens": c_mcp_tok - b_mcp_tok},
            "peak_zone_impact": peak_delta,
        }
        print(_measure_facade.json.dumps(result, indent=2))
        return result

    # Pretty print
    print(f"\nDRIFT REPORT (vs {date_str})")
    print(f"{'=' * 45}")
    print(f"  Total overhead:     {b_overhead:,} -> {c_overhead:,}  ({delta_overhead:+,} tokens, {delta_pct:+.1f}%)")
    if c_skills != b_skills or c_skill_tok != b_skill_tok:
        print(f"    Skills:           {b_skills} -> {c_skills}  ({c_skill_tok - b_skill_tok:+,} tokens)")
    if c_claude != b_claude:
        delta_claude_pct = ((c_claude - b_claude) / b_claude * 100) if b_claude > 0 else 0
        print(f"    CLAUDE.md:        {b_claude:,} -> {c_claude:,}  ({c_claude - b_claude:+,} tokens, {delta_claude_pct:+.0f}%)")
    if c_mcp != b_mcp or c_mcp_tok != b_mcp_tok:
        print(f"    MCP servers:      {b_mcp} -> {c_mcp}  ({c_mcp_tok - b_mcp_tok:+,} tokens)")

    if abs(delta_overhead) > 500:
        print(f"\n  Impact: Peak quality zone {'shrunk' if delta_overhead > 0 else 'grew'} by ~{abs(peak_delta):,} tokens.")
        if delta_overhead > 0:
            msgs_lost = abs(peak_delta) // 5000
            if msgs_lost > 0:
                print(f"          You'll hit degradation ~{msgs_lost} message{'s' if msgs_lost != 1 else ''} sooner per session.")
    else:
        print("\n  No significant drift. Your setup is stable.")

    print("\n  Run /token-optimizer to fix.")
    print()

    # Auto-save new snapshot
    _measure_facade._auto_snapshot(components, totals, ctx_window)


def detect_calibration_gap(components, totals, baselines=None):
    """Compare estimated total against real session baselines. Returns gap info."""
    _measure_facade = _compat.facade()
    if baselines is None:
        baselines = _measure_facade.get_session_baselines(5)
    if not baselines:
        return {"has_data": False, "note": "No session baselines available for calibration."}
    avg_real = sum(b["baseline_tokens"] for b in baselines) / len(baselines)
    estimated = totals["estimated_total"]
    gap = avg_real - estimated
    gap_pct = (gap / estimated * 100) if estimated > 0 else 0
    return {
        "has_data": True,
        "avg_real_baseline": int(avg_real),
        "estimated_total": estimated,
        "gap_tokens": int(gap),
        "gap_pct": round(gap_pct, 1),
        "sessions_sampled": len(baselines),
        "significant": abs(gap_pct) > 15,
    }


def sanitize_label(label):
    """Sanitize snapshot label to prevent path traversal."""
    _measure_facade = _compat.facade()
    if not _measure_facade.re.match(r'^[a-zA-Z0-9_-]+$', label):
        print("[Error] Snapshot label must contain only letters, numbers, hyphens, underscores.")
        _measure_facade.sys.exit(1)
    return label


def _build_snapshot(label):
    """Assemble a snapshot dict (no I/O). Shared by take_snapshot + auto-capture."""
    _measure_facade = _compat.facade()
    components = _measure_facade.measure_components()
    baselines = _measure_facade.get_session_baselines(5)
    totals = _measure_facade.calculate_totals(components)
    calibration = _measure_facade.detect_calibration_gap(components, totals, baselines)
    return {
        "label": label,
        "timestamp": _measure_facade.datetime.now().isoformat(),
        "components": components,
        "session_baselines": baselines,
        "totals": totals,
        "calibration": calibration,
        "context_window": _measure_facade.detect_context_window()[0],
        # Install-time model mix, so realized model-routing savings can be
        # measured later against where the user started (e.g. mostly-Opus).
        "model_mix": dict(_measure_facade._model_mix_shares(days=14), activity_basis=1),
    }


def take_snapshot(label):
    """Save a measurement snapshot (before or after)."""
    _measure_facade = _compat.facade()
    label = _measure_facade.sanitize_label(label)

    try:
        _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        _measure_facade.os.chmod(str(_measure_facade.SNAPSHOT_DIR), 0o700)
    except OSError as e:
        print(f"[Error] Cannot create snapshot directory: {e}")
        _measure_facade.sys.exit(1)

    snapshot = _measure_facade._build_snapshot(label)

    filepath = _measure_facade.SNAPSHOT_DIR / f"snapshot_{label}.json"
    if filepath.exists():
        print(f"  [Note] Overwriting existing snapshot '{label}'")
    fd = _measure_facade.os.open(str(filepath), _measure_facade.os.O_WRONLY | _measure_facade.os.O_CREAT | _measure_facade.os.O_TRUNC, 0o600)
    with _measure_facade.os.fdopen(fd, "w", encoding="utf-8") as f:
        _measure_facade.json.dump(snapshot, f, indent=2, default=str)

    print(f"\n[Token Optimizer] Snapshot '{label}' saved to {filepath}")
    print("  [Note] Snapshot contains system config details. Do not share publicly.")
    _measure_facade.print_snapshot_summary(snapshot)
    return snapshot


def _auto_capture_pristine_baseline():
    """Capture a 'before' baseline snapshot once, silently, if none exists.

    Called from ensure-health at SessionStart. On a fresh install the user's
    config is still pre-pruning, so this records the pristine prefix overhead
    that structural savings are later measured against. Without it, structural
    savings have no anchor and read $0 forever. Idempotent (no-op once
    snapshot_before.json exists) and never raises. Returns True on capture.
    """
    _measure_facade = _compat.facade()
    try:
        filepath = _measure_facade.SNAPSHOT_DIR / "snapshot_before.json"
        if filepath.exists():
            return False
        try:
            _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            _measure_facade.os.chmod(str(_measure_facade.SNAPSHOT_DIR), 0o700)
        except OSError:
            return False
        snapshot = _measure_facade._build_snapshot("before")
        if int(snapshot.get("totals", {}).get("estimated_total", 0) or 0) <= 0:
            return False
        fd = _measure_facade.os.open(str(filepath), _measure_facade.os.O_WRONLY | _measure_facade.os.O_CREAT | _measure_facade.os.O_TRUNC, 0o600)
        with _measure_facade.os.fdopen(fd, "w", encoding="utf-8") as f:
            _measure_facade.json.dump(snapshot, f, indent=2, default=str)
        return True
    except Exception:
        return False


def print_snapshot_summary(snapshot):
    """Print a human-readable summary of a snapshot."""
    _measure_facade = _compat.facade()
    c = snapshot["components"]
    t = snapshot["totals"]
    is_codex = any(key.startswith("agents_md") for key in c)

    print(f"\n{'=' * 55}")
    print(f"  Snapshot: {snapshot['label']} ({snapshot['timestamp'][:16]})")
    print(f"{'=' * 55}")

    # Primary instruction files
    instruction_prefix = "agents_md" if is_codex else "claude_md"
    instruction_label = "AGENTS.md" if is_codex else "CLAUDE.md"
    instruction_total = 0
    for key in c:
        if key.startswith(instruction_prefix):
            tokens = c[key].get("tokens", 0)
            if tokens > 0:
                instruction_total += tokens
                lines = c[key].get("lines", 0)
                print(f"  {key:<35s} {tokens:>6,} tokens  [{lines} lines]")
    if instruction_total == 0:
        print(f"  {instruction_label:<35s}     0 tokens  [not found]")

    # Memory
    if "memory_md" in c:
        mem = c["memory_md"]
        memory_label = "Codex memories (all threads)" if is_codex else "MEMORY.md"
        if is_codex and mem.get("truncated"):
            memory_label = "Codex memories (all threads, partial)"
        _mem_unit = (f"{mem.get('thread_count', 0)} threads" if is_codex
                     else f"{mem.get('lines', 0)} lines")
        print(f"  {memory_label:<35s} {mem.get('tokens', 0):>6,} tokens  [{_mem_unit}]")

    # Skills
    s = c.get("skills", {})
    print(f"  {'Skills (frontmatter)':<35s} {s.get('tokens', 0):>6,} tokens  [{s.get('count', 0)} skills]")
    ps = c.get("plugin_skills", {})
    if ps.get("count", 0) > 0:
        disabled = ps.get("disabled_plugins", [])
        suffix = f", {len(disabled)} disabled" if disabled else ""
        plugins = []
        for plugin in ps.get("plugins", []):
            if isinstance(plugin, str):
                plugins.append(plugin)
            elif isinstance(plugin, dict):
                name = plugin.get("name")
                if name:
                    plugins.append(str(name))
        per_plugin = ps.get("per_plugin") or {}
        if per_plugin:
            plugin_label = ", ".join(f"{n} {name}" for name, n in sorted(per_plugin.items()))
            print(f"    {'+ Plugin skills':<33s} {ps.get('tokens', 0):>6,} tokens  [{ps.get('count', 0)}: {plugin_label}{suffix}]")
        else:
            plugin_label = ", ".join(plugins) if plugins else "plugins"
            print(f"    {'+ Plugin skills':<33s} {ps.get('tokens', 0):>6,} tokens  [{ps.get('count', 0)} from {plugin_label}{suffix}]")
        dupes = ps.get("duplicate_skills", {})
        if dupes:
            dupe_count = sum(len(v) - 1 for v in dupes.values())
            print(f"    {'  ⚠ Duplicate skills':<33s}          [{dupe_count} extra copies from worktrees/stale installs]")
        suspicious = ps.get("suspicious_paths", [])
        if suspicious:
            reasons = set(s["reason"] for s in suspicious)
            print(f"    {'  ⚠ Suspicious paths':<33s}          [plugins loaded from: {', '.join(reasons)}]")

    # Commands
    cmd = c.get("commands", {})
    print(f"  {'Commands (frontmatter)':<35s} {cmd.get('tokens', 0):>6,} tokens  [{cmd.get('count', 0)} commands]")
    pc = c.get("plugin_commands", {})
    if pc.get("count", 0) > 0:
        print(f"    {'+ Plugin commands':<33s} {pc.get('tokens', 0):>6,} tokens  [{pc.get('count', 0)} from plugins]")

    # MCP
    mcp = c.get("mcp_tools", {})
    mcp_tokens = mcp.get("tokens", 0)
    srv_count = mcp.get("server_count", 0)
    tool_est = mcp.get("tool_count_estimate", 0)
    loading_mode = mcp.get("loading_mode", "deferred")
    mcp_label = f"MCP tools ({loading_mode}, tool surface)"
    disabled_proj = mcp.get("disabled_for_project") or []
    disabled_suffix = f", {len(disabled_proj)} disabled for this project" if disabled_proj else ""
    pending_appr = mcp.get("pending_project_approval") or []
    pending_suffix = f", {len(pending_appr)} pending approval" if pending_appr else ""
    print(f"  {mcp_label:<35s} {mcp_tokens:>6,} tokens  [{srv_count} servers, ~{tool_est} tools{disabled_suffix}{pending_suffix}]")
    connectors = mcp.get("account_connectors") or {}
    if connectors.get("status") == "NOT MEASURED":
        print(f"    {'+ Account connectors':<33s} {'NOT MEASURED':>12s}  [server-side at claude.ai, no local config]")
    instructions = mcp.get("server_instructions") or {}
    if instructions.get("status") == "NOT MEASURED" and srv_count > 0:
        print(f"    {'+ Server instructions':<33s} {'NOT MEASURED':>12s}  [runtime-provided, capped 2 KB each, no local copy]")

    # Rules
    rules = c.get("rules", {})
    if rules.get("count", 0) > 0:
        print(f"  {'Rules (.claude/rules/)':<35s} {rules.get('tokens', 0):>6,} tokens  [{rules.get('count', 0)} files, {rules.get('always_loaded', 0)} always-loaded]")

    # @imports
    imports = c.get("imports", {})
    if imports.get("count", 0) > 0:
        print(f"  {'@imports in CLAUDE.md':<35s} {imports.get('tokens', 0):>6,} tokens  [{imports.get('count', 0)} imports]")

    # CLAUDE.local.md
    cl = c.get("claude_local_md", {})
    if cl.get("exists"):
        print(f"  {'CLAUDE.local.md':<35s} {cl.get('tokens', 0):>6,} tokens  [{cl.get('lines', 0)} lines]")

    # Core
    core = c.get("core_system", {})
    # core_system is a desktop-measured constant (12,900 = system prompt +
    # built-in tools, measure.py's _measure_components). In Cowork the real fixed
    # overhead (platform system prompt + deferred MCP tool catalog + ~40 platform
    # skills) is far larger and INVISIBLE to a hook -- there is NO documented way
    # to read the session's own token usage from inside a Cowork session
    # (docs-grounding.md §5: only the external Sessions/Compliance API or opt-in
    # OTel expose it). So we do NOT fabricate a Cowork figure; we label the one we
    # print as desktop-scoped and add an explicit caveat below.
    _cowork = _measure_facade.is_cowork()
    core_label = "Core system (desktop est.)" if _cowork else "Core system (fixed)"
    print(f"  {core_label:<35s} {core.get('tokens', 0):>6,} tokens")

    print(f"  {'=' * 53}")
    # Minimal/external snapshots may carry only `total_overhead`.
    est_total = t.get("estimated_total", t.get("total_overhead", 0))
    total_label = "LOCAL FOOTPRINT (measurable)" if _cowork else "ESTIMATED TOTAL"
    print(f"  {total_label:<35s} {est_total:>6,} tokens")
    ctx_window, ctx_source = _measure_facade.detect_context_window()
    ctx_label = _measure_facade._fmt_context_window(ctx_window)
    pct_of_ctx = est_total / ctx_window * 100 if ctx_window else 0
    if _cowork:
        # The denominator (context window) is real, but the numerator is ONLY the
        # locally-controllable slice: platform overhead is not counted, so this %
        # is a floor, not the whole picture. Say so plainly rather than present
        # "1.3% of window" as if TO could see everything.
        print(f"  {'Local footprint':<35s} {pct_of_ctx:>5.1f}% of {ctx_label} window (controllable slice only)")
        print("  NOTE (Cowork): platform overhead -- the Cowork system prompt, the")
        print("  deferred MCP tool catalog, and the ~40 platform skills -- is NOT")
        print("  measurable from inside a Cowork session, so the real context used")
        print("  is HIGHER than the local footprint above. Token Optimizer reports")
        print("  and optimizes only the slice it can see and control (CLAUDE.md,")
        print("  your skills/commands/MCP config); the platform-managed remainder")
        print("  is not readable by a hook (Sessions/Compliance API or OTel only).")
    else:
        print(f"  {'Context used before typing':<35s} {pct_of_ctx:>5.1f}% of {ctx_label} window")

    # Session baselines
    baselines = snapshot.get("session_baselines", [])
    if baselines:
        avg = sum(b["baseline_tokens"] for b in baselines) / len(baselines)
        print(f"\n  Real session baseline (avg of {len(baselines)}): {avg:,.0f} tokens")
        print("  (includes system reminders, conversation history, etc.)")

    # Extras
    exclusion = c.get("file_exclusion", {})
    hooks = c.get("hooks", {})
    g_rules = len(exclusion.get("global_deny_rules", []))
    p_rules = len(exclusion.get("project_deny_rules", []))
    total_rules = g_rules + p_rules
    excl_str = f"{total_rules} deny rules" if total_rules else "NONE"
    if total_rules:
        parts = []
        if g_rules:
            parts.append(f"{g_rules} global")
        if p_rules:
            parts.append(f"{p_rules} project")
        excl_str = f"{total_rules} deny rules ({', '.join(parts)})"
    print(f"\n  File exclusion rules: {excl_str}")
    print(f"  Hooks: {', '.join(hooks.get('names', [])) if hooks.get('configured') else 'NONE'}")
    for hw in hooks.get("warnings", []):
        print(f"    WARNING: {hw}")

    # Settings env vars
    settings_env = c.get("settings_env", {})
    found_vars = settings_env.get("found", {})
    if found_vars:
        print(f"  Settings env vars: {', '.join(f'{k}={v}' for k, v in found_vars.items())}")

    # Settings local
    settings_local = c.get("settings_local", {})
    if settings_local.get("exists"):
        print("  settings.local.json: Found")

    # Verbose skill descriptions
    quality = c.get("skill_frontmatter_quality", {})
    all_verbose = quality.get("verbose_skills", [])
    truncated_descs = [s for s in all_verbose if s.get("truncated")]
    verbose_descs = [s for s in all_verbose if not s.get("truncated")]
    if truncated_descs:
        # Skill names come from SKILL.md frontmatter — attacker-influenceable.
        names = [_measure_facade._strip_ansi(str(s["name"])) for s in truncated_descs]
        print(f"  TRUNCATED skill descriptions (>1,536 chars): {len(truncated_descs)} ({', '.join(names[:5])}{'...' if len(truncated_descs) > 5 else ''})")
    if verbose_descs:
        names = [_measure_facade._strip_ansi(str(s["name"])) for s in verbose_descs]
        print(f"  Verbose skill descriptions (>200 chars): {len(verbose_descs)} ({', '.join(names[:5])}{'...' if len(verbose_descs) > 5 else ''})")

    # Calibration gap
    cal = snapshot.get("calibration", {})
    if cal.get("significant"):
        print(f"\n  Calibration gap: estimated {t['estimated_total']:,} vs real {cal['avg_real_baseline']:,} ({cal['gap_pct']:+.0f}%)")
        print(f"  (Based on {cal['sessions_sampled']} recent sessions. Gap likely from unmeasured system overhead.)")


def compare_snapshots():
    """Compare before and after snapshots."""
    _measure_facade = _compat.facade()
    before_path = _measure_facade.SNAPSHOT_DIR / "snapshot_before.json"
    after_path = _measure_facade.SNAPSHOT_DIR / "snapshot_after.json"

    if not before_path.exists():
        print(_measure_facade._hint("\n[Error] No 'before' snapshot found. Run: python3 measure.py snapshot before"))
        return

    if not after_path.exists():
        print(_measure_facade._hint("\n[Error] No 'after' snapshot found. Run: python3 measure.py snapshot after"))
        return

    try:
        with open(before_path, "r", encoding="utf-8") as f:
            before = _measure_facade.json.load(f)
    except (_measure_facade.json.JSONDecodeError, OSError) as e:
        print(f"\n[Error] Cannot read 'before' snapshot: {e}")
        print(_measure_facade._hint("  Re-run: python3 measure.py snapshot before"))
        return

    try:
        with open(after_path, "r", encoding="utf-8") as f:
            after = _measure_facade.json.load(f)
    except (_measure_facade.json.JSONDecodeError, OSError) as e:
        print(f"\n[Error] Cannot read 'after' snapshot: {e}")
        print(_measure_facade._hint("  Re-run: python3 measure.py snapshot after"))
        return

    # Warn if 'before' snapshot is stale (>24h old)
    try:
        before_ts = _measure_facade.datetime.fromisoformat(before["timestamp"])
        age_seconds = (_measure_facade.datetime.now() - before_ts).total_seconds()
        if age_seconds > 86400:
            age_days = int(age_seconds / 86400)
            print(f"\n  [Warning] 'before' snapshot is {age_days}d old. Consider re-taking it.")
    except (KeyError, ValueError):
        pass  # Missing or unparseable timestamp, not critical

    bc = before["components"]
    ac = after["components"]

    print(f"\n{'=' * 65}")
    print("  TOKEN OPTIMIZER - BEFORE vs AFTER")
    print(f"  Before: {before['timestamp'][:16]}")
    print(f"  After:  {after['timestamp'][:16]}")
    print(f"{'=' * 65}")

    print(f"\n  {'Component':<25s} {'Before':>8s} {'After':>8s} {'Saved':>8s} {'%':>6s}")
    print(f"  {'-' * 57}")

    rows = []

    # CLAUDE.md total
    before_claude = sum(
        bc[k].get("tokens", 0) for k in bc if k.startswith("claude_md")
    )
    after_claude = sum(
        ac[k].get("tokens", 0) for k in ac if k.startswith("claude_md")
    )
    rows.append(("CLAUDE.md (all)", before_claude, after_claude))

    # MEMORY.md
    _mem_partial = bool(bc.get("memory_md", {}).get("truncated") or ac.get("memory_md", {}).get("truncated"))
    _mem_all = "all_threads" in (bc.get("memory_md", {}).get("scope"), ac.get("memory_md", {}).get("scope"))
    _mem_row = "Codex memories (all threads)" if _mem_all else "MEMORY.md"
    if _mem_partial:
        _mem_row = _mem_row[:-1] + ", partial)" if _mem_all else "MEMORY.md (partial)"
    rows.append((
        _mem_row,
        bc.get("memory_md", {}).get("tokens", 0),
        ac.get("memory_md", {}).get("tokens", 0),
    ))

    # Skills (user + plugin)
    rows.append((
        "Skills",
        bc.get("skills", {}).get("tokens", 0) + bc.get("plugin_skills", {}).get("tokens", 0),
        ac.get("skills", {}).get("tokens", 0) + ac.get("plugin_skills", {}).get("tokens", 0),
    ))

    # Commands (user + plugin)
    rows.append((
        "Commands",
        bc.get("commands", {}).get("tokens", 0) + bc.get("plugin_commands", {}).get("tokens", 0),
        ac.get("commands", {}).get("tokens", 0) + ac.get("plugin_commands", {}).get("tokens", 0),
    ))

    # MCP (now included!)
    rows.append((
        "MCP tools",
        bc.get("mcp_tools", bc.get("mcp_servers", {})).get("tokens", 0),
        ac.get("mcp_tools", ac.get("mcp_servers", {})).get("tokens", 0),
    ))

    # Rules
    rows.append((
        "Rules (.claude/rules/)",
        bc.get("rules", {}).get("tokens", 0),
        ac.get("rules", {}).get("tokens", 0),
    ))

    # @imports
    rows.append((
        "@imports",
        bc.get("imports", {}).get("tokens", 0),
        ac.get("imports", {}).get("tokens", 0),
    ))

    # CLAUDE.local.md
    rows.append((
        "CLAUDE.local.md",
        bc.get("claude_local_md", {}).get("tokens", 0),
        ac.get("claude_local_md", {}).get("tokens", 0),
    ))

    total_before = 0
    total_after = 0
    total_saved = 0

    for name, bv, av in rows:
        saved = bv - av
        pct = f"{saved / bv * 100:.0f}%" if bv > 0 else "-"
        total_before += bv
        total_after += av
        total_saved += saved
        print(f"  {name:<25s} {bv:>7,} {av:>7,} {saved:>+7,} {pct:>6s}")

    print(f"  {'-' * 57}")
    total_pct = f"{total_saved / total_before * 100:.0f}%" if total_before > 0 else "-"
    print(f"  {'CONTROLLABLE TOTAL':<25s} {total_before:>7,} {total_after:>7,} {total_saved:>+7,} {total_pct:>6s}")

    # Context budget impact (not dollar amounts)
    if total_saved > 0:
        ctx_window = _measure_facade.detect_context_window()[0]
        ctx_label = _measure_facade._fmt_context_window(ctx_window)
        before_pct = (total_before + 15000) / ctx_window * 100
        after_pct = (total_after + 15000) / ctx_window * 100
        print(f"\n  Context budget: {before_pct:.1f}% -> {after_pct:.1f}% of {ctx_label} window")
        print(f"  That's {total_saved:,} more tokens for actual work per message.")
        _measure_facade._log_savings_event("setup_optimization", total_saved, detail=f"compare: {total_saved} tokens reduced")

    # File exclusion and hooks changes
    b_excl = bc.get("file_exclusion", {})
    a_excl = ac.get("file_exclusion", {})
    b_deny = len(b_excl.get("global_deny_rules", [])) + len(b_excl.get("project_deny_rules", []))
    a_deny = len(a_excl.get("global_deny_rules", [])) + len(a_excl.get("project_deny_rules", []))
    print(f"\n  File exclusion: {b_deny or 'No'} deny rules -> {a_deny or 'No'} deny rules")
    bh = bc.get("hooks", {})
    ah = ac.get("hooks", {})
    print(f"  Hooks: {'None' if not bh.get('configured') else ', '.join(bh.get('names', []))} -> {'None' if not ah.get('configured') else ', '.join(ah.get('names', []))}")

    # Archived skills
    before_skills = set(bc.get("skills", {}).get("names", []))
    after_skills = set(ac.get("skills", {}).get("names", []))
    archived = before_skills - after_skills
    if archived:
        print(f"\n  Skills archived: {', '.join(sorted(archived))}")

    # Archived commands
    before_cmds = set(bc.get("commands", {}).get("names", []))
    after_cmds = set(ac.get("commands", {}).get("names", []))
    archived_cmds = before_cmds - after_cmds
    if archived_cmds:
        print(f"  Commands archived: {', '.join(sorted(archived_cmds))}")

    # Session baseline comparison (with honest caveat)
    bb = before.get("session_baselines", [])
    ab = after.get("session_baselines", [])
    if bb and ab:
        avg_before = sum(b["baseline_tokens"] for b in bb) / len(bb)
        avg_after = sum(b["baseline_tokens"] for b in ab) / len(ab)
        if abs(avg_before - avg_after) < 100:
            print(f"\n  Session baselines: {avg_before:,.0f} -> {avg_after:,.0f} tokens")
            print("  [Note] These are from the same recent sessions. Start new sessions")
            print("         after optimizing to see real baseline changes.")
        else:
            print(f"\n  Real session baseline: {avg_before:,.0f} -> {avg_after:,.0f} tokens")

    print(f"\n{'=' * 65}")


def full_report():
    """Print a standalone full report."""
    _measure_facade = _compat.facade()
    components = _measure_facade.measure_components()
    baselines = _measure_facade.get_session_baselines(10)
    totals = _measure_facade.calculate_totals(components)

    calibration = _measure_facade.detect_calibration_gap(components, totals, baselines)

    snapshot = {
        "label": "current",
        "timestamp": _measure_facade.datetime.now().isoformat(),
        "components": components,
        "session_baselines": baselines,
        "totals": totals,
        "calibration": calibration,
    }

    print(f"\n{'=' * 55}")
    print("  TOKEN OVERHEAD REPORT")
    print(f"{'=' * 55}")

    _measure_facade.print_snapshot_summary(snapshot)

    if baselines:
        print("\n  --- Recent Session Baselines (from JSONL logs) ---")
        for b in baselines:
            dt = b["date"][:16]
            print(f"    {dt}  {b['baseline_tokens']:>7,} tokens")

    print(f"\n{'=' * 55}")


def _generate_codex_auto_recommendations(components, trends=None, days=None):
    """Generate Codex-native recommendations.

    Keep this separate from the Claude rules so the dashboard never tells a
    Codex user to edit CLAUDE.md, Claude settings, or Anthropic-specific knobs.
    """
    _measure_facade = _compat.facade()
    quick = []
    medium = []
    deep = []
    habits = []

    agents_tokens = 0
    agents_lines = 0
    truncated = []
    for key, info in components.items():
        if key.startswith("agents_md") and info.get("exists"):
            agents_tokens += int(info.get("tokens", 0))
            agents_lines += int(info.get("lines", 0))
            if info.get("truncated"):
                truncated.append(_measure_facade.Path(info.get("path", "")).name)

    if truncated:
        quick.append(
            f"**Fix truncated Codex instructions ({', '.join(truncated)})**: "
            "Codex stops adding project instruction files after `project_doc_max_bytes` "
            "(32 KiB by default). Content past that cap is not reliable context. "
            "Move reference material into docs or skills and keep AGENTS.md focused on durable rules, "
            "commands, and repo-specific constraints."
        )
    if agents_tokens > 4500:
        quick.append(
            f"**Slim Codex AGENTS.md chain ({agents_tokens:,} tokens, {agents_lines} lines)**: "
            "Codex reads AGENTS.md before work starts, then the same instruction chain influences the session. "
            "Keep only rules that should affect every task. Move long workflows into Codex skills, "
            "repo docs, or nested AGENTS.override.md files close to the code they govern. "
            f"Target ~2,500-3,500 tokens for the always-loaded chain; ~{max(0, agents_tokens - 3500):,} tokens recoverable."
        )
    elif agents_tokens > 2500:
        medium.append(
            f"**Review Codex AGENTS.md density ({agents_tokens:,} tokens)**: "
            "The file is still healthy, but this is the best place to keep high-priority rules at the top and bottom "
            "of the instruction chain. Move rare procedures into linked docs or skills."
        )

    memory = components.get("memory_md", {})
    if memory.get("tokens", 0) > 3000:
        medium.append(
            f"**Review Codex memories ({memory.get('tokens', 0):,} tokens across {memory.get('thread_count', len(memory.get('files', [])))} threads on this machine{' - partial, scan cap reached' if memory.get('truncated') else ''})**: "
            "Codex memory guidance can enter developer instructions when memories are enabled. "
            "Delete stale rollout summaries, consolidate duplicate memories, and keep only reusable preferences or project facts. "
            "Use Codex's memory reset only when you intentionally want to clear all persisted local memory."
        )

    skills = components.get("skills", {})
    plugin_skills = components.get("plugin_skills", {})
    skill_count = int(skills.get("count", 0)) + int(plugin_skills.get("count", 0))
    skill_tokens = int(skills.get("tokens", 0)) + int(plugin_skills.get("tokens", 0))
    if skill_count > 80:
        medium.append(
            f"**Prune Codex skill/plugin surface ({skill_count} skills, ~{skill_tokens:,} metadata tokens)**: "
            "Codex skill bodies are loaded on demand, but names/descriptions still shape tool discovery before the first prompt. "
            "Disable plugins you rarely use in `~/.codex/config.toml`, and disable individual noisy skills with `[[skills.config]] enabled = false`. "
            "Start with plugin bundles outside your daily work; they are reversible."
        )
    verbose = components.get("skill_frontmatter_quality", {}).get("verbose_skills", [])
    # The shared scan's `truncated` flag marks >1,536-char descriptions — a
    # Claude Code listing behavior. Codex does not cut descriptions at that
    # limit (the coach's W6 invariant), so those entries are simply very long
    # descriptions here, not a silent-truncation claim.
    very_verbose = verbose
    if very_verbose:
        # Frontmatter names are attacker-influenceable — strip escapes.
        names = ", ".join(_measure_facade._strip_ansi(str(s["name"])) for s in very_verbose[:8])
        quick.append(
            f"**Tighten {len(very_verbose)} Codex skill descriptions (>200 chars)**: "
            f"{names}{'...' if len(very_verbose) > 8 else ''}. "
            "Descriptions should be trigger text, not documentation. Put instructions in the SKILL.md body so Codex reads them only when the skill is selected."
        )

    mcp = components.get("mcp_tools", {})
    mcp_servers = int(mcp.get("server_count", 0))
    if mcp_servers > 8:
        medium.append(
            f"**Audit Codex MCP servers ({mcp_servers} configured)**: "
            "Each MCP server can expand the active tool surface with names, descriptions, and schemas. "
            "Keep high-use servers connected, but disable duplicate or rarely used servers in `~/.codex/config.toml`. "
            "For servers with side effects, avoid enabling broad parallel tool calls unless the server is race-safe."
        )

    hooks = components.get("hooks", {})
    hook_names = set(hooks.get("names", []))
    if "Stop" not in hook_names:
        quick.append(
            "**Install the default Codex hooks for real data**: "
            "The aggressive default (max savings) enables SessionStart/UserPromptSubmit, Stop, plus silent PostToolUse archiving and context-intel, so Token Optimizer tracks prompt quality, loop signals, output bloat, dashboard refresh, and continuity. All hooks run silently (no visible Codex Desktop rows). "
            "Run `" + (f"{_measure_facade.hint_python()} skills/token-optimizer/scripts/measure.py --runtime codex"
                       if _measure_facade._windows_hints()
                       else "TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py")
            + " codex-install --project .`."
        )
    if "UserPromptSubmit" not in hook_names:
        medium.append(
            "**Re-run codex-install for the aggressive (max savings) default**: "
            "`codex-install --project .` now installs the aggressive profile by default: quality cache, loop/nudge timing, and silent PostToolUse archiving + context-intel. "
            "Use `--profile balanced` to drop per-tool archiving, or `--profile quiet` for Stop-only continuity."
        )
    if "PostToolUse" not in hook_names:
        deep.append(
            "**Add PostToolUse for exact output-bloat tracking**: "
            "`codex-install --project .` (the aggressive default) wires tool-output archiving and context-intel measurement, both silenced (no visible Codex rows). "
            "If you installed an older balanced/quiet profile, re-run codex-install to pick it up."
        )
    deep.append(
        "**Do not promise invisible Bash compression in Codex yet**: "
        "Codex PreToolUse can block commands, but current Codex docs/source say `updatedInput` is parsed and not supported. "
        "So true invisible command rewriting is not available today. Track Bash output bloat from JSONL/PostToolUse and keep compression as an experimental opt-in until Codex supports input rewriting."
    )

    habits.append(
        "**Use Codex status line/context remaining as the first compaction signal**: "
        "Codex logs real `model_context_window` and token counts. For long tasks compact well before the window fills (quality sags long before the limit), earlier when switching topics. "
        "Do not assume a 1M API window; trust the logged Codex window for the active session."
    )
    habits.append(
        "**Preserve prompt-cache stability**: "
        "OpenAI prompt caching rewards stable prefixes. Keep AGENTS.md, enabled plugins, MCP servers, and memory settings stable during a session so repeated turns can hit cached input. "
        "Batch related asks rather than drip-feeding many tiny prompts."
    )
    habits.append(
        "**Start fresh between unrelated Codex tasks**: "
        "Session continuity is valuable inside a task, but stale tool outputs and old plans hurt quality. Use a new thread or compact/checkpoint when the objective changes."
    )

    sections = []
    if quick:
        sections.append("## Quick Wins\n\n" + "\n\n".join(f"- [ ] {item}" for item in quick))
    if medium:
        sections.append("## Medium Effort\n\n" + "\n\n".join(f"- [ ] {item}" for item in medium))
    if deep:
        sections.append("## Deep Optimization\n\n" + "\n\n".join(f"- [ ] {item}" for item in deep))
    if habits:
        sections.append("## Behavioral Habits\n\n" + "\n\n".join(f"- [ ] {item}" for item in habits))

    plan_md = "\n\n".join(sections) if sections else ""
    total_count = len(quick) + len(medium) + len(deep) + len(habits)
    return plan_md, total_count


def _generate_foreign_auto_recommendations(components, trends=None, days=None):
    """Optimization plan for runtimes with no Claude/Codex instruction-file
    surface (cursor, antigravity, grok, opencode, copilot, hermes).

    The Claude rules (CLAUDE.md/MEMORY.md trimming, ~/.claude paths, Anthropic
    line guidance) must never reach these users, and we do not fabricate
    runtime-specific knobs we cannot verify. Only runtime-neutral advice is
    emitted; when no measurable surface exists, the plan says so plainly.
    """
    _measure_facade = _compat.facade()
    quick = []
    medium = []
    deep = []
    habits = []

    rt_label = _measure_facade.runtime_name_for_humans()

    skills = components.get("skills", {})
    if skills.get("count", 0) > 0 and skills.get("tokens", 0) > 0:
        medium.append(
            f"**Audit the installed skill surface ({skills['count']} skills, ~{skills['tokens']:,} metadata tokens)**: "
            f"Skill names and descriptions shape tool discovery before the first prompt. "
            f"Disable or remove skills you do not use via {rt_label}'s own configuration."
        )
    verbose = components.get("skill_frontmatter_quality", {}).get("verbose_skills", [])
    if verbose:
        names = ", ".join(_measure_facade._strip_ansi(str(s["name"])) for s in verbose[:8])
        medium.append(
            f"**Tighten {len(verbose)} verbose skill descriptions (>200 chars)**: "
            f"{names}{'...' if len(verbose) > 8 else ''}. "
            "Descriptions should be trigger text, not documentation; keep usage detail in the skill body."
        )

    mcp = components.get("mcp_tools", {})
    mcp_servers = int(mcp.get("server_count", 0) or 0)
    if mcp_servers > 8:
        medium.append(
            f"**Audit MCP servers ({mcp_servers} configured)**: "
            "Each server can expand the active tool surface with names, descriptions, and schemas. "
            f"Disable duplicate or rarely used servers in {rt_label}'s MCP configuration."
        )

    # Unused skills — same review shape as the Claude rule but with
    # runtime-neutral grep/archive wording and the runtime's own slim clause
    # (no name-only / disable-model-invocation claims outside Claude Code).
    if trends:
        never_used = [
            s for s in trends.get("skills", {}).get("never_used", [])
            if not _measure_facade._is_own_tool_skill(s)
        ]
        _si = components.get("skills", {})
        _actual_avg = (_si.get("tokens", 0) // max(_si.get("count", 1), 1)
                       if _si.get("count", 0) > 0 else _measure_facade.TOKENS_PER_SKILL_APPROX)
        installed_count = trends.get("skills", {}).get("installed_count", 0)
        if len(never_used) >= 5:
            overhead = len(never_used) * _actual_avg
            show_count = min(len(never_used), 8)
            skill_list = ", ".join(_measure_facade._strip_ansi(str(s)) for s in sorted(never_used)[:show_count])
            remaining = len(never_used) - show_count
            quick.append(
                f"**Review {show_count} skills not invoked in {days} days ({len(never_used)} of {installed_count})**: "
                f"Each installed skill can cost ~{_actual_avg} tokens in the startup listing, every session, whether you use it or not.\n"
                f"  Start with these: {skill_list}"
                + (f"\n  ({remaining} more will surface after you archive these and re-run.)" if remaining > 0 else "") +
                f"\n  For each skill, ask: do I use this? Is it seasonal? Does anything depend on it? "
                f"(grep for the skill name in {rt_label}'s instruction, rules, and skills files)"
                + _measure_facade._skill_slim_clause(_actual_avg, _measure_facade.detect_runtime()) +
                f"  Archive (harder step, for truly-dead skills) by moving the skill directory to a backup folder OUTSIDE {rt_label}'s skills directory. "
                f"Restore any skill by moving it back. "
                f"~{overhead:,} tokens recoverable across all {len(never_used)}."
            )
        elif len(never_used) >= 2:
            overhead = len(never_used) * _actual_avg
            skill_list = ", ".join(_measure_facade._strip_ansi(str(s)) for s in sorted(never_used))
            medium.append(
                f"**Review {len(never_used)} skills not invoked in {days} days**: "
                f"{skill_list}."
                + _measure_facade._skill_slim_clause(_actual_avg, _measure_facade.detect_runtime()) +
                f"  Or archive truly-dead skills by moving them to a backup folder OUTSIDE {rt_label}'s skills directory. "
                f"~{overhead:,} tokens recoverable."
            )

    habits.append(
        "**Scope sessions to one task**: Every runtime degrades as transcripts grow — "
        "start a fresh session between unrelated tasks rather than letting one "
        "conversation accumulate stale context."
    )
    habits.append(
        "**Keep the stable prompt prefix stable**: Prompt caching across runtimes "
        "rewards an unchanged instruction/tool prefix — avoid churning enabled "
        "skills, servers, or instructions mid-session."
    )

    if not (quick or medium or deep):
        quick.append(
            f"**No locally measurable startup overhead for {rt_label}**: "
            "This runtime does not expose a Claude/Codex-style instruction file or "
            "skill manifest to Token Optimizer, so there is nothing to trim. The "
            "habits below still apply."
        )

    sections = []
    if quick:
        sections.append("## Quick Wins\n\n" + "\n\n".join(f"- [ ] {item}" for item in quick))
    if medium:
        sections.append("## Medium Effort\n\n" + "\n\n".join(f"- [ ] {item}" for item in medium))
    if deep:
        sections.append("## Deep Optimization\n\n" + "\n\n".join(f"- [ ] {item}" for item in deep))
    if habits:
        sections.append("## Behavioral Habits\n\n" + "\n\n".join(f"- [ ] {item}" for item in habits))

    plan_md = "\n\n".join(sections) if sections else ""
    total_count = len(quick) + len(medium) + len(deep) + len(habits)
    return plan_md, total_count


def _is_own_tool_skill(name) -> bool:
    """True if `name` is one of Token Optimizer's own skills (never recommend cutting it)."""
    _measure_facade = _compat.facade()
    if not name:
        return False
    n = str(name).strip().lower()
    base = n.rsplit(":", 1)[-1].rsplit("/", 1)[-1]
    return (
        n in _measure_facade._OWN_TOOL_SKILLS
        or base in _measure_facade._OWN_TOOL_SKILLS
        or base.startswith("token-optimizer")
        or base.startswith("token-coach")
        or base.startswith("token-dashboard")
        or base.startswith("fleet-auditor")
        or base.startswith("resume-checkpoint")
    )


def _skill_slim_clause(avg_tokens, runtime):
    """Softer-than-archive options for an unused skill, inserted ABOVE the archive
    step. Keeps the skill installed while dropping its per-turn description cost.

    Returns a block that STARTS and ends with a newline so it drops cleanly
    between the skill list and the archive step (each call site therefore does
    NOT add its own leading newline). Claude gets the two REAL, distinct Claude
    Code levers -- do not conflate them:
      - skillOverrides "name-only": NAME stays visible to the model (still
        invocable by name), description dropped -> stops auto-triggering, saves
        the description tokens. The true "name-only" middle mode.
      - disable-model-invocation: true (frontmatter): removes the skill from the
        model's context ENTIRELY (name too); only the user invokes it via /name.
    Foreign runtimes have no per-skill toggle, so they get an honest note.
    """
    _measure_facade = _compat.facade()
    if runtime == "claude":
        return (
            f"\n  Softer than archiving -- two Claude Code levers keep the skill installed and recover "
            f"~{avg_tokens} tokens/skill per turn (Claude may already have dropped some descriptions past "
            f"its listing budget, so treat this as an upper bound):"
            f"\n    - Name-only (keep it discoverable): in the `/skills` menu press Space to cycle a skill to "
            f"`name-only` (writes `skillOverrides` in .claude/settings.local.json). The model still sees the "
            f"NAME so it can invoke it, but the description drops from context and it stops auto-triggering. "
            f"Best for skills you still want suggested by name."
            f"\n    - Fully hidden (user-only): set `disable-model-invocation: true` in the SKILL.md frontmatter "
            f"(or the `off` / `user-invocable-only` `skillOverrides` state). The model sees nothing; only you "
            f"invoke it via `/name`. Best for skills you alone ever trigger."
            f"\n  Both are Claude-Code-specific and survive `/compact`. Keep skills whose value is proactive "
            f"auto-triggering on the default.\n"
        )
    return (
        f"\n  Softer than archiving: this runtime ({runtime}) has no per-skill 'hide description' toggle. "
        f"The equivalent is converting a skill you only trigger yourself into a command/prompt primitive, or "
        f"fully disabling it. Keep skills you want auto-suggested as normal skills.\n"
    )


def generate_auto_recommendations(components, trends=None, days=None):
    """Generate rule-based optimization recommendations without any LLM.

    Produces a markdown plan string in the same format as the LLM-generated
    optimization plan, so the existing dashboard parsePlan() rendering works.

    Each recommendation includes nuanced, contextual guidance designed to be
    pasted into Claude Code as a prompt. The guidance tells the model WHAT to
    optimize, WHY it matters, and HOW to do it without losing important content.

    Returns (plan_markdown_string, recommendation_count).
    """
    _measure_facade = _compat.facade()
    _rec_rt = _measure_facade.detect_runtime()
    if _rec_rt == "codex":
        return _measure_facade._generate_codex_auto_recommendations(components, trends=trends, days=days)
    if _rec_rt != "claude":
        # Foreign runtimes (hermes, cursor, antigravity, grok, opencode,
        # copilot): the rules below emit Claude-only content — CLAUDE.md,
        # MEMORY.md, ~/.claude paths, Anthropic line guidance — so they get a
        # runtime-neutral plan instead of the Claude rule set.
        return _measure_facade._generate_foreign_auto_recommendations(components, trends=trends, days=days)

    quick = []
    medium = []
    deep = []
    habits = []

    # --- Rule 1: MEMORY.md over 200 lines ---
    mem = components.get("memory_md", {})
    mem_lines = mem.get("lines", 0)
    mem_tokens = mem.get("tokens", 0)
    if mem_lines > 200:
        excess = mem_lines - 200
        est_waste = int(excess * (mem_tokens / max(mem_lines, 1)))
        quick.append(
            f"**Trim MEMORY.md from {mem_lines} to under 200 lines**: "
            f"Claude auto-loads the first 200 lines of MEMORY.md every session. "
            f"Your file is {mem_lines} lines ({mem_tokens:,} tokens). The extra {excess} lines "
            f"are truncated from the visible context but their tokens are still counted toward your window.\n"
            f"  Review each entry and ask: is this still accurate? Is it actionable today? "
            f"Could it live in a topic-specific file (e.g., debugging.md, patterns.md) in the memory/ directory instead? "
            f"Entries to prioritize for removal: resolved issues, completed migrations, one-time setup notes, "
            f"and verbose implementation details that belong in reference files. "
            f"Preserve: active project context, recurring patterns, correction logs, and partner/relationship notes. "
            f"~{est_waste:,} tokens recoverable."
        )
    elif mem_lines > 150:
        quick.append(
            f"**MEMORY.md approaching 200-line limit ({mem_lines} lines)**: "
            f"Claude truncates MEMORY.md after 200 lines. You have {200 - mem_lines} lines of headroom. "
            f"Proactively move detailed notes to topic files in the memory/ directory. "
            f"Keep MEMORY.md as an index of high-signal, frequently-referenced items."
        )

    # --- Rule 2: CLAUDE.md too large (PER FILE) ---
    # PR #100 (danikdanik). measure_components() keys each ancestor CLAUDE.md as
    # its own component (claude_md_global, claude_md_home,
    # claude_md_project_<dir>), mirroring how Claude Code loads CLAUDE.md files
    # up the directory tree. Summing N files into one "Slim CLAUDE.md" number
    # is not actionable on any single file someone owns, and could fire on a
    # combined total when no individual file was over the threshold. Each file
    # is now evaluated and reported on its own.
    #
    # Savings math is ours, unchanged: Anthropic's documented 200-line guidance
    # with tokens-per-line derived from that file, plus the >200-line guard so a
    # token-dense but short file never gets a "slim to 200 lines" line with ~0
    # tokens recoverable.
    for key in sorted(components):
        if not key.startswith("claude_md") or not components[key].get("exists"):
            continue
        _c = components[key]
        claude_tokens = _c.get("tokens", 0)
        claude_lines = _c.get("lines", 0)
        _path = _c.get("path", "") or key
        _label = _path.replace(str(_measure_facade.HOME), "~") if _path else key
        if claude_tokens > 6000 and claude_lines > 200:
            tokens_per_line = claude_tokens / max(claude_lines, 1)
            target_tokens = int(200 * tokens_per_line)
            recoverable = max(0, claude_tokens - target_tokens)
            if recoverable > 0:
                quick.append(
                    f"**Slim {_label} ({claude_tokens:,} tokens, {claude_lines} lines; target under 200 lines)**: "
                    f"Everything in this CLAUDE.md loads every single message you send. "
                    f"Anthropic recommends under 200 lines per CLAUDE.md file (https://code.claude.com/docs/en/memory). "
                    f"The ~300 lines / ~4,500 tokens figure is an internal heuristic, not an Anthropic recommendation — "
                    f"Anthropic documents only the 200-line target, no token threshold.\n"
                    f"  Move to skills (loaded on-demand, ~100 tokens in menu): workflow guides, coding standards, "
                    f"deployment procedures, detailed templates. "
                    f"Move to reference files (zero cost until read): API docs, config examples, architecture notes. "
                    f"Keep in CLAUDE.md: identity/personality, critical behavioral rules, key file paths, "
                    f"and short pointers to skills and references. "
                    f"Don't delete content, reorganize it. A 2-line pointer to a skill costs 100x less than "
                    f"the same content inline. ~{recoverable:,} tokens recoverable (slimming to 200 lines at ~{tokens_per_line:.1f} tokens/line)."
                )
        elif claude_lines > 200 and claude_tokens > 5000:
            medium.append(
                f"**Consider slimming {_label} ({claude_lines} lines, {claude_tokens:,} tokens)**: "
                f"This CLAUDE.md is over Anthropic's documented 200-line guidance (https://code.claude.com/docs/en/memory) but not critically large. "
                f"Review for any sections that could become skills or reference files. "
                f"Focus on content that's only relevant for specific workflows."
            )

    # --- Rule 3: Unused skills (requires trends data) ---
    # Use actual measured avg if available, else fallback to constant
    _si = components.get("skills", {})
    _actual_avg = _si.get("tokens", 0) // max(_si.get("count", 1), 1) if _si.get("count", 0) > 0 else _measure_facade.TOKENS_PER_SKILL_APPROX
    # Harness-aware "slim it, don't delete it" clause inserted ABOVE the archive
    # step (see _skill_slim_clause). Codex is dispatched to
    # _generate_codex_auto_recommendations and never reaches here, so this only
    # shapes Claude / other-foreign-runtime advice.
    _runtime = _measure_facade.detect_runtime()
    if trends:
        # Never recommend cutting our own measurement skills.
        never_used = [
            s for s in trends.get("skills", {}).get("never_used", [])
            if not _measure_facade._is_own_tool_skill(s)
        ]
        installed_count = trends.get("skills", {}).get("installed_count", 0)
        if len(never_used) >= 5:
            overhead = len(never_used) * _actual_avg
            show_count = min(len(never_used), 8)
            skill_list = ", ".join(_measure_facade._strip_ansi(str(s)) for s in sorted(never_used)[:show_count])
            remaining = len(never_used) - show_count
            quick.append(
                f"**Review {show_count} skills not invoked in {days} days ({len(never_used)} of {installed_count}, counting Skill calls and slash commands)**: "
                f"Each installed skill costs ~{_actual_avg} tokens in the startup menu, every session, whether you use it or not.\n"
                f"  Start with these: {skill_list}"
                + (f"\n  ({remaining} more will surface after you archive these and re-run.)" if remaining > 0 else "") +
                f"\n  For each skill, ask: do I use this? Is it seasonal? Does anything depend on it? "
                f"(`grep -r \"[skill-name]\" ~/.claude/CLAUDE.md ~/.claude/rules/ ~/.claude/skills/`)"
                + _measure_facade._skill_slim_clause(_actual_avg, _runtime) +
                f"  Archive (harder step, for truly-dead skills) by moving to ~/.claude/_backups/skills-archived-$(date +%Y%m%d)/ (NOT inside skills/). "
                f"Restore any skill by moving it back. "
                f"~{overhead:,} tokens recoverable across all {len(never_used)}."
            )
        elif len(never_used) >= 2:
            overhead = len(never_used) * _actual_avg
            skill_list = ", ".join(_measure_facade._strip_ansi(str(s)) for s in sorted(never_used))
            medium.append(
                f"**Review {len(never_used)} skills not invoked in {days} days**: "
                f"No Skill call or slash command for these in {days} days: {skill_list}."
                + _measure_facade._skill_slim_clause(_actual_avg, _runtime) +
                f"  Or archive truly-dead skills to ~/.claude/_backups/skills-archived-$(date +%Y%m%d)/ (NOT inside skills/). "
                f"~{overhead:,} tokens recoverable."
            )

    # --- Rule 3a: Skills audit fallback (no trends data) ---
    skill_info = components.get("skills", {})
    skill_count = skill_info.get("count", 0)
    skill_tokens = skill_info.get("tokens", 0)
    avg_per_skill = skill_tokens // max(skill_count, 1)
    if not trends and skill_count > 10:
        est_archive = skill_count - 10
        est_savings = est_archive * avg_per_skill
        medium.append(
            f"**Review {skill_count} skills ({skill_tokens:,} tokens, no usage data)**: "
            f"You have {skill_count} skills but no session data to determine which are unused. "
            f"Each skill costs ~{avg_per_skill} tokens at startup whether you use it or not.\n"
            f"  Install the SessionEnd hook (`{_measure_facade._measure_cli()} setup-hook`) to enable usage-based "
            f"recommendations. Meanwhile, manually review: do you use all {skill_count} regularly?"
            + _measure_facade._skill_slim_clause(avg_per_skill, _runtime) +
            f"  Archiving {est_archive} (harder step, truly-dead skills) would free ~{est_savings:,} tokens/session. "
            f"~{est_savings:,} tokens recoverable."
        )

    # --- Rule 3b: Removed in v2.3.0 ---
    # Aggregate "skills consume N tokens" was not actionable. Specific rules (Rule 3
    # for unused skills, Rule 5 for verbose descriptions) give better guidance.

    # --- Rule 0: Removed in v2.3.0 ---
    # "Startup overhead is X%" just restated the bar chart with no specific action.
    # The bar chart + component cards already show this. Specific per-component
    # rules (CLAUDE.md, skills, commands, MCP) are the actionable counterparts.

    # --- Rule 4: Missing file exclusion rules ---
    exclusion = components.get("file_exclusion", {})
    if not exclusion.get("has_rules"):
        medium.append(
            "**Add file exclusion rules**: "
            "No permissions.deny rules found. Without them, Claude Code can read large "
            "or sensitive files, wasting tokens on irrelevant content. Apply at the "
            "project level first (.claude/settings.json in the repo), not global. "
            "Never deny *.sqlite or *.db globally because that breaks tools that read "
            "databases for session memory, search indexes, and WhatsApp. Credential "
            "denies like .env and *.key are usually safe and desired. "
            "Starter template to drop into .claude/settings.json at the project root: "
            '{ "permissions": { "deny": [ "Read(./.env)", "Read(./.env.*)", '
            '"Read(./build/**)", "Read(./dist/**)", "Read(./node_modules/**)", '
            '"Read(./**/*.log)", "Read(./**/*.key)", "Read(./**/*.pem)" ] } }. '
            "Tune the globs to match your project layout."
        )

    # --- Rule 5: Verbose skill descriptions ---
    quality = components.get("skill_frontmatter_quality", {})
    verbose = quality.get("verbose_skills", [])
    truncated = [s for s in verbose if s.get("truncated")]
    very_verbose = [s for s in verbose if not s.get("truncated")]
    moderate_verbose = [s for s in verbose if 120 < s.get("description_chars", 0) <= 200]
    if truncated:
        names = [_measure_facade._strip_ansi(str(s["name"])) for s in truncated[:10]]
        est_waste = sum(int((s["description_chars"] - _measure_facade._SKILL_DESC_TRUNCATION_LIMIT) / _measure_facade.CHARS_PER_TOKEN) for s in truncated)
        quick.append(
            f"**{len(truncated)} skill descriptions TRUNCATED by Claude Code (>1,536 chars)**: "
            f"{', '.join(names)}{'...' if len(truncated) > 10 else ''}. "
            f"Claude Code silently cuts combined description + when_to_use at 1,536 characters. "
            f"The overflow tokens load every session but Claude never sees them.\n"
            f"  Move detailed usage instructions into the SKILL.md body (loaded only when invoked). "
            f"~{est_waste:,} tokens wasted."
        )
    if very_verbose:
        names = [_measure_facade._strip_ansi(str(s["name"])) for s in very_verbose[:10]]
        est_waste = sum(int((s["description_chars"] - 80) / _measure_facade.CHARS_PER_TOKEN) for s in very_verbose)
        quick.append(
            f"**Tighten {len(very_verbose)} verbose skill descriptions (>200 chars)**: "
            f"{', '.join(names)}{'...' if len(very_verbose) > 10 else ''}. "
            f"Target: under 80 characters. The description field loads every session.\n"
            f"  Move detailed usage instructions into the SKILL.md body (loaded only when invoked). "
            f"~{est_waste:,} tokens recoverable."
        )
    if moderate_verbose:
        names = [_measure_facade._strip_ansi(str(s["name"])) for s in moderate_verbose[:10]]
        est_waste = sum(int((s["description_chars"] - 80) / _measure_facade.CHARS_PER_TOKEN) for s in moderate_verbose)
        medium.append(
            f"**Tighten {len(moderate_verbose)} verbose skill descriptions (120-200 chars, target 80)**: "
            f"{', '.join(names)}{'...' if len(moderate_verbose) > 10 else ''}. "
            f"The description field loads every session as part of the skill menu.\n"
            f"  Tighten each to under 80 characters while keeping the core trigger phrase. "
            f"~{est_waste:,} tokens recoverable."
        )

    # --- Rule 6: High command count ---
    cmds = components.get("commands", {})
    cmd_count = cmds.get("count", 0)
    cmd_tokens = cmds.get("tokens", 0)
    if cmd_count > 30:
        quick.append(
            f"**Archive unused commands ({cmd_count} commands, {cmd_tokens:,} tokens)**: "
            f"You have {cmd_count} custom commands. Each adds ~50 tokens to the command menu, every session. "
            f"Review the list and archive rarely-used commands to ~/.claude/commands/_archived/.\n"
            f"  Good archive candidates: one-time setup commands, project-specific commands for finished projects, "
            f"and commands superseded by skills. Keep: daily-use commands, automation triggers, "
            f"and anything referenced in hooks or scripts. "
            f"~{cmd_tokens:,} tokens recoverable."
        )
    elif cmd_count > 20:
        medium.append(
            f"**Review {cmd_count} commands ({cmd_tokens:,} tokens)**: "
            f"Consider archiving rarely-used commands to ~/.claude/commands/_archived/ to reduce menu overhead. "
            f"~{cmd_tokens:,} tokens recoverable."
        )

    # --- Rule 7: Model mix imbalance (requires trends) ---
    default_model = components.get("settings_local", {}).get("defaultModel")
    if trends:
        model_mix = trends.get("model_mix", {})
        total_tokens = sum(model_mix.values()) if model_mix else 0
        if total_tokens > 0:
            opus_pct = model_mix.get("opus", 0) / total_tokens * 100
            haiku_pct = model_mix.get("haiku", 0) / total_tokens * 100
            if opus_pct > 50 and haiku_pct < 15:
                # Root cause: hardcoded model in settings.json → split into Quick Win
                if (default_model and "opus" in str(default_model).lower()
                        and not _measure_facade._model_routing_handled_elsewhere()):
                    quick.append(
                        f"**Remove hardcoded model from settings.json (`\"model\": \"{default_model}\"`)**: "
                        f"This forces ALL operations to use {default_model}, overriding any CLAUDE.md routing. "
                        f"Subagents inherit this default even when Haiku would suffice.\n"
                        f"  Fix: open ~/.claude/settings.json, delete the `\"model\"` key entirely. "
                        f"Then add routing instructions to CLAUDE.md instead (see Behavioral Habits below). "
                        f"This one change lets Claude auto-select appropriate models per task."
                    )
                # Behavioral advice (always shown when mix is imbalanced)
                habits.append(
                    f"**Route subagents by task type ({opus_pct:.0f}% Opus, {haiku_pct:.0f}% Haiku)**: "
                    f"For data-gathering agents (file reads, counting, directory scans, grep searches), "
                    f"Haiku is 60x cheaper and often just as accurate.\n"
                    f"  Add to CLAUDE.md: 'Default subagents to model=\"haiku\" for data gathering, "
                    f"model=\"sonnet\" for analysis and judgment calls. Reserve model=\"opus\" for "
                    f"complex multi-step reasoning.' This doesn't save context tokens but significantly "
                    f"reduces cost and rate limit consumption."
                )

    # --- Rule 8: No SessionEnd hook (one-time setup → Quick Win, not habit) ---
    # v5.3.8: plugin users get SessionEnd via hooks/hooks.json -- the old
    # check only inspected settings.json, so plugin users always saw a
    # false-positive "no hook detected" recommendation contradicting
    # their actual installed state. Short-circuit when the plugin is
    # installed so we never recommend installing what is already there.
    hooks = components.get("hooks", {})
    hook_missing_in_settings = not hooks.get("configured") or "SessionEnd" not in hooks.get("names", [])
    if hook_missing_in_settings and not _measure_facade._is_plugin_installed():
        quick.append(
            "**Install SessionEnd hook for usage tracking**: "
            "No SessionEnd hook detected. One-time setup, takes 10 seconds. "
            f"Run `{_measure_facade._measure_cli()} setup-hook`. "
            "This enables the Trends tab (which skills you actually use, model mix, daily patterns) "
            "and the Health tab (stale sessions, version checks). Without it, you only get data "
            "from manual `measure.py collect` runs. The hook runs automatically after every session "
            "(~2 seconds, no background process)."
        )

    # --- Rule 9: Broken skill symlinks ---
    skills_dir = _measure_facade.CLAUDE_DIR / "skills"
    broken_links = []
    if skills_dir.exists():
        for item in skills_dir.iterdir():
            if item.is_symlink() and not item.exists():
                broken_links.append(item.name)
    if broken_links:
        quick.append(
            f"**Remove {len(broken_links)} broken skill symlinks**: "
            f"These skill directories are broken symlinks pointing to deleted targets: "
            f"{', '.join(broken_links)}. "
            f"Claude Code still tries to parse them at startup, generating errors. "
            f"Safe to delete: rm {' '.join(str(skills_dir / b) for b in broken_links)}"
        )

    # --- Rule 9b: Duplicate plugin skills (worktrees / node_modules) ---
    plugin_dupes = components.get("plugin_skills", {}).get("duplicate_skills", {})
    plugin_suspicious = components.get("plugin_skills", {}).get("suspicious_paths", [])
    if plugin_dupes:
        dupe_count = sum(len(v) - 1 for v in plugin_dupes.values())
        dupe_names = [_measure_facade._strip_ansi(str(n)) for n in plugin_dupes.keys()]
        # Estimate wasted tokens: each duplicate copy loads the same skill frontmatter again
        avg_tokens = _measure_facade.TOKENS_PER_SKILL_APPROX
        ps_data = components.get("plugin_skills", {})
        if ps_data.get("count", 0) > 0:
            avg_tokens = ps_data.get("tokens", 0) // ps_data.get("count", 1)
        wasted = dupe_count * avg_tokens
        paths_example = list(plugin_dupes.values())[0][:2]
        quick.append(
            f"**Remove {dupe_count} duplicate plugin skills (likely from worktrees)**: "
            f"These skills are loaded {len(paths_example)}+ times each because the plugin registry "
            f"has multiple install paths: {', '.join(dupe_names[:5])}.\n"
            f"  Claude Code loads skills from EVERY registered install path, so duplicates "
            f"genuinely consume extra context tokens (a Claude Code bug).\n"
            f"  Fix: `{_measure_facade._measure_cli()} plugin-cleanup` (or `--dry-run` to preview). "
            f"Run `--dry-run` first to preview changes. "
            f"~{wasted:,} tokens recoverable."
        )
    if plugin_suspicious:
        node_mod = [s for s in plugin_suspicious if s["reason"] == "node_modules"]
        worktree = [s for s in plugin_suspicious if s["reason"] == "worktree"]
        if node_mod:
            quick.append(
                f"**Plugin loaded from node_modules ({len(node_mod)} path{'s' if len(node_mod) > 1 else ''})**: "
                f"Plugin '{node_mod[0]['plugin']}' has an install path inside node_modules. "
                f"This is likely unintentional and may load skills from dependency internals.\n"
                f"  Path: {node_mod[0]['path']}\n"
                f"  Fix: `{_measure_facade._measure_cli()} plugin-cleanup` removes stale/suspicious paths."
            )
        if worktree and not plugin_dupes:
            quick.append(
                f"**Plugin loaded from worktree directory ({len(worktree)} path{'s' if len(worktree) > 1 else ''})**: "
                f"Plugin '{worktree[0]['plugin']}' has install paths inside worktree directories. "
                f"These accumulate as you create worktrees and may cause duplicate skill loading "
                f"(a Claude Code bug).\n"
                f"  Fix: 1) Remove old manual worktrees: `git worktree list` then `git worktree remove <name>` "
                f"for unused ones. 2) Use `claude -w` instead of `git worktree add` going forward, "
                f"the built-in flag avoids the duplication bug. "
                f"3) `{_measure_facade._measure_cli()} plugin-cleanup` removes stale cache dirs."
            )

    # --- Rule 10: Rules directory overhead ---
    rules = components.get("rules", {})
    rules_count = rules.get("count", 0)
    rules_tokens = rules.get("tokens", 0)
    always_loaded = rules.get("always_loaded", 0)
    always_loaded_tokens = rules.get("always_loaded_tokens", rules_tokens)
    if rules_count > 5 and rules_tokens > 300:
        medium.append(
            f"**Review {rules_count} rule files ({rules_tokens:,} tokens, {always_loaded} always-loaded)**: "
            f"Files in .claude/rules/ without a paths: frontmatter field load every session regardless "
            f"of which project you're in. Review whether all {always_loaded} always-loaded rules are still relevant.\n"
            f"  Add 'paths:' frontmatter to scope rules to specific directories. "
            f"Consolidate overlapping rules into fewer files. "
            f"Archive stale rules (old project conventions, resolved style decisions). "
            f"~{always_loaded_tokens:,} tokens recoverable by scoping always-loaded rules."
        )

    # --- Rule 11: @imports overhead ---
    imports = components.get("imports", {})
    imports_count = imports.get("count", 0)
    imports_tokens = imports.get("tokens", 0)
    if imports_count > 0 and imports_tokens > 500:
        medium.append(
            f"**Review @imports in CLAUDE.md ({imports_count} imports, {imports_tokens:,} tokens)**: "
            f"Each @import pulls a file into every message. Total: {imports_tokens:,} tokens.\n"
            f"  Ask for each import: does this need to load every single message? "
            f"If it's a reference doc, coding standard, or config guide, consider converting it to "
            f"a skill reference file (loaded only when invoked) or removing the @import and reading "
            f"the file on demand. Keep imports only for content that genuinely affects every interaction. "
            f"~{imports_tokens:,} tokens recoverable."
        )

    # --- Rule 12: Large number of MCP tools ---
    mcp = components.get("mcp_tools", {})
    mcp_tokens = mcp.get("tokens", 0)
    mcp_servers = mcp.get("server_count", 0)
    if mcp_tokens > 2000:
        medium.append(
            f"**Review MCP server overhead ({mcp_servers} servers, ~{mcp_tokens:,} tokens)**: "
            f"MCP tools add up. Each deferred tool costs ~15 tokens in the Tool Search menu, "
            f"plus server instructions.\n"
            f"  Review your MCP servers in settings.json. Disable servers you rarely use "
            f"(you can re-enable anytime). Check for duplicate tools across servers. "
            f"Note: ask yourself which servers you actually use in conversation before disabling. "
            f"Some servers are used interactively even if they have no code references. "
            f"~{mcp_tokens:,} tokens recoverable."
        )

    # --- Rule 14: Git instructions in system prompt ---
    settings_local = components.get("settings_local", {})
    include_git = settings_local.get("includeGitInstructions", True) if isinstance(settings_local, dict) else True
    if str(_measure_facade._resolve_feature_env("CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS") or "") == "1":
        include_git = False
    if include_git:
        deep.append(
            "**Disable built-in git instructions (includeGitInstructions: false)**: "
            "Claude Code injects ~2,000 tokens of commit/PR workflow instructions into "
            "every session. If you don't use Claude for git operations, disable them. "
            "Two equivalent ways: add the key `\"includeGitInstructions\": false` to "
            "the top level of ~/.claude/settings.json, or export the env var "
            "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS=1. This is the only user-facing "
            "setting that reduces Core System overhead directly. ~2,000 tokens recoverable."
        )

    # --- Rule 15: claude.ai MCP servers ---
    # Resolve across process env + all settings files (project overrides global)
    # so a project-level opt-out suppresses this nudge too (the same opt-out class of setting).
    claudeai_val = _measure_facade._resolve_feature_env("ENABLE_CLAUDEAI_MCP_SERVERS") or ""
    if str(claudeai_val).lower() != "false":
        # Estimate: each cloud-synced server adds ~300-500 tokens (tool defs + instructions)
        mcp_info = components.get("mcp_tools", {})
        local_server_count = mcp_info.get("server_count", 0)
        medium.append(
            "**Check for cloud-synced MCP servers (~300-500 tokens each)**: "
            f"You have {local_server_count} locally configured MCP servers, but Claude Code can also "
            f"sync additional servers from your claude.ai account settings.\n"
            f"  Diagnostic: run `/mcp` in Claude Code and count servers. If you see more than "
            f"{local_server_count} (your local count), the extras are cloud-synced.\n"
            f"  To opt out of cloud MCPs in CLI: add `\"ENABLE_CLAUDEAI_MCP_SERVERS\": \"false\"` "
            f"to the `env` section of ~/.claude/settings.json. "
            f"This prevents cloud MCPs from loading in CLI sessions while keeping them on claude.ai."
        )

    # --- Rule 16: effortLevel reporting (informational, not prescriptive) ---
    # User's model and effort choices reflect their intent. We report, not recommend.
    effort_level = components.get("settings_local", {}).get("effortLevel")
    if effort_level and str(effort_level).lower() == "high":
        habits.append(
            "**`effortLevel` is set to \"high\" (FYI)**: "
            "Your settings.json has `effortLevel: \"high\"`. This maximizes response quality "
            "and thinking depth. If you chose this deliberately, no action needed, "
            "the optimizer respects your model and effort choices.\n"
            "  For awareness: \"high\" uses ~15-25% more output tokens per response than \"medium\". "
            "You can check token usage with `/cost`. Claude's adaptive thinking still adjusts "
            "within the effort level based on task complexity."
        )

    # --- Rule 13: Compact habits (always include) ---
    try:
        _cw = _measure_facade._resolve_compact_window(None)
        _compact_where = (f"Auto-compact fires at ~{_cw['tokens']:,} tokens "
                          f"({_cw['source']}). ")
    except Exception:
        _compact_where = ""
    habits.append(
        "**Compact on your own schedule, not at the auto-compact line**: "
        "Output quality degrades as context fills, well before auto-compact runs. "
        f"{_compact_where}"
        "Run /compact proactively when the conversation gets long or the topic changes, "
        "and use `/autocompact <tokens>` to pull the line in for a model. "
        "`measure.py compact-advice` replays your own session history to estimate "
        "whether compacting earlier would save tokens."
    )
    habits.append(
        "**Use /clear between unrelated topics**: "
        "Each message re-sends your entire config stack. Starting fresh with /clear "
        "gives you a clean context window without stale conversation history dragging down quality."
    )
    habits.append(
        "**Batch related requests into one message**: "
        "Every message round-trip re-sends your full config stack. "
        "Instead of 5 separate messages, combine related requests into one. "
        "This is especially impactful with large CLAUDE.md or many skills."
    )

    # --- Assemble markdown ---
    sections = []
    if quick:
        sections.append("## Quick Wins\n\n" + "\n\n".join(
            f"- [ ] {item}" for item in quick
        ))
    if medium:
        sections.append("## Medium Effort\n\n" + "\n\n".join(
            f"- [ ] {item}" for item in medium
        ))
    if deep:
        sections.append("## Deep Optimization\n\n" + "\n\n".join(
            f"- [ ] {item}" for item in deep
        ))
    if habits:
        sections.append("## Behavioral Habits\n\n" + "\n\n".join(
            f"- [ ] {item}" for item in habits
        ))

    plan_md = "\n\n".join(sections) if sections else ""
    total_count = len(quick) + len(medium) + len(deep) + len(habits)
    return plan_md, total_count


def _deterministic_candidates_data(days=None, budget_s=None,
                                   max_sessions=None,
                                   use_cache=None, progress=None):
    """Deterministic-candidate analysis over local transcripts (see deterministic_candidates.py).

    Local, read-only, no model calls. Never raises: the coach must not fail
    because of this block, so any error comes back as ``status: "error"``.
    """
    _measure_facade = _compat.facade()
    runtime = "unknown"
    try:
        runtime = _measure_facade.detect_runtime()
        if runtime not in ("claude", "codex"):
            return _measure_facade.deterministic_candidates.run(runtime, [], lambda *a: ((_measure_facade := _compat.facade()), 0.0)[1], days=days)
        tier = _measure_facade._load_pricing_tier()

        def price(model, fresh, out, cache_read, cache_create, cc_1h, cc_5m):
            _measure_facade = _compat.facade()
            if not model or model == "unknown":
                return None
            if runtime == "codex":
                return _measure_facade._get_model_cost(model, fresh, out, cache_read, cache_create, tier=tier)
            if cc_1h or cc_5m:
                return _measure_facade._get_model_cost(model, fresh, out, cache_read, cache_create, tier=tier,
                                       cache_create_1h=cc_1h, cache_create_5m=cc_5m)
            return _measure_facade._get_model_cost(model, fresh, out, cache_read, cache_create, tier=tier)

        files = [(_measure_facade.Path(jf), mt) for jf, mt, _proj in _measure_facade._find_all_jsonl_files(days=days)
                 if _measure_facade._sidechain_path_reason(jf) is None]
        return _measure_facade.deterministic_candidates.run(
            runtime, files, price, days=days, budget_s=budget_s, max_sessions=max_sessions,
            cache_dir=_measure_facade.SNAPSHOT_DIR, tier=tier, use_cache=use_cache, progress=progress,
            split_usage=lambda model, usage: ((_measure_facade := _compat.facade()), _measure_facade._billed_usage_parts(usage, model, tier=tier))[1])
    except Exception as exc:  # never break coach --json
        return {"status": "error", "runtime": runtime, "partial": True, "candidates": [],
                "basis": _measure_facade.deterministic_candidates.BASIS,
                "error": f"{type(exc).__name__}: {str(exc)[:120]}"}


def _cmd_deterministic_candidates(args):
    """measure.py deterministic-candidates [--days N] [--json] [--budget SECONDS] [--max-sessions N] [--no-cache]"""
    _measure_facade = _compat.facade()
    as_json = "--json" in args

    def _opt(flag, cast, default):
        _measure_facade = _compat.facade()
        if flag in args:
            i = args.index(flag)
            if i + 1 < len(args):
                try:
                    return cast(args[i + 1])
                except ValueError:
                    pass
        return default

    days = max(1, _opt("--days", int, 30))
    budget = max(1.0, _opt("--budget", float, _measure_facade._DETCAND_CLI_BUDGET_S))
    cap = max(1, _opt("--max-sessions", int, _measure_facade._DETCAND_CLI_MAX_SESSIONS))

    def _progress(msg):
        # Quiet unless a person is watching: never in --json, never when stderr is piped or captured.
        # deterministic_candidates.run() throttles this to one line per 2 seconds.
        _measure_facade = _compat.facade()
        if as_json:
            return
        try:
            is_tty = _measure_facade.sys.stderr.isatty()
        except (AttributeError, ValueError, OSError):
            is_tty = False
        if is_tty:
            print(f"  {msg}", file=_measure_facade.sys.stderr)

    data = _measure_facade._deterministic_candidates_data(days=days, budget_s=budget, max_sessions=cap,
                                          use_cache="--no-cache" not in args, progress=_progress)
    if as_json:
        print(_measure_facade.json.dumps(data, indent=2))
        return
    print()
    print(f"  {_measure_facade._strip_ansi(str(data.get('summary') or _measure_facade.deterministic_candidates.summary_line(data)))}")
    for i, c in enumerate(data.get("candidates") or [], 1):
        tok = c["tokens"]
        print(f"\n  {i}. [{c['kind']}] seen {c['times_seen']}x in {c['sessions_seen']} session(s)")
        print(f"     {_measure_facade._strip_ansi(str(c['example']))}")
        print(f"     the turns that ran them used {tok['total_tokens']:,} tokens ({tok['input_tokens']:,} in incl. "
              f"{tok['cache_read_tokens']:,} cache read, {tok['output_tokens']:,} out), "
              f"~${c['cost_usd']:.2f} API-equivalent, {_measure_facade.deterministic_candidates.BASIS}")
        print(f"     -> {c['suggestion']}")
    if data.get("candidates"):
        print(f"\n  {_measure_facade.deterministic_candidates.USED_NOT_SAVED_NOTE}")
    print()


def generate_coach_data(focus=None, components=None, trends=None, include_deterministic=None):
    """Generate structured coaching data for Token Coach mode.

    Args:
        focus: Optional focus area ('skills', 'agentic', 'memory')
        components: Pre-computed measure_components() result (avoids duplicate call)
        trends: Pre-computed trends data (avoids duplicate call)
        include_deterministic: Add the ``deterministic_candidates`` block (a
            bounded transcript scan). Only the ``coach`` CLI asks for it; the
            dashboard and rollup callers skip the scan.

    Returns a dict with:
    - snapshot: current component measurements
    - patterns: detected patterns (good and bad)
    - questions: suggested clarifying questions
    - health_score: 0-100 composite score
    - focus_area: if user specified a focus
    """
    _measure_facade = _compat.facade()
    if components is None:
        components = _measure_facade.measure_components()
    totals = _measure_facade.calculate_totals(components)
    context_window = _measure_facade.detect_context_window()[0]
    _rt = _measure_facade.detect_runtime()
    is_codex = _rt == "codex"
    is_claude = _rt == "claude"
    # Claude-family = Claude-model runtimes where Sonnet/Haiku routing advice
    # is valid. File surfaces (CLAUDE.md, ~/.claude paths) are Claude-only —
    # hermes runs Claude models but has no CLAUDE.md.
    is_claude_family = _rt in ("claude", "hermes")
    if is_codex:
        instruction_label = "AGENTS.md"
        memory_label = "Codex memories"
    elif is_claude:
        instruction_label = "CLAUDE.md"
        memory_label = "MEMORY.md"
    else:
        # Foreign runtimes (cursor, antigravity, grok, opencode, copilot,
        # hermes) have no CLAUDE.md/MEMORY.md surface — label generically.
        instruction_label = "agent instructions"
        memory_label = "agent memory"

    # Collect trends if not provided
    if trends is None:
        try:
            trends = _measure_facade._collect_trends_data(days=30)
        except Exception:
            pass

    # --- Pattern Detection ---
    patterns_good = []
    patterns_bad = []
    questions = []

    # Score components (0-100, start at 75 base, earned signals add up to +15)
    # Neutral facts (skill count, MCP count, effort level) don't affect score.
    # Only genuinely earned items (lean CLAUDE.md, SessionEnd hook) add points.
    # Deductions push below 75 for real problems.
    score = 75

    # Check skills count — only flag if overhead is significant relative to context
    # and there are genuinely unused skills. Skill count alone is not a problem.
    skills = components.get("skills", {})
    skill_count = skills.get("count", 0)
    skill_tokens = skills.get("tokens", 0)
    if is_codex:
        # The Codex scan splits plugin-delivered skills into plugin_skills, but
        # their name+description metadata is advertised to the model exactly
        # like user skills — the headline must count both or it under-reports
        # the real startup surface.
        plugin_skills = components.get("plugin_skills", {})
        skill_count += plugin_skills.get("count", 0)
        skill_tokens += plugin_skills.get("tokens", 0)
    # context_window already set above via detect_context_window() — don't overwrite with stale snapshot value
    skill_pct = skill_tokens / context_window * 100 if context_window else 0
    unused_skills = trends.get("skills", {}).get("never_used", []) if trends else []
    active_names = set(skills.get("names", []))
    if is_codex:
        active_names.update(components.get("plugin_skills", {}).get("names", []))
    unused_skills = sorted({name for name in unused_skills
                            if name in active_names and not _measure_facade._is_own_tool_skill(name)})
    unused_count = len(unused_skills)
    # Savings come from per-skill measurement only: sum the unused candidates'
    # own measured frontmatter tokens. Unmeasured names are reported as such —
    # never an inventory-wide upper bound and never count x a flat constant.
    skills_detail = components.get("skills_detail", {})
    unused_measured_tokens = 0
    unused_unmeasured = 0
    for _name in unused_skills:
        _raw_toks = (skills_detail.get(_name) or {}).get("frontmatter_tokens")
        try:
            _toks = int(_raw_toks)
        except (TypeError, ValueError, OverflowError):
            # NaN/inf/non-numeric entries are unmeasured, not a coach crash.
            _toks = 0
        if _toks > 0:
            unused_measured_tokens += _toks
        else:
            unused_unmeasured += 1
    if unused_measured_tokens > 0 and unused_unmeasured == 0:
        unused_savings = f"~{unused_measured_tokens:,} tokens (measured descriptions of the {unused_count} unused skills)"
    elif unused_measured_tokens > 0:
        unused_savings = (f"~{unused_measured_tokens:,} tokens measured for the unused set "
                          f"({unused_unmeasured} of {unused_count} lack per-skill measurement)")
    else:
        unused_savings = "Unknown: per-skill token measurements unavailable for the unused set"
    unused_ratio = unused_count / skill_count if skill_count > 0 else 0
    if unused_count > 20 and skill_pct > 2 and unused_ratio > 0.8:
        skill_fix = (
            "Review unused Codex skills and plugins. Disable truly stale user skills with "
            "`measure.py codex-skill disable --path ...`; do not edit plugin cache directories directly."
            if is_codex
            else "Review unused skills. Some may be seasonal, but archiving truly abandoned ones saves tokens. Move to ~/.claude/skills/_archived/"
        )
        # Truly excessive: >80% of skills unused AND significant token overhead
        patterns_bad.append({
            "name": "Unused Skill Overhead",
            "severity": "high",
            "detail": f"{unused_count} of {skill_count} skills unused in 30 days ({skill_tokens:,} tokens, {skill_pct:.1f}% of context)",
            "fix": skill_fix,
            "savings": unused_savings,
        })
        score -= 7
    elif unused_count > 15 and unused_ratio > 0.6:
        # Moderate: many unused but could be seasonal
        patterns_bad.append({
            "name": "Many Unused Skills",
            "severity": "low",
            "detail": f"{unused_count} of {skill_count} skills unused in 30 days. Some may be seasonal.",
            "fix": "Review for skills you've truly abandoned vs. ones you use occasionally",
            "savings": unused_savings,
        })
        score -= 3
    elif skill_count > 0:
        patterns_good.append({
            "name": "Installed Skill Set",
            "detail": f"{skill_count} skills ({skill_tokens:,} tokens, {skill_pct:.1f}% of context)",
        })

    # Check instruction-file size — thresholds relative to context window.
    claude_tokens = 0
    claude_lines = 0
    for key in components:
        if (
            (is_claude and key.startswith("claude_md"))
            or (is_codex and key.startswith("agents_md"))
        ) and components[key].get("exists"):
            claude_tokens += components[key].get("tokens", 0)
            claude_lines += components[key].get("lines", 0)
    claude_pct = claude_tokens / context_window * 100 if context_window else 0
    # Savings target = Anthropic's documented 200-line guidance (same math as
    # quick_scan): derive recoverable tokens from 200 × measured tokens-per-line
    # so the figure tracks line slimming, not the dropped ~4,500-token heuristic.
    # Guard on claude_lines > 200 so a token-dense but already-lean file reports
    # "Preventive" instead of a self-contradicting ~0 token saving.
    tokens_per_line = claude_tokens / max(claude_lines, 1)
    target_tokens = int(200 * tokens_per_line)
    recoverable = max(0, claude_tokens - target_tokens)
    savings_value = f"~{recoverable:,} tokens per message" if claude_lines > 200 else "Preventive"
    if claude_pct > 3:
        patterns_bad.append({
            "name": f"{instruction_label} Could Be Leaner",
            "severity": "medium",
            "detail": f"{instruction_label} chain totals {claude_tokens:,} tokens ({claude_pct:.1f}% of context)",
            "fix": "Split long always-loaded guidance into narrower project files and keep the root instruction file as a lean index.",
            "savings": savings_value,
        })
        score -= 5
    elif claude_pct > 2:
        patterns_bad.append({
            "name": f"{instruction_label} Growing",
            "severity": "low",
            "detail": f"{instruction_label} at {claude_tokens:,} tokens ({claude_pct:.1f}% of context)",
            "fix": "Move verbose guidance into narrower project files or on-demand skills.",
            "savings": savings_value,
        })
        score -= 5
    elif claude_tokens > 0:
        patterns_good.append({
            "name": f"Lean {instruction_label}",
            "detail": f"{claude_tokens:,} tokens ({claude_pct:.1f}% of context)",
            "earned": True,
        })
        score += 5

    # Check MEMORY.md — a Claude/Codex-only surface. Foreign runtimes have no
    # auto-load memory file and never produce this component; do not evaluate
    # an injected/foreign one under a Claude label.
    mem = components.get("memory_md", {}) if (is_claude or is_codex) else {}
    mem_lines = mem.get("lines", 0)
    if mem_lines > 200:
        patterns_bad.append({
            "name": f"Oversized {memory_label}",
            "severity": "medium",
            "detail": f"{mem_lines} lines (200-line auto-load cutoff)",
            "fix": "Move detailed notes to topic files and keep injected memory guidance short.",
            "savings": f"~{(mem_lines - 200) * 15:,} tokens",
        })
        score -= 10
    elif mem_lines > 150:
        patterns_bad.append({
            "name": f"{memory_label} Approaching Limit",
            "severity": "low",
            "detail": f"{mem_lines} lines ({200 - mem_lines} lines of headroom)",
            "fix": "Proactively move detailed notes to topic files",
            "savings": "Preventive",
        })
        score -= 3

    # Check MCP servers
    mcp = components.get("mcp_tools", {})
    mcp_servers = mcp.get("server_count", 0)
    mcp_tokens = mcp.get("tokens", 0)
    # MCP servers: with Tool Search, deferred tools cost ~15 tokens each.
    # Only flag when token overhead is genuinely significant (>3% of context).
    mcp_pct = mcp_tokens / context_window * 100 if context_window else 0
    if mcp_servers > 20 and mcp_pct > 3:
        patterns_bad.append({
            "name": "MCP Sprawl",
            "severity": "medium",
            "detail": f"{mcp_servers} MCP servers ({mcp_tokens:,} tokens, {mcp_pct:.1f}% of context)",
            "fix": ("Disable unused servers in ~/.codex/config.toml ([mcp_servers] sections)"
                    if is_codex else
                    "Disable unused servers in settings.json"),
            "savings": "~50-100 tokens per disabled server",
        })
        score -= 5
    elif mcp_servers > 0:
        patterns_good.append({
            "name": "MCP Servers",
            "detail": f"{mcp_servers} servers ({mcp_tokens:,} tokens, {mcp_pct:.1f}% of context)",
        })

    # Check file exclusion rules (permissions.deny) — .claude/settings.json is
    # a Claude-only config surface, so the advice is Claude-only too.
    exclusion = components.get("file_exclusion", {})
    if is_claude and not exclusion.get("has_rules"):
        patterns_bad.append({
            "name": "Missing file exclusion rules",
            "severity": "medium",
            "detail": "No permissions.deny rules found",
            "fix": "Add Read() deny patterns to .claude/settings.json",
            "savings": "500-2,000 tokens (excludes files from context)",
        })
        score -= 8

    # Check rules
    rules = components.get("rules", {})
    rules_count = rules.get("count", 0)
    always_loaded = rules.get("always_loaded", 0)
    if always_loaded > 5:
        patterns_bad.append({
            "name": "Unscoped Rules",
            "severity": "medium",
            "detail": f"{always_loaded} of {rules_count} rules lack paths: scoping",
            "fix": "Add paths: frontmatter to scope rules to specific directories",
            "savings": f"~{rules.get('always_loaded_tokens', rules.get('tokens', 0)):,} tokens recoverable by scoping always-loaded rules",
        })
        score -= 8

    # Check @imports
    imports = components.get("imports", {})
    if imports.get("count", 0) > 0 and imports.get("tokens", 0) > 500:
        patterns_bad.append({
            "name": "Import Avalanche",
            "severity": "medium",
            "detail": f"{imports['count']} @imports totaling {imports['tokens']:,} tokens",
            "fix": "Move large imports to skills or reference files",
            "savings": f"~{imports['tokens']:,} tokens per message",
        })
        score -= 10

    # Check hooks
    hooks = components.get("hooks", {})
    if is_codex and hooks.get("configured") and "Stop" in hooks.get("names", []):
        patterns_good.append({
            "name": "Codex Stop Hook Installed",
            "detail": "Dashboard refresh and continuity checkpoints are active",
            "earned": True,
        })
        score += 5
    elif is_claude and hooks.get("configured") and "SessionEnd" in hooks.get("names", []):
        patterns_good.append({
            "name": "SessionEnd Hook Installed",
            "detail": "Usage tracking active",
            "earned": True,
        })
        score += 5
    elif is_claude or is_codex:
        # Hook-install advice exists only for runtimes TO can install into;
        # foreign runtimes get no hook pattern at all.
        patterns_bad.append({
            "name": "No Codex Stop Hook" if is_codex else "No SessionEnd Hook",
            "severity": "low",
            "detail": "Automatic dashboard refresh and continuity checkpoints are not active",
            "fix": _measure_facade._hint("Run: TOKEN_OPTIMIZER_RUNTIME=codex python3 measure.py codex-install --project .") if is_codex else _measure_facade._hint("Run: python3 measure.py setup-hook"),
            "savings": "Enables trends data for better coaching",
        })
        score -= 3

    # Check model mix from trends
    _opus_addiction_fired = False
    default_model = components.get("settings_local", {}).get("defaultModel")
    if trends:
        model_mix = trends.get("model_mix", {})
        total_model_tokens = sum(model_mix.values()) if model_mix else 0
        if total_model_tokens > 0:
            opus_pct = model_mix.get("opus", 0) / total_model_tokens * 100
            haiku_pct = model_mix.get("haiku", 0) / total_model_tokens * 100
            _opus_addiction_fired = False
            if is_claude_family and opus_pct > 85:
                fix_msg = "Route data-gathering agents to Haiku, analysis to Sonnet"
                if default_model and "opus" in str(default_model).lower():
                    fix_msg += f". Root cause: settings.json has \"model\": \"{default_model}\" which may override routing"
                patterns_bad.append({
                    "name": "Opus Addiction",
                    "severity": "medium",
                    "detail": f"{opus_pct:.0f}% Opus, {haiku_pct:.0f}% Haiku",
                    "fix": fix_msg,
                    "savings": "50-75% cost reduction (same context, less spend)",
                })
                score -= 5
                _opus_addiction_fired = True

        # Unused skills check is handled earlier (line ~3737) via the
        # proportional "Unused Skill Overhead" / "Some Unused Skills" patterns.
        # Removed duplicate check here to prevent double-penalty.

    # Check verbose skill descriptions. The warning claims ">200 chars", so the
    # filter applies that stated threshold (the Codex scan also flags a 120-200
    # band for its own medium-tier nudge — those are not "over 200").
    quality = components.get("skill_frontmatter_quality", {})
    verbose = [s for s in quality.get("verbose_skills", [])
               if not _measure_facade._is_own_tool_skill(s.get("name"))
               and s.get("description_chars", 0) > 200]
    if is_claude:
        truncated_skills = [s for s in verbose if s.get("truncated")]
        verbose_only = [s for s in verbose if not s.get("truncated")]
    else:
        # The 1,536-char silent truncation is a Claude Code behavior — Codex
        # and every other runtime do not cut descriptions at that limit. A
        # truncated-flagged entry off-Claude is simply a very long
        # description: keep it in the verbose set so it still surfaces instead
        # of vanishing from every warning.
        verbose_only = verbose
        truncated_skills = []
    if truncated_skills:
        patterns_bad.append({
            "name": "Truncated Skill Descriptions",
            "severity": "high",
            "detail": f"{len(truncated_skills)} skills exceed 1,536-char limit (silently truncated by Claude Code)",
            "fix": "Move overflow content to the SKILL.md body",
            "savings": f"~{sum(int((s.get('description_chars', 0) - _measure_facade._SKILL_DESC_TRUNCATION_LIMIT) / _measure_facade.CHARS_PER_TOKEN) for s in truncated_skills):,} tokens wasted",
        })
        score -= 8
    if len(verbose_only) >= 3:
        patterns_bad.append({
            "name": "Verbose Skill Descriptions",
            "severity": "low",
            "detail": f"{len(verbose_only)} skills have descriptions over 200 chars",
            "fix": "Tighten descriptions to under 80 characters for efficiency",
            "savings": "Minor per-skill, adds up with many skills",
        })
        score -= 3

    # Check effortLevel (informational, not a penalty)
    effort_level = components.get("settings_local", {}).get("effortLevel")
    if effort_level and str(effort_level).lower() == "high":
        patterns_good.append({
            "name": "Effort Level Set",
            "detail": "effortLevel: \"high\" — deliberate quality choice. Uses ~15-25% more output tokens than \"medium\".",
        })

    # Check settings env vars for optimization opportunities. Resolve across
    # process env + all settings files so a project-level opt-out is honoured.
    claudeai_val = _measure_facade._resolve_feature_env("ENABLE_CLAUDEAI_MCP_SERVERS") or ""
    if is_claude and str(claudeai_val).lower() != "false" and mcp_servers > 3:
        questions.append("Cloud-synced MCP servers from claude.ai may be adding overhead. Have you reviewed which servers are cloud-synced vs local?")

    # WebSearch routing nudge (post-hoc detector)
    if trends:
        try:
            from detectors.websearch_routing import detect_websearch_routing
            ws_findings = detect_websearch_routing(trends)
            for f in ws_findings:
                if f.get("confidence", 0) >= 0.5:
                    patterns_bad.append({
                        "name": "Web Search Overhead",
                        "severity": "medium" if f["confidence"] >= 0.7 else "low",
                        "detail": f["evidence"],
                        "fix": f["suggestion"],
                        "savings": f"~{f['savings_tokens']:,} tokens across sessions",
                    })
                    score -= 5
        except ImportError:
            pass

    # Session-level detectors (run on recent sessions, aggregate findings)
    try:
        from detectors.registry import run_all_detectors, triage
        recent_files = _measure_facade._find_all_jsonl_files(days=7)[:10]  # cap at 10 sessions
        all_findings = []

        # Inject CLAUDE.md content once for cache_instability detector
        _claude_md_content = ""
        for key in ("claude_md_global", "claude_md_home", "claude_md_project", "claude_md_dotclaude"):
            md_comp = components.get(key, {})
            if md_comp.get("exists") and md_comp.get("content"):
                _claude_md_content = md_comp["content"]
                break
        if not _claude_md_content and is_claude:
            for path in (_measure_facade.CLAUDE_DIR / "CLAUDE.md", _measure_facade.Path.home() / "CLAUDE.md", _measure_facade.Path.cwd() / "CLAUDE.md"):
                if path.exists():
                    try:
                        _claude_md_content = path.read_text(encoding="utf-8", errors="replace")[:50_000]
                    except (PermissionError, OSError):
                        pass
                    if _claude_md_content:
                        break

        _claude_md_content = _claude_md_content[:50_000]

        total_messages_scanned = 0
        for jf, _, _ in recent_files:
            parsed = _measure_facade._parse_session_jsonl(str(jf))
            if parsed and parsed.get("total_input_tokens", 0) > 0:
                parsed["jsonl_path"] = str(jf)
                parsed["claude_md_content"] = _claude_md_content
                try:
                    parsed["turns"] = _measure_facade.parse_session_turns(str(jf))
                except Exception:
                    parsed["turns"] = []
                total_messages_scanned += parsed.get("message_count", 0)
                session_findings = run_all_detectors(parsed)
                all_findings.extend(session_findings)

        best_by_name = {}
        for f in all_findings:
            name = f.get("name", "")
            if name not in best_by_name or f.get("confidence", 0) > best_by_name[name].get("confidence", 0):
                best_by_name[name] = f

        triaged = triage(list(best_by_name.values()))

        for f in triaged:
            # Only flag detectors when they affect a significant percentage (>5%)
            # of messages. 7 loops out of 972 messages is noise, not a pattern.
            occurrence_count = f.get("occurrence_count", 1)
            # Config checks (always_show) have no occurrence rate — they must
            # bypass the message-frequency noise gate or they'd be suppressed
            # for any session with more than ~20 messages.
            if not f.get("always_show") and total_messages_scanned > 50:
                pct = occurrence_count / total_messages_scanned * 100
                if pct < 5:
                    continue  # skip noise-level findings

            # Skip overpowered detector if Opus Addiction already covers model routing
            if f["name"] == "overpowered" and _opus_addiction_fired:
                continue

            # Enrich overpowered findings with counterfactual (Claude model
            # ladder only — under Codex a "switch to Sonnet" estimate would be
            # foreign-runtime advice). Detector evidence interpolates raw
            # session-log strings (e.g. a model_usage key), which are
            # attacker-influenceable — strip terminal escapes before the text
            # reaches patterns_bad detail/fix and the coach print.
            detail = _measure_facade._strip_ansi(str(f["evidence"]))
            if f["name"] == "overpowered" and recent_files and is_claude_family:
                try:
                    latest = _measure_facade._parse_session_jsonl(str(recent_files[0][0]))
                    if latest:
                        sim = _measure_facade._simulate_model_switch(latest, "sonnet")
                        if sim["savings_usd"] > 0:
                            detail += f". If Sonnet: ~${sim['savings_usd']:.2f} saved ({sim['savings_pct']:.0f}%)"
                except Exception:
                    pass

            severity = "medium" if f.get("confidence", 0) >= 0.7 else "low"
            savings = (
                "Config fix"
                if f.get("always_show")
                else f"~{f.get('savings_tokens', 0):,} tokens"
            )
            patterns_bad.append({
                "name": _measure_facade._strip_ansi(str(f["name"].replace("_", " ").title())),
                "severity": severity,
                "detail": detail,
                "fix": _measure_facade._strip_ansi(str(f["suggestion"])),
                "savings": savings,
            })
            score -= 3
    except ImportError:
        pass

    # --- Historical Trend Analysis (from trends.db) ---
    history = {}
    if trends and trends.get("daily"):
        daily = trends["daily"]
        quality_trend = trends.get("quality_trend", [])

        # Collect recent (7d) vs older sessions
        recent_sessions = []
        older_sessions = []
        for i, d in enumerate(daily):
            details = [sd for sd in d.get("session_details", []) if not sd.get("continuation")]
            if i < 7:
                recent_sessions.extend(details)
            else:
                older_sessions.extend(details)

        # 1. Quality trend: detect declining quality
        if len(quality_trend) >= 10:
            recent_7d = quality_trend[-7:]
            prior = quality_trend[:-7]
            if recent_7d and prior:
                recent_avg_q = sum(d["avg_quality"] for d in recent_7d) / len(recent_7d)
                prior_avg_q = sum(d["avg_quality"] for d in prior) / len(prior)
                history["quality_recent_avg"] = round(recent_avg_q, 1)
                history["quality_prior_avg"] = round(prior_avg_q, 1)
                delta = prior_avg_q - recent_avg_q
                if delta > 10:
                    patterns_bad.append({
                        "name": "Quality Declining",
                        "severity": "high",
                        "detail": f"Average quality dropped from {prior_avg_q:.0f} to {recent_avg_q:.0f} over the last week",
                        "fix": f"Check for new MCP servers, growing {instruction_label}, or longer sessions causing context fill",
                        "savings": "Quality recovery prevents retry waste (typically 5,000-20,000 tokens per failed turn)",
                    })
                    score -= 8
                elif delta > 5:
                    patterns_bad.append({
                        "name": "Quality Drifting Down",
                        "severity": "low",
                        "detail": f"Quality trending from {prior_avg_q:.0f} to {recent_avg_q:.0f}",
                        "fix": "Monitor for another week; consider proactive /compact in longer sessions",
                        "savings": "Preventive",
                    })
                    score -= 3

        # 2. Session duration creep
        if recent_sessions and older_sessions:
            recent_durs = [s.get("duration_minutes", 0) for s in recent_sessions if s.get("duration_minutes")]
            older_durs = [s.get("duration_minutes", 0) for s in older_sessions if s.get("duration_minutes")]
            if recent_durs and older_durs:
                recent_avg_dur = sum(recent_durs) / len(recent_durs)
                older_avg_dur = sum(older_durs) / len(older_durs)
                history["duration_recent_avg"] = round(recent_avg_dur, 1)
                history["duration_prior_avg"] = round(older_avg_dur, 1)
                if recent_avg_dur > older_avg_dur * 1.5 and recent_avg_dur > 60:
                    # Elapsed minutes include idle time — duration alone is NOT
                    # evidence of token waste. The score only moves when real
                    # per-session token volume grew alongside the duration.
                    recent_toks = [s.get("input_tokens", 0) for s in recent_sessions if s.get("input_tokens")]
                    older_toks = [s.get("input_tokens", 0) for s in older_sessions if s.get("input_tokens")]
                    tokens_grew = (
                        bool(recent_toks) and bool(older_toks)
                        and (sum(recent_toks) / len(recent_toks)) > (sum(older_toks) / len(older_toks)) * 1.25
                    )
                    if tokens_grew:
                        patterns_bad.append({
                            "name": "Session Duration Creep",
                            "severity": "medium",
                            "detail": f"Sessions averaging {recent_avg_dur:.0f} min (was {older_avg_dur:.0f} min) with per-session input volume up {((sum(recent_toks) / len(recent_toks)) / (sum(older_toks) / len(older_toks)) - 1) * 100:.0f}% — real context growth, not idle time",
                            "fix": "Use /compact proactively around the midpoint. Break large tasks into focused sessions",
                            "savings": "Not estimated: duration is not a token measure; compacting earlier reduces context fill",
                        })
                        score -= 5
                    else:
                        patterns_bad.append({
                            "name": "Session Duration Creep",
                            "severity": "low",
                            "detail": f"Elapsed sessions averaging {recent_avg_dur:.0f} min (was {older_avg_dur:.0f} min); may include idle time and does not by itself indicate token waste",
                            "fix": "If sessions are genuinely busier (not just idle), compact proactively around the midpoint",
                            "savings": "Not estimated: elapsed session duration includes idle time and does not measure token waste",
                        })

        # 3. Cache hit rate degradation (model-switch aware)
        multi_model_recent = sum(1 for s in recent_sessions if s.get("model_count", 1) > 1)
        multi_model_pct = (multi_model_recent / len(recent_sessions) * 100) if recent_sessions else 0
        history["multi_model_session_pct"] = round(multi_model_pct, 1)
        if recent_sessions and older_sessions:
            recent_chr = [s.get("cache_hit_rate", 0) for s in recent_sessions if s.get("cache_hit_rate") is not None]
            older_chr = [s.get("cache_hit_rate", 0) for s in older_sessions if s.get("cache_hit_rate") is not None]
            if recent_chr and older_chr:
                recent_avg_chr = sum(recent_chr) / len(recent_chr)
                older_avg_chr = sum(older_chr) / len(older_chr)
                history["cache_hit_recent_avg"] = round(recent_avg_chr, 3)
                history["cache_hit_prior_avg"] = round(older_avg_chr, 3)
                if older_avg_chr - recent_avg_chr > 0.10 and older_avg_chr > 0.4:
                    if multi_model_pct > 25:
                        patterns_bad.append({
                            "name": "Cache Hit Rate Dropping (Model Switches)",
                            "severity": "low",
                            "detail": f"Cache hit rate fell from {older_avg_chr:.0%} to {recent_avg_chr:.0%}, but {multi_model_pct:.0f}% of recent sessions switched models mid-session. Model switches invalidate the prompt cache (expected behavior)",
                            "fix": ("Set the session model up front (config.toml `model`) instead of switching mid-session. Codex subagents run in separate contexts, so a smaller model for them does not disturb the main session's cache"
                                    if is_codex else
                                    "Pick one model per session when possible. Use /model at session start, not mid-conversation. Subagent model routing (Haiku/Sonnet) is fine, it runs in separate contexts"
                                    if is_claude_family else
                                    "Pick one model per session when possible — mid-session switches invalidate the cached prefix"),
                            "savings": "Avoiding mid-session model switches can recover 10-20% cache hit rate",
                        })
                        score -= 2
                    else:
                        patterns_bad.append({
                            "name": "Cache Hit Rate Dropping",
                            "severity": "medium",
                            "detail": f"Cache hit rate fell from {older_avg_chr:.0%} to {recent_avg_chr:.0%}. Lower cache = higher cost per turn",
                            "fix": f"Check for new MCP servers or {instruction_label} changes that shift the stable prefix. Avoid tools that rewrite existing context",
                            "savings": "Cost impact depends on the session model and uncached input volume",
                        })
                        score -= 5

        # 4. Grade distribution: too many low-grade sessions
        all_grades = [s.get("quality_grade") for s in recent_sessions if s.get("quality_grade")]
        if len(all_grades) >= 5:
            d_or_worse = sum(1 for g in all_grades if g in ("D", "F"))
            d_pct = d_or_worse / len(all_grades) * 100
            history["grade_d_pct_recent"] = round(d_pct, 1)
            history["grade_distribution"] = {}
            for g in all_grades:
                history["grade_distribution"][g] = history["grade_distribution"].get(g, 0) + 1
            if d_pct > 50:
                patterns_bad.append({
                    "name": "Majority Low-Grade Sessions",
                    "severity": "high",
                    "detail": f"{d_pct:.0f}% of recent sessions scored D or below",
                    "fix": ("Run a full audit (token-optimizer skill or `measure.py coach --json`). Common causes: bloated tool outputs, stale reads, long sessions without compaction"
                            if is_codex else
                            "Run /token-optimizer for a full audit. Common causes: bloated tool outputs, stale reads, long sessions without compaction"
                            if is_claude_family else
                            "Run a full audit (`measure.py coach --json`). Common causes: bloated tool outputs, stale reads, long sessions without compaction"),
                    "savings": "Improving average grade from D to B typically saves 15-30% of session cost",
                })
                score -= 8
            elif d_pct > 30:
                patterns_bad.append({
                    "name": "Many Low-Grade Sessions",
                    "severity": "medium",
                    "detail": f"{d_pct:.0f}% of recent sessions scored D or below",
                    "fix": "Focus on the longest sessions first. Quality degrades fastest after context passes 60%",
                    "savings": "Each grade improvement saves ~5-10% per session",
                })
                score -= 4

        # 5. Cost awareness
        if trends.get("total_cost_usd"):
            total_cost = trends["total_cost_usd"]
            period = trends.get("period_days", 30)
            session_count_t = trends.get("session_count", 1)
            cost_per_session = total_cost / max(session_count_t, 1)
            history["total_cost_usd"] = round(total_cost, 2)
            history["cost_per_session_usd"] = round(cost_per_session, 4)
            history["sessions_in_period"] = session_count_t
            if cost_per_session > 2.0:
                patterns_bad.append({
                    "name": "High Cost Per Session",
                    "severity": "medium",
                    "detail": f"Estimated API-equivalent cost: ${cost_per_session:.2f}/session (${total_cost:.2f} across {session_count_t} sessions in {period} days); not a billing statement",
                    "fix": ("Choose a lower reasoning effort or a smaller Codex model for simple tasks. Compact long sessions when needed"
                            if is_codex else
                            "Route simple tasks to Sonnet/Haiku. Use /compact in long sessions. Review unused skills"
                            if is_claude_family else
                            "Route simple tasks to a cheaper model tier. Restart or compact long sessions. Review unused skills"),
                    "savings": "Not estimated: requires a measured model-routing or compression comparison",
                })
                score -= 3

        # 6. Optimal session length hint (correlate duration with quality)
        dur_quality_pairs = [
            (s.get("duration_minutes", 0), s.get("quality_score", 0))
            for s in recent_sessions
            if s.get("duration_minutes") and s.get("quality_score")
        ]
        if len(dur_quality_pairs) >= 10:
            short_sessions = [(d, q) for d, q in dur_quality_pairs if d < 60]
            long_sessions = [(d, q) for d, q in dur_quality_pairs if d >= 120]
            if short_sessions and long_sessions:
                short_avg_q = sum(q for _, q in short_sessions) / len(short_sessions)
                long_avg_q = sum(q for _, q in long_sessions) / len(long_sessions)
                history["quality_short_sessions"] = round(short_avg_q, 1)
                history["quality_long_sessions"] = round(long_avg_q, 1)
                if short_avg_q - long_avg_q > 15:
                    history["optimal_session_hint"] = "Sessions under 60 min score significantly higher. Consider breaking long tasks into focused sessions"

        # 7. Compression effectiveness (savings being left on the table)
        try:
            coverage = _measure_facade._get_compression_coverage(days=30)
            if coverage and coverage.get("available"):
                measured_tiers = coverage.get("tiers", {}).get("measured", [])
                opportunity_tiers = coverage.get("tiers", {}).get("opportunity", [])
                measured_saved = sum(f.get("tokens_saved", 0) for f in measured_tiers)
                opportunity_tokens = sum(f.get("tokens_saved", 0) for f in opportunity_tiers)
                history["compression_measured_saved"] = measured_saved
                history["compression_opportunity_tokens"] = opportunity_tokens
                if opportunity_tokens > measured_saved * 2 and opportunity_tokens > 100_000:
                    patterns_bad.append({
                        "name": "Compression Opportunity Gap",
                        "severity": "low",
                        "detail": f"{opportunity_tokens:,} tokens of shadow-only savings vs {measured_saved:,} measured. Active compression could save more",
                        "fix": "First-read skeletons and broader bash coverage can close this gap as cohorts promote",
                        "savings": f"~{opportunity_tokens:,} additional tokens recoverable",
                    })
                    score -= 2
        except Exception:
            pass

        # 8. Frequent model switching (independent of cache impact)
        if multi_model_pct > 40 and len(recent_sessions) >= 5:
            patterns_bad.append({
                "name": "Frequent Model Switching",
                "severity": "medium",
                "detail": f"{multi_model_pct:.0f}% of recent sessions used multiple models. Each switch invalidates the prompt cache and can cause context quality drops",
                "fix": ("Set the session model up front (config.toml `model`) instead of switching mid-session; each switch invalidates the cached prefix"
                        if is_codex else
                        "Set your preferred model at session start with /model. Route subagents to cheaper models via agent() opts instead of switching the main session model"
                        if is_claude_family else
                        "Set the session model up front instead of switching mid-session; each switch invalidates the cached prefix"),
                "savings": "Consistent model usage improves cache hit rate by 10-20% and avoids quality grade drops",
            })
            score -= 4

    # Clamp score
    score = max(0, min(100, score))

    # Session logs, skill frontmatter, and detector evidence are all
    # attacker-influenceable; nothing carrying terminal control sequences may
    # reach the coach's printed output or the --json payload.
    for _p in patterns_bad + patterns_good:
        for _k in ("name", "detail", "fix", "savings"):
            if isinstance(_p.get(_k), str):
                _p[_k] = _measure_facade._strip_ansi(_p[_k])
    questions = [_measure_facade._strip_ansi(str(q)) for q in questions]

    # Build result
    overhead_pct = (totals["estimated_total"] / context_window * 100) if context_window else 0
    # Room ends where auto-compact fires: the window minus the ~33K buffer by
    # default, or the user's own compact window when they set a smaller one.
    compact_ceiling = context_window - 33000
    try:
        _cw = _measure_facade._resolve_compact_window(None)
        if _cw.get("user_override") and 0 < _cw["tokens"] < compact_ceiling:
            compact_ceiling = _cw["tokens"]
    except Exception:
        pass
    usable = compact_ceiling - totals["estimated_total"]

    result = {
        "snapshot": {
            "total_overhead": totals["estimated_total"],
            "controllable": totals["controllable_tokens"],
            "fixed": totals["fixed_tokens"],
            "context_window": context_window,
            "overhead_pct": round(overhead_pct, 1),
            "usable_tokens": max(0, usable),
            "skill_count": skill_count,
            "skill_tokens": skill_tokens,
            "claude_md_tokens": claude_tokens,
            "instruction_label": instruction_label,
            "memory_md_lines": mem_lines,
            "memory_md_scope": components.get("memory_md", {}).get("scope", "file"),
            "memory_md_lines_measured": components.get("memory_md", {}).get("scope", "file") != "all_threads",
            "mcp_server_count": mcp_servers,
            "mcp_tokens": mcp_tokens,
            "rules_count": rules_count,
            "rules_always_loaded": always_loaded,
            "imports_count": imports.get("count", 0),
            "imports_tokens": imports.get("tokens", 0),
        },
        "patterns_good": patterns_good,
        "patterns_bad": patterns_bad,
        "questions": questions,
        "health_score": score,
        "focus_area": focus,
        "history": history,
    }

    if is_codex:
        # The skill figures above count the installed inventory Codex
        # advertises to the model (user skills + skills of enabled plugins;
        # skills under config-disabled plugins are excluded by the scan). What
        # a given session's prompt actually receives is not directly observed —
        # the count is the advertised surface, labeled as such.
        result["snapshot"]["skills_basis"] = (
            "installed inventory advertised to the model (user skills + enabled-plugin "
            "skills); per-session prompt inclusion not directly observed"
        )
        # claude_md_tokens holds AGENTS.md-chain tokens under Codex; keep the
        # legacy key for existing consumers and add a correctly named alias.
        result["snapshot"]["agents_md_tokens"] = claude_tokens

    # Add compaction timing guide when relevant
    has_compaction_patterns = (
        claude_tokens > 5000
        or any(p["name"] in ("Unused Skill Overhead", "Some Unused Skills", "Unused Skills", f"{instruction_label} Could Be Leaner", f"Oversized {memory_label}")
               for p in patterns_bad)
    )
    if has_compaction_patterns:
        result["compaction_guide"] = {
            "compact_after": [
                "Research/exploration phase",
                "Debugging session",
                "Failed approach",
                "Completing a milestone (commit/merge)",
            ],
            "avoid_during": [
                "Mid-implementation",
                "Mid-debugging",
                "Multi-step operations",
            ],
        }

    # Subagent cost breakdown + costly prompts (from recent sessions)
    recent_files = _measure_facade._find_all_jsonl_files(days=7)[:5]
    tier = _measure_facade._load_pricing_tier()

    all_subagent_costs = []
    all_costly_prompts = []
    total_session_cost = 0
    total_subagent_cost = 0

    for jf, _, _ in recent_files:
        parsed = _measure_facade._parse_session_jsonl(str(jf))
        if not parsed or parsed.get("total_input_tokens", 0) == 0:
            continue
        dom_model = max(parsed["model_usage"], key=parsed["model_usage"].get) if parsed["model_usage"] else "unknown"
        total_input = parsed["total_input_tokens"]
        chr_val = parsed.get("cache_hit_rate", 0)
        # _safe_int: a non-finite product (corrupt cache_hit_rate) degrades to
        # 0 instead of raising ValueError/OverflowError and killing the coach.
        cache_read = _measure_facade._safe_int(total_input * chr_val)
        session_cost = _measure_facade._get_model_cost(dom_model, max(0, total_input - cache_read),
                                        parsed["total_output_tokens"], cache_read, 0, tier=tier)
        total_session_cost += session_cost

        sub_costs = _measure_facade._analyze_subagent_costs(str(jf), tier=tier)
        sub_total = sum(s["cost_usd"] for s in sub_costs)
        total_subagent_cost += sub_total
        all_subagent_costs.extend(sub_costs)

        prompts = _measure_facade._extract_costly_prompts(str(jf), tier=tier, top_n=3)
        all_costly_prompts.extend(prompts)

    all_subagent_costs.sort(key=lambda x: ((_measure_facade := _compat.facade()), x["cost_usd"])[1], reverse=True)
    all_costly_prompts.sort(key=lambda x: ((_measure_facade := _compat.facade()), x.get("cost_usd", 0))[1], reverse=True)

    # Subagent tokens are billed separately, not inside parent session total.
    # Compute % against combined (session + subagent) spend.
    combined_cost = total_session_cost + total_subagent_cost
    if combined_cost > 0 and total_subagent_cost > 0:
        sub_pct = round(total_subagent_cost / combined_cost * 100, 1)
        result["subagent_costs"] = {
            "total_usd": round(total_subagent_cost, 2),
            "pct_of_spend": sub_pct,
            "top_subagents": all_subagent_costs[:5],
        }
        if sub_pct > 30:
            patterns_bad.append({
                "name": "Heavy Subagent Spend",
                "severity": "medium",
                "detail": f"Subagents consumed ${total_subagent_cost:.2f} ({sub_pct}% of recent spend)",
                "fix": ("Route data-gathering subagents to a smaller Codex model or lower reasoning effort. Reserve the frontier model for synthesis."
                        if is_codex else
                        "Route data-gathering subagents to Haiku. Reserve Opus for synthesis."),
                "savings": ("Not estimated: requires a measured model-routing comparison"
                            if is_codex else
                            f"~${total_subagent_cost * 0.6:.2f} with Haiku routing"),
            })
            score = max(0, result["health_score"] - 5)
            result["health_score"] = score

    if all_costly_prompts:
        result["costly_prompts"] = all_costly_prompts[:5]

    if include_deterministic:
        result["deterministic_candidates"] = _measure_facade._deterministic_candidates_data(
            days=30, budget_s=_measure_facade._DETCAND_COACH_BUDGET_S, max_sessions=_measure_facade._DETCAND_COACH_MAX_SESSIONS)
    # Compact-window replay of the user's own history: capped, cached, fail-open
    # (Claude transcripts only). Never lets this block fail the coach. Like the
    # deterministic scan it replays transcripts, so only the coach CLI asks for
    # it: the dashboard and rollup callers must not pay for a history replay.
    if include_deterministic and not is_codex:
        try:
            advice = _measure_facade._coach_compact_advice_block()
            if advice:
                result["compact_advice"] = advice
        except Exception:
            pass

    # Usage recommendations: the coach data channel also feeds the
    # dashboard's "From your own usage" section. This is the dashboard data
    # collection spawn point too: a missing/stale record starts the detached
    # refresh, the record itself is only ever READ here.
    try:
        _measure_facade._recs_ensure_fresh()
        _rb = _measure_facade._recs_block()
        if _rb is not None:
            result["usage_recommendations"] = _rb
    except Exception:
        pass

    return result


def generate_coach_block(components=None, trends=None):
    """Generate a passive coaching advice block from quality + savings data.

    Returns markdown string suitable for CLAUDE.md injection.
    """
    _measure_facade = _compat.facade()
    if components is None:
        components = _measure_facade.measure_components()
    is_codex = _measure_facade.detect_runtime() == "codex"
    instruction_label = "AGENTS.md" if is_codex else "CLAUDE.md"
    if trends is None:
        try:
            trends = _measure_facade._collect_trends_data(days=30)
        except Exception:
            trends = None

    lines = [
        "## Session Coaching (by Token Optimizer)",
    ]

    # Compaction timing
    if trends:
        avg_duration = trends.get("avg_duration_minutes", 0)
        if avg_duration > 20:
            lines.append(f"- Sessions average {avg_duration:.0f} min. Use /compact proactively around the midpoint.")

    # Unused skills
    if trends:
        # Check for unused MCP via skill analysis proxy
        never_used = trends.get("skills", {}).get("never_used", [])
        if len(never_used) > 5:
            lines.append(f"- {len(never_used)} skills not invoked (Skill call or slash command) in 30 days. Review for archiving.")

    # Cache hit rate
    if trends:
        daily = trends.get("daily", [])
        if daily:
            recent_sessions = []
            for d in daily[:7]:
                recent_sessions.extend(
                    sd for sd in d.get("session_details", []) if not sd.get("continuation"))
            if recent_sessions:
                avg_chr = sum(s.get("cache_hit_rate", 0) for s in recent_sessions) / len(recent_sessions)
                avg_chr_pct = round(avg_chr * 100)
                if avg_chr_pct < 60:
                    lines.append(f"- Cache hit rate: {avg_chr_pct}%. Keep {instruction_label} and enabled plugins stable during a session for better cache reuse.")

    if len(lines) < 2:
        return None  # No useful advice to give

    return "\n".join(lines)


def _parse_sections(filepath):
    """Parse a markdown file into sections split on # or ## headers.

    Returns list of dicts:
      {title, level, content, char_start, char_end, lines}
    where char_start/char_end are character offsets in the file.
    """
    _measure_facade = _compat.facade()
    try:
        text = _measure_facade.Path(filepath).read_text(encoding="utf-8", errors="replace")
    except (OSError, IOError):
        return []

    sections = []
    header_re = _measure_facade.re.compile(r'^(#{1,2})\s+(.+)', _measure_facade.re.MULTILINE)
    matches = list(header_re.finditer(text))

    if not matches:
        # Whole file is one section
        lines = text.splitlines()
        return [{
            "title": _measure_facade.Path(filepath).name,
            "level": 0,
            "content": text,
            "char_start": 0,
            "char_end": len(text),
            "lines": lines,
        }]

    # If there's content before the first header, capture it
    if matches[0].start() > 0:
        pre = text[:matches[0].start()]
        if pre.strip():
            sections.append({
                "title": "(preamble)",
                "level": 0,
                "content": pre,
                "char_start": 0,
                "char_end": matches[0].start(),
                "lines": pre.splitlines(),
            })

    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start:end]
        sections.append({
            "title": m.group(2).strip(),
            "level": len(m.group(1)),
            "content": content,
            "char_start": start,
            "char_end": end,
            "lines": content.splitlines(),
        })

    return sections


def _find_critical_rules(lines):
    """Find lines containing critical keywords. Returns list of stripped line texts."""
    _measure_facade = _compat.facade()
    results = []
    for line in lines:
        if _measure_facade._CRITICAL_PATTERN.search(line):
            stripped = line.strip().lstrip("-*> ").strip()
            if stripped and len(stripped) > 5:
                results.append(stripped)
    return results


def _classify_zone(pos_start, pos_end):
    """Classify a section's zone based on its midpoint position (0.0-1.0)."""
    _measure_facade = _compat.facade()
    mid = (pos_start + pos_end) / 2
    if mid < _measure_facade._LOW_ZONE_START:
        return "HIGH"
    elif mid > _measure_facade._LOW_ZONE_END:
        return "HIGH"
    else:
        return "LOW"


def _score_attention(sections_analyzed):
    """Calculate overall attention score (0-100).

    100 = all critical rules in HIGH zone
    Deductions for each critical rule in LOW zone.
    """
    _measure_facade = _compat.facade()
    total_critical = 0
    low_critical = 0
    for s in sections_analyzed:
        total_critical += s["critical_count"]
        if s["zone"] == "LOW":
            low_critical += s["critical_count"]
    if total_critical == 0:
        return 100
    ratio = low_critical / total_critical
    # Score: 100 minus penalty proportional to ratio of critical rules in LOW zone
    score = max(0, int(100 - (ratio * 100 * 0.8)))
    return score


def _mr_parse_memory_index(filepath):
    """Parse a MEMORY.md file into structured entries.

    Returns dict with:
      entries: list of {heading, body, links, line_start, line_end, date, raw_lines}
      total_lines: int
      links_all: list of {text, target, line, entry_idx}
    """
    _measure_facade = _compat.facade()
    if not filepath or not _measure_facade.Path(filepath).exists():
        return {"entries": [], "total_lines": 0, "links_all": []}

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    total_lines = len(lines)
    entries = []
    links_all = []
    current_entry = None

    for i, line in enumerate(lines):
        stripped = line.rstrip("\n")
        if stripped.startswith("## "):
            # Close previous entry
            if current_entry is not None:
                current_entry["line_end"] = i - 1
                entries.append(current_entry)
            heading = stripped[3:].strip()
            # Extract date from heading — use the LAST match (Updated > Set)
            all_dates = _measure_facade._MR_DATE_RE.findall(heading)
            date_match = all_dates[-1] if all_dates else None
            current_entry = {
                "heading": heading,
                "body": [],
                "links": [],
                "line_start": i,
                "line_end": None,
                "date": date_match if date_match else None,
                "raw_lines": [],
            }
        elif stripped.startswith("# ") and not stripped.startswith("## "):
            # Top-level heading (e.g. "# Memory") - skip, not an entry
            if current_entry is not None:
                current_entry["line_end"] = i - 1
                entries.append(current_entry)
                current_entry = None
        elif current_entry is not None:
            current_entry["body"].append(stripped)
            current_entry["raw_lines"].append((i, stripped))
            # Extract links
            for m in _measure_facade._MR_LINK_RE.finditer(stripped):
                link = {"text": m.group(1), "target": m.group(2),
                        "line": i, "entry_idx": len(entries)}
                current_entry["links"].append(link)
                links_all.append(link)
        else:
            # Auto-memory also writes flat indexes without section headings.
            # Keep these links for orphan/broken-link checks without creating entries.
            for m in _measure_facade._MR_LINK_RE.finditer(stripped):
                links_all.append({"text": m.group(1), "target": m.group(2),
                                  "line": i, "entry_idx": None})

    # Close last entry
    if current_entry is not None:
        current_entry["line_end"] = total_lines - 1
        entries.append(current_entry)

    return {"entries": entries, "total_lines": total_lines, "links_all": links_all}


def _mr_scan_topic_files(memory_dir):
    """List all .md files in the memory directory excluding MEMORY.md itself.

    Returns list of filenames (not full paths).
    """
    _measure_facade = _compat.facade()
    if not memory_dir or not _measure_facade.Path(memory_dir).is_dir():
        return []
    results = []
    for f in sorted(_measure_facade.Path(memory_dir).glob("*.md")):
        if f.name.upper() != "MEMORY.MD":
            results.append(f.name)
    return results


def _mr_detect_orphans(links_all, files_on_disk):
    """Detect orphaned topic files and broken links.

    Orphan = file on disk but not linked from MEMORY.md (INFO severity).
    Broken = link in MEMORY.md pointing to non-existent file (WARNING severity).
    """
    _measure_facade = _compat.facade()
    findings = []
    # Normalize link targets: resolve path separators to just the filename
    # so "references/notes.md" matches "notes.md" on disk
    linked_basenames = {_measure_facade.Path(lnk["target"]).name for lnk in links_all}
    files_set = set(files_on_disk)

    # Broken links (WARNING) — target basename not found on disk
    for lnk in links_all:
        target_basename = _measure_facade.Path(lnk["target"]).name
        if target_basename not in files_set:
            findings.append({
                "category": "broken_link",
                "severity": "medium",
                "detail": f"Link to '{lnk['target']}' at line {lnk['line'] + 1} points to non-existent file",
                "line": lnk["line"],
                "fix": f"Remove or fix the broken link to {lnk['target']}",
                "savings": 0,
            })

    # Orphaned files — on disk but not linked from MEMORY.md
    for fname in sorted(files_set - linked_basenames):
        findings.append({
            "category": "orphan",
            "severity": "low",
            "detail": f"Topic file '{fname}' exists but is not linked from MEMORY.md",
            "file": fname,
            "fix": f"Add index entry linking to {fname}, or delete if no longer needed",
            "savings": 0,
        })

    return findings


def _mr_detect_inline_content(entries):
    """Detect entries with excessive inline content (should be in topic files).

    Flags entries where body has >3 non-blank lines before the first link.
    """
    _measure_facade = _compat.facade()
    findings = []
    for idx, entry in enumerate(entries):
        non_blank_before_link = 0
        first_link_found = False
        for line_no, line_text in entry.get("raw_lines", []):
            if _measure_facade._MR_LINK_RE.search(line_text):
                first_link_found = True
                break
            if line_text.strip():
                non_blank_before_link += 1

        if non_blank_before_link > 3:
            est_tokens = non_blank_before_link * 15  # rough estimate based on inline lines only
            findings.append({
                "category": "inline_content",
                "severity": "low",
                "detail": f"Entry '{entry['heading'][:60]}' has {non_blank_before_link} lines of inline content"
                          + (" (no topic file link)" if not first_link_found and not entry["links"] else ""),
                "line": entry["line_start"],
                "entry_idx": idx,
                "fix": "Move inline content to a topic file and add a one-line index entry with link",
                "savings": est_tokens,
            })

    return findings


def _mr_detect_staleness(entries, stale_days=None):
    """Detect stale entries by keyword and date.

    Primary: RESOLVED/SUPERSEDED/PRIOR STATE keywords.
    Secondary: entries with dates older than stale_days.
    """
    _measure_facade = _compat.facade()
    findings = []
    today = _measure_facade.datetime.now()

    for idx, entry in enumerate(entries):
        heading = entry["heading"]

        # Keyword-based staleness (always flagged)
        for _, line_text in entry.get("raw_lines", []):
            if _measure_facade._MR_STALE_KEYWORD_RE.search(line_text):
                findings.append({
                    "category": "stale_keyword",
                    "severity": "low",
                    "detail": f"Entry '{heading[:60]}' contains staleness marker",
                    "line": entry["line_start"],
                    "entry_idx": idx,
                    "fix": "Remove resolved/superseded entry or move to archive",
                    "savings": len(entry.get("raw_lines", [])) * 15,
                })
                break  # One finding per entry

        # Date-based staleness (advisory)
        if entry["date"]:
            try:
                entry_date = _measure_facade.datetime.strptime(entry["date"], "%Y-%m-%d")
                age_days = (today - entry_date).days
                if age_days > stale_days:
                    # Don't double-flag if already caught by keyword
                    already_flagged = any(f["entry_idx"] == idx for f in findings)
                    if not already_flagged:
                        findings.append({
                            "category": "stale_date",
                            "severity": "low",
                            "detail": f"Entry '{heading[:60]}' is {age_days} days old (threshold: {stale_days})",
                            "line": entry["line_start"],
                            "entry_idx": idx,
                            "fix": "Review if still relevant. Remove or update if stale.",
                            "savings": 0,
                        })
            except ValueError:
                pass

    return findings


def _mr_detect_duplicates(entries, claude_md_contents):
    """Detect duplicate rules within MEMORY.md and cross-check against CLAUDE.md.

    Uses normalized comparison of NEVER/ALWAYS/MUST rule lines.
    """
    _measure_facade = _compat.facade()
    findings = []

    def _extract_rules(text_lines):
        """Extract and normalize rule-bearing lines."""
        _measure_facade = _compat.facade()
        rules = []
        for line in text_lines:
            if _measure_facade._MR_RULE_RE.search(line):
                normalized = line.strip().lower()
                # Strip common prefixes
                for prefix in ("- ", "* ", "> "):
                    if normalized.startswith(prefix):
                        normalized = normalized[len(prefix):]
                        break
                rules.append(normalized)
        return rules

    # Within-MEMORY.md duplicates
    all_rules = {}  # normalized_rule -> [(entry_idx, line)]
    for idx, entry in enumerate(entries):
        body_text = [ln for _, ln in entry.get("raw_lines", [])]
        rules = _extract_rules(body_text)
        for rule in rules:
            if rule not in all_rules:
                all_rules[rule] = []
            all_rules[rule].append((idx, entry["heading"][:60]))

    for rule, locations in all_rules.items():
        if len(locations) > 1:
            headings = [loc[1] for loc in locations]
            findings.append({
                "category": "duplicate_internal",
                "severity": "low",
                "detail": f"Similar rule appears in {len(locations)} entries: {', '.join(headings[:3])}",
                "fix": "Consolidate duplicate rules into a single entry",
                "savings": (len(locations) - 1) * 15,
            })

    # Cross-check against CLAUDE.md
    if claude_md_contents:
        claude_rules = set()
        for content in claude_md_contents:
            for rule in _extract_rules(content.splitlines()):
                claude_rules.add(rule)

        memory_rules = set()
        for idx, entry in enumerate(entries):
            body_text = [ln for _, ln in entry.get("raw_lines", [])]
            for rule in _extract_rules(body_text):
                memory_rules.add(rule)

        overlap = memory_rules & claude_rules
        if overlap:
            findings.append({
                "category": "duplicate_cross",
                "severity": "low",
                "detail": f"{len(overlap)} rule(s) in MEMORY.md also appear in CLAUDE.md (may be intentional)",
                "fix": "CLAUDE.md is always loaded. Rules duplicated there waste MEMORY.md budget.",
                "savings": len(overlap) * 15,
            })

    return findings


def _mr_detect_task_leakage(entries):
    """Detect task-tracking content that belongs in a task tracker, not memory.

    Only flags: - [ ] checkbox syntax or task-tracking section headings.
    """
    _measure_facade = _compat.facade()
    findings = []
    for idx, entry in enumerate(entries):
        # Check heading
        if _measure_facade._MR_TASK_HEADING_RE.match("## " + entry["heading"]):
            findings.append({
                "category": "task_leakage",
                "severity": "low",
                "detail": f"Task-tracking section '{entry['heading'][:60]}' should be in a task tracker",
                "line": entry["line_start"],
                "entry_idx": idx,
                "fix": "Move to project PLANS.md or task tracker. Memory is for durable facts, not ephemeral tasks.",
                "savings": len(entry.get("raw_lines", [])) * 15,
            })
            continue

        # Check for checkbox syntax in body
        checkbox_count = sum(1 for _, ln in entry.get("raw_lines", []) if _measure_facade._MR_CHECKBOX_RE.match(ln))
        if checkbox_count >= 2:
            findings.append({
                "category": "task_leakage",
                "severity": "low",
                "detail": f"Entry '{entry['heading'][:60]}' contains {checkbox_count} task checkboxes",
                "line": entry["line_start"],
                "entry_idx": idx,
                "fix": "Move task list to project PLANS.md or task tracker",
                "savings": checkbox_count * 15,
            })

    return findings


def _mr_detect_truncation_waste(entries, links_all, total_lines):
    """Detect entries and links below the visible line limit that are invisible to Claude.

    Claude auto-loads only the first _MR_MEMORY_LINE_LIMIT lines of MEMORY.md.
    """
    _measure_facade = _compat.facade()
    CUTOFF = _measure_facade._MR_MEMORY_LINE_LIMIT
    if total_lines <= CUTOFF:
        return []

    findings = []
    invisible_entries = []
    invisible_links = []

    for idx, entry in enumerate(entries):
        if entry["line_start"] >= CUTOFF:
            invisible_entries.append(entry)

    for lnk in links_all:
        if lnk["line"] >= CUTOFF:
            invisible_links.append(lnk)

    if invisible_entries:
        entry_headings = [e["heading"][:50] for e in invisible_entries[:5]]
        remaining = len(invisible_entries) - 5
        detail = f"{len(invisible_entries)} entries below line 200 (invisible to Claude)"
        if entry_headings:
            detail += f": {', '.join(entry_headings)}"
            if remaining > 0:
                detail += f" (+{remaining} more)"
        findings.append({
            "category": "truncation",
            "severity": "medium",
            "detail": detail,
            "fix": f"Promote important entries above line 200 or remove low-value ones to make room. "
                   f"Currently {total_lines} lines, {total_lines - CUTOFF} over the limit.",
            "savings": (total_lines - CUTOFF) * 15,
        })

    if invisible_links:
        findings.append({
            "category": "truncation_links",
            "severity": "medium",
            "detail": f"{len(invisible_links)} topic file links below line 200 — their topic files are effectively orphaned",
            "fix": "Move these links above line 200 so Claude can discover the topic files",
            "savings": 0,
        })

    return findings


def _mr_detect_taxonomy(files_on_disk):
    """Check topic file naming conventions (INFO severity only).

    Expected prefixes: feedback_, project_, session_, reference_, user_.
    Files without a recognized prefix get flagged as advisory.
    """
    _measure_facade = _compat.facade()
    KNOWN_PREFIXES = ("feedback_", "project_", "session_", "reference_", "user_")
    findings = []
    untyped = []
    for fname in files_on_disk:
        if not any(fname.startswith(p) for p in KNOWN_PREFIXES):
            untyped.append(fname)

    if untyped and len(untyped) >= 3:
        findings.append({
            "category": "taxonomy",
            "severity": "low",
            "detail": f"{len(untyped)} topic files lack standard prefix (feedback_/project_/session_/reference_/user_): "
                      + ", ".join(sorted(untyped)[:5])
                      + (f" (+{len(untyped) - 5} more)" if len(untyped) > 5 else ""),
            "fix": "Consider renaming for consistency. This is advisory, not a requirement.",
            "savings": 0,
        })

    return findings


def _mr_compute_savings(findings):
    """Aggregate savings breakdown by category with net result projection."""
    _measure_facade = _compat.facade()
    by_category = {}
    total = 0
    for f in findings:
        cat = f.get("category", "other")
        savings = f.get("savings", 0)
        by_category[cat] = by_category.get(cat, 0) + savings
        total += savings
    return {"total_tokens": total, "by_category": by_category}


def memory_review(as_json=None, apply=None, stale_days=None, project_dir=None):
    """Structural audit of MEMORY.md and CLAUDE.md.

    Detects: orphaned/broken links, inline content, staleness, duplicates,
    task leakage, truncation waste. Returns structured report.
    """
    # Resolve project directory (with path containment check)
    _measure_facade = _compat.facade()
    if project_dir:
        projects_dir = _measure_facade.Path(project_dir).resolve()
        # Containment: must be under user home or ~/.claude/projects/
        home = _measure_facade.Path.home().resolve()
        if not str(projects_dir).startswith(str(home)):
            if as_json:
                return {"error": "project-dir must be under user home directory",
                        "findings": [], "summary": {}}
            print("  [Error] --project-dir must be under your home directory.")
            return None
    else:
        projects_dir = _measure_facade.find_projects_dir()

    if not projects_dir:
        if as_json:
            return {"error": "No project directory found", "findings": [], "summary": {}}
        print("  [Error] No Claude Code project directory found.")
        print("  Use --project-dir PATH to specify manually.")
        return None

    memory_dir = projects_dir / "memory"
    memory_path = memory_dir / "MEMORY.md"

    if not memory_path.exists():
        if as_json:
            return {"error": "No MEMORY.md found", "target": str(memory_dir),
                    "findings": [], "summary": {}}
        print(f"  [Info] No MEMORY.md found at {memory_dir}")
        return None

    # Parse MEMORY.md
    parsed = _measure_facade._mr_parse_memory_index(str(memory_path))
    entries = parsed["entries"]
    links_all = parsed["links_all"]
    total_lines = parsed["total_lines"]

    # Scan topic files
    files_on_disk = _measure_facade._mr_scan_topic_files(str(memory_dir))

    # Load CLAUDE.md contents for cross-check
    components = _measure_facade.measure_components()
    claude_md_contents = []
    for key in components:
        if key.startswith("claude_md") and components[key].get("exists"):
            cpath = components[key].get("path")
            if cpath and _measure_facade.Path(cpath).exists():
                try:
                    claude_md_contents.append(_measure_facade.Path(cpath).read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    pass

    # Run all detectors
    all_findings = []
    all_findings.extend(_measure_facade._mr_detect_orphans(links_all, files_on_disk))
    all_findings.extend(_measure_facade._mr_detect_inline_content(entries))
    all_findings.extend(_measure_facade._mr_detect_staleness(entries, stale_days=stale_days))
    all_findings.extend(_measure_facade._mr_detect_duplicates(entries, claude_md_contents))
    all_findings.extend(_measure_facade._mr_detect_task_leakage(entries))
    all_findings.extend(_measure_facade._mr_detect_truncation_waste(entries, links_all, total_lines))
    all_findings.extend(_measure_facade._mr_detect_taxonomy(files_on_disk))

    # Compute savings
    savings = _measure_facade._mr_compute_savings(all_findings)

    # Build summary
    severity_counts = {"high": 0, "medium": 0, "low": 0}
    for f in all_findings:
        sev = f.get("severity", "info")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1

    category_counts = {}
    for f in all_findings:
        cat = f.get("category", "other")
        category_counts[cat] = category_counts.get(cat, 0) + 1

    # Build rule inventory for in-session contradiction detection
    # The LLM can review these pairs semantically when run inside a Claude session
    rule_inventory = []
    for idx, entry in enumerate(entries):
        for line_no, line_text in entry.get("raw_lines", []):
            if _measure_facade._MR_RULE_RE.search(line_text):
                rule_inventory.append({
                    "text": line_text.strip(),
                    "source": "MEMORY.md",
                    "entry": entry["heading"][:80],
                    "line": line_no + 1,
                })
    for ci, content in enumerate(claude_md_contents):
        for i, line in enumerate(content.splitlines()):
            if _measure_facade._MR_RULE_RE.search(line):
                rule_inventory.append({
                    "text": line.strip(),
                    "source": "CLAUDE.md",
                    "entry": "",
                    "line": i + 1,
                })

    result = {
        "target": str(memory_path),
        "total_lines": total_lines,
        "entry_count": len(entries),
        "topic_files_count": len(files_on_disk),
        "linked_files_count": len({_measure_facade.Path(lnk["target"]).name for lnk in links_all} & set(files_on_disk)),
        "findings": all_findings,
        "severity_counts": severity_counts,
        "category_counts": category_counts,
        "savings": savings,
        "truncated": total_lines > 200,
        "truncated_lines": max(0, total_lines - 200),
        "rule_inventory": rule_inventory,
    }

    if as_json:
        return result

    # CLI output
    print(f"\n{'=' * 55}")
    print("  MEMORY REVIEW")
    print(f"{'=' * 55}")
    print(f"\n  Target: {memory_path}")
    print(f"  Lines: {total_lines} / 200 limit" + (f" ({total_lines - 200} over)" if total_lines > 200 else " (OK)"))
    print(f"  Entries: {len(entries)} | Topic files: {len(files_on_disk)} | Linked: {len({link['target'] for link in links_all})}")

    if not all_findings:
        print("\n  No structural issues found.")
        return result

    # Group by severity
    sev_order = ["high", "medium", "low"]
    sev_labels = {"high": "CRITICAL", "medium": "WARNING", "low": "NOTICE"}
    sev_colors = {"high": "\033[31m", "medium": "\033[33m", "low": "\033[36m"}
    reset = "\033[0m"

    print(f"\n  Found {len(all_findings)} issue(s): "
          f"{severity_counts.get('medium', 0)} warnings, "
          f"{severity_counts.get('low', 0)} notices, "
          f"{severity_counts.get('info', 0)} info")

    for sev in sev_order:
        sev_findings = [f for f in all_findings if f.get("severity") == sev]
        if not sev_findings:
            continue
        print(f"\n  [{sev_labels[sev]}]")
        for f in sev_findings:
            prefix = sev_colors.get(sev, "")
            print(f"  {prefix}  {f['detail']}{reset}")
            if f.get("fix"):
                print(f"      Fix: {f['fix']}")

    if savings["total_tokens"] > 0:
        print(f"\n  Estimated savings: ~{savings['total_tokens']:,} tokens")
        for cat, tokens in sorted(savings["by_category"].items(), key=lambda x: ((_measure_facade := _compat.facade()), -x[1])[1]):
            if tokens > 0:
                cat_label = cat.replace("_", " ").title()
                print(f"    {cat_label}: ~{tokens:,} tokens")

    # Net result projection
    inline_count = len([f for f in all_findings if f["category"] == "inline_content"])
    stale_count = len([f for f in all_findings if f["category"] in ("stale_keyword", "stale_date")])
    task_count = len([f for f in all_findings if f["category"] == "task_leakage"])
    removable_lines = inline_count * 4 + stale_count * 3 + task_count * 5  # rough per-finding estimate
    projected_lines = max(0, total_lines - removable_lines)
    orphan_count = len([f for f in all_findings if f["category"] == "orphan"])
    files_set = set(files_on_disk)
    linked_count = len({_measure_facade.Path(link["target"]).name for link in links_all} & files_set)

    print("\n  After cleanup (projected):")
    print(f"    Lines: ~{projected_lines} (currently {total_lines})"
          + (f" — {'still over' if projected_lines > 200 else 'under'} 200-line limit" if total_lines > 200 else ""))
    print(f"    Tokens saved: ~{savings['total_tokens']:,}")
    print(f"    Topic files reachable: {linked_count + orphan_count} of {len(files_on_disk)} (currently {linked_count})")
    if total_lines > 200 and projected_lines <= 200:
        print("    Truncation: eliminated (0 lines lost)")
    elif total_lines > 200:
        print(f"    Truncation: ~{projected_lines - 200} lines still over (down from {total_lines - 200})")

    print()

    # Apply mode
    if apply:
        _measure_facade._mr_apply_fixes(all_findings, memory_path, memory_dir)

    return result


def _mr_apply_fixes(findings, memory_path, memory_dir):
    """Preview actionable fixes from memory review findings.

    Currently shows fix suggestions for user to apply manually or in-session
    with Claude. Auto-apply for file operations is planned for a future version.
    """
    _measure_facade = _compat.facade()
    actionable = [f for f in findings if f.get("fix") and f.get("category") in
                  ("broken_link", "stale_keyword", "task_leakage", "inline_content",
                   "truncation", "truncation_links", "orphan")]

    if not actionable:
        print("  No actionable fixes found.")
        return

    print(f"\n  {len(actionable)} actionable fix(es):")
    print("  (Copy these into a Claude session to apply, or edit MEMORY.md manually)\n")

    for i, f in enumerate(actionable, 1):
        print(f"  {i}. [{f['category']}] {f['detail']}")
        print(f"     Fix: {f['fix']}")
        if f.get("savings", 0) > 0:
            print(f"     Saves: ~{f['savings']} tokens")
        print()


def _analyze_attention_sections(sections):
    """Shared analysis for attention_score and attention_optimize.

    Returns (analyzed, total_chars, total_tokens) where analyzed is a list
    of dicts with position, zone, critical rules, density, and content.
    """
    _measure_facade = _compat.facade()
    total_chars = sum(s["char_end"] - s["char_start"] for s in sections)
    total_tokens = int(total_chars / _measure_facade.CHARS_PER_TOKEN)

    analyzed = []
    cumulative = 0
    for s in sections:
        section_chars = s["char_end"] - s["char_start"]
        pos_start = cumulative / total_chars if total_chars > 0 else 0
        cumulative += section_chars
        pos_end = cumulative / total_chars if total_chars > 0 else 0
        zone = _measure_facade._classify_zone(pos_start, pos_end)
        critical_rules = _measure_facade._find_critical_rules(s["lines"])
        tokens = int(section_chars / _measure_facade.CHARS_PER_TOKEN)
        line_count = len([ln for ln in s["lines"] if ln.strip()])
        density = len(critical_rules) / max(line_count, 1)

        analyzed.append({
            "title": s["title"],
            "level": s["level"],
            "pos_start": pos_start,
            "pos_end": pos_end,
            "zone": zone,
            "critical_rules": critical_rules,
            "critical_count": len(critical_rules),
            "density": density,
            "tokens": tokens,
            "chars": section_chars,
            "content": s["content"],
            "lines": s["lines"],
        })

    return analyzed, total_chars, total_tokens


def attention_score(filepath=None, as_json=None):
    """Score a file against the U-shaped attention curve."""
    _measure_facade = _compat.facade()
    if filepath is None:
        filepath = str(_measure_facade.CLAUDE_DIR / "CLAUDE.md")

    fp = _measure_facade.Path(filepath).expanduser()
    if not fp.exists():
        print(f"[Error] File not found: {fp}")
        _measure_facade.sys.exit(1)

    sections = _measure_facade._parse_sections(str(fp))
    if not sections:
        print(f"[Error] No content found in: {fp}")
        _measure_facade.sys.exit(1)

    analyzed, total_chars, total_tokens = _measure_facade._analyze_attention_sections(sections)

    score = _measure_facade._score_attention(analyzed)
    low_critical_total = sum(a["critical_count"] for a in analyzed if a["zone"] == "LOW")

    # Collect warnings
    warnings = []
    for a in analyzed:
        if a["zone"] == "LOW" and a["critical_count"] > 0:
            pct_start = int(a["pos_start"] * 100)
            pct_end = int(a["pos_end"] * 100)
            warnings.append({
                "section": a["title"],
                "position": f"{pct_start}-{pct_end}%",
                "critical_count": a["critical_count"],
                "critical_rules": a["critical_rules"],
            })

    if as_json:
        result = {
            "file": str(fp),
            "sections": len(analyzed),
            "total_tokens": total_tokens,
            "score": score,
            "critical_in_low_zone": low_critical_total,
            "sections_detail": [
                {
                    "title": a["title"],
                    "position": f"{int(a['pos_start'] * 100)}-{int(a['pos_end'] * 100)}%",
                    "zone": a["zone"],
                    "critical_count": a["critical_count"],
                    "tokens": a["tokens"],
                    "critical_rules": a["critical_rules"],
                }
                for a in analyzed
            ],
            "warnings": warnings,
        }
        print(_measure_facade.json.dumps(result, indent=2))
        return result

    # Pretty print
    display_name = str(fp).replace(str(_measure_facade.HOME), "~")
    print(f"\n  Attention Score: {fp.name}")
    print(f"  {'=' * 50}")
    print(f"  File: {display_name}")
    print(f"  Sections: {len(analyzed)} | Tokens: ~{total_tokens:,}")
    print(f"  Critical rules in LOW attention zone: {low_critical_total}")
    print()
    print("  Section Analysis:")
    print(f"    {'Position':<10} {'Zone':<6}  {'Section':<32} {'Critical':<10} {'Tokens':>6}")
    print(f"    {'--------':<10} {'------':<6}  {'----------------------------':<32} {'--------':<10} {'------':>6}")

    for a in analyzed:
        pct_start = int(a["pos_start"] * 100)
        pct_end = int(a["pos_end"] * 100)
        pos_str = f"{pct_start}-{pct_end}%"
        title_trunc = a["title"][:30]
        flag = "  !!!" if (a["zone"] == "LOW" and a["critical_count"] > 0) else ""
        crit_str = str(a["critical_count"]) if a["critical_count"] > 0 else "0"
        print(f"    {pos_str:<10} {a['zone']:<6}  {title_trunc:<32} {crit_str:<10}{flag:>5} {a['tokens']:>6}")

    if warnings:
        print()
        print("  ATTENTION WARNINGS:")
        for w in warnings:
            print(f"  - \"{w['section']}\" has {w['critical_count']} critical rule{'s' if w['critical_count'] != 1 else ''} in LOW zone ({w['position']})")
            for rule in w["critical_rules"][:5]:
                display = rule[:80] + "..." if len(rule) > 80 else rule
                print(f"    -> {display}")
            print("    -> Move to first 30% or last 30% of file")

    print(f"\n  Overall score: {score}/100 ({low_critical_total} critical rule{'s' if low_critical_total != 1 else ''} at risk)")
    print()
    return {"score": score, "sections": analyzed, "warnings": warnings}


def attention_optimize(filepath=None, dry_run=None, apply=None):
    """Reorder sections to maximize attention for critical rules."""
    _measure_facade = _compat.facade()
    if filepath is None:
        filepath = str(_measure_facade.CLAUDE_DIR / "CLAUDE.md")

    fp = _measure_facade.Path(filepath).expanduser()
    if not fp.exists():
        print(f"[Error] File not found: {fp}")
        _measure_facade.sys.exit(1)

    sections = _measure_facade._parse_sections(str(fp))
    if not sections:
        print(f"[Error] No content found in: {fp}")
        _measure_facade.sys.exit(1)

    scored, total_chars, _ = _measure_facade._analyze_attention_sections(sections)

    # Map shared analysis fields to optimize-specific names
    for s in scored:
        s["original_pos_start"] = s["pos_start"]
        s["original_pos_end"] = s["pos_end"]
        s["original_zone"] = s["zone"]

    # Calculate before-score
    before_score = _measure_facade._score_attention(scored)

    # Sort into three zones:
    # Zone 1 (top 30%): highest critical density
    # Zone 3 (bottom 30%): medium critical density + paths/reminders/security
    # Zone 2 (middle 40%): lowest critical density (reference material)

    # Separate preamble (always stays at top)
    preamble = [s for s in scored if s["title"] == "(preamble)"]
    rest = [s for s in scored if s["title"] != "(preamble)"]

    # Sort by critical density descending
    rest_sorted = sorted(rest, key=lambda s: ((_measure_facade := _compat.facade()), s["density"])[1], reverse=True)

    # Partition: top third -> Zone 1, bottom third -> Zone 3, middle -> Zone 2
    n = len(rest_sorted)
    if n <= 2:
        zone1 = rest_sorted
        zone2 = []
        zone3 = []
    else:
        cut1 = max(1, n // 3)
        cut2 = max(cut1 + 1, n - n // 3)
        zone1 = rest_sorted[:cut1]
        zone2 = rest_sorted[cut1:cut2]
        zone3 = rest_sorted[cut2:]

    reordered = preamble + zone1 + zone2 + zone3

    # Calculate after-score by simulating new positions
    new_total = sum(s["tokens"] * _measure_facade.CHARS_PER_TOKEN for s in reordered)
    new_cumulative = 0
    after_analyzed = []
    for s in reordered:
        section_chars = s["tokens"] * _measure_facade.CHARS_PER_TOKEN
        pos_start = new_cumulative / new_total if new_total > 0 else 0
        new_cumulative += section_chars
        pos_end = new_cumulative / new_total if new_total > 0 else 0
        zone = _measure_facade._classify_zone(pos_start, pos_end)
        after_analyzed.append({
            "zone": zone,
            "critical_count": s["critical_count"],
        })
    after_score = _measure_facade._score_attention(after_analyzed)

    # Determine moves
    moves = []
    original_order = [s["title"] for s in scored]
    new_order = [s["title"] for s in reordered]
    for i, title in enumerate(new_order):
        old_idx = original_order.index(title)
        old_s = scored[old_idx]
        new_s = reordered[i]
        # Calculate new position
        chars_before = sum(r["tokens"] * _measure_facade.CHARS_PER_TOKEN for r in reordered[:i])
        new_pos_start = chars_before / new_total if new_total > 0 else 0
        new_pos_end = (chars_before + new_s["tokens"] * _measure_facade.CHARS_PER_TOKEN) / new_total if new_total > 0 else 0
        new_zone = _measure_facade._classify_zone(new_pos_start, new_pos_end)

        old_pct = f"{int(old_s['original_pos_start'] * 100)}-{int(old_s['original_pos_end'] * 100)}%"
        new_pct = f"{int(new_pos_start * 100)}-{int(new_pos_end * 100)}%"

        if old_idx == i:
            moves.append(f"KEEP: \"{title}\" stays at {old_pct}")
        else:
            reason = ""
            if new_s["critical_count"] > 0 and old_s["original_zone"] == "LOW" and new_zone == "HIGH":
                reason = f" <- has {new_s['critical_count']} critical rule{'s' if new_s['critical_count'] != 1 else ''}"
            moves.append(f"MOVE: \"{title}\" ({old_pct} -> {new_pct}){reason}")

    display_name = str(fp).replace(str(_measure_facade.HOME), "~")

    if dry_run and not apply:
        print("\n  Attention Optimizer (DRY RUN)")
        print(f"  {'=' * 50}")
        print(f"  File: {display_name}")
        print()
        print("  Proposed reordering:")
        for m in moves:
            print(f"    {m}")
        print()
        print(f"  Before: {before_score}/100 attention score")
        print(f"  After:  {after_score}/100 attention score (estimated)")
        print()
        print(_measure_facade._hint(f"  To apply: python3 measure.py attention-optimize {display_name} --apply"))
        print()
        return {"before_score": before_score, "after_score": after_score, "moves": moves}

    if apply:
        # Build reordered content
        new_content = ""
        for s in reordered:
            new_content += s["content"]
            # Ensure section ends with newline
            if not new_content.endswith("\n"):
                new_content += "\n"

        # Backup original (with timestamp like jsonl_trim/dedup)
        ts = _measure_facade.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = _measure_facade.Path(str(fp) + f".{ts}.bak")
        try:
            import shutil
            shutil.copy2(str(fp), str(backup_path))
        except OSError as e:
            print(f"[Error] Could not create backup: {e}")
            _measure_facade.sys.exit(1)

        # Atomic write via temp file + rename
        try:
            fd, tmp_path = _measure_facade.tempfile.mkstemp(dir=str(fp.parent), suffix=".tmp")
            try:
                with _measure_facade.os.fdopen(fd, "w", encoding="utf-8") as tmp_f:
                    tmp_f.write(new_content)
                _measure_facade.os.replace(tmp_path, str(fp))
            except Exception:
                try:
                    _measure_facade.os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as e:
            print(f"[Error] Could not write file: {e}")
            _measure_facade.sys.exit(1)

        print("\n  Attention Optimizer (APPLIED)")
        print(f"  {'=' * 50}")
        print(f"  File: {display_name}")
        print(f"  Backup: {backup_path}")
        print()
        print("  Reordering applied:")
        for m in moves:
            print(f"    {m}")
        print()
        print(f"  Before: {before_score}/100 attention score")
        print(f"  After:  {after_score}/100 attention score")
        print()
        return {"before_score": before_score, "after_score": after_score, "backup": backup_path}
