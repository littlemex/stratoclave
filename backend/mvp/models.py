"""Anthropic → Bedrock モデル ID マッピング (MVP はハードコード).

Claude Code が送ってくる Anthropic 形式の model id を
Bedrock の inference profile ID に変換する.

このマッピングは 2026-04-25 時点で `aws bedrock list-inference-profiles` で
実在を確認済み. 将来的には Parameter Store から取得して外部化予定.
"""
import os
from typing import Optional


# MVP default: Opus 4.7 (必須要件)
DEFAULT_MODEL = os.getenv(
    "DEFAULT_BEDROCK_MODEL",
    "us.anthropic.claude-opus-4-7",
)


# 左: Claude Code / Anthropic SDK が送ってくるモデル名 (Anthropic 命名規則)
# 右: Bedrock inference profile ID (us-east-1、2026-04-25 実在確認済み)
_MAPPING: dict[str, str] = {
    # Claude 4 系 (MVP で必須: Opus 4.6, 4.7)
    "claude-opus-4-7": "us.anthropic.claude-opus-4-7",
    "claude-opus-4-6": "us.anthropic.claude-opus-4-6-v1",
    "claude-opus-4-5": "us.anthropic.claude-opus-4-5-20251101-v1:0",
    "claude-opus-4-1": "us.anthropic.claude-opus-4-1-20250805-v1:0",
    "claude-opus-4": "us.anthropic.claude-opus-4-20250514-v1:0",
    "claude-sonnet-4-6": "us.anthropic.claude-sonnet-4-6",
    "claude-sonnet-4-5": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "claude-haiku-4-5": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    # Claude 3.x 系 (互換性のため)
    "claude-3-7-sonnet": "us.anthropic.claude-3-7-sonnet-20250219-v1:0",
    "claude-3-5-haiku": "us.anthropic.claude-3-5-haiku-20241022-v1:0",
    "claude-3-haiku": "us.anthropic.claude-3-haiku-20240307-v1:0",
    "claude-3-opus": "us.anthropic.claude-3-opus-20240229-v1:0",
    "claude-3-sonnet": "us.anthropic.claude-3-sonnet-20240229-v1:0",
    # 日付サフィックス付きのエイリアス (Anthropic SDK が送ってくる形式)
    "claude-opus-4-5-20251101": "us.anthropic.claude-opus-4-5-20251101-v1:0",
    "claude-opus-4-1-20250805": "us.anthropic.claude-opus-4-1-20250805-v1:0",
    "claude-opus-4-20250514": "us.anthropic.claude-opus-4-20250514-v1:0",
    "claude-sonnet-4-5-20250929": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "claude-haiku-4-5-20251001": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "claude-3-7-sonnet-20250219": "us.anthropic.claude-3-7-sonnet-20250219-v1:0",
    "claude-3-5-haiku-20241022": "us.anthropic.claude-3-5-haiku-20241022-v1:0",
}


# Bedrock 側モデル ID の allowlist (Claude ファミリのみ).
# `_MAPPING` の value に加え、環境変数 `DEFAULT_BEDROCK_MODEL` の値と
# 既知の inference profile 接頭辞を含む "anthropic" を必須とする。
#
# なお Bedrock には Llama / Nova / Mistral 等 Anthropic 以外のモデルも存在するが、
# 本プロキシは Claude 系専用であり、それ以外を指定されてもコスト計算・credit 消費
# ロジックが前提としていないため allowlist 外として 400 を返す。
_ALLOWED_BEDROCK_MODELS: frozenset[str] = frozenset(
    list(_MAPPING.values()) + [DEFAULT_MODEL]
)


def resolve_bedrock_model(anthropic_model: Optional[str]) -> str:
    """Anthropic 形式のモデル名を Bedrock inference profile ID に変換.

    - Anthropic 名 (`_MAPPING` の key) → 対応する Bedrock ID
    - 既に Bedrock 形式の ID でも `_ALLOWED_BEDROCK_MODELS` に含まれるもののみ通す
    - それ以外 (Llama / Nova / Mistral / 独自 region prefix) は ValueError で 400 を起こす

    呼び出し側で ValueError を HTTPException(400) にマップすること。
    """
    if not anthropic_model:
        return DEFAULT_MODEL

    # Anthropic 形式 (alias) 経由
    mapped = _MAPPING.get(anthropic_model)
    if mapped is not None:
        return mapped

    # Bedrock ID 直指定は allowlist 限定
    if anthropic_model in _ALLOWED_BEDROCK_MODELS:
        return anthropic_model

    raise ValueError(
        f"model '{anthropic_model}' is not allowed. "
        f"Only Claude family models are supported by this proxy."
    )
