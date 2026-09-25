import { homedir } from "node:os";
import { join } from "node:path";
import { mkdirSync, readFileSync, writeFileSync, renameSync } from "node:fs";

export type Settings = {
  enabled: boolean;
  retainDays: number;
  archiveToolOutput: boolean;
};
export const DEFAULT_SETTINGS: Settings = { enabled: false, retainDays: 7, archiveToolOutput: false };
export function dataDir(env: NodeJS.ProcessEnv = process.env): string {
  return env.TOKEN_OPTIMIZER_PI_HOME || join(homedir(), ".pi", "agent", "token-optimizer");
}
export function readSettings(root = dataDir()): Settings {
  try {
    const data: unknown = JSON.parse(readFileSync(join(root, "settings.json"), "utf8"));
    if (!data || typeof data !== "object") return { ...DEFAULT_SETTINGS };
    const v = data as Partial<Settings>;
    return {
      enabled: v.enabled === true,
      retainDays: Number.isSafeInteger(v.retainDays) && v.retainDays! >= 1 && v.retainDays! <= 365 ? v.retainDays! : 7,
      archiveToolOutput: v.archiveToolOutput === true,
    };
  } catch { return { ...DEFAULT_SETTINGS }; }
}
export function saveSettings(settings: Settings, root = dataDir()): void {
  mkdirSync(root, { recursive: true, mode: 0o700 });
  const tmp = join(root, `settings-${process.pid}-${Date.now()}.tmp`);
  writeFileSync(tmp, JSON.stringify(settings, null, 2) + "\n", { mode: 0o600, flag: "wx" });
  renameSync(tmp, join(root, "settings.json"));
}
