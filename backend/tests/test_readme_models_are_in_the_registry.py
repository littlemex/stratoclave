"""The non-Claude model families the README names are in the shipped registry.

Checked at family level for the names below (GPT-5.x, Grok, Gemma, Nemotron, Qwen):
each must match an alias, family or Bedrock id in `backend/mvp/defaults/models.json`.
Claude names and other vendors are not checked, and a version inside a family (for
example Sol versus Terra) is not.
"""
from __future__ import annotations

import json
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
README = REPO / "README.md"
REGISTRY = REPO / "backend" / "mvp" / "defaults" / "models.json"
NAMES = re.compile(r"\b(gpt-5\.\d+|grok[- ]?\d+(?:\.\d+)?|gemma(?:[- ]?\d+)?|nemotron|qwen\d*)(?![\d.])", re.I)


def _norm(name: str) -> str:
    return re.sub(r"[- ]", "", name.lower())


def _registry_strings() -> list[str]:
    out = []
    for m in json.loads(REGISTRY.read_text(encoding="utf-8"))["models"]:
        out += [m["bedrock_model_id"], m.get("model_family", "")] + list(m.get("aliases", []))
    return [_norm(s) for s in out if s]


def _missing(text: str, registry: list[str]) -> list[str]:
    named = {_norm(n) for n in NAMES.findall(text)}
    return sorted(n for n in named
                  if not any(re.search(re.escape(n) + r"(?![\d])", r) for r in registry))


def test_the_named_families_are_shipped():
    assert _missing(README.read_text(encoding="utf-8"), _registry_strings()) == []


def test_the_check_can_fail():
    registry = [_norm(s) for s in ("us.openai.gpt-5.6-sol", "us.xai.grok-4.6", "gpt-5.10")]
    assert _missing("served: GPT-5.4, Gemma 4, GPT-5.1", registry) == ["gemma4", "gpt5.1", "gpt5.4"]
    assert _missing("GPT-5.6 Sol and Grok 4.6", registry) == []
