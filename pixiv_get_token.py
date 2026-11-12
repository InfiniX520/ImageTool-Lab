"""Compatibility entry point for the legacy Pixiv token helper.

The browser-aware implementation is maintained in ``utils.pixiv_auth``.
"""

from utils.pixiv_auth import (
    AUTH_TOKEN_URL,
    CLIENT_ID,
    CLIENT_SECRET,
    LOGIN_URL,
    REDIRECT_URI,
    USER_AGENT,
    get_token,
    oauth_pkce,
    s256,
)

__all__ = [
    "AUTH_TOKEN_URL",
    "CLIENT_ID",
    "CLIENT_SECRET",
    "LOGIN_URL",
    "REDIRECT_URI",
    "USER_AGENT",
    "get_token",
    "oauth_pkce",
    "s256",
]


if __name__ == "__main__":
    get_token()
