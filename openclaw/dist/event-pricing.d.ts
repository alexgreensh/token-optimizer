export declare function resolvePricedModel(model: string, openclawDir: string): string | null;
/** Billing class a savings event's avoided tokens would have been charged at. */
export type SavingsEventClass = "input" | "output" | "cacheRead" | "cacheWrite" | "cacheWrite1h";
export declare function savingsEventClass(value: unknown): SavingsEventClass | undefined;
type SessionEvidence = {
    sessionId?: string;
    model?: string;
    modelTokenBuckets?: Record<string, {
        input?: number;
        output?: number;
        cacheRead?: number;
        cacheWrite?: number;
    }>;
};
export declare function loadSavingsSessions(openclawDir: string): Map<string, SessionEvidence>;
export declare function priceSavingsEvent(tokens: number, eventType: string, session: SessionEvidence | undefined, openclawDir: string, tokenClass?: SavingsEventClass): {
    cost: number;
    unpricedTokens: number;
    floorTokens: number;
};
export {};
//# sourceMappingURL=event-pricing.d.ts.map