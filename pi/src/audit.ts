import type { Entry, Rollup } from "./rollup.ts";
import { estimateTokens } from "./signals.ts";
export type Audit = { toolCalls: number; repeatedToolCalls: number; largeToolResults: number; toolResultTokensEstimate: number; modelCostNative: number; modelCostEstimated: number; nativeCalls: number; estimatedCalls: number; unpricedCalls: number; totalModelCalls: number; savingsVerified: number; advice: string[] };
/** Read-only audit; potential waste is never presented as actual saved dollars. */
export function audit(branch: Entry[], summary: Rollup): Audit {
  let toolCalls = 0, repeatedToolCalls = 0, largeToolResults = 0, toolResultTokensEstimate = 0;
  let lastCall = '';
  for (const e of branch) {
    if (e.type !== 'message' || !e.message) continue;
    const msg = e.message as unknown as { role: string; content?: { type?: string; name?: string; arguments?: unknown; text?: string }[] };
    for (const part of msg.content ?? []) {
      if (msg.role === 'assistant' && part.type === 'toolCall') {
        toolCalls++;
        const signature = JSON.stringify([part.name, part.arguments]);
        if (signature === lastCall) repeatedToolCalls++;
        lastCall = signature;
      }
      if (msg.role === 'toolResult' && part.type === 'text') {
        const n = estimateTokens(part.text ?? '');
        toolResultTokensEstimate += n;
        if (n > 3000) largeToolResults++;
      }
    }
  }
  const advice = [];
  if (repeatedToolCalls) advice.push(`${repeatedToolCalls} repeated adjacent tool calls; check whether the same result was read again`);
  if (largeToolResults) advice.push(`${largeToolResults} large tool results; consider narrower reads`);
  if (!advice.length) advice.push('No repeated calls or large tool results detected on this branch');
  return { toolCalls, repeatedToolCalls, largeToolResults, toolResultTokensEstimate,
    modelCostNative: summary.models.reduce((v,m)=>v+m.nativeCost,0),
    modelCostEstimated: summary.models.reduce((v,m)=>v+m.estimatedCost,0),
    nativeCalls: summary.models.reduce((v,m)=>v+m.calls-m.estimatedCalls-m.unpricedCalls,0),
    estimatedCalls: summary.models.reduce((v,m)=>v+m.estimatedCalls,0),
    unpricedCalls: summary.models.reduce((v,m)=>v+m.unpricedCalls,0),
    totalModelCalls: summary.models.reduce((v,m)=>v+m.calls,0),
    savingsVerified: 0, advice };
}
