#!/usr/bin/env python3
"""MCP Info-Retrieval Hub — FastMCP orchestrator with dual-backend failover."""
import logging
import sys
import httpx
from mcp.server.fastmcp import FastMCP
from backends import SearXNGBackend, DDSGBackend, TrafilaturaBackend
from rate_limiter import RateLimiter, CircuitOpenError, RateLimitError, BackendHealthError
from cache import TTLCache
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("hub")

ddgs_backend = DDSGBackend()
searxng_backend = SearXNGBackend()
trafilatura_backend = TrafilaturaBackend()

ddgs_limiter = RateLimiter(
    name="DDGS", rate_per_minute=settings.ddgs_rate_limit,
    burst=settings.ddgs_burst, cool_down_seconds=settings.ddgs_cool_down, failure_threshold=3,
)
searxng_limiter = RateLimiter(
    name="SearXNG", rate_per_minute=settings.searxng_rate_limit,
    burst=settings.searxng_burst, cool_down_seconds=settings.searxng_cool_down, failure_threshold=5,
)

# [CACHE] Cache TTL+LRU condivisa dei risultati: abbatte chiamate ai backend e
# pressione sul rate limiter per query ripetute (vedi cache.py).
# [CACHE-PERSIST] db_path -> SQLite su volume: la cache sopravvive ai restart del container.
search_cache = TTLCache(
    max_size=settings.cache_max_size,
    default_ttl=settings.cache_ttl_web,
    db_path=settings.cache_db_path if settings.cache_persistent else None,
)

import os as _os

_HERE = _os.path.dirname(_os.path.abspath(__file__))


def _load_doc(name: str, default: str = "") -> str:
    try:
        with open(_os.path.join(_HERE, name), encoding="utf-8") as _f:
            return _f.read()
    except OSError:
        return default


_INSTRUCTIONS = (
    "Self-hosted web search hub: web/news/image search + reader-view content "
    "extraction, with DDGS -> SearXNG failover and trafilatura. Reach for "
    "`unified_web_search(query)` first; `unified_news_search` for dated news, "
    "`unified_image_search` for images, `unified_content_extract(url)` to turn a "
    "page into clean markdown, `cache_stats` for read-only cache metrics. Results "
    "are cached. NOTE: on total backend failure `unified_web_search` and "
    "`unified_image_search` return a dict {\"error\": ...} instead of a list — "
    "handle both shapes. Full agent guide: read resource skill://web-search-hub."
)

mcp = FastMCP(
    "MCP Info-Retrieval Hub",
    instructions=_INSTRUCTIONS,
    host=settings.host,
    port=settings.port,
)


async def _try_search(backend_name, limiter, search_fn, query, max_results, timelimit, region):
    await limiter.acquire()
    try:
        results = await search_fn(query, max_results, timelimit, region)
        if not results:
            logger.warning("[%s] 0 results for '%s'", backend_name, query[:30])
        await limiter.record_success()
        return results
    except (httpx.HTTPStatusError, ConnectionError, TimeoutError) as exc:
        await limiter.record_failure()
        status = getattr(exc, "response", None)
        status_code = status.status_code if status else None
        raise BackendHealthError(f"{backend_name} HTTP {status_code}: {exc}") from exc
    except RuntimeError as exc:
        await limiter.record_failure()
        raise BackendHealthError(f"{backend_name} error: {exc}") from exc


def _is_cacheable(result) -> bool:
    # [CACHE] Memorizza solo risultati validi: liste non vuote o dict di contenuto.
    # Mai cache di errori ({"error": ...}) o vuoti -> cosi' un retry puo' riprovare.
    if isinstance(result, list):
        return len(result) > 0
    if isinstance(result, dict):
        return "error" not in result
    return False


def _tag_engine(results, engine: str):
    """[B3] Stamp the answering backend on each result for observability/debugging."""
    if isinstance(results, list):
        for r in results:
            if isinstance(r, dict):
                r.setdefault("engine", engine)
    return results


def _classify_extract_error(exc: Exception | None) -> tuple[str, int | None]:
    """[B4] Map an extraction exception to (kind, http_status). Lets the caller distinguish
    404/403/timeout/disconnect/empty instead of one opaque message."""
    if exc is None:
        return ("unknown", None)
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code if exc.response is not None else None
        return (f"http_{code}", code)
    if isinstance(exc, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout)):
        return ("timeout", None)
    if isinstance(exc, httpx.RemoteProtocolError):
        return ("disconnect", None)
    if isinstance(exc, httpx.ConnectError):
        return ("connect_error", None)
    if isinstance(exc, ValueError):  # trafilatura "no main content"
        return ("empty_content", None)
    return (type(exc).__name__, None)


# [B1b] Below this many image hits we treat the answer as weak: do NOT cache it (so a retry
# can hit the failover) and trigger the SearXNG fallback rather than freezing junk for the TTL.
IMAGE_MIN_OK = int(getattr(settings, "image_min_ok", 0) or 0) or 3


@mcp.tool()
async def unified_web_search(query: str, max_results: int = 10, timelimit: str = None, region: str = "us-en") -> list[dict]:
    """Unified web search with DDGS -> SearXNG failover. Args: query (str), max_results (int 1-20, default 10), timelimit (str: d|w|m|y), region (str: us-en default). Returns list of title+url+snippet."""
    # [CACHE] lookup
    cache_key = TTLCache.make_key("unified_web_search",
                                  {"query": query, "max_results": max_results,
                                   "timelimit": timelimit, "region": region})
    if settings.cache_enabled:
        cached = await search_cache.get(cache_key)
        if cached is not None:
            logger.info("[cache] HIT unified_web_search '%s'", query[:30])
            return cached
    chain = [
        ("DDGS", ddgs_limiter, lambda q, m, t, r: ddgs_backend.search_text(q, m, t, r)),
        ("SearXNG", searxng_limiter, lambda q, m, t, r: searxng_backend.search_text(q, m, t, r)),
    ]
    errors = []
    for name, limiter, search_fn in chain:
        try:
            results = await _try_search(name, limiter, search_fn, query, max_results, timelimit, region)
            if results:
                results = _tag_engine(results, name)  # [B3]
                if settings.cache_enabled:
                    await search_cache.set(cache_key, results, ttl=settings.cache_ttl_web)
                return results
            logger.info("[search] %s empty, trying next", name)
        except (CircuitOpenError, RateLimitError, BackendHealthError) as exc:
            errors.append(f"{name}: {exc}")
            continue
    if errors:
        return {"error": f"All backends failed: {'; '.join(errors)}", "query": query}
    return []


@mcp.tool()
async def unified_content_extract(url: str, fmt: str = "text_markdown") -> dict:
    """Extract web page content to clean markdown (reader-view).

    Uses trafilatura as the primary extractor: it strips nav/ads/sidebars/footers and
    returns just the main content (markdown), falling back to the legacy DDGS/SearXNG
    extraction only if trafilatura yields nothing. Args: url (str), fmt (str:
    text_markdown|text_plain). Returns {url, content, extractor}; content truncated to 50k."""
    # [CACHE] lookup
    cache_key = TTLCache.make_key("unified_content_extract", {"url": url, "fmt": fmt})
    if settings.cache_enabled:
        cached = await search_cache.get(cache_key)
        if cached is not None:
            logger.info("[cache] HIT unified_content_extract '%s'", url[:50])
            return cached
    # [FASE 1] Primary: trafilatura reader-view extraction. This is a direct fetch of the
    # target page, so it does NOT consume the DDGS/SearXNG global rate-limiters (it offloads
    # them). Falls through to the legacy DDGS -> SearXNG chain only if it yields nothing.
    primary_err = None
    try:
        result = await trafilatura_backend.extract_content(url, fmt)
        if settings.cache_enabled and _is_cacheable(result):
            await search_cache.set(cache_key, result, ttl=settings.cache_ttl_extract)
        return result
    except Exception as exc0:
        primary_err = exc0
        logger.info("[extract] trafilatura primary failed, falling back: %s", exc0)
    try:
        await ddgs_limiter.acquire()
        result = await ddgs_backend.extract_content(url, fmt)
        await ddgs_limiter.record_success()
        if settings.cache_enabled and _is_cacheable(result):
            await search_cache.set(cache_key, result, ttl=settings.cache_ttl_extract)
        return result
    except Exception as exc:
        logger.warning("[extract] DDGS failed: %s", exc)
        await ddgs_limiter.record_failure()
    try:
        await searxng_limiter.acquire()
        result = await searxng_backend.extract_content(url, fmt)
        await searxng_limiter.record_success()
        if settings.cache_enabled and _is_cacheable(result):
            await search_cache.set(cache_key, result, ttl=settings.cache_ttl_extract)
        return result
    except Exception as exc2:
        logger.error("[extract] All backends failed: %s", exc2)
        await searxng_limiter.record_failure()
        # [B4] Classify the failure: surface HTTP status / error kind from the PRIMARY fetch
        # (trafilatura's direct GET carries the real status) instead of the last backend's
        # generic "Server disconnected".
        kind, status = _classify_extract_error(primary_err or exc2)
        return {
            "url": url,
            "error": f"Extraction failed ({kind}): {primary_err or exc2}",
            "error_kind": kind,
            "http_status": status,
        }


@mcp.tool()
async def unified_image_search(query: str, max_results: int = 10, region: str = "us-en") -> list[dict]:
    """Search for images. Args: query (str), max_results (int 1-20, default 10), region (str). Returns list of title+url+source."""
    # [CACHE] lookup
    cache_key = TTLCache.make_key("unified_image_search",
                                  {"query": query, "max_results": max_results, "region": region})
    if settings.cache_enabled:
        cached = await search_cache.get(cache_key)
        if cached is not None:
            logger.info("[cache] HIT unified_image_search '%s'", query[:30])
            return cached
    # [B1] DDGS images, then SearXNG images on failure OR on a weak (< IMAGE_MIN_OK) answer.
    # DDGS images frequently degrades to a single off-topic hit; the failover recovers it.
    errors = []
    ddgs_results = []
    try:
        await ddgs_limiter.acquire()
        ddgs_results = await ddgs_backend.search_images(query, max_results, region)
        await ddgs_limiter.record_success()
    except Exception as exc:
        await ddgs_limiter.record_failure()
        errors.append(f"DDGS: {exc}")

    if isinstance(ddgs_results, list) and len(ddgs_results) >= IMAGE_MIN_OK:
        results = _tag_engine(ddgs_results, "DDGS")
        if settings.cache_enabled:
            await search_cache.set(cache_key, results, ttl=settings.cache_ttl_image)
        return results

    # Weak or failed → try SearXNG images.
    try:
        await searxng_limiter.acquire()
        sx_results = await searxng_backend.search_images(query, max_results, region)
        await searxng_limiter.record_success()
        if sx_results:
            results = _tag_engine(sx_results, "SearXNG")
            if settings.cache_enabled and len(results) >= IMAGE_MIN_OK:
                await search_cache.set(cache_key, results, ttl=settings.cache_ttl_image)
            return results
    except Exception as exc2:
        await searxng_limiter.record_failure()
        errors.append(f"SearXNG: {exc2}")

    # Nothing strong: return DDGS's weak hits if any (uncached so a retry can recover), else error.
    if ddgs_results:
        return _tag_engine(ddgs_results, "DDGS")
    return {"error": f"Image search failed: {'; '.join(errors) or 'no results'}", "query": query}


@mcp.tool()
async def unified_news_search(query: str, max_results: int = 10, timelimit: str = None, region: str = "us-en") -> list[dict]:
    """Search for news articles. Args: query (str), max_results (int 1-20, default 10), timelimit (str: d|w|m|y), region (str). Returns list of title+url+source+date."""
    # [CACHE] lookup
    cache_key = TTLCache.make_key("unified_news_search",
                                  {"query": query, "max_results": max_results,
                                   "timelimit": timelimit, "region": region})
    if settings.cache_enabled:
        cached = await search_cache.get(cache_key)
        if cached is not None:
            logger.info("[cache] HIT unified_news_search '%s'", query[:30])
            return cached
    try:
        await ddgs_limiter.acquire()
        results = await ddgs_backend.search_news(query, max_results, region, timelimit)
        await ddgs_limiter.record_success()
        results = _tag_engine(results, "DDGS")  # [B3]
        if settings.cache_enabled and _is_cacheable(results):
            await search_cache.set(cache_key, results, ttl=settings.cache_ttl_news)
        return results
    except Exception as exc:
        await ddgs_limiter.record_failure()
        logger.warning("[news] DDGS failed: %s, fallback", exc)
        try:
            # [B2] Fallback to web search but NORMALIZE to the news schema and mark it
            # degraded, so the caller never silently gets a different result shape.
            web = await unified_web_search(f"news {query}", max_results, timelimit, region)
            if isinstance(web, dict):  # error envelope
                return {"error": "News search unavailable", "detail": web.get("error")}
            normalized = []
            for r in web:
                normalized.append({
                    "title": r.get("title", ""),
                    "url": r.get("href") or r.get("url", ""),
                    "body": r.get("body", ""),
                    "source": r.get("engine", "web"),
                    "date": "",                 # web search has no reliable date
                    "engine": r.get("engine", "web"),
                    "degraded": True,           # signals the news→web fallback path
                })
            return normalized
        except Exception:
            return {"error": "News search unavailable"}


@mcp.tool()
async def cache_stats() -> dict:
    """Cache observability (read-only). Returns hits, misses, size, max_size, hit_ratio, enabled, persistent."""
    return {**search_cache.stats, "enabled": settings.cache_enabled, "persistent": search_cache.persistent}



@mcp.resource(
    "skill://web-search-hub",
    name="SKILL.md",
    description="Agent-facing usage guide for this server.",
    mime_type="text/markdown",
)
def skill_doc() -> str:
    """Agent-facing skill card (how to use this server well)."""
    return _load_doc("SKILL.md", _INSTRUCTIONS)


@mcp.resource(
    "readme://web-search-hub",
    name="README.md",
    description="Human-facing README for this server.",
    mime_type="text/markdown",
)
def readme_doc() -> str:
    """Human-facing README."""
    return _load_doc("README.md", "")


def main():
    logger.info("Starting MCP Hub on %s:%d", settings.host, settings.port)
    logger.info("DDGS: %d/min burst=%d cool=%ds", settings.ddgs_rate_limit, settings.ddgs_burst, settings.ddgs_cool_down)
    logger.info("SearXNG: %d/min burst=%d cool=%ds", settings.searxng_rate_limit, settings.searxng_burst, settings.searxng_cool_down)
    logger.info("SearXNG URL: %s", settings.searxng_url)
    logger.info("Cache: enabled=%s persistent=%s db=%s max=%d ttl web/news/img/extract=%d/%d/%d/%d",
                settings.cache_enabled, settings.cache_persistent, settings.cache_db_path, settings.cache_max_size,
                settings.cache_ttl_web, settings.cache_ttl_news,
                settings.cache_ttl_image, settings.cache_ttl_extract)
    mcp.run(transport="sse")


if __name__ == "__main__":
    main()
