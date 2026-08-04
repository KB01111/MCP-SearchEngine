"""Multi-source search engine.

Queries several public sources concurrently and normalizes the hits into a
common :class:`SearchResult` shape so they can be presented side-by-side in
the interactive picker UI.

Available sources
-----------------
- ``web``       DuckDuckGo instant-answer API (related topics / abstract)
- ``duckduckgo`` DuckDuckGo HTML endpoint (general web results, no API key)
- ``wikipedia`` Wikipedia OpenSearch + summary API
- ``arxiv``     arXiv Atom API (papers)
- ``hackernews`` Algolia Hacker News search API
- ``reddit``    Reddit public JSON search
- ``x``         X/Twitter via syndication API + nitter fallback
- ``perplexity`` Perplexity Chat API (needs PERPLEXITY_API_KEY; opt-in, not in defaults)
"""

from __future__ import annotations

import asyncio
import html
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict, field
from typing import Any

import httpx
from bs4 import BeautifulSoup

try:
    from .net import ephemeral_client, fetch_text
except ImportError:  # pragma: no cover - standalone (Horizon/fastmcp CLI) import
    from search_engine.net import ephemeral_client, fetch_text

DEFAULT_SOURCES = ["web", "duckduckgo", "wikipedia", "arxiv", "hackernews", "reddit", "x"]

# A descriptive UA is required by Wikipedia/arXiv policy; a fake browser UA
# gets 403s from their APIs.
_UA = "MCP-SearchEngine/0.1 (https://github.com/modelcontextprotocol; research-bot) python-httpx"


@dataclass
class SearchResult:
    """A single normalized hit from any source."""

    id: str
    source: str
    title: str
    url: str
    snippet: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clean(text: str | None, limit: int = 300) -> str:
    if not text:
        return ""
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text).strip()
    return text[: limit - 1].rstrip() + "…" if len(text) > limit else text


async def _search_duckduckgo_api(
    client: httpx.AsyncClient, query: str, max_results: int
) -> list[SearchResult]:
    """DuckDuckGo instant-answer API (abstract + related topics)."""
    resp = await client.get(
        "https://api.duckduckgo.com/",
        params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
    )
    resp.raise_for_status()
    data = resp.json()
    results: list[SearchResult] = []
    if data.get("AbstractURL") and data.get("AbstractText"):
        results.append(
            SearchResult(
                id=f"ddg-api-{len(results)}",
                source="web",
                title=_clean(data.get("Heading") or query, 120),
                url=data["AbstractURL"],
                snippet=_clean(data.get("AbstractText")),
            )
        )
    for topic in data.get("RelatedTopics", []):
        if len(results) >= max_results:
            break
        if "Topics" in topic:  # nested group
            topic = topic["Topics"][0] if topic["Topics"] else {}
        url = topic.get("FirstURL")
        text = topic.get("Text")
        if url and text:
            results.append(
                SearchResult(
                    id=f"ddg-api-{len(results)}",
                    source="web",
                    title=_clean(text.split(" - ")[0], 120),
                    url=url,
                    snippet=_clean(text),
                )
            )
    return results


async def _search_duckduckgo_html(
    client: httpx.AsyncClient, query: str, max_results: int
) -> list[SearchResult]:
    """Scrape DuckDuckGo's HTML endpoint for general web results."""
    resp = await client.get(
        "https://html.duckduckgo.com/html/",
        params={"q": query},
        headers={"User-Agent": _UA},
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    results: list[SearchResult] = []
    for res in soup.select(".result"):
        if len(results) >= max_results:
            break
        # Skip ad / sponsored result blocks.
        if "result--ad" in (res.get("class") or []):
            continue
        a = res.select_one(".result__a")
        if not a or not a.get("href"):
            continue
        href = a["href"]
        # DDG wraps outbound links in a redirect — unwrap the real URL.
        parsed = urllib.parse.urlparse(href)
        qs = urllib.parse.parse_qs(parsed.query)
        url = qs.get("uddg", [href])[0]
        snippet_el = res.select_one(".result__snippet")
        results.append(
            SearchResult(
                id=f"ddg-web-{len(results)}",
                source="duckduckgo",
                title=_clean(a.get_text(), 120),
                url=url,
                snippet=_clean(snippet_el.get_text() if snippet_el else ""),
            )
        )
    return results


async def _search_wikipedia(
    client: httpx.AsyncClient, query: str, max_results: int
) -> list[SearchResult]:
    resp = await client.get(
        "https://en.wikipedia.org/w/api.php",
        params={
            "action": "opensearch",
            "search": query,
            "limit": max_results,
            "namespace": 0,
            "format": "json",
        },
        headers={"User-Agent": _UA},
    )
    resp.raise_for_status()
    titles, descriptions, urls = resp.json()[1:4]
    return [
        SearchResult(
            id=f"wiki-{i}",
            source="wikipedia",
            title=_clean(t, 120),
            url=u,
            snippet=_clean(d),
        )
        for i, (t, d, u) in enumerate(zip(titles, descriptions, urls))
    ]


async def _search_arxiv(
    client: httpx.AsyncClient, query: str, max_results: int
) -> list[SearchResult]:
    resp = await client.get(
        "https://export.arxiv.org/api/query",
        params={
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": max_results,
            "sortBy": "relevance",
        },
    )
    resp.raise_for_status()
    # Parse the Atom feed with the stdlib to avoid an lxml dependency.
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(resp.text)
    results: list[SearchResult] = []
    for i, entry in enumerate(root.findall("a:entry", ns)):
        def _txt(tag: str) -> str:
            el = entry.find(f"a:{tag}", ns)
            return (el.text or "") if el is not None else ""

        url = _txt("id")
        for link in entry.findall("a:link", ns):
            if link.get("title") == "pdf" and link.get("href"):
                url = link.get("href")
                break
        authors = ", ".join(
            (a.find("a:name", ns).text or "")
            for a in entry.findall("a:author", ns)[:3]
            if a.find("a:name", ns) is not None
        )
        results.append(
            SearchResult(
                id=f"arxiv-{i}",
                source="arxiv",
                title=_clean(_txt("title"), 160),
                url=url,
                snippet=_clean(_txt("summary")),
                extra={"authors": authors, "published": _txt("published")[:10]},
            )
        )
    return results


async def _search_hackernews(
    client: httpx.AsyncClient, query: str, max_results: int
) -> list[SearchResult]:
    resp = await client.get(
        "https://hn.algolia.com/api/v1/search",
        params={"query": query, "hitsPerPage": max_results, "tags": "story"},
    )
    resp.raise_for_status()
    results: list[SearchResult] = []
    for i, hit in enumerate(resp.json().get("hits", [])):
        url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit['objectID']}"
        points = hit.get("points", 0)
        comments = hit.get("num_comments", 0)
        results.append(
            SearchResult(
                id=f"hn-{i}",
                source="hackernews",
                title=_clean(hit.get("title"), 140),
                url=url,
                snippet=f"{points} points · {comments} comments on Hacker News",
                extra={
                    "points": points,
                    "comments": comments,
                    "hn_id": hit.get("objectID"),
                },
            )
        )
    return results


_NITTER_INSTANCES = ["https://nitter.net", "https://nitter.privacydev.net"]


async def _search_reddit(
    client: httpx.AsyncClient, query: str, max_results: int
) -> list[SearchResult]:
    """Reddit public JSON search (no API key)."""
    # Reddit rate-limits generic UAs, so use a distinct descriptive one here.
    resp = await client.get(
        "https://www.reddit.com/search.json",
        params={"q": query, "limit": max_results, "sort": "relevance", "raw_json": 1},
        headers={"User-Agent": "MCP-SearchEngine/0.1 research (by u/unknown)"},
    )
    resp.raise_for_status()
    results: list[SearchResult] = []
    for i, child in enumerate(resp.json().get("data", {}).get("children", [])):
        data = child.get("data", {})
        url = data.get("url") or f"https://www.reddit.com{data.get('permalink', '')}"
        permalink = f"https://www.reddit.com{data.get('permalink', '')}"
        results.append(
            SearchResult(
                id=f"reddit-{i}",
                source="reddit",
                title=_clean(data.get("title"), 140),
                url=url,
                snippet=_clean(data.get("selftext")),
                extra={
                    "subreddit": data.get("subreddit_name_prefixed", ""),
                    "score": data.get("score", 0),
                    "comments": data.get("num_comments", 0),
                    "permalink": permalink,
                },
            )
        )
    return results


async def _search_x(
    client: httpx.AsyncClient, query: str, max_results: int
) -> list[SearchResult]:
    """X/Twitter via nitter HTML instances (syndication API is per-profile,
    so keyword search goes through nitter mirrors)."""
    # Per-request headers override the shared client's headers in httpx: the
    # client merges its own headers with the per-request ones, giving the
    # per-request values precedence. So we can switch the UA just for this
    # source without touching the shared client.
    browser_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
    configured = os.environ.get("X_NITTER_INSTANCE")
    instances = (
        [configured] + _NITTER_INSTANCES
        if configured
        else _NITTER_INSTANCES
    )[:3]

    last_error: Exception | None = None
    for instance in instances:
        try:
            resp = await client.get(
                f"{instance}/search",
                params={"q": query, "f": "tweets"},
                headers={"User-Agent": browser_ua},
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            items = soup.select(".timeline .timeline-item .tweet-content .tweet-body")
            if not items:
                items = soup.select(".timeline-item")
            if not items:
                continue  # tried this instance, it yielded nothing
            results: list[SearchResult] = []
            for i, item in enumerate(items[:max_results]):
                text_el = item.select_one(".tweet-content")
                text = text_el.get_text(" ", strip=True) if text_el else ""
                if not text:
                    continue
                link_el = item.select_one("a.tweet-link") or item.select_one("a")
                href = link_el.get("href") if link_el and link_el.get("href") else ""
                url = href if href.startswith("http") else f"{instance}{href}" if href else f"{instance}/search"
                author_el = item.select_one(".tweet-avatar") or item.select_one(".username")
                author = author_el.get_text(" ", strip=True) if author_el else ""
                results.append(
                    SearchResult(
                        id=f"x-{i}",
                        source="x",
                        title=_clean(text[:80], 80),
                        url=url,
                        snippet=_clean(text),
                        extra={"author": author, "via": instance},
                    )
                )
            if results:
                return results
            last_error = RuntimeError(f"no results from {instance}")
        except Exception as e:  # noqa: BLE001 — any nitter instance may fail
            last_error = e
            continue
    raise RuntimeError("X search failed on all nitter instances") from last_error


async def _search_perplexity(
    client: httpx.AsyncClient, query: str, max_results: int
) -> list[SearchResult]:
    """Perplexity Chat API (requires PERPLEXITY_API_KEY)."""
    key = os.environ.get("PERPLEXITY_API_KEY")
    if not key:
        # search_all surfaces this as an error card, matching the repo contract.
        raise RuntimeError("PERPLEXITY_API_KEY not set")
    resp = await client.post(
        "https://api.perplexity.ai/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": "sonar",
            "messages": [{"role": "user", "content": query}],
        },
    )
    resp.raise_for_status()
    data = resp.json()
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message", {})
    content = message.get("content", "")
    citations = message.get("citations") or data.get("citations") or []
    url = citations[0] if citations else ""
    extra: dict[str, Any] = {}
    if citations:
        extra["citations"] = citations
    return [
        SearchResult(
            id="perplexity-0",
            source="perplexity",
            title=f"Perplexity: {query[:60]}",
            url=url,
            snippet=_clean(content, 300),
            extra=extra,
        )
    ]


_SEARCHERS = {
    "web": _search_duckduckgo_api,
    "duckduckgo": _search_duckduckgo_html,
    "wikipedia": _search_wikipedia,
    "arxiv": _search_arxiv,
    "hackernews": _search_hackernews,
    "reddit": _search_reddit,
    "x": _search_x,
    "perplexity": _search_perplexity,
}


async def search_all(
    query: str,
    sources: list[str] | None = None,
    max_per_source: int = 4,
) -> list[SearchResult]:
    """Search every requested source concurrently and merge the results.

    A failing source never sinks the whole search — its error is recorded in a
    synthetic result so the UI can show the source came back empty.
    """
    sources = [s for s in (sources or DEFAULT_SOURCES) if s in _SEARCHERS]
    if not sources:
        sources = DEFAULT_SOURCES

    # Route the shared client through the privacy layer so the proxy
    # (SEARCH_PROXY_URL / HTTPS_PROXY / ALL_PROXY) and the no-persistence
    # guarantee apply to every search source too, not just delve scrapes.
    timeout = httpx.Timeout(15.0, connect=8.0)
    headers = {"User-Agent": _UA, "Accept": "*/*"}
    async with ephemeral_client(timeout=timeout, headers=headers) as client:
        tasks = [
            _SEARCHERS[name](client, query, max_per_source) for name in sources
        ]
        settled = await asyncio.gather(*tasks, return_exceptions=True)

    results: list[SearchResult] = []
    for name, outcome in zip(sources, settled):
        if isinstance(outcome, Exception):
            results.append(
                SearchResult(
                    id=f"{name}-error",
                    source=name,
                    title=f"{name} search failed",
                    url="",
                    snippet=_clean(str(outcome), 160),
                    extra={"error": True},
                )
            )
        else:
            results.extend(outcome)
    return results


async def scrape_page(url: str, max_chars: int = 6000) -> dict[str, Any]:
    """Fetch a URL and extract readable text content.

    Egress goes through :func:`.net.fetch_text`, so the proxy (``SEARCH_PROXY_URL``
    / ``HTTPS_PROXY`` / ``ALL_PROXY``), the ``SEARCH_SANDBOX_URL`` relay and the
    no-persistence guarantee all apply to ``delve_sources`` scraping too.
    """
    # Wikipedia/arXiv 403 on fake/absent browser UAs — always send the repo UA.
    headers = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    body = await fetch_text(url, headers=headers, timeout=20.0)
    resp_url = url  # sandbox relay may rewrite the request; keep the original

    soup = BeautifulSoup(body, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "form"]):
        tag.decompose()

    title = soup.title.string.strip() if soup.title and soup.title.string else url
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    truncated = len(text) > max_chars
    return {
        "url": resp_url,
        "title": title,
        "content": text[:max_chars],
        "truncated": truncated,
        "content_length": len(text),
    }
