"""
MVP module.

Minimal implementation added for MVP (Bedrock proxy + credit management).
Runs in parallel with the existing backend/api, backend/auth, and backend/acp
without touching them.

Routers:
- /v1/messages         (Anthropic Messages API compatible, Bedrock proxy)
- /api/mvp/me          (whoami + credit)
- /api/mvp/admin/users (User creation by Admin)
- /api/mvp/auth/login  (CLI Cognito User/Pass authentication helper)
"""
