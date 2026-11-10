"""Score a removal made inside a sub-agent against that sub-agent's own turns.

Claude Code writes each sub-agent (Task/Agent tool) to its own transcript,
``<session>/subagents/agent-<id>.jsonl``, and its hooks log to the parent
session's id. The counted pass used to walk only the parent transcript, so a
compression that happened inside a sub-agent was credited with the parent's
later turns: turns that never held that content. Pricing version 6 moves each
such removal onto the turns of the transcript that actually held it, up to that
sub-agent's end or its own compaction, priced per turn at the exact card of the
model that ran each turn (sub-agents often run a different model).

Locating an event (never by time alone):
  * ``tool_archive_reexpand``: the archived result's tool_use_id is in the
    event detail; the transcript holding that tool result is the location.
  * ``tool_archive``: the detail states the tool and the original size; the
    location is the tool result of that tool whose archive pointer states the
    same size, nearest in time.
  * compression rows with a command: the Bash tool result whose command,
    redacted and cut the way the hook stores it, equals the stored command.
  * anything else, no candidate, or candidates in two transcripts at about the
    same time: the event stays on the parent (the previous behaviour) and is
    counted as unlocated or ambiguous.

The facade is passed in (helpers and constants), so this module holds no
import-time state and imports nothing from measure.
"""

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

# Candidate tool results further than this from the logged event are not it.
MATCH_SECONDS = 120
# Candidates in two different transcripts this close together are ambiguous.
TIE_SECONDS = 2
SCOPE_PARENT = "parent"
SCOPE_SUBAGENT = "subagent"


def collect_stats():
    return dict.fromkeys(("located_subagent", "located_parent", "unlocated", "ambiguous"), 0)


def add_stats(total, session):
    for key, count in session.items():
        total[key] += count

_TOOL_USE_ID_RX = re.compile(r"\b(toolu_[A-Za-z0-9_\-]+)")
_ARCHIVE_DETAIL_RX = re.compile(r"replaced (\S+) \(([\d,]+) chars")
_POINTER_SIZE_RX = re.compile(r"\[Full result archived \((\d{1,3}(?:,\d{3})*|\d+) chars")


def walk(facade, path, collect_markers=None, sidechain=False):
    """One pass over a transcript for the counted-cumulative engine.

    Returns (turns, compacts, markers):
      turns   = [(utc-naive datetime, raw model id, full prompt tokens,
                  (cache write tokens, 1h part, 5m part, cache read tokens) or None)]
                -- API calls, deduped on requestId/message.id (Claude Code
                writes one assistant line per content block, all sharing the
                same usage);
      compacts= [utc-naive datetime] of real compact_boundary system lines;
      markers = [(utc-naive datetime-or-None, tokens-or-None)] for
                "Full result archived" injections (collect_markers only).
    A parent transcript (sidechain=False) skips sidechain records; a sub-agent
    transcript (sidechain=True) is made only of them, so they are its turns.
    Never raises; returns ([], [], []) on unreadable files.
    """
    turns, compacts, markers = [], [], []
    seen = {}
    last_ts = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if collect_markers and "Full result archived" in line:
                    m = facade._COUNTED_MARKER_RX.search(line)
                    tok = None
                    if m and m.group(1):
                        tok = max(1, int(m.group(1).replace(",", "")) // 4)
                    markers.append((last_ts, tok))
                if '"assistant"' not in line and "compact_boundary" not in line:
                    continue
                try:
                    d = facade.json.loads(line)
                except (facade.json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(d, dict):
                    continue
                t = d.get("type")
                ts = d.get("timestamp")
                dt = None
                if ts:
                    try:
                        dt = facade.datetime.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S")
                    except (ValueError, TypeError):
                        dt = None
                if dt is not None:
                    last_ts = dt
                if t == "system" and d.get("subtype") == "compact_boundary":
                    if dt is not None:
                        compacts.append(dt)
                    continue
                if t != "assistant" or (d.get("isSidechain") and not sidechain):
                    continue
                msg = d.get("message") or {}
                if not isinstance(msg, dict) or not msg.get("usage"):
                    continue
                rid = facade._usage_request_key(d, msg)
                if dt is None:
                    continue
                # Raw model id: re-reads are priced by generation card
                # (Opus 5.5 and Fable 5.1 read cache far cheaper than their family).
                raw_model = msg.get("model")
                usage = msg["usage"] if isinstance(msg["usage"], dict) else {}
                prompt_parts = {k: usage[k] for k in (
                    "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens") if k in usage}
                nested = usage.get("cache_creation")
                nested = nested if isinstance(nested, dict) else {}
                for key in ("ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens"):
                    if key in nested or key in usage:
                        prompt_parts[key] = nested.get(key) or usage.get(key, 0)
                if rid is not None and rid in seen:
                    index, previous_parts = seen[rid]
                    for key, value in previous_parts.items():
                        current = prompt_parts.get(key)
                        if isinstance(value, int) and (not isinstance(current, int) or value > current):
                            prompt_parts[key] = value
                prompt_tokens = None
                cache_class = None
                if ("input_tokens" in prompt_parts
                        and all(isinstance(v, int) and v >= 0 for v in prompt_parts.values())):
                    # Flat and TTL fields describe overlapping cache writes.
                    # Partial chunks can fill either form; never add both.
                    creation = max(prompt_parts.get("cache_creation_input_tokens", 0),
                                   prompt_parts.get("ephemeral_1h_input_tokens", 0)
                                   + prompt_parts.get("ephemeral_5m_input_tokens", 0))
                    prompt_tokens = facade._context_token_count(dict(prompt_parts, cache_creation_input_tokens=creation))
                    cache_class = (creation, prompt_parts.get("ephemeral_1h_input_tokens", 0),
                                   prompt_parts.get("ephemeral_5m_input_tokens", 0),
                                   prompt_parts.get("cache_read_input_tokens", 0))
                if rid is not None and rid in seen:
                    previous = turns[index]
                    turns[index] = (previous[0], previous[1] or raw_model, prompt_tokens, cache_class)
                    seen[rid] = (index, prompt_parts)
                    continue
                if rid is not None:
                    seen[rid] = (len(turns), prompt_parts)
                turns.append((dt, raw_model if isinstance(raw_model, str)
                              and not raw_model.startswith("<") else None, prompt_tokens, cache_class))
    except (OSError, PermissionError):
        return [], [], []
    turns.sort(key=lambda x: x[0])
    compacts.sort()
    return turns, compacts, markers


def subagent_files(path):
    """Sub-agent transcripts of a parent session transcript, sorted. Never raises."""
    try:
        folder = Path(path).with_suffix("") / "subagents"
        return sorted(folder.glob("*.jsonl")) if folder.is_dir() else []
    except (OSError, TypeError, ValueError):
        return []


def _texts(content):
    if isinstance(content, str):
        return [content]
    out = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                out.append(block["text"])
    return out


def index_results(path, sidechain):
    """Tool results in one transcript: [(utc-naive dt, tool_use_id, tool name,
    command or None, pointer original chars or None)]. Never raises."""
    uses, results = {}, []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if '"tool_use"' not in line and '"tool_result"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict) or (record.get("isSidechain") and not sidechain):
                    continue
                message = record.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use" and block.get("id"):
                        command = (block.get("input") or {}).get("command") if isinstance(block.get("input"), dict) else None
                        uses[block["id"]] = (block.get("name"), command if isinstance(command, str) else None)
                        continue
                    if block.get("type") != "tool_result":
                        continue
                    try:
                        dt = datetime.strptime(str(record.get("timestamp"))[:19], "%Y-%m-%dT%H:%M:%S")
                    except (ValueError, TypeError):
                        continue
                    text = "\n".join(_texts(block.get("content")))
                    size = _POINTER_SIZE_RX.search(text[-2000:]) if "Full result archived" in text else None
                    name, command = uses.get(block.get("tool_use_id"), (None, None))
                    results.append((dt, block.get("tool_use_id"), name, command,
                                    int(size.group(1).replace(",", "")) if size else None))
    except (OSError, PermissionError):
        return []
    return results


def _stored_command(command):
    """The command as the hook stores it: redacted, then cut to 100 chars."""
    try:
        from credential_patterns import redact_credentials
        return redact_credentials(command)[:100]
    except Exception:
        return command[:100]


def event_info(conn, keys):
    """{event_key: (kind, a, b)} from the ledger rows behind counted keys."""
    out = {}
    se = [int(k[3:]) for k in keys if k.startswith("se:") and k[3:].isdigit()]
    ce = [int(k[3:]) for k in keys if k.startswith("ce:") and k[3:].isdigit()]
    for chunk in range(0, len(se), 500):
        part = se[chunk:chunk + 500]
        for rid, kind, detail in conn.execute(
                "SELECT id, event_type, detail FROM savings_events WHERE id IN (%s)"
                % ",".join("?" * len(part)), part):
            out["se:%d" % rid] = (kind, detail or "", None)
    for chunk in range(0, len(ce), 500):
        part = ce[chunk:chunk + 500]
        for rid, feature, pattern, detail in conn.execute(
                "SELECT id, feature, command_pattern, detail FROM compression_events WHERE id IN (%s)"
                % ",".join("?" * len(part)), part):
            marker = "missing-scope" if feature == "first_read_skeleton" and str(detail or "").endswith("; subagent") else None
            out["ce:%d" % rid] = (feature, pattern or "", marker)
        try:
            for rid, tool_hash in conn.execute(
                    "SELECT event_id,tool_id_hash FROM first_read_confirmation_scopes WHERE event_id IN (%s)"
                    % ",".join("?" * len(part)), part):
                key = "ce:%d" % rid
                if key in out:
                    out[key] = (out[key][0], out[key][1], tool_hash)
        except sqlite3.OperationalError:
            pass
    return out


class SessionScope:
    """Where each counted event of one session happened, and its turns there.

    Built once per session walk. With no sub-agent transcript on disk every
    event stays on the parent and nothing beyond a directory check is read.
    """

    def __init__(self, facade, conn, path, events):
        """events: [(event_key, local-naive timestamp)]."""
        self.facade = facade
        self.files = subagent_files(path)
        self.location = {}
        self.required_subagents = set()
        self.stats = collect_stats()
        self._walks = {}
        if not events:
            return
        try:
            info = event_info(conn, [key for key, _ts in events])
        except Exception:
            return
        self.required_subagents = {key for key, data in info.items() if data[0] == "first_read_skeleton" and data[2]}
        if not self.files:
            self.stats["unlocated"] += len(self.required_subagents)
            return
        results = [(dt, -1, tid, name, cmd, size) for dt, tid, name, cmd, size in index_results(path, False)]
        for index, sub in enumerate(self.files):
            results += [(dt, index, tid, name, cmd, size) for dt, tid, name, cmd, size in index_results(sub, True)]
        by_id = {r[2]: r for r in results if r[2]}
        used = set()
        stored_commands = {}
        for key, ts in sorted(events, key=lambda e: str(e[1])):
            ev_dt = facade._counted_event_utc(ts)
            kind = info.get(key)
            if ev_dt is None or kind is None:
                self.stats["unlocated"] += 1
                continue
            name, detail, _ = kind
            if key in self.required_subagents:
                hits = [r for r in results if r[2] and hashlib.sha256(str(r[2]).encode("utf-8", "replace")).hexdigest() == kind[2]]
                scopes = {r[1] for r in hits}
                if len(scopes) == 1 and next(iter(scopes)) >= 0:
                    self._place(key, hits[:1], ev_dt, used, exact_id=True)
                else:
                    self.stats["ambiguous" if len(scopes) > 1 else "unlocated"] += 1
                continue
            if name == "tool_archive_reexpand":
                match = _TOOL_USE_ID_RX.search(detail)
                hit = by_id.get(match.group(1)) if match else None
                self._place(key, [hit] if hit else [], ev_dt, used, exact_id=True)
                continue
            if name == "tool_archive":
                match = _ARCHIVE_DETAIL_RX.search(detail)
                if not match:
                    self.stats["unlocated"] += 1
                    continue
                tool, chars = match.group(1), int(match.group(2).replace(",", ""))
                cands = [r for r in results if r[3] == tool and r[5] == chars]
            elif key.startswith("ce:") and detail:
                cands = []
                for r in results:
                    if r[3] != "Bash" or not r[4]:
                        continue
                    if r[4] not in stored_commands:
                        stored_commands[r[4]] = _stored_command(r[4])
                    if stored_commands[r[4]] == detail:
                        cands.append(r)
            else:
                self.stats["unlocated"] += 1
                continue
            self._place(key, cands, ev_dt, used)

    def _place(self, key, cands, ev_dt, used, exact_id=False):
        if exact_id:
            near = [(0.0, r) for r in cands]
        else:
            near = sorted(((abs((r[0] - ev_dt).total_seconds()), r) for r in cands
                           if (r[1], r[2]) not in used
                           and abs((r[0] - ev_dt).total_seconds()) <= MATCH_SECONDS),
                          key=lambda item: item[0])
        if not near:
            self.stats["unlocated"] += 1
            return
        gap, best = near[0]
        rivals = {r[1] for g, r in near if g <= gap + TIE_SECONDS}
        if len(rivals) > 1:
            self.stats["ambiguous"] += 1
            return
        if not exact_id:
            used.add((best[1], best[2]))
        if best[1] < 0:
            self.stats["located_parent"] += 1
            return
        self.location[key] = best[1]
        self.stats["located_subagent"] += 1

    def turns_for(self, key, parent_turns, parent_compacts):
        """(turns, compacts, scope) to score this event against."""
        index = self.location.get(key)
        if index is None:
            if key in self.required_subagents:
                return [], [], SCOPE_SUBAGENT
            return parent_turns, parent_compacts, SCOPE_PARENT
        if index not in self._walks:
            turns, compacts, _ = walk(self.facade, self.files[index], sidechain=True)
            self._walks[index] = (turns, compacts)
        turns, compacts = self._walks[index]
        return turns, compacts, SCOPE_SUBAGENT
