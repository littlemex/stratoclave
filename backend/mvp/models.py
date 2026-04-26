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


def resolve_bedrock_model(anthropic_model: Optional[str]) -> str:
    """Anthropic 形式のモデル名を Bedrock inference profile ID に変換.

    - 既に Bedrock 形式 (us.anthropic.* / global.anthropic.* 等) ならそのまま返す
    - マッピングにないモデルは fallback として DEFAULT_MODEL を返す
    """
    if not anthropic_model:
        return DEFAULT_MODEL
    if anthropic_model.startswith(("us.", "apac.", "eu.", "global.", "anthropic.")):
        return anthropic_model
    return _MAPPING.get(anthropic_model, DEFAULT_MODEL)
