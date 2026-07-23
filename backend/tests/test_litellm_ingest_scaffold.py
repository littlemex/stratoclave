"""The litellm config.yaml ingestion is SCAFFOLD-ONLY in this slice. This test
locks that contract: the entry point exists (import works) and fails LOUD
(NotImplementedError) rather than silently returning an empty/"nothing migrates"
report. Replace with the real four-way-classification suite when slice-3 lands
(reference implementation: commit 267d6ba)."""
from __future__ import annotations

import pytest

from mvp.litellm_ingest import translate_litellm_config


def test_translate_is_scaffold_and_fails_loud():
    with pytest.raises(NotImplementedError):
        translate_litellm_config({"model_list": []})
