"""
Authentication and authorization for the platform API.

Design decision (interview-defensible): API keys with role scoping, not JWT.
This is a service/platform API, not a user-facing session system -- the
industry pattern for exactly this shape is API keys (Stripe, Anthropic, and
OpenAI all authenticate their APIs with keys). JWT earns its complexity when
you have multi-user sessions, token expiry/refresh, and an identity provider
-- none of which exist here. Adding JWT now would be resume-driven
engineering; the upgrade path (an /auth/token endpoint issuing short-lived
JWTs from these same keys, or SSO via an IdP) is clear and documented.

RBAC model -- two roles, least privilege:
  ADMIN:    everything, including pause/resume (kill switch) and task assignment
  REVIEWER: read agents/audit, approve/reject escalations, submit feedback --
            the human-in-the-loop role, without the power to reconfigure agents

Keys come from environment variables (12-factor config):
  AGENT_OPS_ADMIN_KEY, AGENT_OPS_REVIEWER_KEY
If NEITHER is set, the API runs in explicit dev-mode (no auth) and logs a
prominent warning -- so the out-of-the-box demo still works, but you can't
accidentally deploy secured-looking-but-open endpoints: the state is loud.
"""
import logging
import os
import secrets
from enum import Enum

from fastapi import Depends, HTTPException, Security
from fastapi.security import APIKeyHeader

logger = logging.getLogger("agent_ops.auth")

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


class Role(str, Enum):
    ADMIN = "admin"
    REVIEWER = "reviewer"


def _configured_keys() -> dict[str, Role]:
    keys = {}
    admin_key = os.environ.get("AGENT_OPS_ADMIN_KEY")
    reviewer_key = os.environ.get("AGENT_OPS_REVIEWER_KEY")
    if admin_key:
        keys[admin_key] = Role.ADMIN
    if reviewer_key:
        keys[reviewer_key] = Role.REVIEWER
    return keys


def auth_enabled() -> bool:
    return bool(_configured_keys())


def get_current_role(api_key: str = Security(API_KEY_HEADER)) -> Role:
    keys = _configured_keys()
    if not keys:
        logger.warning("AUTH DISABLED: no API keys configured -- running in dev mode. "
                        "Set AGENT_OPS_ADMIN_KEY / AGENT_OPS_REVIEWER_KEY before deploying.")
        return Role.ADMIN  # dev mode: full access, loudly logged
    if api_key is None:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header.")
    # constant-time comparison to avoid timing side-channels on key checks
    for known_key, role in keys.items():
        if secrets.compare_digest(api_key, known_key):
            return role
    raise HTTPException(status_code=401, detail="Invalid API key.")


def require_admin(role: Role = Depends(get_current_role)) -> Role:
    if role != Role.ADMIN:
        raise HTTPException(status_code=403, detail="Admin role required for this operation.")
    return role


def require_reviewer_or_admin(role: Role = Depends(get_current_role)) -> Role:
    # Both roles pass; exists as a named dependency so route intent is explicit.
    return role
