#!/usr/bin/env python3
"""Stdlib-only protobuf wire-format decoder for Antigravity ``gen_metadata``.

Token Optimizer is zero-dependency, so instead of the ``protobuf`` package we
parse the protobuf wire format directly. The field numbers are pinned to the
``agy`` 1.1.23 binary's embedded descriptors (CortexGeneratorMetadata and its
Usage sub-message), verified against a real decoded row (conversation
``d3619d02``, gen idx 3) during research:

  gen_metadata.data:
    field 1  -> CortexGeneratorMetadata (sub-message)
      field 4  -> usage {2 input, 3 output, 5 cache_read, 9 thinking, 10 response}
      field 21 -> model_display_name (string)
      field 9  -> chat_start_metadata (sub-message)
        field 10 -> context_window_metadata {1 estimated_tokens_used, 4 max_context_tokens}
      field 13 -> credit_cost (varint)
      field 18 -> consumed_credits (varint)

Fail-closed: any structural overrun, unknown wire type, recursion-depth overrun,
or sanity-gate failure returns ``None``. Rows that fail are counted by the
reader as ``undecodable`` and excluded, never guessed.
"""
from __future__ import annotations

DECODER_VERSION = "ag-v1"

# 5 MiB cap: a real gen_metadata blob is a few KB at most. Anything this large
# is not a genuine single-generation record and is rejected before parsing.
_MAX_BYTES = 5 * 1024 * 1024

# Recursion depth bound (outer -> field 1 -> field 4/9 -> field 10 = depth 3).
_MAX_DEPTH = 6

# Per-message field-count cap: a record with 100k repeated junk fields is a
# denial attempt, not real data. Hit the cap -> None (in well under 200 ms).
_MAX_FIELDS_PER_MESSAGE = 1000

# Path-aware descent: a length-delimited field is only descended into as a
# sub-message when its (nesting depth, field number) pair is listed here.
#
#   (0, 1)  outer -> field 1  = CortexGeneratorMetadata
#   (1, 4)  generator metadata -> field 4 = usage
#   (1, 9)  generator metadata -> field 9 = chat_start_metadata
#   (2, 10) chat_start_metadata -> field 10 = context_window_metadata
#
# Depth an entry describes is the depth of the MESSAGE CONTAINING the field
# (0 = the raw blob), so the true inner path is 1 -> 4/9 -> 10. Field NUMBER
# alone is ambiguous: the outer blob's field 4 is a conversation-id STRING
# (not a message), while the generator-metadata field 4 IS the usage message.
# A naive "descend every field 4" misparses the conversation id as a message
# and fails the whole record. Everything else length-delimited (custom_metadata
# field 20, response_model field 19, etc.) is kept opaque.
_DESCEND_PATHS = frozenset({(0, 1), (1, 4), (1, 9), (2, 10)})

_WIRETYPE_VARINT = 0
_WIRETYPE_FIXED64 = 1
_WIRETYPE_LENGTH_DELIMITED = 2
_WIRETYPE_FIXED32 = 5


def _read_varint(data: bytes, pos: int):
    """Read a base-128 varint starting at ``pos``. Returns (value, new_pos)
    or (None, pos) on overrun/no-progress. Bounds varint length to 10 bytes."""
    value = 0
    shift = 0
    start = pos
    n = len(data)
    while pos < n:
        if pos - start >= 10:
            return None, pos
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return value, pos
        shift += 7
        if shift >= 70:
            return None, pos
    return None, pos


def _decode_message(data: bytes, depth: int):
    """Parse one protobuf message into {field_number: [(wire_type, value)]}.

    Returns None on any structural failure. ``value`` is an int for varint, a
    raw ``bytes`` for fixed32/fixed64/opaque length-delimited payloads, or a
    nested ``{field_number: [...]}`` dict for whitelisted sub-messages.
    """
    if depth > _MAX_DEPTH:
        return None
    if len(data) > _MAX_BYTES:
        return None

    fields: dict = {}
    pos = 0
    n = len(data)
    field_count = 0
    while pos < n:
        key, pos2 = _read_varint(data, pos)
        if key is None:
            return None
        pos = pos2
        field_count += 1
        if field_count > _MAX_FIELDS_PER_MESSAGE:
            return None

        field_number = key >> 3
        if field_number == 0:
            return None  # field 0 is invalid protobuf
        wire_type = key & 0x07

        if wire_type == _WIRETYPE_VARINT:
            value, pos3 = _read_varint(data, pos)
            if value is None:
                return None
            pos = pos3
            fields.setdefault(field_number, []).append((_WIRETYPE_VARINT, value))
        elif wire_type == _WIRETYPE_FIXED64:
            if pos + 8 > n:
                return None
            fields.setdefault(field_number, []).append(
                (_WIRETYPE_FIXED64, data[pos:pos + 8])
            )
            pos += 8
        elif wire_type == _WIRETYPE_LENGTH_DELIMITED:
            length, pos3 = _read_varint(data, pos)
            if length is None:
                return None
            pos = pos3
            if length < 0 or pos + length > n:
                return None
            payload = data[pos:pos + length]
            pos += length
            if (depth, field_number) in _DESCEND_PATHS and depth < _MAX_DEPTH:
                sub = _decode_message(payload, depth + 1)
                if sub is None:
                    return None
                fields.setdefault(field_number, []).append(
                    (_WIRETYPE_LENGTH_DELIMITED, sub)
                )
            else:
                fields.setdefault(field_number, []).append(
                    (_WIRETYPE_LENGTH_DELIMITED, payload)
                )
        elif wire_type == _WIRETYPE_FIXED32:
            if pos + 4 > n:
                return None
            fields.setdefault(field_number, []).append(
                (_WIRETYPE_FIXED32, data[pos:pos + 4])
            )
            pos += 4
        else:
            return None  # unknown wire type: abort

    return fields


def _int_value(msg, field_number, default=0):
    """First varint value for ``field_number``, else ``default``."""
    if not msg:
        return default
    entries = msg.get(field_number)
    if not entries:
        return default
    wire_type, value = entries[0]
    if wire_type != _WIRETYPE_VARINT:
        return default
    return value


def _clean_string(value) -> str:
    """Decode a byte payload as UTF-8 (errors replaced) and strip control chars."""
    try:
        if isinstance(value, bytes):
            text = value.decode("utf-8", errors="replace")
        else:
            text = str(value)
    except Exception:
        return ""
    out = []
    for ch in text:
        cp = ord(ch)
        if ch == "\t" or (32 <= cp < 127) or cp >= 160:
            out.append(ch)
        elif len(out) and out[-1] != " ":
            out.append(" ")
    return "".join(out).strip()


def _string_value(msg, field_number):
    """First length-delimited payload for ``field_number`` as a cleaned string."""
    if not msg:
        return ""
    entries = msg.get(field_number)
    if not entries:
        return ""
    wire_type, value = entries[0]
    if wire_type != _WIRETYPE_LENGTH_DELIMITED or isinstance(value, dict):
        return ""
    return _clean_string(value)


def decode_generation(blob) -> dict | None:
    """Decode a ``gen_metadata.data`` blob into a typed usage record or None.

    Returns keys: input_tokens, output_tokens, cache_read_tokens,
    thinking_tokens, response_tokens, model_display_name,
    estimated_tokens_used, max_context_tokens, credit_cost, consumed_credits,
    decoder_version. Never raises.
    """
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        return None
    data = bytes(blob)
    if not data or len(data) > _MAX_BYTES:
        return None

    outer = _decode_message(data, 0)
    if not outer:
        return None

    # field 1 = CortexGeneratorMetadata (the actual generator metadata message).
    gen_entries = outer.get(1)
    if not gen_entries or not isinstance(gen_entries[0][1], dict):
        return None
    gen = gen_entries[0][1]

    # field 4 = usage sub-message. Missing usage -> no token data -> None.
    usage_entries = gen.get(4)
    if not usage_entries or not isinstance(usage_entries[0][1], dict):
        return None
    usage = usage_entries[0][1]
    if not usage:
        return None  # empty usage sub-message carries no token signal

    input_tokens = _int_value(usage, 2)
    output_tokens = _int_value(usage, 3)
    cache_read_tokens = _int_value(usage, 5)
    thinking_tokens = _int_value(usage, 9)
    response_tokens = _int_value(usage, 10)

    model_display_name = _string_value(gen, 21)

    estimated_tokens_used = 0
    max_context_tokens = 0
    chat_entries = gen.get(9)
    if chat_entries and isinstance(chat_entries[0][1], dict):
        chat = chat_entries[0][1]
        cw_entries = chat.get(10)
        if cw_entries and isinstance(cw_entries[0][1], dict):
            cw = cw_entries[0][1]
            estimated_tokens_used = _int_value(cw, 1)
            max_context_tokens = _int_value(cw, 4)

    credit_cost = _int_value(gen, 13)
    consumed_credits = _int_value(gen, 18)

    # Sanity gate (KTD2): output must equal thinking + response whenever a
    # breakdown is present. A mismatch means the field mapping has drifted for
    # this binary version; decode is rejected rather than guessed.
    if thinking_tokens > 0 or response_tokens > 0:
        if thinking_tokens + response_tokens != output_tokens:
            return None

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "thinking_tokens": thinking_tokens,
        "response_tokens": response_tokens,
        "model_display_name": model_display_name,
        "estimated_tokens_used": estimated_tokens_used,
        "max_context_tokens": max_context_tokens,
        "credit_cost": credit_cost,
        "consumed_credits": consumed_credits,
        "decoder_version": DECODER_VERSION,
    }


# ---------------------------------------------------------------------------
# steps.metadata extraction
# ---------------------------------------------------------------------------
#
# A ``steps.metadata`` blob is CortexStepMetadata: field 1 is a Timestamp
# sub-message ({1: seconds varint, 2: nanos varint}) and field 4 is a tool-call
# info sub-message (field 2 = tool name string, field 3 = args). The metadata
# also contains deprecated protobuf GROUPS (wire types 3/4), so a strict
# wire-type abort would reject almost every real row. The helpers below skip
# groups instead; they only ever read the two fields Token Optimizer needs and
# never touch args/step_payload (R21).

_WIRETYPE_START_GROUP = 3
_WIRETYPE_END_GROUP = 4


def _skip_group(data: bytes, pos: int) -> int | None:
    """Skip a protobuf group (deprecated encoding); return position past its
    end-group, or None on structural error. Nested groups are honored."""
    depth = 1
    n = len(data)
    while depth > 0 and pos < n:
        key, pos2 = _read_varint(data, pos)
        if key is None:
            return None
        pos = pos2
        field_number = key >> 3
        wire_type = key & 0x07
        if wire_type == _WIRETYPE_START_GROUP:
            depth += 1
        elif wire_type == _WIRETYPE_END_GROUP:
            depth -= 1
        elif wire_type == _WIRETYPE_VARINT:
            _, pos3 = _read_varint(data, pos)
            if pos3 == pos:
                return None
            pos = pos3
        elif wire_type == _WIRETYPE_FIXED64:
            if pos + 8 > n:
                return None
            pos += 8
        elif wire_type == _WIRETYPE_LENGTH_DELIMITED:
            length, pos3 = _read_varint(data, pos)
            if length is None or length < 0 or pos + length > n:
                return None
            pos = pos3 + length
        elif wire_type == _WIRETYPE_FIXED32:
            if pos + 4 > n:
                return None
            pos += 4
        else:
            return None
    return pos if depth == 0 else None


def _walk_wire(data: bytes):
    """Yield (field_number, wire_type, value) for one message level.

    Length-delimited fields are returned as raw bytes (never descended). Groups
    are consumed and skipped. Stops silently on any structural error.
    """
    pos = 0
    n = len(data)
    field_count = 0
    while pos < n:
        key, pos2 = _read_varint(data, pos)
        if key is None:
            return
        pos = pos2
        field_count += 1
        if field_count > _MAX_FIELDS_PER_MESSAGE:
            return
        field_number = key >> 3
        wire_type = key & 0x07
        if wire_type == _WIRETYPE_VARINT:
            value, pos3 = _read_varint(data, pos)
            if value is None:
                return
            pos = pos3
            yield (field_number, wire_type, value)
        elif wire_type == _WIRETYPE_FIXED64:
            if pos + 8 > n:
                return
            yield (field_number, wire_type, data[pos:pos + 8])
            pos += 8
        elif wire_type == _WIRETYPE_LENGTH_DELIMITED:
            length, pos3 = _read_varint(data, pos)
            if length is None:
                return
            pos = pos3
            if length < 0 or pos + length > n:
                return
            payload = data[pos:pos + length]
            pos += length
            yield (field_number, wire_type, payload)
        elif wire_type == _WIRETYPE_START_GROUP:
            pos = _skip_group(data, pos)
            if pos is None:
                return
        elif wire_type == _WIRETYPE_END_GROUP:
            return  # stray end-group at this level: stop
        elif wire_type == _WIRETYPE_FIXED32:
            if pos + 4 > n:
                return
            yield (field_number, wire_type, data[pos:pos + 4])
            pos += 4
        else:
            return


def decode_step_metadata(blob) -> dict:
    """Extract the tool name and timestamp from a ``steps.metadata`` blob.

    Returns ``{"tool_name": str, "timestamp": int | None}``. ``tool_name`` is
    the lower-snake name from metadata field 4 sub-field 2 (e.g. "run_command");
    empty string when the step is not a tool call. ``timestamp`` is metadata
    field 1 sub-field 1 (epoch seconds); None when absent. Never raises.
    """
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        return {"tool_name": "", "timestamp": None}
    data = bytes(blob)
    tool_name = ""
    timestamp = None
    for field_number, wire_type, value in _walk_wire(data):
        if field_number == 1 and wire_type == _WIRETYPE_LENGTH_DELIMITED:
            for f2, w2, v2 in _walk_wire(value):
                if f2 == 1 and w2 == _WIRETYPE_VARINT:
                    timestamp = v2
                    break
        elif field_number == 4 and wire_type == _WIRETYPE_LENGTH_DELIMITED:
            for f2, w2, v2 in _walk_wire(value):
                if f2 == 2 and w2 == _WIRETYPE_LENGTH_DELIMITED:
                    tool_name = _clean_string(v2)
                    break
    return {"tool_name": tool_name, "timestamp": timestamp}
