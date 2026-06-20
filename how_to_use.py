#!/usr/bin/env python3
"""
how_to_use.py — Web Search Hub MCP: dump schema live + self-test dei tool.

Si connette al server MCP via SSE, stampa lo schema REALE di ogni tool e lancia un
self-test su ciascun endpoint con un esempio d'uso. Exit 0 se tutti rispondono, 1 altrimenti.

ATTENZIONE RATE LIMIT: le ricerche fanno chiamate reali che consumano il budget GLOBALE
condiviso del hub (DDGS 6/min, circuit breaker cooldown 300s). NON eseguirlo in loop.
Le chiamate sono distanziate di HUB_TEST_DELAY secondi per restare entro il rate; le
chiamate ripetute possono risultare HIT dalla cache (vedi cache_stats).

Uso:
    /home/llmadmin/venv/bin/python3 how_to_use.py [URL]
    URL default: http://100.94.187.21:8765/sse   (override anche via env MCP_HUB_URL)
    Spaziatura chiamate: env HUB_TEST_DELAY (default 8s)
"""
import asyncio
import json
import os
import sys

from mcp.client.sse import sse_client
from mcp.client.session import ClientSession

URL = (sys.argv[1] if len(sys.argv) > 1
       else os.environ.get("MCP_HUB_URL", "http://100.94.187.21:8765/sse"))
DELAY = float(os.environ.get("HUB_TEST_DELAY", "8"))

# (tool, argomenti, nota d'uso, rate_limited)
EXAMPLES = [
    ("unified_web_search",
     {"query": "Anthropic Claude", "max_results": 3},
     "ricerca web — DDGS->SearXNG failover; timelimit d|w|m|y, region es. us-en/it-it", True),
    ("unified_news_search",
     {"query": "intelligenza artificiale", "max_results": 3, "timelimit": "w", "region": "it-it"},
     "ricerca news — DDGS news, fallback web 'news:'", True),
    ("unified_image_search",
     {"query": "golden retriever", "max_results": 3},
     "ricerca immagini — SOLO DDGS, nessun failover", True),
    ("unified_content_extract",
     {"url": "https://example.com", "fmt": "text_markdown"},
     "estrazione pagina in markdown (fmt text_markdown|text_plain), troncata a 50k char", True),
    ("cache_stats",
     {},
     "osservabilita' cache (read-only): hits/misses/size/hit_ratio/persistent — no backend", False),
]


def _text(res):
    out = []
    for c in getattr(res, "content", []):
        t = getattr(c, "text", None)
        if t:
            out.append(t)
    return "\n".join(out)


async def main():
    print("== Web Search Hub MCP — how_to_use ==")
    print(f"Endpoint: {URL}   (spaziatura chiamate: {DELAY}s)\n")
    failures = 0
    async with sse_client(URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1) SCHEMI REALI
            tools = await session.list_tools()
            print("=" * 64)
            print(f"TOOL DISPONIBILI: {len(tools.tools)}")
            print("=" * 64)
            for t in tools.tools:
                print(f"\n### {t.name}")
                if t.description:
                    print(f"  desc: {t.description}")
                schema = getattr(t, "inputSchema", None)
                if schema:
                    print("  inputSchema:")
                    for line in json.dumps(schema, indent=2, ensure_ascii=False).splitlines():
                        print("    " + line)

            # 2) SELF-TEST
            print("\n" + "=" * 64)
            print("SELF-TEST (le ricerche consumano il rate budget GLOBALE condiviso)")
            print("=" * 64)
            first_rate_call = True
            for name, args, note, rate_limited in EXAMPLES:
                if rate_limited and not first_rate_call:
                    await asyncio.sleep(DELAY)
                if rate_limited:
                    first_rate_call = False
                print(f"\n--- {name} {json.dumps(args, ensure_ascii=False)}")
                print(f"    ({note})")
                try:
                    res = await session.call_tool(name, args)
                    txt = _text(res)
                    is_err = bool(getattr(res, "isError", False))
                    looks_err = '"error"' in txt[:200]
                    head = txt[:300].replace("\n", " ")
                    if is_err or looks_err or not txt:
                        failures += 1
                        print(f"    [FAIL] isError={is_err} len={len(txt)}")
                        print(f"           {head}")
                    else:
                        print(f"    [OK]   len={len(txt)}")
                        print(f"           {head}")
                except Exception as exc:
                    failures += 1
                    print(f"    [FAIL] eccezione: {exc!r}")

    print("\n" + "=" * 64)
    print("RISULTATO: tutti gli endpoint OK" if not failures
          else f"RISULTATO: {failures} endpoint FALLITI")
    print("=" * 64)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
