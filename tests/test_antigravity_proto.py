"""U2 — stdlib protobuf decoder for Antigravity gen_metadata."""
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "token-optimizer" / "scripts"))

from antigravity_proto import decode_generation  # noqa: E402


# --- tiny protobuf encoder (test-only, never shipped) -----------------------

def _varint(value: int) -> bytes:
    out = bytearray()
    value &= (1 << 64) - 1
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _field_varint(num, value):
    return _varint((num << 3) | 0) + _varint(value)


def _field_bytes(num, payload):
    return _varint((num << 3) | 2) + _varint(len(payload)) + payload


def _usage(input_tokens, output_tokens, cache_read, thinking, response):
    msg = b""
    if input_tokens:
        msg += _field_varint(2, input_tokens)
    if output_tokens:
        msg += _field_varint(3, output_tokens)
    if cache_read:
        msg += _field_varint(5, cache_read)
    if thinking:
        msg += _field_varint(9, thinking)
    if response:
        msg += _field_varint(10, response)
    return msg


def _gen_message(usage_msg, model_display_name, est_used, max_ctx, credit_cost=0, consumed_credits=0):
    msg = b""
    if usage_msg:
        msg += _field_bytes(4, usage_msg)
    if model_display_name:
        msg += _field_bytes(21, model_display_name.encode("utf-8"))
    if est_used or max_ctx:
        cw = b""
        if est_used:
            cw += _field_varint(1, est_used)
        if max_ctx:
            cw += _field_varint(4, max_ctx)
        chat = _field_bytes(10, cw)
        msg += _field_bytes(9, chat)
    if credit_cost:
        msg += _field_varint(13, credit_cost)
    if consumed_credits:
        msg += _field_varint(18, consumed_credits)
    return msg


def _blob(usage_msg, model_display_name, est_used, max_ctx, credit_cost=0, consumed_credits=0):
    gen = _gen_message(usage_msg, model_display_name, est_used, max_ctx,
                       credit_cost, consumed_credits)
    return _field_bytes(1, gen)


def _recorded_sample():
    return _blob(
        _usage(4827, 155, 32630, 72, 83),
        "Gemini 3.5 Flash (Medium)",
        50065,
        256000,
    )


def test_recorded_sample_decodes_exactly():
    rec = decode_generation(_recorded_sample())
    assert rec is not None
    assert rec["input_tokens"] == 4827
    assert rec["output_tokens"] == 155
    assert rec["cache_read_tokens"] == 32630
    assert rec["thinking_tokens"] == 72
    assert rec["response_tokens"] == 83
    assert rec["model_display_name"] == "Gemini 3.5 Flash (Medium)"
    assert rec["estimated_tokens_used"] == 50065
    assert rec["max_context_tokens"] == 256000
    assert rec["decoder_version"] == "ag-v1"


def test_missing_usage_returns_none():
    gen = _gen_message(b"", "Gemini 3.5 Flash (Medium)", 0, 0)
    assert decode_generation(_field_bytes(1, gen)) is None


def test_thinking_response_mismatch_returns_none():
    # output 100 but thinking(60) + response(30) = 90 -> gate fails
    blob = _blob(_usage(100, 100, 0, 60, 30), "Gemini 3.5 Flash (Medium)", 0, 0)
    assert decode_generation(blob) is None


def test_truncated_empty_random_never_raises():
    assert decode_generation(b"") is None
    assert decode_generation(b"\x08\x96\x01") is None  # truncated varint
    assert decode_generation(b"\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\x01") is None
    assert decode_generation(os.urandom(64)) is None


def test_varint_longer_than_ten_bytes_returns_none():
    # 11 continuation bytes then a terminator
    blob = b"\x82\x80\x80\x80\x80\x80\x80\x80\x80\x80\x00"
    assert decode_generation(blob) is None


def test_depth_beyond_limit_returns_none():
    # Path-aware descent means crafted self-nesting no longer recurses deep
    # (only (0,1),(1,4),(1,9),(2,10) are descended). Exercise the hard depth
    # ceiling directly to prove the guard still exists.
    import antigravity_proto as ap

    assert ap._decode_message(b"\x08\x01", ap._MAX_DEPTH + 1) is None
    # and a legitimately-shaped blob still parses at depth 0
    assert decode_generation(_recorded_sample()) is not None


def test_unknown_extra_fields_ignored():
    usage = _usage(10, 10, 0, 0, 10)
    gen = _gen_message(usage, "Model", 0, 0)
    gen += _field_varint(99, 12345)  # unknown varint field at gen level
    gen += _field_bytes(20, b"\x08\x01")  # custom_metadata kv (not descended)
    assert decode_generation(_field_bytes(1, gen)) is not None


def test_field_twenty_nested_payload_never_descended():
    # field 20 carries a length-delimited payload that would fail to decode as a
    # message if we descended; we must treat it as opaque bytes and succeed.
    usage = _usage(5, 5, 0, 0, 5)
    gen = _gen_message(usage, "Model", 0, 0)
    gen += _field_bytes(20, b"\xde\xad\xbe\xef")
    rec = decode_generation(_field_bytes(1, gen))
    assert rec is not None
    assert rec["input_tokens"] == 5


def test_five_mib_blob_returns_none():
    big = b"\x00" * (5 * 1024 * 1024)
    assert decode_generation(big) is None


def test_length_overruns_remaining_returns_none():
    # field 1 declares a 1000-byte payload but only 2 bytes follow
    header = _varint((1 << 3) | 2) + _varint(1000)
    assert decode_generation(header + b"\x08\x01") is None


def test_100k_repeated_unknown_fields_returns_none_quickly():
    # 100k varint fields -> per-message field-count cap trips early.
    msg = _field_varint(1000, 1) * 100_000
    start = time.monotonic()
    res = decode_generation(msg)
    elapsed = time.monotonic() - start
    assert res is None
    assert elapsed < 0.2, f"field-count cap took {elapsed:.3f}s"


def test_control_chars_in_model_name_are_stripped():
    usage = _usage(1, 1, 0, 0, 1)
    gen = _gen_message(usage, "Bad\x00Model\x07Name", 0, 0)
    rec = decode_generation(_field_bytes(1, gen))
    assert rec is not None
    assert "\x00" not in rec["model_display_name"]
    assert "\x07" not in rec["model_display_name"]


def test_empty_usage_zero_tokens_is_none():
    # field 4 present but empty: no token signal -> treated as no data.
    gen = _field_bytes(1, _field_bytes(4, b""))
    assert decode_generation(gen) is None
