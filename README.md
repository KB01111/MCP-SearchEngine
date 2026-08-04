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
| `reddit`      | Reddit public JSON search                 | no API key, own UA           |
| `x`           | Nitter HTML instances                     | keyword search; nitter mirror configurable via `X_NITTER_INSTANCE` |
| `perplexity`  | Perplexity Chat API                       | opt-in, needs `PERPLEXITY_API_KEY`; not in the defaults |

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

## Privacy & proxying

All network egress (search + `delve_sources` scraping) goes through a single
privacy layer (`search_engine/net.py`):

- **No local persistence, ever.** There is no browser: no cache, history or
  cookies on disk. Every request gets a fresh ephemeral client with its own
  empty cookie jar that is discarded afterwards.
- **Proxy via env** — `SEARCH_PROXY_URL` (falling back to `HTTPS_PROXY`, then
  `ALL_PROXY`). Supports `http://`, `socks5://` and `socks5h://` (the `h` means
  DNS is resolved at the proxy, avoiding local DNS leaks — handy for Tor, e.g.
  `SEARCH_PROXY_URL=socks5h://127.0.0.1:9050`). Requires `httpx[socks]`.
- **External sandbox relay** — set `SEARCH_SANDBOX_URL` to a disposable sandbox
  (e.g. a self-hosted agentOS/Rivet VM relay, https://agentos-sdk.dev/docs/apps/)
  and every fetch is rewritten to `{SEARCH_SANDBOX_URL}<url-encoded target>` so
  the local machine never touches the target site. agentOS is TS/Rivet, not
  Python-importable — this env-relay is the integration seam.
- **VPN note** — a VPN is OS-level: when the OS is on one, all traffic already
  routes through it; the proxy and sandbox options are orthogonal to that.

See `.env.example` for all options. No secrets live in code and URLs that might
carry credentials are never logged.

## Deploy to Prefect Horizon (hosted, off your PC)

[Prefect Horizon](https://horizon.prefect.io) is the managed MCP hosting
platform from the FastMCP team. Deploying there runs the engine on **their**
infra — so all searching/scraping egresses from Prefect's IPs, not your PC —
and serves it at an OAuth-protected URL like
`https://<name>.fastmcp.app/mcp`. Free for personal projects.

1. **Verify the entrypoint locally** (this is exactly what Horizon sees):
   ```bash
   .\.venv\Scripts\fastmcp inspect search_engine/server.py:mcp
   ```
   You should see 2 tools (`search`, `delve_sources`) and 1 resource
   (`picker_view`). `server.py` self-bootstraps the repo root onto `sys.path`
   so the standalone import Horizon performs resolves the package.
2. **Dependencies** — `requirements.txt` pins `fastmcp==4.0.0b1` +
   `fastmcp-slim==4.0.0b1` explicitly (pip doesn't read pyproject's `[tool.uv]`
   constraint), plus `httpx[socks]` and `beautifulsoup4`. Horizon auto-detects
   and installs it.
3. **Push to GitHub** and deploy at horizon.prefect.io: sign in with GitHub,
   select the repo, set entrypoint `search_engine/server.py:mcp`, pick a server
   name (sets the URL), enable **Authentication** (built-in OAuth), and add
   secrets (`PERPLEXITY_API_KEY`, optionally `X_NITTER_INSTANCE` /
   `SEARCH_PROXY_URL` / `SEARCH_SANDBOX_URL`).
4. **Connect** — Horizon redeploys on push and generates connection snippets
   for Claude/Cursor/VS Code. Verify with its Inspector / ChatMCP, and confirm
   egress via `delve_sources` on an IP-echo URL.

Note: Horizon is serverless (cold starts, stateless per call, ~170s timeout) —
fine for this stateless engine. Its egress is Prefect's shared IP; it cannot
run a host-level VPN (use a VPS instead if you ever need a dedicated VPN exit).

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
