# MCP SearchEngine

An [MCP](https://modelcontextprotocol.io) server that searches & scrapes
multiple sources, then renders an **interactive picker UI** (a FastMCP App) so
you can click which sources you want to delve into. Built on the FastMCP 4
prerelease and the MCP Apps extension.

## How it works

```
LLM ──search(query)──► server ──► queries web, DuckDuckGo, Wikipedia,
│                                  arXiv & Hacker News concurrently
│                                  and returns normalized results
│
│                        ┌──── ui://search-engine/picker.html ────┐
│                        │ interactive card grid, source filters, │
│◄────── rendered app ───┤ multi-select, "Delve into selected"   │
│                        └──────────────┬─────────────────────────┘
│                                       │ callServerTool('delve_sources')
LLM ◄── delve_sources(picks) ◄──────────┘
        scrapes each picked URL and
        returns full readable content
```

The flow is symmetrical: the LLM gets the same JSON the UI renders, so it can
also pick sources programmatically and call `delve_sources` directly — the UI
is additive, not a gate.

## Sources

| Source        | Backend                                   | Notes                        |
| ------------- | ----------------------------------------- | ---------------------------- |
| `web`         | DuckDuckGo instant-answer API             | abstract + related topics    |
| `duckduckgo`  | DuckDuckGo HTML endpoint                  | general web results, no key  |
| `wikipedia`   | Wikipedia OpenSearch / summary API        |                              |
| `arxiv`       | arXiv Atom API                            | papers, authors, links       |
| `hackernews`  | Algolia HN search API                     | points + comment counts      |

A failing source never sinks the search — it comes back as an error card in the UI.

## Tools

- **`search(query, sources?, max_per_source?)`** — search all sources and render the
  picker UI. Returns JSON `{ mode: "pick", results: [...] }` plus a text summary.
- **`delve_sources(picks, max_chars?)`** — scrape the picked URLs concurrently and
  return full extracted text per page. `picks` items need at least a `url`.

## The interactive UI

`search_engine/ui/picker.html` is served as a `ui://` resource
(`text/html;profile=mcp-app`) and rendered in a sandboxed iframe by hosts that
support MCP Apps (Claude, ChatGPT, VS Code, Goose, …). It uses the
`@modelcontextprotocol/ext-apps` SDK:

- `app.ontoolresult` — receives the search payload pushed by the host
- `app.callServerTool({ name: "delve_sources", … })` — delves into picked sources

Click cards to multi-select, double-click to delve into a single source, filter
by source chip, then press **Delve into selected**. The scraped pages are shown
in the UI and returned to the conversation.

## Install & run

```bash
python -m venv .venv
.\.venv\Scripts\python -m pip install -e .
```

The `pyproject.toml` pins the FastMCP 4 prerelease (`fastmcp==4.0.0b1` +
`fastmcp-slim==4.0.0b1` constraint, per the v4 upgrade guide).

Run over stdio (what most MCP clients use):

```bash
.\.venv\Scripts\python -m search_engine
```

or test it in-process:

```bash
.\.venv\Scripts\python test_client.py
```

There is also a Playwright-driven UI harness (`test_ui_harness.py`) that renders
the real picker HTML in Chromium with the SDK stubbed, clicks through select →
filter → delve, and screenshots each stage. Run it with
`.\.venv\Scripts\python test_ui_harness.py` (requires `playwright install chromium`).

## Example client configuration

```json
{
  "mcpServers": {
    "search-engine": {
      "command": "C:\\Users\\kevin\\Projekt\\MCP-SearchEngine\\.venv\\Scripts\\python.exe",
      "args": ["-m", "search_engine"]
    }
  }
}
```

## Layout

```
search_engine/
├── __init__.py
├── __main__.py          # python -m search_engine
├── server.py            # FastMCP server: tools + ui:// resource
├── sources.py           # multi-source search + scraping engine
└── ui/
    └── picker.html      # interactive MCP App source-picker UI
```
