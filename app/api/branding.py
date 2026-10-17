"""Safe organization branding for the hosted interface.

Stored values are optional. Invalid or missing values resolve to defaults and
are not returned to the browser.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from app.persistence import Organization

DEFAULT_PRIMARY_COLOR = "#1B3A4B"
DEFAULT_ACCENT_COLOR = "#B08D57"
_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_MAX_URL_LENGTH = 2000
_MAX_DISPLAY_NAME_LENGTH = 300


def resolve_display_name(organization: Organization) -> str:
    candidate = (organization.display_name or "").strip()
    if candidate and len(candidate) <= _MAX_DISPLAY_NAME_LENGTH:
        return candidate
    name = organization.name.strip()
    return name or "Organization"


def resolve_color(value: str | None, default: str) -> str:
    if value and _HEX_COLOR.fullmatch(value.strip()):
        return value.strip()
    return default


def resolve_public_url(value: str | None) -> str | None:
    """Accept only credential-free https URLs."""

    if value is None:
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > _MAX_URL_LENGTH:
        return None
    parsed = urlparse(candidate)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    return candidate
