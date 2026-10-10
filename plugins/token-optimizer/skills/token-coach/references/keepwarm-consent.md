# Keep-Warm consent

Keep-Warm pings the prompt cache before it expires so a paused session resumes warm. It pays off only for API-billed Claude Code sessions, it is off by default, and it refuses on subscription auth. Ask once.

## 1. Check whether to ask

```bash
python3 "$MEASURE_PY" keepwarm-consent-status   # JSON: {billing_mode, consent, should_ask}
```

`should_ask` false: say nothing. True (API-billed, not yet asked): continue.

## 2. Compute the user's own projection

```bash
python3 "$MEASURE_PY" keepwarm-backfill --json --no-fence   # read modes."probe-only".net_usd
```

## 3. Pitch

When a session pauses past its cache window and resumes, the whole prefix is written again at up to 2x input. Keep-Warm pings just before expiry (about 0.1x of the prefix, at most 2 pings per pause), and a tripwire switches it off if the pings stop paying for themselves.

- `net_usd` positive: add "a replay of your own last 30 days nets about $<net_usd> per 30 days at the conservative probe-only setting".
- Backfill failed, returned nothing, or `net_usd <= 0`: leave the dollar sentence out and say the saving depends on their own pause-and-resume pattern, and the dashboard shows it once pings have fired. Never supply a number of your own.

## 4. Record the answer, yes or no first

Recording the answer before anything else means an interrupted run never leaves an "asked" marker with no answer.

```bash
python3 "$MEASURE_PY" keepwarm-enable     # yes: records consent and installs the scheduler (macOS)
python3 "$MEASURE_PY" keepwarm-disable    # no
```

Both are final, so the user is not asked again. Only when the user neither accepts nor declines:

```bash
python3 "$MEASURE_PY" keepwarm-consent-asked   # marks the question as shown
```

## 5. Confirm it is armed (after a yes)

```bash
python3 "$MEASURE_PY" keepwarm-scheduler status   # installed and loaded state (macOS)
python3 "$MEASURE_PY" keepwarm-tick --dry-run     # what the next tick would decide
```

On Linux and Windows the scheduler is not shipped yet: Keep-Warm runs watchdog-only until the user wires `keepwarm-tick` to their own cron or timer. Tell them that plainly.
