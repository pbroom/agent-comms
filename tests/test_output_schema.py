"""Structured results, the pure part (output_schema.py): the strict decoder and its numeric domain, the schema
subset and its deliberate differences from Draft 2020-12, the validator's verdicts and reasons, the terminal scan of a
reply, the verdict and its fail-open wrapper, and the REQUIRED differential test against jsonschema's
Draft202012Validator (never skipped: jsonschema is in the dev dependency group). DESIGN_NOTES "Structured results"."""

import json
import random
import time
from decimal import Decimal

import jsonschema       # required, never skipped (R2-08): the differential test below must run
import pytest
from jsonschema import Draft202012Validator

from agent_comms import node_sessions, output_schema as o
from agent_comms.providers import REPLY_MAX_BYTES, REPLY_TRUNCATED, cap_reply

T = "0123456789abcdef"
OTHER = "fedcba9876543210"
REVIEW = {"type": "object", "required": ["verdict", "findings"], "additionalProperties": False,
          "properties": {"verdict": {"enum": ["ok", "changes"]},
                         "findings": {"type": "array", "items": {
                             "type": "object", "required": ["file", "line", "note"], "additionalProperties": False,
                             "properties": {"file": {"type": "string"},
                                            "line": {"type": "integer", "minimum": 1},
                                            "note": {"type": "string", "maxLength": 500}}}}}}
GOOD = {"verdict": "changes", "findings": [{"file": "src/pager.py", "line": 3, "note": "off by one"}]}


def block(value, token=T, before="Done.\n") -> str:
    return f"{before}<<<result-data {token}>>>\n{json.dumps(value)}\n<<<end result-data {token}>>>"


def asked(schema=REVIEW) -> dict:
    _, shown, sha = o.prepare_schema(json.dumps(schema))
    return {"token": T, "by": "parent", "schema": shown, "schema_sha": sha, "state": "asked"}


# ------------------------------------------------------------ numbers (R2-08, R3-04)


@pytest.mark.parametrize("lexeme,value", [
    ("0", 0), ("-0", 0), ("-0.0", 0), ("1.0", 1), ("1e3", 1000), ("1E2", 100), ("12.5e-1", 1.25), ("2.50", 2.5),
    ("0.1", 0.1), ("0.001", 0.001), ("-1.5", -1.5), ("9007199254740991", 9007199254740991),
    ("-9007199254740991", -9007199254740991), ("9007199254740991.0", 9007199254740991), ("1e-300", 1e-300),
    ("1.5e-300", 1.5e-300)])
def test_a_number_in_the_domain_keeps_its_value_and_an_integral_spelling_becomes_an_int(lexeme, value):
    got = o.number(lexeme)
    assert got == value and type(got) is type(value)


@pytest.mark.parametrize("lexeme,label", [
    ("9007199254740992", "safe range"), ("9007199254740993.0", "safe range"), ("1e16", "safe range"),
    ("-9007199254740992", "safe range"), ("0.100000000000000001", "cannot hold"),
    ("100000000000000000000.1", "cannot hold"), ("1.00000000000000001", "cannot hold"),
    ("1e-400", "underflows"), ("1e999999999", "exponent"), ("1e401", "exponent"), ("1.5e-401", "exponent"),
    ("1" * 33, "32 characters"), ("2e308", "safe range"), ("1.7976931348623157e308", "safe range")])
def test_a_number_outside_the_domain_is_refused_before_any_conversion_that_could_cost(lexeme, label):
    with pytest.raises(o.DecodeError) as e:
        o.number(lexeme)
    assert label in e.value.label


def test_an_extreme_exponent_is_refused_at_once():
    t = time.perf_counter()
    for lexeme in ("1e999999999", "9e99999999999999999999999999"[:32], "1e-999999999"):
        with pytest.raises(o.DecodeError):
            o.number(lexeme)
    assert time.perf_counter() - t < 0.05


def test_a_rendered_schema_decodes_back_to_the_same_numbers():
    """R3-04: an admitted fraction's shortest spelling IS its value, so the rendered schema the session is shown, and
    that the verdict re-decodes, holds the same numbers; an integral one renders as an int."""
    schema = {"type": "object", "properties": {"x": {"type": "number", "minimum": 0.1, "maximum": 1e3,
                                                      "exclusiveMaximum": 2.5}, "y": {"const": 0.1},
                                               "z": {"enum": [1.0, 0.25, -0.0]}}}
    s, shown, _ = o.prepare_schema(json.dumps(schema))
    again = o.decode(shown, o.SCHEMA_MAX_BYTES, o.SCHEMA_JSON_DEPTH)
    assert again == s and '"maximum":1000' in shown and '"enum":[1,0.25,0]' in shown


# ------------------------------------------------------------ the decoder's other limits


def test_the_decoder_refuses_what_a_tolerant_parser_would_collapse_or_accept():
    def label(text, most=o.BLOCK_MAX_BYTES, depth=o.BLOCK_JSON_DEPTH):
        with pytest.raises(o.DecodeError) as e:
            o.decode(text, most, depth)
        return e.value.label

    assert "twice" in label('{"a": 1, "a": 2}')
    assert "NaN" in label('{"a": NaN}') and "NaN" in label('{"a": -Infinity}')
    assert "surrogate" in label('{"a": "\\ud800"}') and "surrogate" in label('{"\\udc00": 1}')
    assert "over 8,192 bytes" in label('"' + "x" * 8191 + '"')
    o.decode('"' + "x" * 8190 + '"', o.BLOCK_MAX_BYTES, o.BLOCK_JSON_DEPTH)          # exactly 8,192: fine
    assert "over 8,192 bytes" in label('"' + "é" * 4096 + '"'), "4,098 characters, 8,194 bytes: the bytes decide"
    assert "deeper than 32" in label("[" * 33 + "]" * 33)
    o.decode("[" * 32 + "]" * 32, o.BLOCK_MAX_BYTES, o.BLOCK_JSON_DEPTH)
    assert "deeper than 24" in label("[" * 25 + "]" * 25, o.SCHEMA_MAX_BYTES, o.SCHEMA_JSON_DEPTH)
    many = "{" + ",".join(f'"k{i}": 0' for i in range(201)) + "}"
    assert "more than 200 members" in label(many)
    o.decode("{" + ",".join(f'"k{i}": 0' for i in range(200)) + "}", 100000, 32)
    assert "more than 1000 elements" in label("[" + ",".join(["0"] * 1001) + "]", 100000)
    o.decode("[" + ",".join(["0"] * 1000) + "]", 100000, 32)
    e = pytest.raises(o.DecodeError, o.decode, '{"a": 1,\n "b": }', 100, 32).value
    assert (e.label, e.line, e.col) == ("a value was expected", 2, 7) and e.words() == \
        "a value was expected at line 2 column 7"
    # A duplicate key or a number has no position (the hooks get none): none is promised.
    assert pytest.raises(o.DecodeError, o.decode, '{"a":1,"a":1}', 100, 32).value.line is None


class Counted(str):
    """A string that records being read whole (strip, encode, iteration): R4-03."""
    reads: list = []

    def strip(self, *a):
        Counted.reads.append("strip")
        return str.strip(self, *a)

    def encode(self, *a, **kw):
        Counted.reads.append("encode")
        return str.encode(self, *a, **kw)

    def __iter__(self):
        Counted.reads.append("iter")
        return str.__iter__(self)


def test_a_string_over_the_limit_in_characters_is_refused_without_being_read():
    """R4-03: the length in characters (each at least one byte) is checked before anything strips, encodes or walks
    the string, so an arbitrarily large schema or block costs nothing before its refusal."""
    big = Counted(" " * 1_000_000 + "{}")
    Counted.reads = []
    with pytest.raises(o.SchemaError, match="over 8,192 bytes"):
        o.prepare_schema(big)
    with pytest.raises(o.DecodeError, match="over 8,192 bytes"):
        o.decode(big, o.BLOCK_MAX_BYTES, o.BLOCK_JSON_DEPTH)
    assert Counted.reads == []
    near = Counted("\u00e9" * 4097)                       # 4,097 characters, 8,194 bytes: the bytes decide
    with pytest.raises(o.SchemaError, match="over 8,192 bytes"):
        o.prepare_schema(near)
    assert Counted.reads == ["encode"]


def test_no_decoder_message_passes_through_python_s_own_words():
    for text in ['{"a" 1}', "[1,]", '{"a": "\x01"}', '"\\q"', "{} x", "", '{"a": tru}']:
        with pytest.raises(o.DecodeError) as e:
            o.decode(text, 100, 32)
        assert e.value.label in set(o._JSON_LABELS.values()) | {"not valid JSON"}


# ------------------------------------------------------------ the subset (B), its refusals and differences


def refusal(schema) -> str:
    with pytest.raises(o.SchemaError) as e:
        o.prepare_schema(json.dumps(schema) if not isinstance(schema, str) else schema)
    return str(e.value)


def test_the_review_schema_and_every_admitted_keyword_pass():
    o.prepare_schema(json.dumps(REVIEW))
    every = {"$schema": o.SCHEMA_URIS[1], "type": "object", "title": "t", "description": "d",
             "properties": {"a": {}, "b": {"type": ["string", "null"], "minLength": 1, "maxLength": 9},
                            "c": {"type": "array", "items": {"const": 1}, "minItems": 0, "maxItems": 3},
                            "d": {"type": "integer", "minimum": 0, "maximum": 9, "exclusiveMinimum": -1,
                                  "exclusiveMaximum": 10},
                            "e": {"enum": ["x", 1, True, None, 1.5]}, "f": {"type": "boolean"}},
             "required": ["a"], "additionalProperties": True}
    o.prepare_schema(json.dumps(every))
    o.prepare_schema(json.dumps(every | {"$schema": o.SCHEMA_URIS[0]}))


@pytest.mark.parametrize("schema,words", [
    ({"type": "array"}, 'the top level must be "type": "object"'),
    ({"type": ["object"]}, 'the top level must be "type": "object"'),
    ({"type": "object", "$schema": "https://example.com/s"}, "never fetched"),
    ({"type": "object", "properties": {"a": {"$ref": "#/$defs/x"}}}, "references are not supported"),
    ({"type": "object", "$defs": {}}, "references are not supported"),
    ({"type": "object", "properties": {"a": {"type": "string", "pattern": "a+"}}}, "no pattern or format check"),
    ({"type": "object", "properties": {"a": {"type": "string", "format": "email"}}}, "no pattern or format check"),
    ({"type": "object", "properties": {"a": {"anyOf": [{}]}}}, "combinators are not supported"),
    ({"type": "object", "properties": {"a": {"type": "array", "uniqueItems": True}}}, "not in this subset"),
    ({"type": "object", "x-mine": 1}, "an unknown keyword"),
    ({"type": "object", "properties": {"a": True}}, "is true or false"),
    ({"type": "object", "additionalProperties": {}}, "must be true or false"),
    ({"type": "object", "required": ["a"]}, "that are in properties"),
    ({"type": "object", "properties": {"a": {"maxLength": 3}}}, 'needs "type" to include string'),
    ({"type": "object", "properties": {"a": {"type": "integer", "maxLength": 3}}}, 'needs "type" to include string'),
    ({"type": "object", "properties": {"a": {"type": "string", "minimum": 3}}}, "integer or number"),
    ({"type": "object", "properties": {"a": {"type": "date"}}}, '"type" at #/properties/"a" must be one of'),
    ({"type": "object", "properties": {"a": {"type": ["string", "string"]}}}, "must be one of"),
    ({"type": "object", "properties": {"a": {"enum": [[1]]}}}, "`enum` at"),
    ({"type": "object", "properties": {"a": {"enum": [1, 1.0]}}}, "distinct"),
    ({"type": "object", "properties": {"a": {"enum": []}}}, "1 to 50"),
    ({"type": "object", "properties": {"a": {"const": {"x": 1}}}}, "`const` at"),
    ({"type": "object", "properties": {"a": {"type": "string", "maxLength": 8193}}}, "0 to 8192"),
    ({"type": "object", "properties": {"a": {"type": "string", "maxLength": 1.5}}}, "0 to 8192"),
    ({"type": "object", "properties": {"a": {"type": "array", "maxItems": True}}}, "0 to 1000"),
    ({"type": "object", "title": "x" * 101}, "a title at #"),
    ({"type": "object", "description": "x" * 301}, "a description at #"),
    ({"type": "object", "properties": {"": {}}}, "a property name at #"),
    ({"type": "object", "properties": {"a\nb": {}}}, "control character"),
    ({"type": "object", "properties": {"a\x7fb": {}}}, "control character"),
])
def test_every_refusal_has_its_words(schema, words):
    assert words in refusal(schema)


def test_the_structural_limits_each_have_a_refusal():
    deep = {"type": "object"}
    cur = deep
    for _ in range(7):
        cur["properties"] = {"a": {"type": "object"}}
        cur = cur["properties"]["a"]
    o.prepare_schema(json.dumps(deep))                      # 8 levels of schemas
    cur["properties"] = {"a": {"type": "object"}}
    assert "deeper than 8 levels of schemas" in refusal(deep)
    wide = {"type": "object", "properties": {f"p{i}": {} for i in range(51)}}
    assert "more than 50 properties" in refusal(wide)
    nodes = {"type": "object", "properties": {f"p{i}": {"type": "object", "properties": {
        f"q{j}": {} for j in range(39)}} for i in range(5)}}       # 1 + 5 + 5*39 = 201
    assert "more than 200 schemas" in refusal(nodes)
    assert "is over 8,192 bytes" in refusal('{"type": "object", "description": "' + " " * 8200 + '"}')
    assert "not strict JSON" in refusal('{"type": "object", "type": "object"}')
    assert "not strict JSON" in refusal('{"type": "object", "properties": {"a": {"const": 9007199254740993}}}')


def test_the_rendered_schema_has_a_cap_of_its_own():
    """R2-04: a schema within 8,192 bytes as given can be far bigger once ASCII-escaped (the form shown)."""
    schema = {"type": "object", "properties": {f"p{i}": {"description": "漢" * 300} for i in range(7)}}
    text = json.dumps(schema, ensure_ascii=False)
    assert len(text.encode()) <= o.SCHEMA_MAX_BYTES < len(o.rendered(schema).encode())
    assert "once written as compact ASCII-escaped JSON" in refusal(text)


def test_a_short_name_is_quoted_whole_and_ascii_escaped_on_the_short_path_too():
    """R7: a name that fits takes the short path (no cut), and that path escapes as well: non-ASCII, a bidi
    override, a line separator and a quote come back as escapes, on one line."""
    name = 'éé"‮ '
    quoted = o.quote(name)
    assert len(name) < o.QUOTE_MAX and not quoted.endswith('..."'), "the short path"
    assert quoted == '"e\\u0301\\u00e9\\"\\u202e\\u2028"'
    assert quoted.isascii() and json.loads(quoted) == name


def test_a_path_in_a_refusal_quotes_names_whole_and_escaped():
    name = 'a"b\\c‮' + "x" * 80
    words = refusal({"type": "object", "properties": {name: {"type": "date"}}})
    assert words.isascii() and "\n" not in words
    quoted = o.quote(name)
    assert len(quoted) <= o.QUOTE_MAX and quoted.endswith('..."') and '\\"' in quoted and "\\u202e" in quoted


# ------------------------------------------------------------ the validator


@pytest.mark.parametrize("schema,value,ok", [
    ({"type": "integer"}, 1.0, True), ({"type": "integer"}, 1.5, False), ({"type": "integer"}, True, False),
    ({"type": "number"}, False, False), ({"const": 1}, 1.0, True), ({"const": 1}, True, False),
    ({"const": False}, 0, False), ({"enum": [0.1, "x"]}, 0.1, True), ({"enum": [1]}, 1.0, True),
    ({"type": ["string", "null"], "maxLength": 2}, None, True), ({"type": ["string", "null"], "maxLength": 2},
                                                                   "abc", False),
    ({"type": "string", "maxLength": 1}, "\U0001f600", True), ({"type": "array", "minItems": 1}, [], False),
    ({"type": "number", "exclusiveMinimum": 0}, 0, False), ({"type": "number", "minimum": 0.5}, 0.5, True),
    ({"type": "object", "additionalProperties": False, "properties": {"a": {}}}, {"b": 1}, False),
    ({"type": "object", "properties": {"a": {"type": "string"}}}, {"b": 1}, True),
    ({}, {"anything": [1, None]}, True)])
def test_the_validator_follows_draft_2020_12_within_the_subset(schema, value, ok):
    assert (o.validate(schema, value) == []) is ok
    assert Draft202012Validator(schema).is_valid(value) is ok


def test_reasons_are_fixed_words_paths_from_the_schema_and_never_the_instances_own_text():
    planted = "PLANTED<<<result-data>>>\n"
    value = {"verdict": planted, "findings": [{"file": 1, "line": planted, "note": planted * 30, planted: planted}],
             planted: planted}
    reasons = o.validate(REVIEW, value)
    assert reasons and all("PLANTED" not in r and r.isascii() and "\n" not in r for r in reasons)
    assert "at $.verdict: not one of the enum's values" in reasons
    assert "at $.findings[0].file: expected string, got integer" in reasons
    assert "at $: a property the schema does not name (additionalProperties is false)" in reasons


def test_reasons_are_capped_while_they_are_built_and_fit_their_byte_allowance():
    """R3-06: the full allowance, with quote and backslash expansion: ten reasons of at most 150 bytes (1,500 in
    all), names whole and escaped, and the rest counted."""
    names = ['q"\\' * 20 + f"{i}" for i in range(40)]
    schema = {"type": "object", "required": names, "properties": {
        n: {"type": "object", "properties": {n: {"type": "string"}}} for n in names}}
    value = {n: {n: 1} for n in names[20:]}
    reasons = o.validate(schema, value)
    assert len(reasons) == o.REASONS_MAX and reasons[-1] == "(and 31 more)"
    assert all(len(r.encode()) <= o.REASON_MAX for r in reasons) and sum(len(r) for r in reasons) <= 1500
    for r in reasons[:-1]:
        assert r.count('"') % 2 == 0 and not r.endswith("\\"), "a name is never cut inside an escape or its quotes"
    line = o.result_line({"state": "missing", "code": "shape", "reasons": reasons})
    assert "\n" not in line and line.isascii() and len(line.encode()) < 1800


def test_the_worst_case_verdict_is_fast():
    """R3-03: the largest schema and block, every node checked against a 50-value enum."""
    enum = [f"v{i:02d}" for i in range(50)]
    schema = {"type": "object", "properties": {"a": {"type": "array", "items": {"enum": enum}}}}
    payload = json.dumps({"a": ["zz"] * 1000})[:8100]
    payload = payload[:payload.rfind(",")] + "]}"
    reply = "x\n<<<result-data %s>>>\n%s\n<<<end result-data %s>>>" % (T, payload, T)
    out = asked(schema)
    t = time.perf_counter()
    for _ in range(20):
        v = o.verdict(out, ok=True, code=None, reply=reply)
    assert v["code"] == "shape" and (time.perf_counter() - t) / 20 < 0.05


# ------------------------------------------------------------ the terminal scan (C, R3-05)


@pytest.mark.parametrize("reply,code", [
    (block(GOOD), None),
    (block(GOOD) + "\n\n  \t\n", None),                                       # trailing ASCII blank lines
    (block(GOOD).replace("\n", "\r\n") + "\r\n", None),                       # CRLF
    (block(GOOD) + "   ", None),                                              # a final line without LF, blank
    (f"<<<result-data {T}>>>   \n{json.dumps(GOOD)}\n<<<end result-data {T}>>>\t", None),   # trailing spaces/tabs
    ("no block at all", "no_block"),
    (f"I will write <<<result-data {T}>>> then the JSON.", "no_block"),        # the frame's line echoed (inline)
    (block(GOOD, OTHER), "no_block"),                                         # another message's token only
    (block(GOOD) + "\nThat is all.", "form"),                                 # text after
    (block(GOOD) + f"\n<<<end result-data {OTHER}>>>", "form"),               # a foreign marker after: trailing text
    (block(GOOD) + "\n" + block(GOOD), "form"),                               # two current-token blocks
    (block(GOOD, before=f"Example:\n<<<result-data {T}>>>\n"), "form"),       # a second opening line
    (f"x\n<<<end result-data {T}>>>", "form"),                                # a closing line without its mate
    (f"x\n<<<result-data {T}>>>\n{{}}", "form"),                              # an opening line without its mate
    (f"<<<end result-data {T}>>>\n{{}}\n<<<result-data {T}>>>", "form"),      # out of order
    (f"<<<result-data {OTHER}>>>\n{{}}\n<<<end result-data {T}>>>", "form"),  # mixed tokens: a close without mate
    (f"  <<<result-data {T}>>>\n{{}}\n<<<end result-data {T}>>>", "form"),    # an indented marker never counts
    (f"﻿<<<result-data {T}>>>\n{{}}\n<<<end result-data {T}>>>", "form"),  # a BOM before it: not a marker
    (block(GOOD, before=f"  <<<result-data {T}>>>\n  (an indented example)\n"), None),   # an indented example
    (f"```\n<<<result-data {T}>>>\n```\n" + block(GOOD), "form"),            # a fenced example still counts
    (block(GOOD).replace(json.dumps(GOOD), "```json\n" + json.dumps(GOOD) + "\n```"), "not_json"),
    (f"x\n<<<result-data {T}>>>\n<<<result-data {OTHER}>>>\n<<<end result-data {T}>>>", "not_json"),  # inside payload
    (f"x\n<<<result-data {T}>>>\n[1]\n<<<end result-data {T}>>>", "not_object"),
    (f"x\n<<<result-data {T}>>>\n{{\"a\": \" \"}}\n<<<end result-data {T}>>>", None),  # U+2028 is no line break
    (block(GOOD) + REPLY_TRUNCATED, "truncated"),
])
def test_the_terminal_scan_follows_the_grammar(reply, code):
    assert o.extract(reply, T)[0] == code


def test_the_payloads_original_bytes_count_toward_the_cap_and_the_separators_do_not():
    def with_payload(n, nl="\n", final=""):
        value = '{"a": "' + "x" * (n - 9) + '"}'
        assert len(value) == n
        return f"x{nl}<<<result-data {T}>>>{nl}{value}{nl}<<<end result-data {T}>>>{final}", value

    for nl in ("\n", "\r\n"):
        for final in ("", nl):
            reply, value = with_payload(o.BLOCK_MAX_BYTES, nl, final)
            code, _, payload = o.scan(reply, T)
            assert code is None and payload == value, "the LF (and CR) before the closing line is a separator"
            assert o.extract(reply, T)[0] is None
            assert o.extract(with_payload(o.BLOCK_MAX_BYTES + 1, nl, final)[0], T)[0] == "too_big"


def test_a_near_cap_reply_whose_block_ends_at_the_cap_is_read_and_one_cut_by_it_is_truncated():
    tail = f"\n<<<result-data {T}>>>\n{json.dumps(GOOD)}\n<<<end result-data {T}>>>"
    prose = "p" * (REPLY_MAX_BYTES - len(tail.encode()))
    exact = cap_reply(prose + tail)
    assert len(exact.encode()) == REPLY_MAX_BYTES and exact.endswith(tail) and o.extract(exact, T)[0] is None
    cut = cap_reply("p" + prose + tail)
    assert cut.endswith(REPLY_TRUNCATED) and o.extract(cut, T)[0] == "truncated"


# ------------------------------------------------------------ the verdict and its fail-open wrapper


def test_the_verdict_is_a_fresh_dict_without_the_schema_and_never_a_copy_of_the_data():
    out = asked()
    v = o.verdict(out, ok=True, code=None, reply=block(GOOD))
    assert v is not out and out["state"] == "asked" and "schema" in out, "nothing shared is changed"
    assert v["state"] == "accepted" and "schema" not in v and v["data_sha"] == o.digest(GOOD)
    assert "changes" not in json.dumps(v) and "off by one" not in json.dumps(v)
    bad = o.verdict(out, ok=True, code=None, reply=block({"verdict": "maybe", "findings": []}))
    assert (bad["state"], bad["code"], bad["reasons"]) == ("missing", "shape",
                                                           ["at $.verdict: not one of the enum's values"])
    assert o.verdict(out, ok=False, code="timed_out", reply=block(GOOD))["code"] == "timed_out", \
        "a turn that did not complete is never read"


@pytest.mark.parametrize("stage", ["scan", "decode_block", "validate", "digest", "decode"])
def test_any_fault_in_the_verdict_is_missing_internal(stage, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("feature fault")
    out = asked()
    monkeypatch.setattr(o, stage, boom)
    v = o.safe_verdict(out, ok=True, code=None, reply=block(GOOD))
    assert (v["state"], v["code"]) == ("missing", "internal") and "schema" not in v


def test_derive_gives_the_data_only_on_a_digest_match():
    reply = block(GOOD)
    assert o.derive(reply, T, o.digest(GOOD)) == GOOD
    assert o.derive(reply, T, o.digest({"x": 1})) is None
    assert o.derive(reply, OTHER, o.digest(GOOD)) is None
    assert o.derive(None, T, o.digest(GOOD)) is None and o.derive(reply + "\nmore", T, o.digest(GOOD)) is None


def test_the_hub_line_is_one_line_and_says_what_was_checked():
    line = o.result_line({"state": "accepted"})
    assert "\n" not in line and "only its shape was checked" in line.lower() and "correlation, not proof" in line
    assert "between this result's child-output lines" in line
    missing = o.result_line({"state": "missing", "code": "truncated"})
    assert "may have been cut" in missing and missing.endswith("Nothing was put in its place.")


def test_the_node_loss_prefix_is_the_nodes_own_words():
    assert node_sessions.LOST.startswith(o.NODE_LOST_PREFIX)


# ------------------------------------------------------------ the differential test (required)


def _oracle(schema_text: str, value_text: str) -> bool:
    """Draft 2020-12 on the same raw texts, decoded independently with exact numbers (Decimal), with an integer and
    number checker compatible with the subset (an integral Decimal is an integer; a bool is neither)."""
    schema = json.loads(schema_text, parse_float=Decimal)
    value = json.loads(value_text, parse_float=Decimal)
    def is_int(_, x):
        return not isinstance(x, bool) and (isinstance(x, int) or (isinstance(x, Decimal) and x == x.to_integral_value()))

    def is_num(_, x):
        return not isinstance(x, bool) and isinstance(x, (int, Decimal))
    checker = Draft202012Validator.TYPE_CHECKER.redefine_many({"integer": is_int, "number": is_num})
    cls = jsonschema.validators.extend(Draft202012Validator, type_checker=checker)
    return cls(schema).is_valid(value)


def _gen_schema(rng: random.Random, depth: int = 0) -> dict:
    kind = rng.choice(["object", "array", "string", "integer", "number", "boolean", "null", "enum", "any", "union"])
    if depth >= 3 and kind in ("object", "array"):
        kind = "string"
    if kind == "object":
        props = {f"p{i}": _gen_schema(rng, depth + 1) for i in range(rng.randint(0, 3))}
        s = {"type": "object", "properties": props}
        if props and rng.random() < 0.6:
            s["required"] = rng.sample(sorted(props), rng.randint(1, len(props)))
        if rng.random() < 0.5:
            s["additionalProperties"] = rng.random() < 0.5
        return s
    if kind == "array":
        s = {"type": "array", "items": _gen_schema(rng, depth + 1)}
        if rng.random() < 0.5:
            s["maxItems"] = rng.randint(0, 3)
        return s
    if kind == "string":
        return {"type": "string", "maxLength": rng.randint(0, 4)} if rng.random() < 0.5 else {"type": "string"}
    if kind in ("integer", "number"):
        s = {"type": kind}
        for k in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
            if rng.random() < 0.3:
                s[k] = rng.choice([0, 1, -1, 0.5, 2.5, 1.0, 10])
        return s
    if kind == "enum":
        return {"enum": rng.sample([0, 1, 1.5, "a", "b", True, False, None], rng.randint(1, 4))}
    if kind == "union":
        return {"type": rng.sample(["string", "null", "integer", "number"], 2), "maxLength": 2} if rng.random() < 0.5 \
            else {"type": ["integer", "null"], "minimum": 0}
    return {"type": rng.choice(["boolean", "null"])} if kind in ("boolean", "null") else {}


def _gen_value(rng: random.Random, depth: int = 0):
    k = rng.randint(0, 9 if depth < 3 else 6)
    makers = [lambda: None, lambda: True, lambda: False, lambda: rng.choice([0, 1, -1, 2, 10]),
              lambda: rng.choice([0.5, 1.0, 2.5, -0.5, 1.5]), lambda: rng.choice(["", "a", "ab", "abc", "b"]),
              lambda: "x" * rng.randint(0, 5),
              lambda: [_gen_value(rng, depth + 1) for _ in range(rng.randint(0, 3))],
              lambda: {rng.choice(["p0", "p1", "p2", "q"]): _gen_value(rng, depth + 1) for _ in range(rng.randint(0, 3))},
              lambda: {f"p{i}": _gen_value(rng, depth + 1) for i in range(3)}]
    return makers[k]()


def test_the_validator_agrees_with_draft_2020_12_on_admitted_schemas_and_instances():
    """REQUIRED (never skipped). Verdicts are compared only for schemas our checker admits and instances our decoder
    admits; both sides decode the same raw texts independently."""
    rng = random.Random(20261123)
    compared = disagreements = 0
    for _ in range(400):
        inner = _gen_schema(rng)
        schema = {"type": "object", "properties": {"v": inner}, "required": ["v"]}
        schema_text = json.dumps(schema)
        try:
            s, _, _ = o.prepare_schema(schema_text)
        except o.SchemaError:
            continue
        for _ in range(10):
            value_text = json.dumps({"v": _gen_value(rng)})
            try:
                value = o.decode_block(value_text)
            except o.DecodeError:
                continue
            ours = o.validate(s, value) == []
            if ours != _oracle(schema_text, value_text):
                disagreements += 1
            compared += 1
    assert compared > 2000 and disagreements == 0


@pytest.mark.parametrize("schema", [
    {"type": "object", "required": ["a"]}, {"type": "object", "properties": {"a": True}},
    {"type": "object", "additionalProperties": {"type": "string"}}, {"type": "object", "properties": {"a": {
        "enum": [[1]]}}}, {"type": "object", "properties": {"a": {"maxLength": 3}}},
    {"type": "object", "properties": {"a": {"const": {"x": 1}}}}])
def test_a_deliberate_admission_difference_is_a_schema_draft_accepts_and_we_refuse(schema):
    Draft202012Validator.check_schema(schema)
    with pytest.raises(o.SchemaError):
        o.prepare_schema(json.dumps(schema))
