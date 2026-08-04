"""Render the picker UI in a real browser, stub the MCP Apps SDK, and drive a
full pick-and-delve interaction. Captures screenshots + the delve call args."""

import asyncio
import json
import pathlib

from playwright.sync_api import sync_playwright

from search_engine.server import mcp
from fastmcp import Client

OUT = pathlib.Path("ui-test-output")
OUT.mkdir(exist_ok=True)
HTML = (pathlib.Path("search_engine/ui/picker.html")).read_text(encoding="utf-8")


def get_payload():
    async def _run():
        async with Client(mcp) as c:
            r = await c.call_tool("search", {"query": "model context protocol", "max_per_source": 2})
            return r.structured_content
    return asyncio.run(_run())


STUB = """
// Stub the ESM import the page does.
window.__delveCalls = [];
const mod = document.createElement('script');
mod.type = 'module';
mod.textContent = `
  class App {
    constructor(opts){ window.__app = this; this._opts = opts; }
    async connect(){
      setTimeout(() => {
        if (this.ontoolresult) this.ontoolresult(window.__INJECTED_RESULT);
      }, 30);
    }
    async callServerTool({name, arguments: args}){
      window.__delveCalls.push({name, args});
      return { content: [{ type:'text', text: JSON.stringify(window.__DELVE_RESULT) }] };
    }
  }
  // Shim the module specifier by pre-defining the global the page expects.
`;
document.head.appendChild(mod);
"""


def main():
    payload = get_payload()
    delve_result = {
        "mode": "delve",
        "count": 2,
        "pages": [
            {
                "id": "wiki-0",
                "title": "Model Context Protocol - Wikipedia",
                "source": "wikipedia",
                "url": "https://en.wikipedia.org/wiki/Model_Context_Protocol",
                "content": "The Model Context Protocol (MCP) is an open standard, open-source framework introduced by Anthropic to standardize the way artificial intelligence systems like large language models connect to external data and tools.",
                "truncated": True,
                "content_length": 15832,
            }
        ],
    }

    # Load the real page but replace the remote SDK import with an inline stub.
    injected_html = HTML.replace(
        'import { App } from "https://unpkg.com/@modelcontextprotocol/ext-apps@0.4.0/app-with-deps";',
        "class App {\n"
        "  constructor(o){ window.__app=this; }\n"
        "  async connect(){ setTimeout(()=>{ this.ontoolresult && this.ontoolresult(window.__INJECTED_RESULT); "
        "this.connectCount=(this.connectCount||0)+1; window.__connected=(window.__connected||0)+1; }, 30); }\n"
        "  async callServerTool({name, arguments: args}){ window.__delveCalls.push({name, args}); "
        "    return { content:[{type:'text', text: JSON.stringify(window.__DELVE_RESULT)}], structuredContent: window.__DELVE_RESULT }; }\n"
        "}\n"
        "window.__delveCalls = [];"
    )

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 920, "height": 720})
        page.on("console", lambda m: print("BROWSER:", m.type, m.text))
        page.on("pageerror", lambda e: print("PAGEERROR:", e))
        inj = {'content': [{'type': 'text', 'text': json.dumps(payload)}], 'structuredContent': payload}
        inj_block = (
            f"<script>window.__INJECTED_RESULT = {json.dumps(inj)};"
            f"window.__DELVE_RESULT = {json.dumps(delve_result)};</script>"
        )
        inj_html = injected_html.replace("<head>", "<head>\n" + inj_block, 1)
        tmp = OUT / "picker_injected.html"
        tmp.write_text(inj_html, encoding="utf-8")
        page.goto(tmp.resolve().as_uri(), wait_until="load")
        page.wait_for_timeout(900)
        print("root html (first 600):", page.evaluate("document.getElementById('app').innerHTML.slice(0,600)"))

        # State 1: results rendered
        cards = page.locator(".card")
        print("cards rendered:", cards.count())
        assert cards.count() > 0
        page.screenshot(path=str(OUT / "1_results.png"), full_page=True)

        # Click the wikipedia card and the arxiv card
        wiki_card = page.locator(".card:has(.badge:has-text('wikipedia'))").first
        wiki_title = wiki_card.locator(".title").inner_text()
        arxiv_card = page.locator(".card:has(.badge:has-text('arxiv'))").first
        arxiv_title = arxiv_card.locator(".title").inner_text()
        wiki_card.click()
        arxiv_card.click()
        page.wait_for_timeout(150)
        print("selected:", wiki_title, "|", arxiv_title)
        page.screenshot(path=str(OUT / "2_selected.png"))

        # Filter by source chip
        page.locator(".chip:has-text('hackernews')").click()
        page.wait_for_timeout(150)
        print("after HN filter, visible cards:", page.locator(".card").count())
        page.locator(".chip:has-text('All sources')").click()
        page.wait_for_timeout(150)

        # Delve
        page.locator("#delve").click()
        page.wait_for_timeout(350)
        calls = page.evaluate("window.__delveCalls")
        print("delve calls:", json.dumps(calls, indent=2))
        assert calls and calls[0]["name"] == "delve_sources"
        assert len(calls[0]["args"]["picks"]) == 2
        page.screenshot(path=str(OUT / "3_delved.png"), full_page=True)

        browser.close()
    print("\nUI HARNESS PASSED — screenshots in", OUT)


if __name__ == "__main__":
    main()
