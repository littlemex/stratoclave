"""
MVP モジュール。

MVP (Bedrock プロキシ + クレジット管理) のために新設した最小構成。
既存の backend/api, backend/auth, backend/acp には触らず、並列で動作する。

ルーター:
- /v1/messages         (Anthropic Messages API 互換、Bedrock プロキシ)
- /api/mvp/me          (whoami + credit)
- /api/mvp/admin/users (Admin によるユーザー作成)
- /api/mvp/auth/login  (CLI Cognito User/Pass 認証ヘルパー)
"""
