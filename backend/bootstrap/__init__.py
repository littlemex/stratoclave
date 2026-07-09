"""Backend bootstrap (lifespan initialization).

Provides idempotent seed functions for OSS zero-touch startup.
"""
from .seed import seed_all, seed_permissions, seed_default_tenant

__all__ = ["seed_all", "seed_permissions", "seed_default_tenant"]
