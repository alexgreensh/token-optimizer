export declare function resolvePricedModel(model: string, openclawDir: string): string | null;
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
export declare function priceSavingsEvent(tokens: number, eventType: string, session: SessionEvidence | undefined, openclawDir: string): {
    cost: number;
    unpricedTokens: number;
};
export {};
//# sourceMappingURL=event-pricing.d.ts.map