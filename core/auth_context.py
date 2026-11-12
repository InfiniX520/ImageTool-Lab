"""Credential resolution shared by the GUI and headless download services.

The public downloader APIs historically accepted a raw ``auth_cookie``
argument.  That remains supported, but callers no longer have to thread a
secret through every layer: when it is omitted this resolver reads the
browser-exported Netscape file through :class:`CookieProvider`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock

from core.cookie_provider import CookieProvider


@dataclass(frozen=True)
class AuthContext:
    """Small immutable request-auth context passed across application ports."""

    cookie_header: str = field(default="", repr=False)
    user_agent: str = ""
    source: str = "none"


_DEFAULT_PROVIDER = CookieProvider()
_PROVIDER_LOCK = RLock()


def resolve_cookie_header(explicit: str | None = None) -> str:
    """Return an explicit header or the current automatic cookie export.

    The provider is metadata-cached and the lock only protects its tiny cache
    bookkeeping; no network or file write happens here.
    """

    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    with _PROVIDER_LOCK:
        return _DEFAULT_PROVIDER.get_cookie_header()


def default_auth_context(
    *,
    cookie_header: str | None = None,
    user_agent: str | None = None,
) -> AuthContext:
    """Build an :class:`AuthContext` without exposing credential values."""

    with _PROVIDER_LOCK:
        if cookie_header and cookie_header.strip():
            header = cookie_header.strip()
            source = "explicit"
        else:
            header = _DEFAULT_PROVIDER.get_cookie_header()
            source = _DEFAULT_PROVIDER.source
    return AuthContext(
        cookie_header=header,
        user_agent=(user_agent or "").strip(),
        source=source,
    )


__all__ = ["AuthContext", "default_auth_context", "resolve_cookie_header"]
