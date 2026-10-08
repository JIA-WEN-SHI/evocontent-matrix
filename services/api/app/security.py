import hashlib
import hmac
from typing import Iterable

from fastapi import Depends, Header, HTTPException, Request, status

from .config import Settings, get_settings
from .models import Actor


def get_actor(
    x_user_id: str | None = Header(default=None),
    x_user_role: str | None = Header(default=None),
) -> Actor:
    if not x_user_id or not x_user_role:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication headers",
        )
    return Actor(user_id=x_user_id, role=x_user_role)


def require_roles(*allowed_roles: str):
    allowed = set(allowed_roles)

    def _dependency(actor: Actor = Depends(get_actor)) -> Actor:
        if actor.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{actor.role}' is not allowed",
            )
        return actor

    return _dependency


async def verify_webhook_signature(
    request: Request,
    x_signature: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    if not x_signature:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing signature")

    body = await request.body()
    expected = hmac.new(
        settings.webhook_shared_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, x_signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")


def ensure_role_in(role: str, allowed: Iterable[str]) -> None:
    if role not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

