"""Privacy / network layer for the MCP SearchEngine.

Privacy model
-------------
The server performs no local persistence — ever. There is no browser, so there
is no disk cache, no history and no cookie store to leak: every request gets a
**fresh** ephemeral client with its own empty :class:`httpx.Cookies` jar, and
that client is discarded as soon as the request completes.

Outbound egress is funneled through one spot so the privacy behavior is
uniform across every source (and the ``delve_sources`` scraper):

- *Proxy via env* — a proxy is read from ``SEARCH_PROXY_URL``, then
  ``HTTPS_PROXY``, then ``ALL_PROXY``. SOCKS proxies (``socks5://`` /
  ``socks5h://``) are supported via the ``httpx[socks]`` extra (``socksio``).
  The ``h`` variant of ``socks5h`` resolves DNS at the proxy, which prevents
  local DNS leaks — important when routing through Tor.
- *Optional external sandbox relay* — set ``SEARCH_SANDBOX_URL`` to a base URL
  of a disposable sandbox (e.g. a self-hosted agentOS / Rivet VM relay,
  https://agentos-sdk.dev/docs/apps/) that fetches the target on our behalf.
  When set, ``fetch_text`` rewrites each request to
  ``{SEARCH_SANDBOX_URL}<url-encoded absolute URL>`` and the local machine never
  touches the target site. agentOS is a TypeScript / Rivet library and is **not**
  importable from Python, so this env-relay is the integration seam.
- *VPN is OS-level* — when the operating system is on a VPN, all traffic already
  routes through it; the proxy / sandbox options are orthogonal to that.

There are no secrets in code, and URLs that might embed credentials are never
logged.
"""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

__all__ = [
    "_proxy",
    "ephemeral_client",
    "fetch_text",
    "SEARCH_SANDBOX_URL",
]


def _proxy() -> str | None:
    """Resolve an outbound proxy from the environment.

    Order of precedence: ``SEARCH_PROXY_URL``, then ``HTTPS_PROXY``, then
    ``ALL_PROXY``. Returns ``None`` when no proxy is configured.

    Supported schemes include ``http://``, ``socks5://`` and ``socks5h://``.
    The ``h`` suffix in ``socks5h`` means DNS resolution happens at the proxy,
    preventing local DNS leaks (important for Tor). A plain VPN at the OS level
    already routes everything through it, independent of this proxy.
    """
    for name in ("SEARCH_PROXY_URL", "HTTPS_PROXY", "ALL_PROXY"):
        value = os.environ.get(name)
        if value:
            return value
    return None


@asynccontextmanager
async def ephemeral_client(**kwargs: Any) -> AsyncIterator[httpx.AsyncClient]:
    """Create a fresh, ephemeral :class:`httpx.AsyncClient`.

    Every call produces a brand-new client with its own empty
    :class:`httpx.Cookies` jar (never shared between requests) and the resolved
    proxy applied (``proxy=`` is only passed when one is configured). Useful for
    merges: the provided ``timeout``, ``headers`` and ``verify`` kwargs are merged
    in, and ``follow_redirects`` defaults to ``True``.

    The client never writes anything to disk. Use it as::

        async with ephemeral_client(timeout=..., headers=...) as client:
            resp = await client.get(...)
    """
    proxy = _proxy()
    kwargs.setdefault("follow_redirects", True)
    kwargs.setdefault("cookies", httpx.Cookies())
    if proxy is not None:
        kwargs.setdefault("proxy", proxy)
    async with httpx.AsyncClient(**kwargs) as client:
        yield client


async def fetch_text(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 20.0,
    max_chars: int | None = None,
    verify: bool = True,
) -> str:
    """GET ``url`` over an ephemeral client and return the response text.

    Routes every request through :func:`ephemeral_client` so the proxy, the
    sandbox relay and the no-persistence guarantee apply to everything. Use this
    (or :func:`ephemeral_client`) for **all** network egress.

    If ``SEARCH_SANDBOX_URL`` is set, the request is rewritten to
    ``{SEARCH_SANDBOX_URL}{quote(url, safe='')}`` so an external disposable
    sandbox performs the fetch and the local machine never contacts the target.

    Raises on non-2xx status via ``resp.raise_for_status()``. Truncates the
    returned text to ``max_chars`` characters when given.
    """
    sandbox = os.environ.get("SEARCH_SANDBOX_URL")
    request_url = url
    if sandbox:
        request_url = f"{sandbox}{urllib.parse.quote(url, safe='')}"
    async with ephemeral_client(
        timeout=httpx.Timeout(timeout),
        headers=headers,
        verify=verify,
    ) as client:
        resp = await client.get(request_url)
        resp.raise_for_status()
        body = resp.text
    if max_chars is not None and len(body) > max_chars:
        body = body[:max_chars]
    return body