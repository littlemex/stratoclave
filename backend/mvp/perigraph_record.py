"""The perigraph record of a `/v1/chat/completions` request: the caller's harness, named by digests of its parts.

perigraph (https://github.com/littlemex/perigraph, spec version 3) names a harness by digests of the parts of a request
the model reads, and says for every other part why it is not in the record. Two requests sent under one model name with
different system prompts get different `perigraph.harness_identity` values, so a caller that groups its observations by
model name can see that it is about to pool two harnesses. The gateway reads the request it already holds and returns
the record; the caller decides what to do with it.

**What it reads.** The caller's request body as decoded from JSON, before `ChatCompletionsRequest` coerces any value
and before the gateway builds a provider request from it. The record therefore describes the request the caller sent to
stratoclave. The Converse transport caps `temperature` at 1.0, so the `decoding` digest describes the value the caller
sent, not the value invoked; the other translations touch fields no part reads. None of them changes the system text or
the message text a request template names, the two parts that can enter the identity.

**The model parts.** The gateway does not load weights, so every model part is `not_observable` and the record has no
model identity. The `checkpoint` part carries the provider model id the gateway invoked as a label, which keys nothing.
A receiver admits such a record (requirement PG-047); its claims are not bound to model bytes.

**No runtime dependency on perigraph.** The part lists and the identity rules are read from a vendored copy of
perigraph's `spec/vocabularies.json`; absence reasons, statuses and boundaries are literals here. A copy whose sha256 is
not the one this module implements is not read, and every response then says `collector_failed` and carries no record.

**`record()` never raises.** A body field it cannot read is an absence that says so (`extraction_failed`, with a detail
naming the field and either the JSON type found or the check that failed, never its content). A defect in this module
is a record with status `collector_failed`, which no receiver admits to a verdict.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from core.logging import get_logger

logger = get_logger(__name__)

#: The vendored vocabularies and the digest they must have. A new perigraph spec version changes the file, and the
#: rules below may have to change with it, so a copy with another digest is not read.
VOCABULARIES_PATH = Path(__file__).resolve().parent / "perigraph_vocabularies.json"
VOCABULARIES_SHA256 = "1ca5b18c7a2f66f3fd8d0140709b4e570004590ff2f11b4e3932f74b9e9e62b3"

#: Who wrote the record (`perigraph.collector`) and which revision of this module's reading rules
#: (`perigraph.collector_version`). Bumped when what a body yields changes, so two records can be told apart by the
#: rules that produced them rather than by the gateway's release, which changes for unrelated reasons.
COLLECTOR = "stratoclave"
COLLECTOR_VERSION = "1"

#: The top-level member that carries a request template (binding openai-chat-completions). The gateway removes it
#: before forwarding, because no provider reads it and the OpenAI-compatible endpoint rejects unknown members.
TEMPLATE_FIELD = "perigraph_template"

#: The request header that asks for the whole record, and the three response headers.
HDR_WANT_RECORD = "x-sc-perigraph-return"
WANT_RECORD_VALUE = "record"
HDR_STATUS = "x-sc-perigraph-status"
HDR_HARNESS_IDENTITY = "x-sc-perigraph-harness-identity"
HDR_RECORD = "x-sc-perigraph-record"

#: Stands for a request body the route could not decode as JSON. The record then says the collector failed, rather
#: than describing a body it never saw.
BODY_NOT_DECODED = object()

_REACHES = ("instruction", "request_template", "tool_schemas", "readout", "decoding")
_BOUNDARY = {"instruction": "model_visible", "request_template": "model_visible", "tool_schemas": "parsed",
             "readout": "parsed", "decoding": "parsed"}
_DECODING_KEYS = ("temperature", "top_p", "top_k", "seed")
#: Parts a request body has no field for. Their absence is the sender's (`not_provided`), not a failure to find them.
_NO_REQUEST_FIELD_PARTS = ("loop", "turn_budget", "retry_policy", "context_partitioning")
#: A label is for a human reader; a longer `response_format.type` is not a format name and is left out.
_MAX_LABEL = 64


class VocabularyMismatch(RuntimeError):
    """The vendored vocabularies are not the ones this module implements."""


def _load_vocabularies() -> dict:
    raw = VOCABULARIES_PATH.read_bytes()
    got = hashlib.sha256(raw).hexdigest()
    if got != VOCABULARIES_SHA256:
        raise VocabularyMismatch(f"{VOCABULARIES_PATH.name} has sha256 {got}, and this module implements "
                                 f"{VOCABULARIES_SHA256}; reading lists it was not written against would emit records "
                                 f"that claim a spec version they do not follow")
    return json.loads(raw.decode("utf-8"))


try:
    _VOCAB: Optional[dict] = _load_vocabularies()
except Exception as e:  # noqa: BLE001 -- every response says collector_failed; the request path is unaffected
    _VOCAB = None
    logger.error("perigraph_vocabularies_unloadable", error=f"{type(e).__name__}: {e}")

if _VOCAB is not None:
    SPEC_VERSION: int = _VOCAB["spec_version"]
    _NS: str = _VOCAB["namespace"]
    _PARTS: tuple[str, ...] = tuple(_VOCAB["parts"]["values"])
    _MODEL_PARTS: tuple[str, ...] = tuple(_VOCAB["model_parts"]["values"])
    _NEVER_IDENTIFYING = frozenset(_VOCAB["never_identifying"]["values"])
    _REFINES_ONLY = frozenset(_VOCAB["refines_only"]["values"])
    _MUST_BE_KNOWN = tuple(_VOCAB["must_be_known_for_identity"]["values"])
    _KNOWN_WHEN_ABSENT_AS: str = _VOCAB["must_be_known_for_identity"]["known_when_absent_as"]
    _IDENTIFYING_SOURCING = frozenset(_VOCAB["sourcing"]["may_key_identity"])
    _IDENTIFYING_BOUNDARIES = frozenset(_VOCAB["boundaries"]["may_key_identity"])

_WHITE_SPACE = ("\u0009\u000a\u000b\u000c\u000d\u0020\u0085\u00a0\u1680" + "".join(chr(c) for c in range(0x2000, 0x200b))
                + "\u2028\u2029\u202f\u205f\u3000")


def _blank(text: str) -> bool:
    return not text.strip(_WHITE_SPACE)


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _render_pairs(pairs: list[tuple[str, object]]) -> str:
    """Protocol values rendered for a `parsed` digest, as perigraph's Python reference renders them, so the two
    collectors' `parsed` digests agree; the binding leaves this serialisation to the collector."""
    return ";".join(f"{k}={v!r}" for k, v in pairs)


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "an array"
    if isinstance(value, dict):
        return "an object"
    return type(value).__name__


def _utf8(text: str) -> Optional[bytes]:
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        return None


def _instruction(messages: object) -> tuple[str, str]:
    """The system text, newline-joined, or the reason it cannot be read. A message whose role cannot be read might have
    been a system message, and a system message whose content is an array is rendered by a chat template the request
    does not carry, so both fail the part rather than guess. So does a system text that is not a sequence of Unicode
    scalar values, which has no UTF-8 bytes to digest."""
    if messages is None:
        return "", ""
    if not isinstance(messages, list):
        return "", f"messages is {_json_type(messages)}, not an array"
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            return "", f"messages[{i}] is {_json_type(m)}, not an object"
        if "role" not in m:
            return "", f"messages[{i}].role is missing, not a string"
        if not isinstance(m["role"], str):
            return "", f"messages[{i}].role is {_json_type(m['role'])}, not a string"
    texts = []
    for i, m in enumerate(messages):
        if m.get("role") != "system":
            continue
        if "content" not in m:
            return "", f"messages[{i}].content is missing, not a string"
        if not isinstance(m["content"], str):
            return "", f"messages[{i}].content is {_json_type(m['content'])}, not a string"
        if _utf8(m["content"]) is None:
            return "", f"messages[{i}].content is not a sequence of Unicode scalar values"
        texts.append(m["content"])
    return "\n".join(texts), ""


def _matches_fixed(content: bytes, fixed: list[bytes]) -> bool:
    """Whether content is fixed[0] h1 fixed[1] ... hn fixed[n] for some byte strings h. Taking the earliest occurrence
    of each interior string leaves the most room for the rest."""
    if len(fixed) == 1:
        return content == fixed[0]
    if not content.startswith(fixed[0]):
        return False
    pos = len(fixed[0])
    for seg in fixed[1:-1]:
        j = content.find(seg, pos)
        if j < 0:
            return False
        pos = j + len(seg)
    return len(content) - len(fixed[-1]) >= pos and content.endswith(fixed[-1])


def _template(template: object, messages: object) -> tuple[Optional[str], str]:
    """The `request_template` payload, or the reason it cannot be read. None when no template is sent or its fixed
    strings are all white space. The payload covers the number of entries, each role and the fixed strings, never the
    text in the holes; the white-space test comes after every check."""
    if template is None:
        return None, ""
    where = TEMPLATE_FIELD
    if not isinstance(template, dict):
        return None, f"{where} is {_json_type(template)}, not an object"
    if set(template) != {"messages"}:
        return None, f"{where} has members other than messages, or lacks messages"
    entries = template["messages"]
    if not isinstance(entries, list):
        return None, f"{where}.messages is {_json_type(entries)}, not an array"
    if not isinstance(messages, list):
        return None, f"messages is {'missing' if messages is None else _json_type(messages)}, not an array"
    if len(entries) != len(messages):
        return None, f"{where}.messages has {len(entries)} entries and messages has {len(messages)}"
    payload = [f"{len(entries)}:".encode()]
    names_text = False
    for i, (e, m) in enumerate(zip(entries, messages)):
        at = f"{where}.messages[{i}]"
        if not isinstance(e, dict) or "role" not in e or not set(e) <= {"role", "fixed"}:
            return None, f"{at} is not an object with role and optionally fixed"
        role = e["role"]
        if not isinstance(role, str) or not isinstance(m, dict) or m.get("role") != role:
            return None, f"{at}.role is not a string equal to messages[{i}].role"
        rb = _utf8(role)
        if rb is None:
            return None, f"{at}.role is not a sequence of Unicode scalar values"
        if "fixed" not in e:
            payload.append(f"{len(rb)}:".encode() + rb + b"0:")
            continue
        if role == "system":
            return None, f"{at} is a system entry with fixed strings; its text is the instruction part"
        strings = e["fixed"]
        if not isinstance(strings, list) or not strings or not all(isinstance(g, str) for g in strings):
            return None, f"{at}.fixed is not a non-empty array of strings"
        if any(not g for g in strings[1:-1]):
            return None, f"{at}.fixed has an empty interior string; adjacent holes are one hole"
        content = m.get("content")
        if not isinstance(content, str):
            found = "missing" if "content" not in m else _json_type(content)
            return None, f"messages[{i}].content is {found}, not a string"
        segs = [_utf8(g) for g in strings]
        cb = _utf8(content)
        if cb is None or any(b is None for b in segs):
            return None, f"{at} or messages[{i}].content holds a string that is not a sequence of Unicode scalar values"
        if not _matches_fixed(cb, segs):  # type: ignore[arg-type]
            return None, f"messages[{i}].content does not match {at}.fixed"
        names_text = names_text or any(not _blank(g) for g in strings)
        payload.append(f"{len(rb)}:".encode() + rb + f"{len(segs)}:".encode()
                       + b"".join(f"{len(b)}:".encode() + b for b in segs))  # type: ignore[arg-type]
    if not names_text:
        return None, ""
    return b"".join(payload).decode("utf-8"), ""


def _format_label(fmt: dict) -> str:
    """`response_format.type` as the readout label, when it is a short string; anything else would put request
    content in a header."""
    kind = fmt.get("type")
    if isinstance(kind, str) and 0 < len(kind) <= _MAX_LABEL and _utf8(kind) is not None:
        return kind
    return ""


def _read(body: object) -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]]]:
    """Parts held (`kind -> (digest, label)`) and absences (`kind -> (reason, detail)`) for one body, following the
    binding openai-chat-completions part by part."""
    held: dict[str, tuple[str, str]] = {}
    absent: dict[str, tuple[str, str]] = {}

    def hold(kind: str, payload: str, label: str = "") -> None:
        held[kind] = (_digest(payload), label)

    if not isinstance(body, dict):
        detail = f"the body is {_json_type(body)}, not an object"
        for k in _REACHES:
            absent[k] = ("extraction_failed", detail)
    else:
        instruction, failed = _instruction(body.get("messages"))
        if failed:
            absent["instruction"] = ("extraction_failed", failed)
        elif not _blank(instruction):
            # Hashed raw, down to case and white space: the model reads the string, not its meaning.
            hold("instruction", instruction)
        else:
            absent["instruction"] = ("not_provided", "")

        template, failed = _template(body.get(TEMPLATE_FIELD), body.get("messages"))
        if failed:
            absent["request_template"] = ("extraction_failed", failed)
        elif template is not None:
            hold("request_template", template)
        else:
            absent["request_template"] = ("not_provided", "")

        tools = body.get("tools")
        if tools is not None and not isinstance(tools, list):
            bad_tool = f"tools is {_json_type(tools)}, not an array"
        else:
            bad_tool = next((f"tools[{i}] is {_json_type(t)}, not an object" if not isinstance(t, dict) else
                             f"tools[{i}].function is {_json_type(t.get('function'))}, not an object"
                             for i, t in enumerate(tools or [])
                             if not isinstance(t, dict) or not isinstance(t.get("function", {}), dict)), "")
        if bad_tool:
            absent["tool_schemas"] = ("extraction_failed", bad_tool)
        elif tools:
            # Sorted: the order a caller lists tools in is not part of what it offered.
            shapes = sorted(_render_pairs([("name", (t.get("function") or t).get("name")),
                                           ("params", (t.get("function") or t).get("parameters"))]) for t in tools)
            hold("tool_schemas", "|".join(shapes), label=f"{len(tools)} tool(s)")
        else:
            absent["tool_schemas"] = ("not_provided", "")

        fmt = body.get("response_format")
        bad_fmt = f"response_format is {_json_type(fmt)}, not an object" if fmt is not None and not isinstance(fmt, dict) \
            else ""
        if bad_fmt:
            absent["readout"] = ("extraction_failed", bad_fmt)
        elif fmt:
            hold("readout", _render_pairs([("response_format", fmt)]), label=_format_label(fmt))
        else:
            absent["readout"] = ("not_provided", "")

        # A response_format constrains the grammar of the output, so it is decoding as well as readout.
        knobs = [(k, body[k]) for k in _DECODING_KEYS if body.get(k) is not None] + \
            ([("response_format", fmt)] if fmt else [])
        if bad_fmt:
            absent["decoding"] = ("extraction_failed", bad_fmt)
        elif knobs:
            hold("decoding", _render_pairs(knobs), label=",".join(k for k, _ in knobs))
        else:
            absent["decoding"] = ("not_provided", "")

    for kind in _NO_REQUEST_FIELD_PARTS:
        absent[kind] = ("not_provided", "")
    absent["tool_extension"] = ("not_observable", "")
    # At request time the calls have not happened; earlier tool calls in `messages` are not read as a trace.
    absent["tool_trace"] = ("not_provided", "")
    return held, absent


def _identity(held: dict[str, tuple[str, str]], absent: dict[str, tuple[str, str]]) -> Optional[str]:
    """SPEC section 2: over the identifying parts in kind order, when the record can show it is not missing a part
    that would have keyed it. Every part this module holds is `in_the_request`."""
    identifying = sorted(k for k in held if k not in _NEVER_IDENTIFYING and _BOUNDARY[k] in _IDENTIFYING_BOUNDARIES
                         and "in_the_request" in _IDENTIFYING_SOURCING)
    if not any(k not in _REFINES_ONLY for k in identifying):
        return None
    known = set(held) | {k for k, (reason, _) in absent.items() if reason == _KNOWN_WHEN_ABSENT_AS}
    if not all(k in known for k in _MUST_BE_KNOWN):
        return None
    h = hashlib.sha256()
    for k in identifying:
        h.update(f"{k}={held[k][0]};".encode())
    return h.hexdigest()[:24]


def _attributes(status: str, held: dict, absent: dict, identity: Optional[str],
                model_label: Optional[str]) -> dict[str, Any]:
    out: dict[str, Any] = {
        f"{_NS}.spec_version": SPEC_VERSION,
        f"{_NS}.collector": COLLECTOR,
        f"{_NS}.collector_version": COLLECTOR_VERSION,
        f"{_NS}.collector_reaches": ",".join(_REACHES),
        f"{_NS}.status": status,
    }
    if identity is not None:
        out[f"{_NS}.harness_identity"] = identity
    out[f"{_NS}.model_reaches"] = ""
    for kind in _MODEL_PARTS:
        if kind == "checkpoint" and model_label:
            out[f"{_NS}.model.checkpoint.sourcing"] = "not_observable"
            out[f"{_NS}.model.checkpoint.label"] = model_label
        else:
            out[f"{_NS}.model_absent.{kind}.reason"] = "not_observable"
    for kind, (dg, label) in held.items():
        base = f"{_NS}.part.{kind}"
        out[f"{base}.sourcing"] = "in_the_request"
        out[f"{base}.digest"] = dg
        out[f"{base}.boundary"] = _BOUNDARY[kind]
        if label:
            out[f"{base}.label"] = label
    for kind, (reason, detail) in absent.items():
        out[f"{_NS}.absent.{kind}.reason"] = reason
        if detail:
            out[f"{_NS}.absent.{kind}.detail"] = detail
    return out


def _failed_record(detail: str, model_label: Optional[str]) -> dict[str, Any]:
    """A record about this module's own failure: every part absent `extraction_failed`, status `collector_failed`, and
    no identity, so no receiver can mistake it for a description of the caller's harness."""
    return _attributes("collector_failed", {}, {k: ("extraction_failed", detail) for k in _PARTS}, None, model_label)


def _checkpoint_label(model_id: Optional[str]) -> Optional[str]:
    """The invoked model id as the checkpoint label, unless it is an ARN: an ARN names the gateway's AWS account,
    which is not the caller's to read."""
    if not model_id or model_id.startswith("arn:"):
        return None
    return model_id


def record(body: object, *, model_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """The record of one request body as perigraph wire attributes (SPEC section 6), or None when the vendored
    vocabularies could not be read. Never raises.

    `body` is the request as decoded from JSON, not the validated `ChatCompletionsRequest`: the binding reads decoded
    values, and validation coerces some of them. `model_id` is the provider model id the gateway invoked; it labels the
    checkpoint part and keys nothing.
    """
    if _VOCAB is None:
        return None
    label = _checkpoint_label(model_id)
    if body is BODY_NOT_DECODED:
        return _failed_record("the gateway could not decode the request body as JSON", label)
    try:
        held, absent = _read(body)
        if set(held) | set(absent) != set(_PARTS) or set(held) & set(absent):
            raise AssertionError(f"parts unaccounted for: {sorted(set(_PARTS) - set(held) - set(absent))}")
        return _attributes("complete", held, absent, _identity(held, absent), label)
    except Exception as e:  # noqa: BLE001 -- a defect here must not fail the request it describes
        logger.error("perigraph_record_failed", error=f"{type(e).__name__}: {e}")
        return _failed_record(f"the gateway's collector failed ({type(e).__name__})", label)


def encode_record(attributes: dict[str, Any]) -> str:
    """The record as one header value: the JSON Lines binding's object, base64url without padding. Serialised with
    ASCII escapes, so any string the record holds, a lone surrogate included, encodes."""
    raw = json.dumps(attributes, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_record(value: str) -> dict[str, Any]:
    """The inverse of `encode_record`; used by the tests and the conformance adapter's callers."""
    pad = "=" * (-len(value) % 4)
    return json.loads(base64.urlsafe_b64decode(value + pad).decode("ascii"))


def response_headers(body: object, *, model_id: Optional[str], want_record: bool) -> dict[str, str]:
    """The perigraph headers for one response. Never raises.

    The status is always sent and the identity whenever the record has one. The whole record is sent only when the
    caller asks for it with `x-sc-perigraph-return: record`, because it is far larger than the other two headers.
    """
    try:
        attrs = record(body, model_id=model_id)
        if attrs is None:
            return {HDR_STATUS: "collector_failed"}
        out = {HDR_STATUS: str(attrs[f"{_NS}.status"])}
        identity = attrs.get(f"{_NS}.harness_identity")
        if identity:
            out[HDR_HARNESS_IDENTITY] = str(identity)
        if want_record:
            out[HDR_RECORD] = encode_record(attrs)
        return out
    except Exception as e:  # noqa: BLE001 -- the headers describe the request; they must not fail it
        logger.error("perigraph_headers_failed", error=f"{type(e).__name__}: {e}")
        return {HDR_STATUS: "collector_failed"}


def strip_template(payload: dict) -> dict:
    """Remove the request template from a body about to be forwarded (binding openai-chat-completions, Forwarding).
    No provider the gateway forwards to reads it, and the OpenAI-compatible endpoint rejects it as an unknown
    parameter."""
    payload.pop(TEMPLATE_FIELD, None)
    return payload
