# AGENTS.md

MCP server (FastMCP 4 beta) that searches multiple sources and renders an interactive picker UI via the MCP Apps extension. Small, flat repo — no CI, no lint/typecheck config, no lockfile, no pytest. Python 3.11+ (dev venv is 3.14), Windows.

## Commands (PowerShell, from repo root)

- Install: `.\.venv\Scripts\python -m pip install -e .` (playwright must be installed separately — it is NOT in `pyproject.toml`: `pip install playwright && playwright install chromium`)
- Run server (stdio): `.\.venv\Scripts\python -m search_engine`
- Smoke test: `.\.venv\Scripts\python test_client.py` — in-process FastMCP `Client`; hits live external APIs (DuckDuckGo, Wikipedia, arXiv, HN), so failures may be network, not code. Prints `ALL CHECKS PASSED` on success.
- UI test: `.\.venv\Scripts\python test_ui_harness.py` — Playwright/Chromium harness; writes screenshots to `ui-test-output/` (gitignored). Prints `UI HARNESS PASSED` on success.
- Horizon entrypoint check: `.\.venv\Scripts\fastmcp inspect search_engine/server.py:mcp` — shows exactly what Prefect Horizon sees (2 tools + 1 resource). Must pass before deploying.

There is no test framework — both root-level `test_*.py` files are standalone scripts with inline asserts, run directly as above.

## Dependency pinning gotcha

`pyproject.toml` pins the **FastMCP 4 prerelease**: `fastmcp==4.0.0b1` plus `[tool.uv] constraint-dependencies = ["fastmcp-slim==4.0.0b1"]`. Do not upgrade fastmcp casually — the code uses v4-only APIs (`fastmcp.apps.AppConfig`, `ResourceCSP`, `UI_EXTENSION_ID`, `Context.client_supports_extension`) that don't exist in stable 2.x. When in doubt about fastmcp APIs, fetch current docs rather than guessing from training data.

## Architecture (all under `search_engine/`)

- `server.py` — FastMCP app: `search` + `delve_sources` tools, and the `ui://search-engine/picker.html` resource. `picker.html` is read from disk **once at import time**, so restart the server to pick up HTML edits. Self-bootstraps the repo root onto `sys.path` and uses a relative→absolute import fallback so Horizon/`fastmcp inspect` can load it standalone.
- `net.py` — privacy/network layer. `ephemeral_client()` (fresh client + empty cookie jar per request, proxy applied) and `fetch_text()` are the ONLY egress paths. Proxy from `SEARCH_PROXY_URL`→`HTTPS_PROXY`→`ALL_PROXY` (supports `socks5h://`); `SEARCH_SANDBOX_URL` rewrites fetches to a disposable-sandbox relay.
- `sources.py` — 8 source backends queried concurrently (`_SEARCHERS` dict maps names to async fetchers; `DEFAULT_SOURCES` lists the default 7 — `perplexity` is opt-in, needs `PERPLEXITY_API_KEY`). Contract: a failing fetcher **raises**, and `search_all` converts that into a `SearchResult` with `extra={"error": True}` — so the UI shows an error card instead of sinking the batch. Keep it when adding sources. Both `search_all` and `scrape_page` route through `net.py` (proxy/sandbox apply to search AND delve).
- `ui/picker.html` — self-contained MCP App (imports `@modelcontextprotocol/ext-apps` from unpkg; the server's `ResourceCSP` whitelists only `https://unpkg.com` — adding other CDN domains requires updating the CSP in `server.py`). Communicates via `app.ontoolresult` and `app.callServerTool("delve_sources")`.

## Conventions worth preserving

- `SearchResult` is the normalized shape for every source (`id`, `source`, `title`, `url`, `snippet`, `extra`); both the JSON payload and the UI consume `to_dict()`.
- The `search` tool returns the payload three ways: JSON text content, human summary text, and `structured_content` — the UI's `parseToolResult` handles host variants of this. Keep all three in sync if you change the payload.
- `sources.py` sets a descriptive `_UA` on purpose: Wikipedia/arXiv 403 fake browser UAs. Don't "fix" it to look like Chrome.
- README's "How it works" diagram documents the intended contract: the LLM gets the same JSON the UI renders, so `delve_sources` must stay callable without the UI.
