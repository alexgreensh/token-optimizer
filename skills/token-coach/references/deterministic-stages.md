# Which steps need a model at all

Reference for Token Coach, goal e, and for any session where the coach data lists `deterministic_candidates`.

## Contents

- The one test
- Why it matters more than trimming config
- Finding candidates in the user's own data
- Steps that are nearly always code
- Steps that stay with the model
- The usual shape: model at the edges
- Where the code lives, per platform
- How to present it
- When to leave a step alone

## The one test

For each step in a workflow ask: **do the inputs fully decide the output?** If two careful people given the same inputs would always produce the same result, the step is a function, and a function belongs in code. If the right result depends on reading meaning, weighing trade-offs, or writing for a person, it needs a model.

A second question catches the rest: **what would check this step's output?** If the honest answer is "a test, a schema, or a diff", the step is usually code already and the model is acting as a slow interpreter for it.

## Why it matters more than trimming config

Config cleanup saves a fixed number of tokens once per session. A model step costs on every run:

- The whole context is read again for that turn (cached reads are cheap per token, but the context is large and the turn count is high).
- The output is generated at output-token price, the most expensive kind.
- The result joins the context and is re-read by every later turn until compaction.
- It can come back different the next time, so something downstream has to tolerate that.

A script costs no tokens, takes milliseconds, and returns the same answer every time. Moving one repeated step from a model to code often beats every setting change the coach can suggest, and it makes the workflow more reliable as a side effect. Lead with the reliability when the user's spend is small.

## Finding candidates in the user's own data

`python3 "$MEASURE_PY" coach --json` may include `deterministic_candidates`: tool-call sequences the user repeats across sessions with the same shape, subagents launched again and again from near-identical prompts, and loops that poll or retry. Each entry carries how often it ran and what it cost. Start from those, since they are measured.

When the list is empty or missing, ask the user to describe the workflow as numbered steps, then apply the one test to each. Useful prompts for them:

- "What do you ask for in the same words most days?"
- "Which steps do you never read the reasoning for, only the result?"
- "Where does the agent wait for something?"

## Steps that are nearly always code

| Step | Why it is a function | Typical replacement |
|------|----------------------|---------------------|
| Routing by a fixed rule (file type, label, sender, path) | A lookup | A `case` or a small table in a script |
| Counting, summing, ranking, deduplicating | Arithmetic | `jq`, SQL, a few lines of Python |
| Filling a template, formatting a report, renaming files | The shape is fixed | A template plus a script |
| Extracting fields from structured text (JSON, CSV, logs with a stable format) | A parse | A parser or a regular expression |
| Waiting, polling, "check again in a minute" | No judgement, and every poll re-reads the context | A shell loop, a scheduled job, or a background task that wakes the agent once |
| "Did it pass?" on a build, test run, or linter | The tool already returns an exit code | The exit code, with the model reading output only on failure |
| Fetching the same data at the start of every session | Same query each time | A SessionStart hook or a script whose output is piped in |
| Validation against a schema, a style rule, a naming rule | A check | A linter, a JSON schema, a pre-commit hook |
| Retrying a failed call | A policy | A retry wrapper with a limit |
| Moving results between steps | Plumbing | Files on disk, read by the next step |

## Steps that stay with the model

- Reading intent from messy human text.
- Choosing between options with real trade-offs.
- Writing for a reader: prose, explanations, reviews.
- Diagnosing a failure nobody has seen before.
- Work where the input format changes too often for a parser to keep up.

## The usual shape: model at the edges

Most workflows are a mix, and the gain comes from changing who does the middle:

1. **Code gathers.** A script fetches, filters, parses and totals, then writes a compact result file.
2. **The model judges.** It reads that small file, not the raw material, and makes the one decision or writes the one piece of text that needs it.
3. **Code applies and checks.** A script writes the output where it belongs and validates it. The model is called again only when the check fails.

Compare the context each way. A model that reads fifty files to count something holds all fifty until compaction. A script that counts and hands over one line leaves the context almost untouched.

## Where the code lives, per platform

| Mechanism | Use it when | Notes |
|-----------|-------------|-------|
| A script the skill or command runs | The step belongs to one workflow | Keep it beside the skill; print errors that name the fix |
| A hook (Claude Code, Codex) | The step must happen every time, whether or not the model remembers | Claude Code's exec form (`"command"` plus `"args"`, no shell) starts faster and avoids quoting problems; on Windows use the `Bash\|PowerShell` matcher so both shells are covered |
| Command text that runs a shell line before the prompt is sent | The model needs fresh data at the start | The model sees only the output |
| A scheduled job (cron, launchd, a scheduled cloud agent) | The work recurs on a clock | Call a model inside it only at the judgement step |
| A headless call (`claude -p`, `codex exec`) inside a script | A pipeline needs one judgement in the middle | Give it only the gathered result, and a cheaper model when the judgement is simple |
| A subagent | The judgement step needs to read a lot | It keeps the reading out of the main context; it is still a model call, so it does not replace code |

## How to present it

Give the user a table of their own steps:

| Step | Today | Inputs decide the output? | Move to | What it saves |
|------|-------|---------------------------|---------|---------------|

Fill "what it saves" from measured data where there is some (runs per week and tokens per run from `deterministic_candidates`), and write "not measured" where there is none. Then offer to write the first script with them, starting with the step that runs most often.

## When to leave a step alone

- It runs rarely. A script nobody maintains costs more than a few model calls a month.
- The input is messy enough that the parser would need its own maintenance.
- The user reads the model's reasoning for that step and values it.
- A wrong result is costly and the model is the one catching edge cases. Keep the model and add a code check after it instead.
