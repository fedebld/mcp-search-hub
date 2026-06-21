# mcp-search-hub

Self-hosted **MCP web-search hub**: one MCP server giving agents web / news / image search
and clean page extraction, with no paid API. DDGS → SearXNG failover behind a shared
circuit-breaking rate-limiter, persistent TTL+LRU result cache, and **trafilatura**
reader-view extraction (boilerplate removal) for `unified_content_extract`.

## Tools
- `unified_web_search(query, max_results, timelimit, region)`
- `unified_news_search(query, max_results, timelimit, region)`
- `unified_image_search(query, max_results, region)`
- `unified_content_extract(url, fmt)` — trafilatura primary, DDGS/SearXNG fallback → `{url, content, extractor}`
- `cache_stats()` — read-only cache observability

## Run
```
cp mcp-web-search/mcp-hub/src/.env.example mcp-web-search/mcp-hub/src/.env
cp mcp-web-search/searxng/settings.yml.example mcp-web-search/searxng/settings.yml
# set a real secret_key in settings.yml:  openssl rand -hex 32
docker compose up -d --build
```
Exposed (Tailscale-only by default): `http://<host>:8765/sse`. See `INSTALL.md` and `SKILL.md`.
