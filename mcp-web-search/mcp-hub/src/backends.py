"""Backend wrappers for SearXNG, DDGS and trafilatura."""
import asyncio
import logging
import httpx
import trafilatura
from ddgs import DDGS
from markdownify import markdownify as md

from config import settings

logger = logging.getLogger("backends")

# Browser-like UA shared by the direct page-fetch extractors (trafilatura + SearXNG path).
_EXTRACT_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_EXTRACT_MAX_CHARS = 50000


class TrafilaturaBackend:
    """Primary HTML->markdown extractor with reader-view boilerplate removal.

    Fetches the target page directly (its own GET — it does NOT consume the DDGS/SearXNG
    rate-limiters, so it offloads the search backends) and runs trafilatura to strip
    nav/ads/footers while preserving the main content. Raises on empty extraction so the
    caller can fall back to the legacy markdownify/DDGS path.
    """

    async def extract_content(self, url: str, fmt: str = "text_markdown") -> dict:
        async with httpx.AsyncClient(
            timeout=15, follow_redirects=True, headers={"User-Agent": _EXTRACT_UA},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text

        output_format = "markdown" if fmt == "text_markdown" else "txt"

        def _extract():
            return trafilatura.extract(
                html,
                url=url,
                output_format=output_format,
                favor_precision=True,      # prefer clean main content over recall (less boilerplate)
                include_comments=False,    # drop comment sections
                include_tables=True,       # keep data tables (often the substance)
                include_formatting=True,   # keep bold/headings structure in markdown
                deduplicate=True,          # drop repeated boilerplate blocks
            )

        content = await asyncio.to_thread(_extract)
        if not content or not content.strip():
            raise ValueError("trafilatura produced no main content")

        logger.info("[trafilatura] extracted %d chars from '%s'", len(content), url[:50])
        return {"url": url, "content": content[:_EXTRACT_MAX_CHARS], "extractor": "trafilatura"}


class SearXNGBackend:
    def __init__(self):
        self.base_url = settings.searxng_url
        self.timeout = settings.searxng_timeout
        self.host_header = settings.searxng_host_header

    async def search_text(
        self, query: str, max_results: int = 10,
        timelimit: str | None = None, region: str = "us-en"
    ) -> list[dict]:
        time_map = {"d": "day", "w": "week", "m": "month", "y": "year"}
        time_range = time_map.get(timelimit) if timelimit else None

        params = {"q": query, "format": "json", "pageno": 1}
        if time_range:
            params["time_range"] = time_range

        async with httpx.AsyncClient(
            timeout=self.timeout,
            headers={"Host": self.host_header},
        ) as client:
            resp = await client.get(self.base_url + "/search", params=params)
            resp.raise_for_status()

        data = resp.json()
        results = []
        for r in data.get("results", [])[:max_results]:
            results.append({
                "title": r.get("title", ""),
                "href": r.get("url", ""),
                "body": r.get("content", "")[:500],
            })

        logger.info("[SearXNG] %d results for '%s'", len(results), query[:40])
        return results

    async def extract_content(self, url: str, fmt: str = "text_markdown") -> dict:
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=headers) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text

        if fmt == "text_markdown":
            content = md(html, heading_style="ATX")[:50000]
        elif fmt == "text_plain":
            content = md(html, strip=["*"])[:50000]
        else:
            content = html[:50000]

        return {"url": url, "content": content}


class DDSGBackend:
    def __init__(self):
        self.proxy = settings.ddgs_proxy
        self.timeout = settings.ddgs_timeout

    async def search_text(
        self, query: str, max_results: int = 10,
        timelimit: str | None = None, region: str = "us-en",
        backend: str = "auto"
    ) -> list[dict]:
        def _search():
            ddgs = DDGS(proxy=self.proxy, timeout=self.timeout)
            return ddgs.text(query=query, region=region, safesearch="moderate",
                            timelimit=timelimit, max_results=max_results, backend=backend)

        results = await asyncio.to_thread(_search)
        logger.info("[DDGS] %d results for '%s'", len(results), query[:40])
        return results

    async def search_images(
        self, query: str, max_results: int = 10, region: str = "us-en"
    ) -> list[dict]:
        def _search():
            ddgs = DDGS(proxy=self.proxy, timeout=self.timeout)
            return ddgs.images(query=query, region=region, safesearch="moderate", max_results=max_results)

        results = await asyncio.to_thread(_search)
        logger.info("[DDGS] %d image results", len(results))
        return results

    async def search_news(
        self, query: str, max_results: int = 10,
        region: str = "us-en", timelimit: str | None = None
    ) -> list[dict]:
        def _search():
            ddgs = DDGS(proxy=self.proxy, timeout=self.timeout)
            return ddgs.news(query=query, region=region, safesearch="moderate",
                            timelimit=timelimit, max_results=max_results)

        results = await asyncio.to_thread(_search)
        logger.info("[DDGS] %d news results", len(results))
        return results

    async def extract_content(self, url: str, fmt: str = "text_markdown") -> dict:
        def _extract():
            ddgs = DDGS(proxy=self.proxy, timeout=self.timeout)
            return ddgs.extract(url, fmt=fmt)

        result = await asyncio.to_thread(_extract)
        return result
