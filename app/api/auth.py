"""Verified bearer-token authentication for protected API routes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any
from urllib.parse import urlparse
from uuid import UUID

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from app.config import get_settings
from app.domain import DomainValidationError, Person

_REVIEWER_EMAIL_MAX_LENGTH = 200


class AuthenticationError(Exception):
    """Raised when supplied authentication is absent or invalid."""


class AuthenticationConfigurationError(Exception):
    """Raised when authentication cannot fail closed due to missing configuration."""


class AuthenticationServiceError(Exception):
    """Raised when signing-key metadata cannot be obtained."""


class OrganizationAccessDeniedError(Exception):
    """Raised when a principal has no active membership in the selected tenant."""


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    """Identity claims trusted after JWT verification."""

    user_id: UUID
    email: str

    @property
    def actor_reference(self) -> str:
        return str(self.user_id)


class JWTVerifier:
    """Verify Supabase-compatible asymmetric JWTs against a JWKS endpoint."""

    _algorithms = ("RS256", "ES256")

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str,
        jwks_client: PyJWKClient | None = None,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._jwks_client = jwks_client or PyJWKClient(jwks_url)

    def verify(self, token: str) -> AuthenticatedPrincipal:
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
        except jwt.PyJWKClientConnectionError as exc:
            raise AuthenticationServiceError from exc
        except (jwt.PyJWKSetError, jwt.PyJWKError) as exc:
            raise AuthenticationServiceError from exc
        except json.JSONDecodeError as exc:
            raise AuthenticationServiceError from exc
        except jwt.PyJWKClientError as exc:
            raise AuthenticationError from exc
        except jwt.InvalidTokenError as exc:
            raise AuthenticationError from exc

        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(self._algorithms),
                audience=self._audience,
                issuer=self._issuer,
                options={
                    "require": ["sub", "email", "iss", "aud", "iat", "exp"],
                },
            )
        except jwt.InvalidKeyError as exc:
            raise AuthenticationError from exc
        except jwt.InvalidTokenError as exc:
            raise AuthenticationError from exc
        except TypeError as exc:
            raise AuthenticationError from exc

        try:
            user_id = UUID(str(claims["sub"]))
        except (TypeError, ValueError):
            raise AuthenticationError from None
        return AuthenticatedPrincipal(
            user_id=user_id,
            email=_display_email(claims.get("email")),
        )


@lru_cache
def _configured_verifier(
    issuer: str,
    audience: str,
    jwks_url: str,
) -> JWTVerifier:
    return JWTVerifier(issuer=issuer, audience=audience, jwks_url=jwks_url)


def get_jwt_verifier() -> JWTVerifier:
    settings = get_settings()
    issuer = settings.auth_issuer
    audience = settings.auth_audience
    jwks_url = settings.auth_jwks_url
    if (
        not issuer
        or not issuer.strip()
        or not audience
        or not audience.strip()
        or not jwks_url
        or not jwks_url.strip()
    ):
        raise AuthenticationConfigurationError
    normalized_jwks_url = jwks_url.strip()
    if settings.environment == "production":
        _require_https_jwks_url(normalized_jwks_url)
    return _configured_verifier(
        issuer.strip(),
        audience.strip(),
        normalized_jwks_url,
    )


def _require_https_jwks_url(jwks_url: str) -> None:
    parsed = urlparse(jwks_url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise AuthenticationConfigurationError


_bearer = HTTPBearer(auto_error=False)


def _require_bearer_token(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(_bearer),
    ],
) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError
    return credentials.credentials


def _display_email(value: object) -> str:
    """Accept a signed email only as reviewer display text, not as ownership proof."""

    if not isinstance(value, str):
        raise AuthenticationError
    try:
        email = Person.create(name=value, email=value).email
    except DomainValidationError:
        raise AuthenticationError from None
    if len(email) > _REVIEWER_EMAIL_MAX_LENGTH:
        raise AuthenticationError
    return email


def get_authenticated_principal(
    token: Annotated[str, Depends(_require_bearer_token)],
    verifier: Annotated[JWTVerifier, Depends(get_jwt_verifier)],
) -> AuthenticatedPrincipal:
    return verifier.verify(token)
