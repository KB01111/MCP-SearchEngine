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
"""

from __future__ import annotations

import asyncio
import html
import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict, field
from typing import Any

import httpx
from bs4 import BeautifulSoup

DEFAULT_SOURCES = ["web", "duckduckgo", "wikipedia", "arxiv", "hackernews"]

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


_SEARCHERS = {
    "web": _search_duckduckgo_api,
    "duckduckgo": _search_duckduckgo_html,
    "wikipedia": _search_wikipedia,
    "arxiv": _search_arxiv,
    "hackernews": _search_hackernews,
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

    timeout = httpx.Timeout(15.0, connect=8.0)
    headers = {"User-Agent": _UA, "Accept": "*/*"}
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
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
    """Fetch a URL and extract readable text content."""
    timeout = httpx.Timeout(20.0, connect=8.0)
    async with httpx.AsyncClient(
        timeout=timeout,
        headers={"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"},
        follow_redirects=True,
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "form"]):
        tag.decompose()

    title = soup.title.string.strip() if soup.title and soup.title.string else url
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    truncated = len(text) > max_chars
    return {
        "url": str(resp.url),
        "title": title,
        "content": text[:max_chars],
        "truncated": truncated,
        "content_length": len(text),
    }
