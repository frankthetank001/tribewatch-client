"""Shared aiohttp helpers — SSL context built from certifi's CA bundle.

PyInstaller-frozen builds on Windows can't reliably read the system CA
store, so any plain ``aiohttp.ClientSession()`` call to an HTTPS host
fails with::

    SSLCertVerificationError: certificate verify failed:
    unable to get local issuer certificate

certifi ships its own (Mozilla-curated) bundle and PyInstaller packages
it correctly. Force aiohttp to use it via an explicit SSL context.

All HTTPS requests originating from the desktop client should go through
``make_session()`` (or ``make_connector()`` for cases where the caller
needs to pass extra session args).
"""

from __future__ import annotations

import logging
import ssl
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

_CACHED_SSL_CONTEXT: ssl.SSLContext | None = None


def _build_ssl_context() -> ssl.SSLContext | None:
    """Build an SSL context using certifi's CA bundle.

    Returns None if certifi isn't importable (extremely unusual — it's
    a transitive dep of aiohttp). Callers fall back to aiohttp's
    default context, matching pre-fix behavior.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        log.warning(
            "Could not build certifi-backed SSL context — HTTPS requests "
            "will use the system CA store (may fail in frozen builds).",
            exc_info=True,
        )
        return None


def _ssl_context() -> ssl.SSLContext | None:
    """Return a cached certifi-backed SSL context (built once per process)."""
    global _CACHED_SSL_CONTEXT
    if _CACHED_SSL_CONTEXT is None:
        _CACHED_SSL_CONTEXT = _build_ssl_context()
    return _CACHED_SSL_CONTEXT


def make_connector() -> aiohttp.TCPConnector:
    """Build a TCPConnector with the certifi SSL context.

    Use this when you need to pass extra args to ClientSession that
    can't be expressed via make_session().
    """
    ctx = _ssl_context()
    if ctx is None:
        return aiohttp.TCPConnector()
    return aiohttp.TCPConnector(ssl=ctx)


def make_session(**session_kwargs: Any) -> aiohttp.ClientSession:
    """Build an aiohttp.ClientSession that uses certifi for SSL.

    Drop-in replacement for ``aiohttp.ClientSession()`` — accepts the
    same keyword arguments. The TCPConnector is configured to verify
    against certifi's CA bundle, which sidesteps the missing-system-cert
    problem in PyInstaller-frozen builds on Windows.
    """
    return aiohttp.ClientSession(connector=make_connector(), **session_kwargs)
