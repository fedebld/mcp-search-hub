"""Regression tests for the mcp-search-hub fixes (run after `pip install -r requirements.txt`).

Covered:
  • [B1]  SearXNG image-search backend exists (failover path)
  • [B3]  _tag_engine stamps the answering backend
  • [B4]  _classify_extract_error maps exceptions to (kind, http_status)
  • [B1b] IMAGE_MIN_OK threshold is defined

Run:  pytest -q ci/test_searchhub_fixes.py   (from mcp-web-search/mcp-hub/src on PYTHONPATH)
"""
import importlib
import httpx


def test_searxng_has_image_search():
    backends = importlib.import_module("backends")
    assert hasattr(backends.SearXNGBackend, "search_images")


def test_tag_engine():
    hub = importlib.import_module("hub")
    out = hub._tag_engine([{"title": "x"}, {"title": "y"}], "DDGS")
    assert all(r["engine"] == "DDGS" for r in out)
    # does not clobber an existing tag
    assert hub._tag_engine([{"engine": "SearXNG"}], "DDGS")[0]["engine"] == "SearXNG"


def test_classify_extract_error():
    hub = importlib.import_module("hub")
    req = httpx.Request("GET", "https://example.com")
    resp = httpx.Response(404, request=req)
    kind, status = hub._classify_extract_error(httpx.HTTPStatusError("nf", request=req, response=resp))
    assert kind == "http_404" and status == 404
    assert hub._classify_extract_error(httpx.RemoteProtocolError("disc"))[0] == "disconnect"
    assert hub._classify_extract_error(httpx.ConnectTimeout("to"))[0] == "timeout"
    assert hub._classify_extract_error(ValueError("no main content"))[0] == "empty_content"


def test_image_min_ok_defined():
    hub = importlib.import_module("hub")
    assert isinstance(hub.IMAGE_MIN_OK, int) and hub.IMAGE_MIN_OK >= 1
