"""End-to-end smoke test driving the server through FastMCP's in-process Client."""

import asyncio
import json

from fastmcp import Client

from search_engine.server import mcp, PICKER_URI


async def main() -> None:
    async with Client(mcp) as client:
        print("=== TOOLS ===")
        tools = await client.list_tools()
        for t in tools:
            app_meta = (t.meta or {}).get("ui", {}) if hasattr(t, "meta") else {}
            print(f"  {t.name:16} ui={app_meta or '-'}")
        assert {t.name for t in tools} == {"search", "delve_sources"}

        print("\n=== RESOURCES ===")
        resources = await client.list_resources()
        for r in resources:
            print(f"  {r.uri}  mime={r.mime_type}")
        assert any(str(r.uri) == PICKER_URI for r in resources), "picker UI not registered"

        print("\n=== READ UI RESOURCE ===")
        contents = await client.read_resource(PICKER_URI)
        html = contents[0].text
        print(f"  mime={contents[0].mime_type}  bytes={len(html)}")
        assert "ext-apps" in html and "callServerTool" in html

        print("\n=== CALL search ===")
        res = await client.call_tool(
            "search", {"query": "model context protocol", "max_per_source": 2}
        )
        payload = res.structured_content
        print(f"  mode={payload['mode']}  count={payload['count']}")
        for r in payload["results"]:
            flag = "ERR " if r["extra"].get("error") else "    "
            print(f"  {flag}[{r['source']:11}] {r['title'][:52]:52} {r['url'][:44]}")
        assert payload["mode"] == "pick"
        real = [r for r in payload["results"] if not r["extra"].get("error") and r["url"]]
        assert real, "no usable results"

        print("\n=== CALL delve_sources (pick first 2 real results) ===")
        picks = [
            {"id": r["id"], "url": r["url"], "title": r["title"], "source": r["source"]}
            for r in real[:2]
        ]
        dres = await client.call_tool(
            "delve_sources", {"picks": picks, "max_chars": 1200}
        )
        data = dres.structured_content
        print(f"  mode={data['mode']}  pages={data['count']}")
        for p in data["pages"]:
            status = f"ERROR: {p['error']}" if p.get("error") else f"{p.get('content_length', 0)} chars scraped (returned {len(p.get('content',''))})"
            print(f"  [{p['source']:11}] {p['title'][:48]:48} {status}")

        ok = [p for p in data["pages"] if not p.get("error")]
        assert ok, "all delves failed"
        print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
