"""Small, test-friendly HTTP transport with per-thread connection pooling."""

from __future__ import annotations

import threading
import weakref
from typing import Any

import requests
from requests.adapters import HTTPAdapter


class HttpClient:
    """Own one ``requests.Session`` per worker thread.

    ``requests.get`` remains a supported seam for tests and integrations: if
    callers monkeypatch it, :func:`request_get` delegates to that replacement
    instead of bypassing it.
    """

    def __init__(self, pool_size: int = 4) -> None:
        self._local = threading.local()
        self._sessions: weakref.WeakSet[requests.Session] = weakref.WeakSet()
        self._lock = threading.RLock()
        self._pool_size = max(1, int(pool_size))

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is not None:
            return session
        session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=self._pool_size,
            pool_maxsize=self._pool_size,
            max_retries=0,
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        with self._lock:
            self._sessions.add(session)
        self._local.session = session
        return session

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self._session().get(url, **kwargs)

    def close_current(self) -> None:
        session = getattr(self._local, "session", None)
        if session is not None:
            session.close()
            self._local.session = None
            with self._lock:
                self._sessions.discard(session)

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            session.close()
        self._local.session = None


_DEFAULT_CLIENT = HttpClient()
_ORIGINAL_REQUESTS_GET = requests.get


def request_get(url: str, **kwargs: Any) -> requests.Response:
    """Use a pooled session while retaining the conventional test seam."""

    if requests.get is not _ORIGINAL_REQUESTS_GET:
        return requests.get(url, **kwargs)
    return _DEFAULT_CLIENT.get(url, **kwargs)


def close_http_sessions() -> None:
    """Close all pooled sessions owned by this process."""

    _DEFAULT_CLIENT.close_all()


def close_current_http_session() -> None:
    """Close only the session owned by the calling worker thread."""

    _DEFAULT_CLIENT.close_current()


__all__ = ["HttpClient", "close_current_http_session", "close_http_sessions", "request_get"]
