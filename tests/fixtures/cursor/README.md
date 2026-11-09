# Cursor transcript fixtures (SYNTHETIC - bundle-schema)

These files are GENERATED, not native captures. Their line shapes mirror the
Cursor CLI 2026.11.05 on-disk TranscriptStore writer (bundle
7923.index.js: U @10908, R @9679, H @10444): `{"role", "message":
{"content": [{type:"text",text}|{type:"tool_use",name,input}]}}` plus
`{"type":"turn_ended", status, error?}` terminal lines. Disk transcripts
carry NO tool-result text by writer design. They become stale the day the
host writer changes; cursor-doctor's observed ledger is the live check.
