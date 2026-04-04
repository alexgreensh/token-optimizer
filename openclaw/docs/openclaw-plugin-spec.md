# OpenClaw Plugin Specification Reference

Researched 2026-03-25 from docs.openclaw.ai. Use this as the single source of truth when building or debugging the Token Optimizer OpenClaw plugin.

---

## 1. Required Directory Structure

Minimum:
```
my-plugin/
  package.json              # npm metadata + openclaw namespace (REQUIRED)
  openclaw.plugin.json      # Plugin manifest (REQUIRED)
  dist/index.js             # Entry point referenced in openclaw.extensions
```

Full layout (our plugin):
```
token-optimizer/
  package.json
  openclaw.plugin.json
  dist/index.js
  hooks/
    smart-compact/
      HOOK.md               # Frontmatter + docs (REQUIRED for hooks)
      handler.ts            # Handler implementation (REQUIRED for hooks)
  skills/
    token-optimizer/
      SKILL.md
  src/
  tsconfig.json
```

---

## 2. package.json: The `openclaw` Namespace

The `openclaw` block tells the plugin system what your plugin provides at the packaging level.

### Full field reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `extensions` | `string[]` | **YES** | Entry point files, relative to package root. Installer validates these exist. |
| `hooks` | `string[]` | No | Hook directories to register. Each must contain `HOOK.md` + `handler.ts`. |
| `setupEntry` | `string` | No | Lightweight setup-only entry for channel plugins |
| `channel` | `object` | No | Channel metadata: `id`, `label`, `blurb`, `selectionLabel`, `docsPath`, `order`, `aliases` |
| `providers` | `string[]` | No | Provider ids registered by this plugin |
| `install` | `object` | No | Install hints: `npmSpec`, `localPath`, `defaultChoice` |
| `startup` | `object` | No | Startup behavior flags |

### Example
```json
{
  "name": "token-optimizer-openclaw",
  "version": "1.2.0",
  "openclaw": {
    "extensions": ["./dist/index.js"],
    "hooks": ["./hooks/smart-compact"]
  }
}
```

### Installer validation on package.json
- Checks `openclaw.extensions` array exists (HARD ERROR if missing)
- Validates each extension path resolves to a file inside the package root
- For `openclaw.hooks`, validates each entry points to a directory inside the package root
- Runs `npm install --ignore-scripts` for dependencies (no postinstall builds)

---

## 3. openclaw.plugin.json: The Manifest

Read **before loading plugin code**. Used for discovery, config validation, auth metadata, UI hints.

### Minimum viable manifest
```json
{
  "id": "token-optimizer",
  "configSchema": {
    "type": "object",
    "additionalProperties": false
  }
}
```

### Full field reference

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `id` | **YES** | `string` | Canonical plugin id. Must match `plugins.entries.<id>`. |
| `configSchema` | **YES** | `object` | Inline JSON Schema for plugin config. Even if empty, must be present. |
| `name` | No | `string` | Human-readable plugin name |
| `description` | No | `string` | Short summary |
| `version` | No | `string` | Informational plugin version |
| `enabledByDefault` | No | `true` | Only for bundled plugins |
| `kind` | No | `"memory"` or `"context-engine"` | Declares exclusive plugin kind |
| `channels` | No | `string[]` | Channel ids owned by this plugin |
| `providers` | No | `string[]` | Provider ids owned by this plugin |
| `providerAuthEnvVars` | No | `Record<string, string[]>` | Provider-auth env metadata |
| `providerAuthChoices` | No | `object[]` | Auth-choice metadata for onboarding |
| `skills` | No | `string[]` | Skill directories to load, relative to plugin root |
| `uiHints` | No | `Record<string, object>` | UI labels, placeholders, sensitivity hints |

### What does NOT belong in the manifest
- Runtime behavior registration (goes in entry point code)
- Code entrypoints (goes in `package.json` `openclaw.extensions`)
- npm install metadata (goes in `package.json`)
- Custom/undocumented fields are silently ignored

---

## 4. Entry Point: definePluginEntry()

The file referenced in `openclaw.extensions` must export a plugin definition.

```typescript
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

export default definePluginEntry({
  id: "token-optimizer",      // MUST match openclaw.plugin.json id
  name: "Token Optimizer",
  description: "Token waste auditor for OpenClaw",
  register(api) {
    api.registerService("token-optimizer", { ... });
    api.on("gateway:startup", () => { ... });
    api.on("session:compact:before", (ctx) => { ... });
  },
});
```

### Required fields in definePluginEntry

| Field | Type | Required |
|-------|------|----------|
| `id` | `string` | YES, must match manifest id |
| `name` | `string` | YES |
| `description` | `string` | YES |
| `register` | `(api: OpenClawPluginApi) => void` | YES |
| `kind` | `string` | No (for exclusive slots) |
| `configSchema` | `schema or () => schema` | No |

### Import convention
```typescript
// CORRECT (specific subpath)
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

// WRONG (deprecated, will be removed)
import { definePluginEntry } from "openclaw/plugin-sdk";
```

---

## 5. Hook Registration

### Via `openclaw.hooks` in package.json (hook packs)

Each entry points to a directory containing `HOOK.md` + `handler.ts`:

```
hooks/smart-compact/
  HOOK.md       # Frontmatter with metadata
  handler.ts    # Default export: async (event) => { ... }
```

### HOOK.md frontmatter format

```markdown
---
name: smart-compact
description: "Protect session state across compaction"
metadata:
  openclaw:
    emoji: "💾"
    events: ["session:compact:before", "session:compact:after"]
    requires:
      bins: ["node"]
---
```

### HOOK.md metadata fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | `string` | Hook name |
| `description` | `string` | Short description |
| `metadata.openclaw.emoji` | `string` | Display emoji for CLI |
| `metadata.openclaw.events` | `string[]` | Events to listen for |
| `metadata.openclaw.export` | `string` | Named export (defaults to `"default"`) |
| `metadata.openclaw.homepage` | `string` | Documentation URL |
| `metadata.openclaw.os` | `string[]` | Required platforms |
| `metadata.openclaw.requires.bins` | `string[]` | Required binaries on PATH |
| `metadata.openclaw.requires.anyBins` | `string[]` | At least one required |
| `metadata.openclaw.requires.env` | `string[]` | Required env vars |
| `metadata.openclaw.requires.config` | `string[]` | Required config paths |
| `metadata.openclaw.always` | `boolean` | Bypass eligibility checks |

### handler.ts format

```typescript
const handler = async (event) => {
  if (event.type !== "session" || event.action !== "compact:before") return;
  // your logic
  event.messages.push("Checkpoint saved!");
};
export default handler;
```

### Via entry point (plugin SDK approach)

```typescript
register(api) {
  api.on("before_compaction", (ctx) => { ... });
  api.registerHook(["session:compact:before"], handler);
}
```

---

## 6. Available Event Types

| Event | Type | When It Fires |
|-------|------|---------------|
| `command:new` | command | `/new` command issued |
| `command:reset` | command | `/reset` command issued |
| `command:stop` | command | `/stop` command issued |
| `session:compact:before` | session | Right before compaction |
| `session:compact:after` | session | After compaction completes |
| `session:patch` | session | When session properties change |
| `agent:bootstrap` | agent | Before workspace bootstrap files injected |
| `gateway:startup` | gateway | After channels start and hooks load |
| `message:received` | message | Inbound message from any channel |
| `message:transcribed` | message | After audio transcription |
| `message:preprocessed` | message | After all media/link processing |
| `message:sent` | message | Outbound message sent |
| `before_compaction` | plugin hook | Before compaction (plugin API) |
| `after_compaction` | plugin hook | After compaction (plugin API) |
| `before_tool_call` | plugin hook | Before tool execution |
| `message_sending` | plugin hook | Before message send |
| `tool_result_persist` | plugin hook | Transform tool results before persisting |

Note: `session:start` and `session:end` are listed as future/planned events.

---

## 7. Detection Precedence

When installing from a local directory, OpenClaw checks format in this order:
1. `openclaw.plugin.json` or valid `package.json` with `openclaw.extensions` = **native plugin**
2. `.codex-plugin/plugin.json` = Codex bundle
3. `.claude-plugin/plugin.json` or default Claude layout = Claude bundle
4. `.cursor-plugin/plugin.json` = Cursor bundle

Native format always wins over bundle detection.

---

## 8. Installer Validation Checklist

What `openclaw plugins install ./` validates:

1. `package.json` exists and is valid JSON
2. `openclaw.extensions` exists in package.json (HARD ERROR)
3. Each extension file resolves to a real file inside the package root
4. `openclaw.plugin.json` exists (HARD ERROR for native plugins)
5. `id` field exists in openclaw.plugin.json (HARD ERROR)
6. `configSchema` field exists in openclaw.plugin.json (HARD ERROR)
7. Dependencies can install with `npm install --ignore-scripts`
8. Hook paths stay inside the package directory (boundary check)
9. Entry point doesn't escape the plugin root (security check)
10. Path ownership looks reasonable
11. No world-writable paths in the plugin tree

---

## 9. Bugs Found and Fixed (2026-03-25)

| Issue | File | Fix |
|-------|------|-----|
| Missing `@types/node` | `package.json` | Added to devDependencies |
| Missing `openclaw.extensions` | `package.json` | Added `openclaw` namespace |
| Missing `openclaw.hooks` | `package.json` | Added to `openclaw` namespace |
| Missing `id` | `openclaw.plugin.json` | Added `id` field |
| Missing `configSchema` | `openclaw.plugin.json` | Added empty schema |
| Undocumented fields | `openclaw.plugin.json` | Removed `entry`, `events`, `minOpenClawVersion` |
| Wrong export format | `src/index.ts` | Changed from `{ activate }` to `definePluginEntry()` |
| Missing handler | `hooks/smart-compact/` | Created `handler.ts` |
| Wrong frontmatter | `hooks/smart-compact/HOOK.md` | Changed to `metadata.openclaw.events` format |
