import type { PluginModule } from "@opencode-ai/plugin";
import { TokenOptimizerPlugin } from "./index.js";

export const id = "token-optimizer-opencode";
export { TokenOptimizerPlugin };
export default { id, server: TokenOptimizerPlugin } satisfies PluginModule;
