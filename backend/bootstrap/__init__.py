"""Backend bootstrap (lifespan-time 初期化処理).

OSS zero-touch 起動のための idempotent seed を提供する.
"""
from .seed import seed_all, seed_permissions, seed_default_tenant

__all__ = ["seed_all", "seed_permissions", "seed_default_tenant"]
