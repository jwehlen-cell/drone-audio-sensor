from __future__ import annotations

import asyncio
import time
from typing import Any

import jwt as pyjwt
import structlog

from .config import settings
from .registry import DeviceRegistry

log = structlog.get_logger(__name__)


class AuthError(Exception):
    """Raised when a device cannot be authenticated."""


class JwtAuth:
    """Validates Android-signed ES256 JWTs against Firestore-registered
    device public keys, with a short-lived in-memory cache to keep the hot
    path off of Firestore."""

    def __init__(
        self,
        registry: DeviceRegistry,
        audience: str | None = None,
        issuer: str | None = None,
        cache_ttl_seconds: int | None = None,
    ) -> None:
        self._registry = registry
        self._audience = audience or settings.jwt_audience
        self._issuer = issuer or settings.jwt_issuer
        self._cache_ttl = cache_ttl_seconds or settings.jwt_public_key_cache_seconds
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    async def validate(self, token: str) -> dict[str, Any]:
        if not token:
            raise AuthError("missing token")
        try:
            unverified_header = pyjwt.get_unverified_header(token)
        except pyjwt.PyJWTError as e:
            raise AuthError(f"unparseable token header: {e}") from e

        kid = unverified_header.get("kid")
        if not kid:
            raise AuthError("missing kid in JWT header")

        public_key_pem = await self._get_public_key(kid)
        if public_key_pem is None:
            raise AuthError(f"unknown or revoked device: {kid}")

        try:
            claims = pyjwt.decode(
                token,
                public_key_pem,
                algorithms=["ES256"],
                audience=self._audience or None,
                issuer=self._issuer or None,
                options={
                    "require": ["exp", "iat", "sub"],
                    "verify_aud": bool(self._audience),
                    "verify_iss": bool(self._issuer),
                },
            )
        except pyjwt.ExpiredSignatureError as e:
            raise AuthError("token expired") from e
        except pyjwt.InvalidSignatureError as e:
            raise AuthError("invalid signature") from e
        except pyjwt.InvalidAudienceError as e:
            raise AuthError(f"invalid audience (expected {self._audience})") from e
        except pyjwt.InvalidIssuerError as e:
            raise AuthError(f"invalid issuer") from e
        except pyjwt.PyJWTError as e:
            raise AuthError(f"invalid token: {e}") from e

        if claims.get("sub") != kid:
            raise AuthError("sub does not match kid")
        return claims

    def invalidate(self, device_id: str) -> None:
        # Called by admin tooling when revoking; best-effort, in-process only.
        self._cache.pop(device_id, None)

    async def _get_public_key(self, device_id: str) -> str | None:
        now = time.monotonic()
        async with self._lock:
            cached = self._cache.get(device_id)
            if cached and cached[1] > now:
                return cached[0]

        pem = await self._registry.get_public_key(device_id)
        if pem is None:
            return None

        async with self._lock:
            self._cache[device_id] = (pem, now + self._cache_ttl)
        return pem
