"""MCP SearchEngine server.

Search multiple sources, render an interactive picker UI (FastMCP App) so the
user can choose which sources to delve into, then scrape & return full content.

Flow
----
1. ``search_all`` — LLM calls this with a query. Results are returned as JSON
   AND rendered in the linked ``ui://search-engine/picker.html`` app.
2. The user picks sources in the UI (or the LLM picks from the JSON).
3. ``delve_sources`` — scrapes the picked URLs and returns full page content.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Annotated

from fastmcp import FastMCP, Context
from fastmcp.apps import AppConfig, ResourceCSP, UI_EXTENSION_ID
from fastmcp.tools import ToolResult
from mcp.types import TextContent
from pydantic import Field

from .sources import DEFAULT_SOURCES, search_all, scrape_page

mcp = FastMCP("MCP SearchEngine")

PICKER_URI = "ui://search-engine/picker.html"
_PICKER_HTML = (Path(__file__).parent / "ui" / "picker.html").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# UI resource                                                                  #
# --------------------------------------------------------------------------- #
@mcp.resource(
    PICKER_URI,
    app=AppConfig(
        csp=ResourceCSP(resource_domains=["https://unpkg.com"]),
        prefers_border=True,
    ),
)
def picker_view() -> str:
    """Interactive source-picker UI rendered in a sandboxed iframe."""
    return _PICKER_HTML


# --------------------------------------------------------------------------- #
# Tools                                                                        #
# --------------------------------------------------------------------------- #
@mcp.tool(
    app=AppConfig(resource_uri=PICKER_URI),
    annotations={"readOnlyHint": True, "openWorldHint": True},
)
async def search(
    query: Annotated[str, Field(description="The search query")],
    sources: Annotated[
        list[str] | None,
        Field(
            description=(
                f"Subset of sources to query. Any of {DEFAULT_SOURCES}. "
                "Defaults to all."
            )
        ),
    ] = None,
    max_per_source: Annotated[
        int, Field(description="Max results per source", ge=1, le=10)
    ] = 4,
    ctx: Context | None = None,
) -> ToolResult:
    """Search multiple sources (web, DuckDuckGo, Wikipedia, arXiv, Hacker News)
    and present the results in an interactive picker UI.

    The user can then pick which sources to delve into (full scrape) via the UI,
    or the model can call ``delve_sources`` directly with chosen URLs.
    """
    results = await search_all(query, sources, max_per_source)
    payload = {
        "mode": "pick",
        "query": query,
        "count": len(results),
        "results": [r.to_dict() for r in results],
    }

    supports_ui = ctx.client_supports_extension(UI_EXTENSION_ID) if ctx else False
    summary_lines = [f"Found {len(results)} results for “{query}”:"]
    for r in results[:12]:
        if r.extra.get("error"):
            summary_lines.append(f"  [{r.source}] ERROR: {r.snippet}")
        else:
            summary_lines.append(f"  [{r.source}] {r.title} — {r.url}")
    summary_lines.append(
        "\nAn interactive source-picker UI is displayed. Ask the user which "
        "sources to delve into, then call `delve_sources` with their picks."
        if supports_ui
        else "\nRespond with these options and call `delve_sources` for the ones the user picks."
    )

    return ToolResult(
        content=[
            TextContent(type="text", text=json.dumps(payload)),
            TextContent(type="text", text="\n".join(summary_lines)),
        ],
        structured_content=payload,
    )


@mcp.tool(
    annotations={"readOnlyHint": True, "openWorldHint": True},
)
async def delve_sources(
    picks: Annotated[
        list[dict[str, Any]],
        Field(
            description=(
                "Sources to delve into. Each item needs at least a `url`; "
                "`id`, `title`, and `source` are carried through for display."
            )
        ),
    ],
    max_chars: Annotated[
        int, Field(description="Max characters of page content to return per source", ge=500, le=20000)
    ] = 6000,
) -> ToolResult:
    """Scrape the picked sources and return their full readable content."""
    import asyncio

    async def _one(pick: dict[str, Any]) -> dict[str, Any]:
        url = pick.get("url", "")
        base = {
            "id": pick.get("id", ""),
            "title": pick.get("title", url),
            "source": pick.get("source", "web"),
            "url": url,
        }
        if not url:
            return {**base, "error": "no url", "content": ""}
        try:
            page = await scrape_page(url, max_chars=max_chars)
            return {**base, **page}
        except Exception as e:  # surface per-source failures, don't sink the batch
            return {**base, "error": f"{type(e).__name__}: {e}", "content": ""}

    pages = await asyncio.gather(*[_one(p) for p in picks])

    payload = {"mode": "delve", "count": len(pages), "pages": list(pages)}
    text_parts = []
    for p in pages:
        if p.get("error"):
            text_parts.append(f"## {p['title']}\n[{p['source']}] {p['url']}\nERROR: {p['error']}")
        else:
            text_parts.append(
                f"## {p['title']}\n[{p['source']}] {p['url']}"
                f"{'  (truncated)' if p.get('truncated') else ''}\n\n{p['content']}"
            )

    return ToolResult(
        content=[TextContent(type="text", text="\n\n---\n\n".join(text_parts))],
        structured_content=payload,
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
