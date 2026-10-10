# Fleet Auditor Report Template

Reference file for Fleet Auditor. Read it when presenting audit findings (Phase 3).

Order findings by severity, then by monthly savings. Show confidence beside each finding and
leave out anything below 0.4.

```
[Fleet Auditor Results]

SYSTEMS DETECTED
- Claude Code: X runs ($Y.YY)
- Codex: X runs ($Y.YY)
- OpenClaw: X runs ($Y.YY)

WASTE PATTERNS FOUND
1. [SEVERITY] Description
   Est. savings: $X.XX/month
   Fix: recommendation

2. [SEVERITY] Description
   ...

TOTAL POTENTIAL SAVINGS: $X.XX/month

Ready to act? I can:
1. Show detailed fix snippets for each finding
2. Generate the fleet dashboard for visual analysis
3. Run /token-optimizer for deeper Claude Code optimization
```

## Dollars on Codex and other non-Claude systems

If a model's price is missing from the local pricing table, report the token waste and say the
dollar impact depends on current model pricing. Do not invent a cost.

## OpenClaw checks

OpenClaw instances that are out of date or exposed can run rogue agents that burn tokens
unnoticed, so treat these as spend findings as well as security findings:

- Compare the detected OpenClaw version with the project's current security advisories. Flag an
  outdated install as high severity and recommend upgrading.
- Look for installed ClawHub skills that make extra API calls or send data out, since those
  inflate token spend.
- Flag a gateway with rate limiting disabled: without it, brute-force traffic can spawn unlimited
  agent sessions. The fix is `openclaw config set security.rateLimit.enabled true`.

These checks come from public advisories, not from the audit data, so state that when reporting
them and let the user verify before acting.
